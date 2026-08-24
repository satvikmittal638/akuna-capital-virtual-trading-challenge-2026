"""What the bot actually traded -- not what it earned.

Every harness in this project reported PnL and rank, and every one of them missed this: across the
fifteen scored test cases we hold recorded flow for, the bot won 394 requests for quote and **every
single fill was at 0.01 or 0.99, one lot at a time**, while all 820 fill-or-kill orders it was
shown -- averaging thirteen lots each -- were declined. It wins nearly half the auctions it enters
and never once traded in the middle of the distribution, which is the only place a market maker
earns. No score could have shown that; only the composition of the fills does.

So this module makes composition the primary output. It attaches to the shared matching engine as
an observer, records what was quoted, what filled, and what was passed up, and prints it beside the
recorded truth. If a harness cannot reproduce a profile we already know the answer to, nothing else
it reports should be believed.

    python3.13 profile.py                     # profile the current bot over the replayed cases
    python3.13 profile.py --against-recorded  # and print the recorded profile beside it
"""

import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)

import json
import statistics
import sys
from collections import Counter

from bot import OrderType

PRICE_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("0.00-0.02", -0.01, 0.02),
    ("0.02-0.10", 0.02, 0.10),
    ("0.10-0.90", 0.10, 0.90),
    ("0.90-0.98", 0.90, 0.98),
    ("0.98-1.00", 0.98, 1.01),
)
RECORDED_PATH = _os.path.join(_ROOT, "data", "competitor_flow_data.json")


def bucket_of(price: float) -> str:
    for name, low, high in PRICE_BUCKETS:
        if low <= price < high:
            return name
    return PRICE_BUCKETS[-1][0]


class Profile:
    """Observer for `exchange.allocate_rfq` / `allocate_fok`, scoped to one maker."""

    def __init__(self, subject: str, theo_fn=None) -> None:
        self.subject = subject
        self.theo_fn = theo_fn
        self.rfq_shown = 0
        self.rfq_won = 0
        self.fills: list[tuple[float, int]] = []          # (price, quantity)
        self.size_pairs: list[tuple[int, int]] = []       # (quoted, filled) when we won
        self.loss_gap: list[float] = []                   # our price minus the winning price
        self.lost_to_nobody = 0                           # nobody filled: the customer walked
        self.fok_shown = 0
        self.fok_taken = 0
        self.fok_edge: dict[str, list[float]] = {"taken": [], "declined": []}
        self.fok_size: dict[str, list[int]] = {"taken": [], "declined": []}

    # -- engine hooks ----------------------------------------------------------------------
    def on_rfq(self, option, side, requested, quotes, fills, counterparty_id, limit) -> None:
        mine = next((q for name, q in quotes if name == self.subject), None)
        if mine is None:
            return
        self.rfq_shown += 1
        buying = side == OrderType.BUY
        my_price = mine.offer_price if buying else mine.bid_price
        my_size = mine.offer_quantity if buying else mine.bid_quantity
        filled = fills.get(self.subject, 0)
        if filled:
            self.rfq_won += 1
            self.fills.append((my_price, filled))
            self.size_pairs.append((my_size, filled))
            return
        if not fills:
            self.lost_to_nobody += 1
            return
        # Somebody traded and it was not us: by how much were we out of the running?
        winner = min(fills, key=lambda n: next(
            (q.offer_price if buying else -q.bid_price) for name, q in quotes if name == n))
        best = next((q.offer_price if buying else q.bid_price) for name, q in quotes if name == winner)
        self.loss_gap.append(my_price - best if buying else best - my_price)

    def on_fok(self, option, order, shown, acceptors) -> None:
        if self.subject not in shown:
            return
        self.fok_shown += 1
        took = self.subject in acceptors
        self.fok_taken += int(took)
        key = "taken" if took else "declined"
        self.fok_size[key].append(order.quantity)
        if self.theo_fn is not None:
            theo = self.theo_fn(option)
            # They buy, we sell: our edge is what they overpay. They sell, we buy: the reverse.
            self.fok_edge[key].append(
                order.price - theo if order.order_type == OrderType.BUY else theo - order.price)

    # -- reporting -------------------------------------------------------------------------
    def report(self) -> str:
        out: list[str] = []
        total = sum(quantity for _, quantity in self.fills)
        out.append(f"  requests for quote shown   {self.rfq_shown}")
        if self.rfq_shown:
            out.append(f"  won                        {self.rfq_won}  ({self.rfq_won / self.rfq_shown:.1%})")
        out.append(f"  lots filled                {total}")
        if self.fills:
            counts: Counter = Counter()
            for price, quantity in self.fills:
                counts[bucket_of(price)] += quantity
            out.append("\n  fill price (by lot)")
            for name, _, _ in PRICE_BUCKETS:
                share = counts[name] / total if total else 0.0
                bar = "#" * round(40 * share)
                out.append(f"    {name:<11}{counts[name]:>6}  {share:>6.1%} {bar}")
            extreme = counts["0.00-0.02"] + counts["0.98-1.00"]
            out.append(f"    -> settled-already fills: {extreme / total:.1%} of lots")

            sizes = [quantity for _, quantity in self.fills]
            out.append(f"\n  fill size    mean {statistics.mean(sizes):.1f}  median "
                       f"{statistics.median(sizes):g}  max {max(sizes)}")
            quoted = [q for q, _ in self.size_pairs]
            out.append(f"  size we showed on the winning side: mean {statistics.mean(quoted):.1f}  "
                       f"max {max(quoted)}")
            capped = sum(1 for q, f in self.size_pairs if f >= q)
            out.append(f"  fills that took our whole quoted size: {capped}/{len(self.size_pairs)}"
                       f"  ({capped / len(self.size_pairs):.0%})"
                       "   <- high means our own size is the binding constraint")
        if self.loss_gap:
            gaps = sorted(self.loss_gap)
            out.append(f"\n  lost {len(gaps)} auctions to a better price; how far behind we were:")
            out.append(f"    median {statistics.median(gaps):.3f}   "
                       f"p10 {gaps[len(gaps) // 10]:.3f}   p90 {gaps[9 * len(gaps) // 10]:.3f}")
            near = sum(1 for g in gaps if g <= 0.01)
            out.append(f"    within one penny of winning: {near} ({near / len(gaps):.0%})")
        if self.lost_to_nobody:
            out.append(f"  auctions nobody filled: {self.lost_to_nobody}")
        out.append(f"\n  fill-or-kill shown         {self.fok_shown}")
        if self.fok_shown:
            out.append(f"  accepted                   {self.fok_taken}  "
                       f"({self.fok_taken / self.fok_shown:.1%})")
            for key in ("taken", "declined"):
                if self.fok_edge[key]:
                    edges = self.fok_edge[key]
                    out.append(f"    {key:<9} n={len(edges):<5} median model edge "
                               f"{statistics.median(edges):+.3f}  mean size "
                               f"{statistics.mean(self.fok_size[key]):.1f}")
        return "\n".join(out)


