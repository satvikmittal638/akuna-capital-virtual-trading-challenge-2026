"""Sessions seeded from the real test cases in `full_data.json`. Not part of the submission.

    python3.13 real_sim.py            # run every scoring session
    python3.13 real_sim.py --days 10  # shorter sessions

Each session starts from a real case's warm-up history, underlyings, option book and cash, and
by default rolls forward along the *recorded* trajectory in `live_market.json`, for exactly as
many days as the case ran. The bot is never given that trajectory: it only decides where the
underlyings go, and therefore what every contract settles at. That is the whole point -- it is
the answer key for scoring, so putting any of it inside `bot.py` would fake the result.

With `--bootstrap`, forward paths are instead resampled from the case's own daily moves, with the
drift drawn from the cross-case prior rather than inherited from the realised history (a history
that happened to trend would otherwise reward the trend-extrapolation being tested). That mode
covers cases 1-3, which have no recorded trajectory.
"""

import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)

import json
import math
import random
import re
import statistics
import sys
from collections import defaultdict

from bot import (
    AJARAI_UNDERLYING_ID,
    FED_FUNDS_RATE_UNDERLYING_ID,
    THERIODIC_UNDERLYING_ID,
    BinaryOption,
    MarketHistory,
    MarketMaker,
    MarketParameters,
    OptionLeg,
    OrderType,
    Underlying,
)
from bot import _CompanyParams, _Params
from sim import Account, Contract, NaiveMarketMaker, _run_fok, _run_rfq, counterparty_pool

DATA_PATH = _os.path.join(_ROOT, "data", "full_data.json")
LIVE_PATH = _os.path.join(_ROOT, "data", "live_market.json")
NAME_TO_ID = {"FED": FED_FUNDS_RATE_UNDERLYING_ID, "AJR": AJARAI_UNDERLYING_ID, "THR": THERIODIC_UNDERLYING_ID}
ID_TO_NAME = {value: key for key, value in NAME_TO_ID.items()}
COMPANY_IDS = (AJARAI_UNDERLYING_ID, THERIODIC_UNDERLYING_ID)
OPTION_RE = re.compile(r"^(\d+) \((\d+)d (.+) >= (-?[\d.]+)\)$")

# Cross-case prior on the true daily drift, from the dispersion of the estimates across all the
# real warm-up histories once sampling noise is netted out.
DRIFT_PRIOR_MEAN = 0.005
DRIFT_PRIOR_STD_DEV = 0.008


def parse_option(text: str) -> BinaryOption:
    match = OPTION_RE.match(text)
    assert match, text
    option_id, steps, expression, strike = match.groups()
    legs: list[OptionLeg] = []
    sign = 1.0
    for token in expression.split():
        if token == "+":
            sign = 1.0
        elif token == "-":
            sign = -1.0
        else:
            legs.append(OptionLeg(underlying_id=NAME_TO_ID[token], weight=sign))
            sign = 1.0
    return BinaryOption(
        legs=tuple(legs), option_id=int(option_id), steps_until_expiry=int(steps), strike=float(strike)
    )


def load_cases(path: str = DATA_PATH) -> list[dict]:
    with open(path) as handle:
        return [case for case in json.load(handle) if case["type"] == "SCORING_SESSION"]


def underlyings(values: dict[int, float]) -> list[Underlying]:
    return [
        Underlying(name=ID_TO_NAME[underlying_id], underlying_id=underlying_id, value=value)
        for underlying_id, value in values.items()
    ]


def historical_moves(case: dict) -> tuple[list[float], list[float], list[float]]:
    """Daily rate changes and the two companies' log returns, de-meaned."""
    rates = case["history"]["FED"]
    rate_changes = [round(rates[i] - rates[i - 1], 2) for i in range(1, len(rates))]
    residuals: dict[str, list[float]] = {}
    for name in ("AJR", "THR"):
        series = case["history"][name]
        returns = [math.log(series[i] / series[i - 1]) for i in range(1, len(series))]
        mean = statistics.mean(returns)
        residuals[name] = [value - mean for value in returns]
    return rate_changes, residuals["AJR"], residuals["THR"]


