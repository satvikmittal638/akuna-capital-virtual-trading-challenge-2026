"""What is actually wrong in test case 5, measured against realised settlement.

Deliberately avoids `exchange_sim`. The only quantity measured here is **pricing accuracy against
what really happened**, on a neutral grid of contracts -- the same style of measurement as
HANDOFF sec.7, and the one thing about this case that can be established without a simulator whose
rank reproduction is chance.

    python3.13 variants/case5_study.py
    python3.13 variants/case5_study.py 7      # any other case
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

# Neutral and fixed in advance: strikes as fractions of the day's level, every expiry the flow
# actually shows. Nothing here is chosen after seeing a result.
COMPANY_STRIKE_RATIOS = (0.90, 0.95, 1.00, 1.05, 1.10)
EXPIRIES = (1, 2, 3, 4, 5)


def load(case_id: int):
    with open(os.path.join(_ROOT, "data", "full_data.json")) as handle:
        case = {int(c["testcase_id"]): c for c in json.load(handle)}[case_id]
    with open(os.path.join(_ROOT, "data", "live_market.json")) as handle:
        path = {c["test_case_id"]: c["trajectory"] for c in json.load(handle)["test_cases"]}[case_id]
    values = [{NAME_TO_ID[k]: point[k] for k in ("FED", "AJR", "THR")} for point in path]
    return case, values


def contracts(day_values: dict[int, float], day: int, horizon: int):
    """Every contract shape the flow shows, anchored on the day's levels."""
    out = []
    for steps in EXPIRIES:
        if day + steps > horizon:
            continue
        for uid in (AJR, THR):
            for ratio in COMPANY_STRIKE_RATIOS:
                strike = round(day_values[uid] * ratio)
                out.append(bot.BinaryOption(
                    legs=(bot.OptionLeg(underlying_id=uid, weight=1.0),),
                    option_id=0, steps_until_expiry=steps, strike=float(strike)))
        for offset in (-1, 0, 1):
            out.append(bot.BinaryOption(
                legs=(bot.OptionLeg(underlying_id=FED, weight=1.0),),
                option_id=0, steps_until_expiry=steps,
                strike=max(round(day_values[FED] + 0.25 * offset, 2), 0.0)))
        out.append(bot.BinaryOption(
            legs=(bot.OptionLeg(underlying_id=THR, weight=1.0), bot.OptionLeg(underlying_id=AJR, weight=-1.0)),
            option_id=0, steps_until_expiry=steps, strike=0.0))
    return out


def trend_params(params, history: dict[int, tuple[float, ...]], lookback: int, weight: float):
    """Blend each company's estimated drift toward its own recent realised momentum.

    The base estimator shrinks drift hard toward a cross-case prior, which is right on average and
    wrong whenever one name is actually trending. This is the single mechanism under test.
    """
    company = dict(params.company)
    for uid in (AJR, THR):
        series = history.get(uid, ())
        span = min(lookback, len(series) - 1)
        if span < 1:
            continue
        momentum = math.log(series[-1] / series[-1 - span]) / span
        current = company[uid]
        company[uid] = bot._CompanyParams(
            drift=(1.0 - weight) * current.drift + weight * momentum,
            rate_beta=current.rate_beta,
            variance=current.variance,
        )
    return bot.replace(params, company=company)


def score(case_id: int = 5) -> None:
    case, values = load(case_id)
    horizon = len(values) - 1
    history = {NAME_TO_ID[k]: tuple(v) for k, v in case["history"].items()}

    maker = bot.MarketMaker(
        [bot.Underlying(name=k, underlying_id=NAME_TO_ID[k], value=v)
         for k, v in case["initial_state"]["underlyings"].items()],
        [], case["initial_state"]["starting_cash"])
    maker.warm_up(bot.MarketHistory(history))
    base = maker._params

    print(f"=== case {case_id} ===")
    for name, uid in (("AJR", AJR), ("THR", THR)):
        series = history[uid]
        hist_drift = math.log(series[-1] / series[0]) / (len(series) - 1)
        live_drift = math.log(values[-1][uid] / values[0][uid]) / horizon
        print(f"  {name}: history drift {hist_drift:+.5f}/d   model drift {base.company[uid].drift:+.5f}/d   "
              f"REALISED {live_drift:+.5f}/d   model vol {math.sqrt(base.company[uid].variance):.4f}")

    variants = {"base": base}
    for lookback in (3, 5, 8):
        for weight in (0.5, 1.0):
            variants[f"trend{lookback}w{weight:g}"] = trend_params(base, history, lookback, weight)
    for factor in (2.0, 3.0):
        variants[f"vol{factor:g}"] = base.with_variance_scale(factor)
    variants["trend5w1+vol2"] = trend_params(base, history, 5, 1.0).with_variance_scale(2.0)

    print(f"\n  {'variant':<16} {'Brier':>7} {'bias':>8} {'THR-call bias':>14} {'spread bias':>12}   n")
    for name, params in variants.items():
        briers, bias, thr_bias, spread_bias = [], [], [], []
        for day in range(horizon):
            for option in contracts(values[day], day, horizon):
                price = bot._price_with_params(params, values[day], option)
                actual = option.expiry_valuation(values[day + option.steps_until_expiry])
                briers.append((price - actual) ** 2)
                bias.append(price - actual)
                if len(option.legs) == 2:
                    spread_bias.append(price - actual)
                elif option.legs[0].underlying_id == THR:
                    thr_bias.append(price - actual)
        print(f"  {name:<16} {statistics.mean(briers):>7.4f} {statistics.mean(bias):>+8.4f} "
              f"{statistics.mean(thr_bias):>+14.4f} {statistics.mean(spread_bias):>+12.4f}   {len(briers)}")
    print("\n  bias = mean(price - realised). Negative means the model priced the event too cheaply.")


if __name__ == "__main__":
    score(int(sys.argv[1]) if len(sys.argv) > 1 else 5)