def recorded_profile(path: str = RECORDED_PATH) -> str:
    """The same profile, measured from the grader's own recorded flow."""
    try:
        cases = json.load(open(path))["test_cases"]
    except OSError:
        return (f"  ({path} not found -- regenerate with `python3.13 get_market_events.py`)")
    counts: Counter = Counter()
    sizes: list[int] = []
    rfq = won = fok = 0
    fok_sizes: list[int] = []
    for case in cases:
        events = case["events"]
        for index, event in enumerate(events):
            if event["action"] == "RFQ":
                rfq += 1
            elif event["action"] == "FOK":
                fok += 1
                fok_sizes.append(event["quantity"])
            elif event["action"] == "TRADE":
                won += 1
                quantity = abs(event["quantity"])
                counts[bucket_of(event["price"])] += quantity
                sizes.append(quantity)
                # A trade only ever follows the request it answers.
                if index and events[index - 1]["action"] == "FOK":
                    counts["__fok__"] += 1
    total = sum(v for k, v in counts.items() if k != "__fok__")
    out = [f"  requests for quote shown   {rfq}",
           f"  won                        {won}  ({won / rfq:.1%})",
           f"  lots filled                {total}", "", "  fill price (by lot)"]
    for name, _, _ in PRICE_BUCKETS:
        share = counts[name] / total if total else 0.0
        out.append(f"    {name:<11}{counts[name]:>6}  {share:>6.1%} {'#' * round(40 * share)}")
    extreme = counts["0.00-0.02"] + counts["0.98-1.00"]
    out.append(f"    -> settled-already fills: {extreme / total:.1%} of lots")
    out.append(f"\n  fill size    mean {statistics.mean(sizes):.1f}  median "
               f"{statistics.median(sizes):g}  max {max(sizes)}")
    out.append("    (the dump may record direction rather than true size -- results.txt shows"
               "\n     verbose-test fills of 3 and 17 lots, so treat this line as a lower bound)")
    out.append(f"\n  fill-or-kill shown         {fok}")
    out.append(f"  accepted                   {counts['__fok__']}  "
               f"({counts['__fok__'] / fok:.1%})   mean size {statistics.mean(fok_sizes):.1f}")
    return "\n".join(out)


def main() -> int:
    import exchange_sim
    import real_sim

    truth = exchange_sim.load_truth()
    cases = {int(c["testcase_id"]): c for c in real_sim.load_cases()}
    rows = [r for r in truth if r["test"] in cases and real_sim.LIVE_PATHS.get(r["test"])]

    subject = "Telescoping Theo"
    combined = Profile(subject)
    for row in rows:
        for seed in range(4):
            exchange_sim.run(cases[row["test"]], row, 9000 + seed, observer=combined)
    print(f"FILL PROFILE -- current bot.py, replayed over {len(rows)} recorded sessions x 4 seeds\n")
    print(combined.report())
    if "--against-recorded" in sys.argv:
        print("\n\nRECORDED -- what the grader logged for the submitted bot\n")
        print(recorded_profile())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