def load_live_paths(path: str = LIVE_PATH) -> dict[int, list[dict[int, float]]]:
    """The realised forward trajectories, used to score the bot -- never given to the bot."""
    try:
        with open(path) as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return {}
    return {
        case["test_case_id"]: [
            {NAME_TO_ID[name]: day[name] for name in ("FED", "AJR", "THR")} for day in case["trajectory"]
        ]
        for case in payload["test_cases"]
    }


LIVE_PATHS: dict[int, list[dict[int, float]]] = load_live_paths()

FLOW_PATH = _os.path.join(_ROOT, "data", "competitor_flow_data.json")


def load_flow(path: str = FLOW_PATH) -> dict[int, list[dict]]:
    try:
        with open(path) as handle:
            return {case["test_case_id"]: case["events"] for case in json.load(handle)["test_cases"]}
    except FileNotFoundError:
        return {}


FLOW: dict[int, list[dict]] = load_flow()

# Measured from the recorded flow: what the listed book actually looks like. Nearly half of all
# fill-or-kill orders are struck at the boundary penny, because most listed contracts are deep in
# or out of the money -- not the near-the-money book an earlier version of this harness generated.
MONEYNESS_SAMPLE: tuple[float, ...] = tuple(
    event["price"] for events in FLOW.values() for event in events if event["action"] == "FOK"
) or (0.5,)
# Measured flow intensity, per day, averaged over the recorded sessions.
RFQ_PER_DAY: float = 2.0
FOK_PER_DAY: float = 1.7
NEW_OPTIONS_PER_DAY: float = 1.8
# Option mix and expiries, from the initial books of the real cases.
OPTION_MIX: tuple[tuple[str, float], ...] = (("company", 0.57), ("FED", 0.20), ("spread", 0.23))


def poisson(rng: random.Random, mean: float) -> int:
    """Knuth's method; the counts are small so this is plenty fast."""
    limit = math.exp(-mean)
    count, product = 0, rng.random()
    while product > limit:
        count += 1
        product *= rng.random()
    return count


def inverse_normal_cdf(probability: float) -> float:
    """Acklam's rational approximation; good to about 1e-9, plenty for placing a strike."""
    probability = min(max(probability, 1e-9), 1.0 - 1e-9)
    a = (-39.69683028665376, 220.9460984245205, -275.9285104469687,
         138.3577518672690, -30.66479806614716, 2.506628277459239)
    b = (-54.47609879822406, 161.5858368580409, -155.6989798598866,
         66.80131188771972, -13.28068155288572)
    c = (-0.007784894002430293, -0.3223964580411365, -2.400758277161838,
         -2.549732539343734, 4.374664141464968, 2.938163982698783)
    d = (0.007784695709041462, 0.3224671290700398, 2.445134137142996, 3.754408661907416)
    low, high = 0.02425, 1 - 0.02425
    if probability < low:
        q = math.sqrt(-2 * math.log(probability))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if probability > high:
        q = math.sqrt(-2 * math.log(1 - probability))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = probability - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def extend_path(case: dict, path: list[dict[int, float]], rng: random.Random, extra: int) -> list[dict[int, float]]:
    """Options can outlive the recorded trajectory; roll the tail on with resampled moves."""
    rate_changes, ajr_residuals, thr_residuals = historical_moves(case)
    drifts = {
        underlying_id: statistics.mean(
            [
                math.log(series[i] / series[i - 1])
                for series in [case["history"][ID_TO_NAME[underlying_id]]]
                for i in range(1, len(series))
            ]
        )
        for underlying_id in COMPANY_IDS
    }
    residuals = {AJARAI_UNDERLYING_ID: ajr_residuals, THERIODIC_UNDERLYING_ID: thr_residuals}
    extended = list(path)
    current = dict(extended[-1])
    for _ in range(extra):
        index = rng.randrange(len(rate_changes))
        following = {
            FED_FUNDS_RATE_UNDERLYING_ID: max(
                round(current[FED_FUNDS_RATE_UNDERLYING_ID] + rate_changes[index], 2), 0.0
            )
        }
        for underlying_id in COMPANY_IDS:
            log_return = drifts[underlying_id] + residuals[underlying_id][index]
            following[underlying_id] = round(current[underlying_id] * math.exp(log_return), 2)
        extended.append(following)
        current = following
    return extended


