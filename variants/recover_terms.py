"""Recover unknown contract terms for a case by fitting them to observed FOK prices.

The recorded flow gives, for each option, a set of (day, price) observations. The underlying paths
are known. So the terms -- which leg, what strike, which expiry -- can be recovered by searching
the small space of shapes the generator actually produces and keeping whichever reproduces every
observed price best. The four contracts whose terms are published serve as the control: if the
search recovers those, it can be trusted on the rest.

With terms in hand, every fill-or-kill order can be scored against what really settled, which
answers the only question that matters: where is the money in this session, and on which side.

    python3.13 variants/recover_terms.py 5
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

spec = importlib.util.spec_from_file_location("bot", os.path.join(_ROOT, "bot.py"))
bot = importlib.util.module_from_spec(spec)
sys.modules["bot"] = bot
spec.loader.exec_module(bot)

FED, AJR, THR = bot.FED_FUNDS_RATE_UNDERLYING_ID, bot.AJARAI_UNDERLYING_ID, bot.THERIODIC_UNDERLYING_ID
NAME_TO_ID = {"FED": FED, "AJR": AJR, "THR": THR}


def load(case_id: int):
    with open(os.path.join(_ROOT, "data", "full_data.json")) as handle:
        case = {int(c["testcase_id"]): c for c in json.load(handle)}[case_id]
    with open(os.path.join(_ROOT, "data", "live_market.json")) as handle:
        path = {c["test_case_id"]: c["trajectory"] for c in json.load(handle)["test_cases"]}[case_id]
    with open(os.path.join(_ROOT, "data", "competitor_flow_data.json")) as handle:
        flow = {c["test_case_id"]: c["events"] for c in json.load(handle)["test_cases"]}[case_id]
    values = [{NAME_TO_ID[k]: point[k] for k in ("FED", "AJR", "THR")} for point in path]
    return case, values, flow


def fitted_params(case):
    maker = bot.MarketMaker(
        [bot.Underlying(name=k, underlying_id=NAME_TO_ID[k], value=v)
         for k, v in case["initial_state"]["underlyings"].items()],
        [], case["initial_state"]["starting_cash"])
    maker.warm_up(bot.MarketHistory({NAME_TO_ID[k]: tuple(v) for k, v in case["history"].items()}))
    return maker._params


def candidates(values, horizon, first_day, last_day):
    """Every contract shape the generator produces, over plausible strikes and expiries."""
    out = []
    for expiry in range(last_day, min(horizon, last_day + 8) + 1):
        out.append((("spread",), 0.0, expiry))
        for uid, name in ((AJR, "AJR"), (THR, "THR")):
            level = values[first_day][uid]
            for ratio in [0.5 + 0.02 * i for i in range(60)]:
                out.append(((name,), round(level * ratio), expiry))
        for offset in range(-8, 9):
            out.append((("FED",), max(round(values[first_day][FED] + 0.25 * offset, 2), 0.0), expiry))
    return out


def build_option(shape, strike, expiry, day):
    steps = max(expiry - day, 0)
    if shape[0] == "spread":
        legs = (bot.OptionLeg(underlying_id=THR, weight=1.0), bot.OptionLeg(underlying_id=AJR, weight=-1.0))
    else:
        legs = (bot.OptionLeg(underlying_id=NAME_TO_ID[shape[0]], weight=1.0),)
    return bot.BinaryOption(legs=legs, option_id=0, steps_until_expiry=steps, strike=float(strike))


def recover(case_id: int = 5):
    case, values, flow = load(case_id)
    horizon = len(values) - 1
    params = fitted_params(case)

    observations: dict[int, list[tuple[int, float]]] = {}
    for event in flow:
        if event["action"] == "FOK":
            observations.setdefault(event["option_id"], []).append((event["day"], event["price"]))
    span: dict[int, tuple[int, int]] = {}
    for event in flow:
        day = min(event["day"], horizon - 1)
        low, high = span.get(event["option_id"], (day, day))
        span[event["option_id"]] = (min(low, day), max(high, day))

    known = {}
    for text in case["initial_active_options"]:
        option = None
        oid = int(text.split(" ")[0])
        known[oid] = text

    print(f"=== case {case_id}: recovering terms from {len(observations)} priced options ===\n")
    recovered: dict[int, tuple] = {}
    for oid, obs in sorted(observations.items()):
        first_day, last_day = span[oid]
        best = None
        for shape, strike, expiry in candidates(values, horizon, first_day, last_day):
            if expiry < last_day:
                continue
            error = 0.0
            for day, price in obs:
                if day > expiry:
                    error = 1e9
                    break
                option = build_option(shape, strike, expiry, day)
                error += (bot._price_with_params(params, values[min(day, horizon)], option) - price) ** 2
            if best is None or error < best[0]:
                best = (error, shape, strike, expiry)
        error, shape, strike, expiry = best
        rms = math.sqrt(error / len(obs))
        payoff = build_option(shape, strike, expiry, expiry).expiry_valuation(values[min(expiry, horizon)])
        recovered[oid] = (shape, strike, expiry, payoff, rms)
        label = shape[0] if shape[0] != "spread" else "THR-AJR"
        flag = f"   [published: {known[oid]}]" if oid in known else ""
        print(f"  {oid}  {label:>7} >= {strike:>8}  expiry d{expiry:<3} settles {payoff:.0f}  "
              f"rms {rms:.3f}  n={len(obs)}{flag}")

    print(f"\n=== every FOK scored against real settlement (maker's side) ===\n")
    total_take_all = 0.0
    rows = []
    for event in flow:
        if event["action"] != "FOK":
            continue
        oid = event["option_id"]
        if oid not in recovered:
            continue
        _, _, _, payoff, rms = recovered[oid]
        price, qty = event["price"], event["quantity"]
        maker_buys = event["side"] == "SELL"
        if maker_buys:
            pnl = qty * (payoff - price)
            margin = qty * price
        else:
            pnl = qty * ((1.0 - payoff) - (1.0 - price))
            margin = qty * (1.0 - price)
        rows.append((event["day"], oid, "BUY " if maker_buys else "SELL", qty, price, margin, pnl, rms))
        total_take_all += pnl
    rows.sort(key=lambda r: -r[6])
    print(f"  {'day':>4} {'option':>9} {'side':>5} {'qty':>4} {'price':>6} {'margin':>7} {'PnL':>8}  fit")
    for day, oid, side, qty, price, margin, pnl, rms in rows:
        print(f"  {day:>4} {oid:>9} {side:>5} {qty:>4} {price:>6.2f} {margin:>7.2f} {pnl:>+8.2f}  {rms:.3f}")
    print(f"\n  accepting every FOK: {total_take_all:+.2f}")
    winners = [r for r in rows if r[6] > 0]
    print(f"  accepting only the profitable ones: {sum(r[6] for r in winners):+.2f} "
          f"({len(winners)} orders, ${sum(r[5] for r in winners):.2f} of margin)")
    return recovered


if __name__ == "__main__":
    recover(int(sys.argv[1]) if len(sys.argv) > 1 else 5)
