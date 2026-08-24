"""Head-to-head arena: every saved version of the bot quoting on one exchange.

    python3.13 arena.py             # all recorded cases, 8 seeds each
    python3.13 arena.py --seeds 20  # more repetitions

The graded harness pits the bot against Fixed Width and Stalemate makers, which misprice worse
than it does -- so quoting through your own error is nearly free there. This arena instead fields
every version of the bot at once. They share a pricer to within a constant or two, so each one is
up against opponents that value a contract exactly as well as it does, and the only way to win a
request for quote is to show a genuinely better price or more size. That is the closest available
model of a field of finalists.

Flow is exogenous, as it is on the real exchange: the makers never trade with each other. A
synthetic counterparty raises every request for quote and every fill-or-kill; the makers only
respond. Settlement runs along the *recorded* trajectory for each case, so every contract expires
at its true value.
"""

import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)

import random
import statistics
import sys
from collections import defaultdict

import real_sim
from real_sim import FOK_PER_DAY, RFQ_PER_DAY, poisson, underlyings
from exchange import Rotation, allocate_fok, allocate_rfq, collect_quotes
from sim import Account, INFORMED_FRACTION, true_price
from bot import FokOrder, MarketMaker, OrderType, Quote

# label -> file. Every constant that differs is listed so a result can be read off directly.
VERSIONS: tuple[tuple[str, str], ...] = (
    ("v6  base.05 mult.40 sz.30  [15.30]", _os.path.join(_ROOT, "snapshots", "v6_15.30.py")),
    ("v7  +beta prior            [15.30]", _os.path.join(_ROOT, "snapshots", "v7_15.30.py")),
    ("v9  base.03 SUBMITTABLE    [16.20]", _os.path.join(_ROOT, "snapshots", "v9_16.20_SUBMITTABLE.py")),
    ("v14 +unopposed             [16.30]", _os.path.join(_ROOT, "snapshots", "v14_16.30.py")),
    ("CURRENT bot.py           [16.30]", _os.path.join(_ROOT, "bot.py")),
)


def load(label: str, path: str):
    import importlib.util as u

    spec = u.spec_from_file_location(f"arena_{abs(hash(label))}", path)
    module = u.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses needs the module registered before exec
    spec.loader.exec_module(module)
    return module


def build(module, label: str, values, options, cash: float):
    """Every version reports the same `name`, so relabel it -- accounts are keyed by name."""
    if module is None:  # a round-one opponent clone; it borrows the current pricer and differs only in policy
        import opponents
        clone = opponents.build(label, cash)
        clone.attach(MarketMaker(underlyings(values), options, cash))
        return clone

    class Tagged(module.MarketMaker):
        @property
        def name(self) -> str:
            return label

    return Tagged(underlyings(values), options, cash)


def run_rfq(rng, generator, accounts, contract, day, values, rotation) -> None:
    """One request for quote. Every maker is asked; the counterparty walks the ladder."""
    option = contract.at(day)
    quotes = collect_quotes(accounts, option, 0)
    if not quotes:
        return
    fair = true_price(generator, values, option)
    informed = rng.random() < INFORMED_FRACTION
    quantity = rng.randint(1, 26)
    if informed:
        # Trades only against a price that is actually wrong, and only on the wrong side.
        best_bid = max(q.bid_price for _, q in quotes)
        best_offer = min(q.offer_price for _, q in quotes)
        if best_bid - fair >= 0.015:
            side = OrderType.SELL
        elif fair - best_offer >= 0.015:
            side = OrderType.BUY
        else:
            return
        limit = None  # it already knows the quote is wrong; it has no reserve
    else:
        side = OrderType.BUY if rng.random() < 0.5 else OrderType.SELL
        tolerance = rng.uniform(0.03, 0.15)
        limit = fair + tolerance if side == OrderType.BUY else fair - tolerance
        if side == OrderType.BUY and min(q.offer_price for _, q in quotes) > limit:
            return
        if side == OrderType.SELL and max(q.bid_price for _, q in quotes) < limit:
            return
    allocate_rfq(accounts, option, quotes, side, quantity, 0, rotation, limit)


def run_fok(rng, generator, accounts, contract, day, values, payoff_of, rotation) -> None:
    """One fill-or-kill, shown to every maker at once; acceptors share it equally."""
    option = contract.at(day)
    fair = true_price(generator, values, option)
    quantity = rng.randint(1, 30)
    if rng.random() < INFORMED_FRACTION:
        payoff = payoff_of(contract)
        signal = payoff if rng.random() < 0.75 else 1.0 - payoff
        if signal >= 0.5:
            side, price = OrderType.BUY, min(0.99, fair + rng.uniform(0.005, 0.03))
        else:
            side, price = OrderType.SELL, max(0.01, fair - rng.uniform(0.005, 0.03))
    else:
        edge = rng.gauss(0.01, 0.08)
        if rng.random() < 0.5:
            side, price = OrderType.BUY, min(0.99, max(0.01, fair + edge))
        else:
            side, price = OrderType.SELL, min(0.99, max(0.01, fair - edge))
    order = FokOrder(counterparty_id=1, option_id=option.option_id, order_type=side,
                     price=round(price, 2), quantity=quantity)
    allocate_fok(accounts, option, order, rotation)


