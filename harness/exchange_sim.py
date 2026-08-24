"""Replay of the graders exchange: real cases, real flow, real price paths, real opponents.

Exact from the scraped data -- starting cash, warm-up history, initial book, session length, the
realised trajectory (so settlement is the true one), the recorded event schedule with real days,
counterparties and FOK terms, and the actual field of competitors each test was run against.

The one structural inference: RFQ orders fill *down the quote ladder with no limit price*. That
is forced by the results. Stalemate Quoter bids 0.00 and offers 1.00, never posts a negative
session, and still earned $27 in test 4 -- which is only possible if size a better-priced maker
declined to quote spills onto its free bid. Order size is therefore what decides how much of
that spillage exists, and it is the one thing never recorded.
"""

import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)

import json, math, random, re, statistics as st, sys
from collections import defaultdict

import opponents
import real_sim
from bot import BinaryOption, FokOrder, MarketHistory, MarketMaker, OptionLeg, OrderType, _price_with_params
from exchange import Account, Rotation, allocate_fok, allocate_rfq, collect_quotes
from sim import Contract
from real_sim import (AJARAI_UNDERLYING_ID as AJR, FED_FUNDS_RATE_UNDERLYING_ID as FED,
                      THERIODIC_UNDERLYING_ID as THR, COMPANY_IDS, inverse_normal_cdf, underlyings)

# Recalibrated after the matching engine was corrected. Under the old winner-takes-all rule this
# sat at 4 and Stalemate Quoter's recorded $27 in test 4 was unreachable at any setting; with ties
# split equally it lands at $25.00, and 12 is also the best fit across every recorded session.
_UNSET = object()
RFQ_SIZE_MAX = 12
# How far through fair a counterparty will still deal. `ps.md` says the tests "vary in difficulty,
# including via different counterparties", and the recorded win rates bear that out -- they run from
# 100% in test 4 down to 24% in test 15. So this is fitted per case against the recorded win rate.
# Test 4 needs no reserve at all, which is also the only setting that reproduces Stalemate Quoter's
# recorded $27 there: its 0.00/1.00 market only ever trades on the residual of an order that has
# swept every better price off the book.
#
# Caveat, measured: fitting these does NOT improve rank reproduction (6/16, against 8/16 with no
# reserve at all) and leaves PnL errors near $14. Use this harness to study fill composition, which
# it now matches, and not to predict scores, which it cannot.
CASE_RESERVE: dict[int, float | None] = {4: None, 5: 0.025, 6: 0.01, 7: 0.01, 8: 0.01, 9: 0.01, 10: 0.025, 11: 0.015, 12: 0.015, 13: 0.015, 14: 0.03, 15: 0.01, 16: 0.1, 17: 0.01, 18: 0.015}
RESERVE = 0.02        # fallback for a case with no fitted value
WILD_FRACTION = 0.0


def load_truth(path: str = _os.path.join(_ROOT, "results", "results.txt")) -> list[dict]:
    rows, cur = [], None
    for ln in open(path).read().split("\n"):
        if ln.strip() == "Ranking:":
            cur = []; continue
        m = re.match(r"\s*(\d+)\. (.+?): \$(-?[\d.]+)\s*$", ln)
        if m and cur is not None:
            cur.append((m.group(2), float(m.group(3)))); continue
        s = re.search(r"Result: PASS \(score=([\d.]+)\)", ln)
        if s and cur:
            rows.append({"entries": cur, "score": float(s.group(1))}); cur = None
    out = []
    for i, r in enumerate(rows):
        names = [e[0] for e in r["entries"]]
        out.append({"test": i + 1, "verbose": i < 3, "entries": r["entries"], "score": r["score"],
                    "our": dict(r["entries"])["Telescoping Theo"],
                    "rank": names.index("Telescoping Theo") + 1,
                    "field": [n for n in names if n != "Telescoping Theo"]})
    return out


def synthesise(oid, first_day, last_day, hints, values, rng, generator):
    target = min(max(st.median(hints) if hints else rng.choice(real_sim.MONEYNESS_SAMPLE), 0.005), 0.995)
    expiry = last_day + rng.randint(0, 2)
    steps = max(expiry - first_day, 1)
    draw = rng.random()
    if draw < 0.57:
        uid = rng.choice(COMPANY_IDS)
        legs = (OptionLeg(underlying_id=uid, weight=1.0),)
        c = generator.company_params(uid)
        mean = math.log(values[uid]) + steps * c.drift
        sd = math.sqrt(max(steps * c.variance, 1e-12))
        strike = max(float(round(math.exp(mean - sd * inverse_normal_cdf(target)))), 1.0)
    elif draw < 0.77:
        legs = (OptionLeg(underlying_id=FED, weight=1.0),)
        best, strike = None, values[FED]
        for off in range(-steps - 1, steps + 2):
            cand = max(round(values[FED] + 0.25 * off, 2), 0.0)
            d = abs(_price_with_params(generator, values,
                    BinaryOption(legs=legs, option_id=1, steps_until_expiry=steps, strike=cand)) - target)
            if best is None or d < best: best, strike = d, cand
    else:
        legs = (OptionLeg(underlying_id=THR, weight=1.0), OptionLeg(underlying_id=AJR, weight=-1.0))
        strike = 0.0
    return Contract(option_id=oid, legs=legs, strike=strike, expiry_day=expiry)