def build_path(case: dict, rng: random.Random, days: int) -> tuple[list[dict[int, float]], _Params]:
    rate_changes, ajr_residuals, thr_residuals = historical_moves(case)
    drifts = {
        AJARAI_UNDERLYING_ID: rng.gauss(DRIFT_PRIOR_MEAN, DRIFT_PRIOR_STD_DEV),
        THERIODIC_UNDERLYING_ID: rng.gauss(DRIFT_PRIOR_MEAN, DRIFT_PRIOR_STD_DEV),
    }
    residuals = {AJARAI_UNDERLYING_ID: ajr_residuals, THERIODIC_UNDERLYING_ID: thr_residuals}
    current = {NAME_TO_ID[name]: value for name, value in case["initial_state"]["underlyings"].items()}
    path = [dict(current)]
    for _ in range(days):
        index = rng.randrange(len(rate_changes))  # one index for all three: keeps them correlated
        following = {
            FED_FUNDS_RATE_UNDERLYING_ID: max(
                round(current[FED_FUNDS_RATE_UNDERLYING_ID] + rate_changes[index], 2), 0.0
            )
        }
        for underlying_id in COMPANY_IDS:
            log_return = drifts[underlying_id] + residuals[underlying_id][index]
            following[underlying_id] = round(current[underlying_id] * math.exp(log_return), 2)
        path.append(following)
        current = following
    return path, generator_params(case, drifts)


def naive_params(case: dict) -> _Params:
    """What an unsophisticated competitor estimates: raw sample moments off the warm-up.

    No rate regression, no shrinkage of drift or correlation, no Student-t widening, and the rate
    treated as a random walk on its observed up/down frequencies. Competitors price off this so
    that improvements to the bot's own estimator actually show up as an edge -- handing them the
    bot's model, as an earlier version of this harness did, makes model gains invisible by
    construction, because every improvement lifts them by exactly as much.
    """
    rate_changes, _, _ = historical_moves(case)
    count = len(rate_changes)
    company = {}
    returns = {}
    for underlying_id in COMPANY_IDS:
        series = case["history"][ID_TO_NAME[underlying_id]]
        values = [math.log(series[i] / series[i - 1]) for i in range(1, len(series))]
        returns[underlying_id] = values
        company[underlying_id] = _CompanyParams(
            drift=statistics.mean(values), rate_beta=0.0, variance=max(statistics.variance(values), 1e-10)
        )
    mean_a = statistics.mean(returns[AJARAI_UNDERLYING_ID])
    mean_t = statistics.mean(returns[THERIODIC_UNDERLYING_ID])
    covariance = sum(
        (a - mean_a) * (t - mean_t)
        for a, t in zip(returns[AJARAI_UNDERLYING_ID], returns[THERIODIC_UNDERLYING_ID])
    ) / max(count - 1, 1)
    return _Params(
        rate_up_intercept=sum(1 for c in rate_changes if c > 1e-9) / count,
        rate_down_intercept=sum(1 for c in rate_changes if c < -1e-9) / count,
        rate_reversion=0.0,
        rate_step=0.25,
        company=company,
        covariance=covariance,
    )


def historical_drifts(case: dict) -> dict[int, float]:
    drifts = {}
    for underlying_id in COMPANY_IDS:
        series = case["history"][ID_TO_NAME[underlying_id]]
        drifts[underlying_id] = statistics.mean(
            [math.log(series[i] / series[i - 1]) for i in range(1, len(series))]
        )
    return drifts


def generator_params(case: dict, drifts: dict[int, float]) -> _Params:
    """Analytic description of the bootstrap generator, so counterparties have a fair value."""
    rate_changes, ajr_residuals, thr_residuals = historical_moves(case)
    count = len(rate_changes)
    ups = sum(1 for change in rate_changes if change > 1e-9) / count
    downs = sum(1 for change in rate_changes if change < -1e-9) / count
    variances = {
        AJARAI_UNDERLYING_ID: statistics.variance(ajr_residuals),
        THERIODIC_UNDERLYING_ID: statistics.variance(thr_residuals),
    }
    covariance = sum(a * b for a, b in zip(ajr_residuals, thr_residuals)) / max(count - 1, 1)
    return _Params(
        rate_up_intercept=ups,
        rate_down_intercept=downs,
        rate_reversion=0.0,
        rate_step=0.25,
        company={
            underlying_id: _CompanyParams(drift=drifts[underlying_id], rate_beta=0.0, variance=variances[underlying_id])
            for underlying_id in COMPANY_IDS
        },
        covariance=covariance,
    )


