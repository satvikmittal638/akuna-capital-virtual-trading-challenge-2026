"""Does a candidate change help on ALL cases, or only the ones it was tuned on?

The overfit builds are instruments, not deliverables. Anything lifted out of them into a general
bot has to earn its place on the whole field, not on the two sessions it was fitted to. The metric
is the reliable one: Brier and signed bias against *real settlement*, over a neutral grid of
contracts, on every scored case. No simulator is involved.

    python3.13 variants/cross_case.py
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import statistics
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

spec = importlib.util.spec_from_file_location("bot", os.path.join(_ROOT, "bot.py"))
bot = importlib.util.module_from_spec(spec)
sys.modules["bot"] = bot
spec.loader.exec_module(bot)

FED, AJR, THR = bot.FED_FUNDS_RATE_UNDERLYING_ID, bot.AJARAI_UNDERLYING_ID, bot.THERIODIC_UNDERLYING_ID
NAME_TO_ID = {"FED": FED, "AJR": AJR, "THR": THR}
RATIOS = (0.90, 0.95, 1.00, 1.05, 1.10)
EXPIRIES = (1, 2, 3, 5)


def load_all():
    with open(os.path.join(_ROOT, "data", "full_data.json")) as handle:
        cases = {int(c["testcase_id"]): c for c in json.load(handle)}
    with open(os.path.join(_ROOT, "data", "live_market.json")) as handle:
        paths = {c["test_case_id"]: c["trajectory"] for c in json.load(handle)["test_cases"]}
    out = {}
    for case_id, path in sorted(paths.items()):
        case = cases[case_id]
        values = [{NAME_TO_ID[k]: p[k] for k in ("FED", "AJR", "THR")} for p in path]
        out[case_id] = (case, values)
    return out


def base_params(case):
    maker = bot.MarketMaker(
        [bot.Underlying(name=k, underlying_id=NAME_TO_ID[k], value=v)
         for k, v in case["initial_state"]["underlyings"].items()],
        [], case["initial_state"]["starting_cash"])
    maker.warm_up(bot.MarketHistory({NAME_TO_ID[k]: tuple(v) for k, v in case["history"].items()}))
    return maker._params


def with_trend(params, case, lookback: int, weight: float):
    history = {NAME_TO_ID[k]: tuple(v) for k, v in case["history"].items()}
    company = dict(params.company)
    for uid in (AJR, THR):
        series = history.get(uid, ())
        span = min(lookback, len(series) - 1)
        if span < 1 or series[-1] <= 0 or series[-1 - span] <= 0:
            continue
        momentum = math.log(series[-1] / series[-1 - span]) / span
        current = company[uid]
        blended = (1.0 - weight) * current.drift + weight * momentum
        company[uid] = bot.replace(current, drift=max(-0.05, min(0.05, blended)))
    return bot.replace(params, company=company)


def contracts(day_values, day, horizon):
    out = []
    for steps in EXPIRIES:
        if day + steps > horizon:
            continue
        for uid in (AJR, THR):
            for ratio in RATIOS:
                out.append(bot.BinaryOption(
                    legs=(bot.OptionLeg(underlying_id=uid, weight=1.0),),
                    option_id=0, steps_until_expiry=steps, strike=float(round(day_values[uid] * ratio))))
        out.append(bot.BinaryOption(
            legs=(bot.OptionLeg(underlying_id=THR, weight=1.0), bot.OptionLeg(underlying_id=AJR, weight=-1.0)),
            option_id=0, steps_until_expiry=steps, strike=0.0))
    return out


def score(params, values, horizon):
    briers, bias = [], []
    for day in range(horizon):
        for option in contracts(values[day], day, horizon):
            price = bot._price_with_params(params, values[day], option)
            actual = option.expiry_valuation(values[day + option.steps_until_expiry])
            briers.append((price - actual) ** 2)
            bias.append(price - actual)
    return statistics.mean(briers), statistics.mean(bias)


def main() -> int:
    data = load_all()
    variants = {
        "base": None,
        "trend3w1": (3, 1.0),
        "trend5w1": (5, 1.0),
        "trend8w1": (8, 1.0),
        "trend5w0.5": (5, 0.5),
        "trend8w0.5": (8, 0.5),
    }
    names = list(variants)
    print(f"\n  Brier per case (lower is better). TC = grader label = case + 1.\n")
    print("  case  TC  " + "".join(f"{n:>11}" for n in names))
    totals = {n: [] for n in names}
    biases = {n: [] for n in names}
    wins = {n: 0 for n in names if n != "base"}
    for case_id, (case, values) in data.items():
        horizon = len(values) - 1
        params = base_params(case)
        row = {}
        for name, spec_ in variants.items():
            p = params if spec_ is None else with_trend(params, case, *spec_)
            brier, bias = score(p, values, horizon)
            row[name] = brier
            totals[name].append(brier)
            biases[name].append(bias)
        for name in wins:
            if row[name] < row["base"]:
                wins[name] += 1
        best = min(row, key=row.get)
        cells = "".join(f"{row[n]:>11.4f}" + ("*" if n == best else " ") for n in names)
        print(f"  {case_id:>4} {case_id+1:>3}  " + cells)

    print(f"\n  {'variant':<12} {'mean Brier':>11} {'mean |bias|':>12} {'beats base on':>14}")
    for name in names:
        mb = statistics.mean(totals[name])
        ab = statistics.mean(abs(b) for b in biases[name])
        beat = f"{wins[name]}/{len(data)}" if name in wins else "-"
        print(f"  {name:<12} {mb:>11.4f} {ab:>12.4f} {beat:>14}")
    print("\n  A change belongs in the general bot only if it beats base on a clear majority.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