class Book(Account):
    """The shared `Account`, plus the starting balance the ranking report needs.

    This used to carry its own copy of the margin bookkeeping, which then drifted -- its `apply`
    never learned the `was_fok` flag the others grew. Inheriting keeps one implementation.
    """

    def __init__(self, maker, cash):
        super().__init__(maker=maker, cash=cash)
        self.start = cash

    @property
    def pos(self):
        return self.positions


def run(case, truth_row, seed, rfq_max=RFQ_SIZE_MAX, tol=_UNSET, wild=WILD_FRACTION, observer=None):
    cid = int(case["testcase_id"])
    if tol is _UNSET:
        tol = CASE_RESERVE.get(cid, RESERVE)
    path = real_sim.LIVE_PATHS[cid]
    days = len(path) - 1
    cash = case["initial_state"]["starting_cash"]
    rng = random.Random(seed)
    generator = real_sim.generator_params(case, real_sim.historical_drifts(case))
    history = MarketHistory({real_sim.NAME_TO_ID[k]: tuple(v) for k, v in case["history"].items()})
    initial = [real_sim.parse_option(t) for t in case["initial_active_options"]]

    ours = MarketMaker(underlyings(path[0]), initial, cash)
    books = {ours.name: Book(ours, cash)}
    if observer is not None:
        # Rebind every run: a profile accumulated across cases would otherwise keep pricing with
        # the first session's maker, which reports edges for contracts it has never seen.
        observer.theo_fn = ours.price_option
    for label in truth_row["field"]:
        rival = opponents.build(label, cash)
        pricer = MarketMaker(underlyings(path[0]), initial, cash)
        rival.attach(pricer)
        books[rival.name] = Book(rival, cash)
    for b in books.values():
        b.maker.warm_up(history)
        b.maker.on_step_advance(underlyings(path[0]), initial)

    events, pend = [], None
    for e in sorted(real_sim.FLOW.get(cid, []), key=lambda x: x["day"]):
        if e["action"] == "TRADE":
            if pend is not None and e["option_id"] == pend["option_id"]:
                pend["cp_buys"] = e["quantity"] < 0
            continue
        pend = dict(e, cp_buys=None) if e["action"] == "RFQ" else dict(e)
        events.append(pend)

    contracts = {o.option_id: Contract(option_id=o.option_id, legs=o.legs, strike=o.strike,
                                       expiry_day=o.steps_until_expiry) for o in initial}
    span, hints = {}, defaultdict(list)
    for e in events:
        oid, d = e["option_id"], min(e["day"], days - 1)
        lo, hi = span.get(oid, (d, d)); span[oid] = (min(lo, d), max(hi, d))
        if e["action"] == "FOK": hints[oid].append(e["price"])
    for oid, (lo, hi) in span.items():
        if oid not in contracts:
            contracts[oid] = synthesise(oid, lo, hi, hints[oid], path[lo], rng, generator)

    rotation = Rotation()
    by_day = defaultdict(list)
    for e in events: by_day[min(e["day"], days - 1)].append(e)

    for day in range(days):
        values = path[day]
        for e in by_day[day]:
            c = contracts[e["option_id"]]
            if c.expiry_day < day: continue
            option, cp = c.at(day), e["counterparty_id"]
            if e["action"] == "RFQ":
                quotes = collect_quotes(books, option, cp)
                buys = e["cp_buys"] if e["cp_buys"] is not None else (rng.random() < 0.5)
                qty = rng.randint(1, rfq_max)
                fair = _price_with_params(generator, values, option)
                # Counterparties are not alike. Most will only deal within `tol` of fair -- fitting
                # that against the recorded win rates puts it near two cents. But a minority deal at
                # any price at all, which is the only way Stalemate Quoter's 0.00/1.00 market ever
                # trades, and it earned $27 in test 4. `wild` is that minority's share.
                if tol is None or rng.random() < wild:
                    limit = None
                else:
                    limit = fair + tol if buys else fair - tol
                side = OrderType.BUY if buys else OrderType.SELL
                allocate_rfq(books, option, quotes, side, qty, cp, rotation, limit, observer)
            else:
                order = FokOrder(counterparty_id=cp, option_id=option.option_id,
                                 order_type=OrderType.BUY if e["side"] == "BUY" else OrderType.SELL,
                                 price=e["price"], quantity=e["quantity"])
                allocate_fok(books, option, order, rotation, observer)

        for c in [c for c in contracts.values() if c.expiry_day == day]:
            payoff = c.at(c.expiry_day).expiry_valuation(path[day])
            for b in books.values():
                bought, sold = b.bought.pop(c.option_id, 0), b.sold.pop(c.option_id, 0)
                b.cash += bought * payoff + sold * (1.0 - payoff)
                b.pos.pop(c.option_id, None)
                if hasattr(b.maker, "credit"): b.maker.credit(c.option_id, payoff, bought, sold)
        for b in books.values():
            if not b.bankrupt and b.cash < 0.0: b.bankrupt = True
        nxt = [c.at(day + 1) for c in contracts.values() if c.expiry_day >= day + 1]
        for b in books.values(): b.maker.on_step_advance(underlyings(path[day + 1]), nxt)

    out = {}
    for n, b in books.items():
        mark = sum(q * _price_with_params(generator, path[days], contracts[oid].at(days))
                   for oid, q in b.pos.items() if q and oid in contracts)
        out[n] = -b.start if b.bankrupt else b.cash + mark - b.start
    return out