def fresh_contracts(
    day: int,
    values: dict[int, float],
    rng: random.Random,
    count: int,
    next_id: int,
    generator: _Params,
):
    """List new contracts with the moneyness the real books actually show.

    Rather than striking near spot -- which yields an all-near-the-money book -- a target
    probability is drawn from the recorded fill-or-kill prices and the strike is solved to hit it.
    That reproduces the real shape: most contracts deep in or out of the money, with a large mass
    sitting at the boundary penny, which is precisely where margin is cheap and size is possible.
    """
    from bot import _price_with_params

    contracts = []
    for _ in range(count):
        steps = rng.randint(1, 5)
        target = min(max(rng.choice(MONEYNESS_SAMPLE), 0.005), 0.995)
        draw = rng.random()
        kind = "company" if draw < 0.57 else ("FED" if draw < 0.77 else "spread")

        if kind == "spread":
            legs = (
                OptionLeg(underlying_id=THERIODIC_UNDERLYING_ID, weight=1.0),
                OptionLeg(underlying_id=AJARAI_UNDERLYING_ID, weight=-1.0),
            )
            strike = 0.0
        elif kind == "FED":
            legs = (OptionLeg(underlying_id=FED_FUNDS_RATE_UNDERLYING_ID, weight=1.0),)
            # Walk the grid and take the strike whose probability is closest to the target.
            best, strike = None, values[FED_FUNDS_RATE_UNDERLYING_ID]
            for offset in range(-steps - 1, steps + 2):
                candidate = max(round(values[FED_FUNDS_RATE_UNDERLYING_ID] + 0.25 * offset, 2), 0.0)
                option = BinaryOption(
                    legs=legs, option_id=1, steps_until_expiry=steps, strike=candidate
                )
                distance = abs(_price_with_params(generator, values, option) - target)
                if best is None or distance < best:
                    best, strike = distance, candidate
        else:
            underlying_id = rng.choice(COMPANY_IDS)
            legs = (OptionLeg(underlying_id=underlying_id, weight=1.0),)
            company = generator.company_params(underlying_id)
            mean = math.log(values[underlying_id]) + steps * company.drift
            std_dev = math.sqrt(max(steps * company.variance, 1e-12))
            strike = float(round(math.exp(mean - std_dev * inverse_normal_cdf(target))))
            strike = max(strike, 1.0)
        contracts.append(Contract(option_id=next_id, legs=legs, strike=strike, expiry_day=day + steps))
        next_id += 1
    return contracts, next_id