def run_case(case: dict, modules: list, seed: int) -> dict[str, float]:
    cid = int(case["testcase_id"])
    path = real_sim.LIVE_PATHS[cid]
    days = len(path) - 1
    cash = case["initial_state"]["starting_cash"]
    rng = random.Random(seed)
    generator = real_sim.generator_params(case, real_sim.historical_drifts(case))
    history = real_sim.MarketHistory({real_sim.NAME_TO_ID[k]: tuple(v) for k, v in case["history"].items()})
    initial = [real_sim.parse_option(text) for text in case["initial_active_options"]]
    contracts = [real_sim.Contract(option_id=o.option_id, legs=o.legs, strike=o.strike,
                                   expiry_day=o.steps_until_expiry) for o in initial]
    next_id, book_size = 1_000_000, len(contracts)
    rotation = Rotation()  # one per session, so leftover lots move round the field

    accounts: dict[str, Account] = {}
    for label, module in modules:
        maker = build(module, label, path[0], initial, cash)
        maker.warm_up(history)
        accounts[label] = Account(maker=maker, cash=cash)

    def payoff_of(contract):
        return contract.at(contract.expiry_day).expiry_valuation(path[min(contract.expiry_day, days)])

    for day in range(days):
        values = path[day]
        live = [c for c in contracts if c.expiry_day >= day]
        if not live:
            fresh, next_id = real_sim.fresh_contracts(day, values, rng, book_size, next_id, generator)
            contracts.extend(fresh)
            live = [c for c in contracts if c.expiry_day >= day]
        for _ in range(poisson(rng, RFQ_PER_DAY)):
            run_rfq(rng, generator, accounts, rng.choice(live), day, values, rotation)
        for _ in range(poisson(rng, FOK_PER_DAY)):
            run_fok(rng, generator, accounts, rng.choice(live), day, values, payoff_of, rotation)

        for contract in [c for c in contracts if c.expiry_day == day]:
            payoff = contract.at(contract.expiry_day).expiry_valuation(values)
            for account in accounts.values():
                account.positions.pop(contract.option_id, None)
                bought = account.bought.pop(contract.option_id, 0)
                sold = account.sold.pop(contract.option_id, 0)
                account.cash += bought * payoff + sold * (1.0 - payoff)
                # The bot versions learn about settlement from the book shrinking; the opponent
                # clones keep their own cash and would size off a balance that never grew.
                credit = getattr(account.maker, "credit", None)
                if credit is not None:
                    credit(contract.option_id, payoff, bought, sold)
        contracts = [c for c in contracts if c.expiry_day > day]
        for account in accounts.values():
            if not account.bankrupt and account.cash < 0.0:
                account.bankrupt = True
        fresh, next_id = real_sim.fresh_contracts(day + 1, path[day + 1], rng, 2, next_id, generator)
        contracts.extend(fresh)
        live_next = [c.at(day + 1) for c in contracts if c.expiry_day >= day + 1]
        for account in accounts.values():
            account.maker.on_step_advance(underlyings(path[day + 1]), live_next)

    remaining = {c.option_id: c for c in contracts}
    out = {}
    for label, account in accounts.items():
        mark = sum(
            quantity * true_price(generator, path[days], remaining[option_id].at(days))
            for option_id, quantity in account.positions.items()
            if quantity and option_id in remaining
        )
        out[label] = -cash if account.bankrupt else account.cash + mark - cash
    return out


def main() -> int:
    seeds = 8
    if "--seeds" in sys.argv:
        seeds = int(sys.argv[sys.argv.index("--seeds") + 1])
    modules = [(label, load(label, path)) for label, path in VERSIONS]
    print(f"{len(modules)} versions, exogenous flow, settlement on the recorded trajectories\n")
    pnl: dict[str, list[float]] = defaultdict(list)
    score: dict[str, list[float]] = defaultdict(list)
    bankrupt: dict[str, int] = defaultdict(int)
    cases = [c for c in real_sim.load_cases() if real_sim.LIVE_PATHS.get(int(c["testcase_id"]))]
    for case in cases:
        for seed in range(seeds):
            result = run_case(case, modules, 7000 + seed)
            order = sorted(result, key=lambda k: -result[k])
            for rank, label in enumerate(order, start=1):
                pnl[label].append(result[label])
                score[label].append(real_sim.rank_score(rank, len(order)))
                bankrupt[label] += int(result[label] <= -case["initial_state"]["starting_cash"])
    print(f"{'version':<28} {'mean rank-score':>16} {'mean PnL':>10} {'median':>9} {'worst':>9} {'wins':>7} {'bankrupt':>9}")
    print("-" * 94)
    for label in sorted(pnl, key=lambda k: -statistics.mean(score[k])):
        values = pnl[label]
        wins = sum(1 for i in range(len(values)) if score[label][i] >= 0.999)
        print(f"{label:<28} {statistics.mean(score[label]):>16.3f} {statistics.mean(values):>10.2f} "
              f"{statistics.median(values):>9.2f} {min(values):>9.2f} {wins:>4}/{len(values):<3} {bankrupt[label]:>9}")
    print(f"\n{len(cases)} cases x {seeds} seeds = {len(cases) * seeds} sessions, all versions in each")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