def run_case(case: dict, seed: int, days: int | None = None, use_live: bool = True) -> dict[str, float]:
    rng = random.Random(seed)
    state = case["initial_state"]
    starting_cash = state["starting_cash"]
    live = LIVE_PATHS.get(int(case["testcase_id"])) if use_live else None
    if live is not None:
        # Replay the trajectory that actually occurred. The bot never sees it -- it only sets
        # where the underlyings go and therefore what every contract settles at.
        days = len(live) - 1 if days is None else min(days, len(live) - 1)
        path = extend_path(case, live, rng, 8)
        generator = generator_params(case, historical_drifts(case))
    else:
        days = 30 if days is None else days
        path, generator = build_path(case, rng, days + 8)
    history = MarketHistory({NAME_TO_ID[name]: tuple(values) for name, values in case["history"].items()})

    initial = [parse_option(text) for text in case["initial_active_options"]]
    contracts = [
        Contract(
            option_id=option.option_id,
            legs=option.legs,
            strike=option.strike,
            expiry_day=option.steps_until_expiry,
        )
        for option in initial
    ]
    next_id = 1_000_000
    book_size = len(contracts)
    all_legs = {contract.option_id: contract.legs for contract in contracts}

    bot = MarketMaker(underlyings(path[0]), initial, starting_cash)
    competitors = [
        NaiveMarketMaker("Fixed 0.10", None, half_spread=0.10, noise=0.0, size=30, seed=seed + 1),
        NaiveMarketMaker("Fixed 0.05", None, half_spread=0.05, noise=0.0, size=30, seed=seed + 2),
    ]
    accounts = {maker.name: Account(maker=maker, cash=starting_cash) for maker in [bot, *competitors]}
    naive = naive_params(case)
    for competitor in competitors:
        competitor._account = accounts[competitor.name]
        competitor._parameters = naive  # prices off raw sample moments, not the bot's estimator
    for maker in [bot, *competitors]:
        maker.warm_up(history)
        maker.on_step_advance(underlyings(path[0]), initial)

    expected_events = int((RFQ_PER_DAY + FOK_PER_DAY) * days)
    pool = counterparty_pool(rng, expected_events)
    payoff_by_option_id: dict[int, float] = {}

    def settlement_payoff(contract: Contract) -> float:
        return contract.at(contract.expiry_day).expiry_valuation(path[contract.expiry_day])

    for day in range(days):
        values = path[day]
        live = [contract for contract in contracts if contract.expiry_day >= day]
        if not live:
            new, next_id = fresh_contracts(day, values, rng, book_size, next_id, generator)
            contracts.extend(new)
            all_legs.update({c.option_id: c.legs for c in new})
            live = [contract for contract in contracts if contract.expiry_day >= day]

        # Measured intensities from the recorded flow, drawn as Poisson counts.
        for _ in range(poisson(rng, RFQ_PER_DAY)):
            _run_rfq(rng, generator, accounts, rng.choice(live), day, values, settlement_payoff, pool)
        for _ in range(poisson(rng, FOK_PER_DAY)):
            _run_fok(rng, generator, accounts, rng.choice(live), day, values, settlement_payoff, pool)

        for contract in [c for c in contracts if c.expiry_day == day]:
            payoff = settlement_payoff(contract)
            payoff_by_option_id[contract.option_id] = payoff
            for account in accounts.values():
                account.positions.pop(contract.option_id, None)
                account.cash += account.bought.pop(contract.option_id, 0) * payoff
                account.cash += account.sold.pop(contract.option_id, 0) * (1.0 - payoff)
        contracts = [contract for contract in contracts if contract.expiry_day > day]

        for name, account in accounts.items():
            if not account.bankrupt and account.cash < 0.0:
                account.bankrupt = True

        # The real book grows: roughly two new contracts listed per day, on top of replacing
        # whatever just expired.
        listings = poisson(rng, NEW_OPTIONS_PER_DAY)
        if listings > 0:
            new, next_id = fresh_contracts(day + 1, path[day + 1], rng, listings, next_id, generator)
            contracts.extend(new)
            all_legs.update({c.option_id: c.legs for c in new})
        following = [contract.at(day + 1) for contract in contracts if contract.expiry_day >= day + 1]
        for account in accounts.values():
            account.maker.on_step_advance(underlyings(path[day + 1]), following)

    results: dict[str, float] = {}
    for name, account in accounts.items():
        mark = 0.0
        for contract in contracts:
            fair = float(contract.at(days).expiry_valuation(path[contract.expiry_day]))
            mark += account.bought.get(contract.option_id, 0) * fair
            mark += account.sold.get(contract.option_id, 0) * (1.0 - fair)
        results[name] = account.cash + mark - starting_cash
    results["__bankrupt__"] = 1.0 if accounts[bot.name].bankrupt else 0.0
    results["__volume__"] = float(accounts[bot.name].volume)

    # Attribute the bot's PnL by the kind of contract, to show where any leak actually is.
    legs_by_option_id = all_legs
    for contract in contracts:
        payoff_by_option_id.setdefault(
            contract.option_id, float(contract.at(days).expiry_valuation(path[contract.expiry_day]))
        )
    attribution: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for option_id, _, quantity, price, was_fok in accounts[bot.name].trades:
        payoff = payoff_by_option_id.get(option_id)
        legs = legs_by_option_id.get(option_id)
        if payoff is None or legs is None:
            continue
        if len(legs) > 1:
            kind = "spread"
        elif legs[0].underlying_id == FED_FUNDS_RATE_UNDERLYING_ID:
            kind = "FED"
        else:
            kind = "company"
        label = f"{kind}-{'fok' if was_fok else 'rfq'}"
        attribution[label][0] += quantity * (payoff - price)
        attribution[label][1] += abs(quantity)
    results["__attribution__"] = attribution  # type: ignore[assignment]
    return results


def rank_score(rank: int, field: int) -> float:
    """1.0 for first, a linear slide to 0.4 for last; bankruptcy is scored 0 by the caller."""
    if rank <= 1:
        return 1.0
    if field <= 1:
        return 1.0
    return 0.4 + 0.6 * (field - rank) / (field - 1)


def check_theo_case() -> bool:
    """The one case with published true parameters and published answers."""
    with open(DATA_PATH) as handle:
        cases = [case for case in json.load(handle) if case["type"] == "THEO_VERIFICATION"]
    if not cases:
        return True
    case = cases[0]
    expected = [0.7000, 0.0471, 0.5309, 0.2068, 1.0000, 0.9999]  # as printed by the grader
    parameters = MarketParameters(**case["true_market_parameters"])
    values = {NAME_TO_ID[name]: value for name, value in case["initial_state"]["underlyings"].items()}
    options = [parse_option(text) for text in case["initial_active_options"]]
    maker = MarketMaker(underlyings(values), options, case["initial_state"]["starting_cash"])
    worst = 0.0
    for option, want in zip(options, expected):
        worst = max(worst, abs(maker.price_option_from_parameters(parameters, option) - want))
    print(f"THEO verification: max error {worst:.5f}  {'PASS' if worst < 1e-4 else 'FAIL'}\n")
    return worst < 1e-4


def main() -> int:
    check_theo_case()
    days = None
    if "--days" in sys.argv:
        days = int(sys.argv[sys.argv.index("--days") + 1])
    use_live = "--bootstrap" not in sys.argv
    print("forward paths: " + ("recorded live trajectories" if use_live else "bootstrapped") + "\n")
    replications = 12
    if "--reps" in sys.argv:
        replications = int(sys.argv[sys.argv.index("--reps") + 1])

    cases = load_cases()
    total_score = 0.0
    print(f"{'case':>5} {'cash':>6} {'hist':>5}   {'bot':>8} {'Fixed 0.10':>11} {'Fixed 0.05':>11}   "
          f"{'win rate':>9} {'bankrupt':>9}  score")
    for case in cases:
        outcomes = defaultdict(list)
        wins = 0
        bankruptcies = 0
        placements = []
        for replication in range(replications):
            result = run_case(case, seed=7000 + replication * 13, days=days, use_live=use_live)
            for name, value in result.items():
                if not name.startswith("__"):
                    outcomes[name].append(value)
            pnls = {k: v for k, v in result.items() if not k.startswith("__")}
            field = len(pnls)
            ours = pnls["Telescoping Theo"]
            rank = 1 + sum(1 for k, v in pnls.items() if k != "Telescoping Theo" and v > ours)
            if result["__bankrupt__"]:
                placements.append((field + 1, field))  # scored zero below via bankruptcy branch
            else:
                placements.append((rank, field))
            rivals = max(v for k, v in pnls.items() if k != "Telescoping Theo")
            wins += ours >= rivals
            bankruptcies += int(result["__bankrupt__"])
        # The grader's actual rubric, recovered from published results: it scores by *rank*, not
        # by profit -- 1.0 for first, then 0.4 + 0.6 * (n - rank) / (n - 1), and 0 for bankruptcy.
        # Second of three is worth 0.70, not the 0.40 an earlier version of this harness assumed,
        # which made placing second look no better than placing last.
        score = sum(0.0 if r > n else rank_score(r, n) for r, n in placements) / replications
        total_score += score
        print(
            f"{case['testcase_id']:>5} {case['initial_state']['starting_cash']:>6.0f} "
            f"{case['history_days']:>5}   {statistics.mean(outcomes['Telescoping Theo']):>+8.2f} "
            f"{statistics.mean(outcomes['Fixed 0.10']):>+11.2f} {statistics.mean(outcomes['Fixed 0.05']):>+11.2f}   "
            f"{wins}/{replications:<7} {bankruptcies}/{replications:<7}  {score:.2f}"
        )
    print(f"\nmean score across {len(cases)} cases: {total_score / len(cases):.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
