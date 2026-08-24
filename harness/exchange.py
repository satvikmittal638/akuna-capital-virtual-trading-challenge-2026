"""The exchange's matching rules, in one place.

Until now `sim.py`, `exchange_sim.py` and `arena.py` each carried their own matching loop and they
had drifted into disagreement -- tolerance-band winner-takes-all in one, sorted winner-takes-all in
another, pro-rata with a rotating remainder in the third. A rule fixed in one stayed broken in the
other two, which is exactly what happened when the tie-break bias was found.

Two rules here are set by the recorded logs rather than by guesswork:

**Ties split equally, not pro rata.** `results.txt` records a request to sell 6 where our bot bid
0.00 for 4 and filled 3. Stalemate Quoter is in that session and also bids 0.00, in far more size.
An equal split of 6 between the two makers standing at the best price gives exactly 3; splitting in
proportion to quoted size would have given us well under one. So the size a maker shows sets a cap
on what it can be handed, not its share of the order.

**Customers need not have a reserve price.** In that same fill the customer sold at 0.00 -- the
worst price on the book, accepted without complaint. `ps.md` describes the exchange as routing to
the best bid or offer and splitting if necessary, and says nothing about the customer refusing. The
`limit` argument is therefore optional and defaults to off; harnesses that want a price-sensitive
customer must ask for one.
"""

import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)

from dataclasses import dataclass, field
from collections import defaultdict

from bot import BinaryOption, FokOrder, OrderType, Quote


@dataclass
class Account:
    """A maker's cash, position and margin bookkeeping, mirroring the autograder's own.

    Margin follows `ps.md`: every trade debits its own maximum loss, and each is released
    separately when the contract expires, which is why bought and sold lots are tracked apart.
    """

    maker: object
    cash: float
    positions: dict[int, int] = field(default_factory=lambda: defaultdict(int))
    bought: dict[int, int] = field(default_factory=lambda: defaultdict(int))
    sold: dict[int, int] = field(default_factory=lambda: defaultdict(int))
    bankrupt: bool = False
    volume: int = 0
    # (option_id, counterparty_id, quantity, price, was_fok) for the PnL attribution report.
    trades: list[tuple[int, int, int, float, bool]] = field(default_factory=list)

    def apply(
        self, option: BinaryOption, price: float, quantity: int, counterparty_id: int, was_fok: bool = False
    ) -> None:
        self.cash -= quantity * price if quantity > 0 else (-quantity) * (1.0 - price)
        self.positions[option.option_id] += quantity
        if quantity > 0:
            self.bought[option.option_id] += quantity
        else:
            self.sold[option.option_id] += -quantity
        self.volume += abs(quantity)
        self.trades.append((option.option_id, counterparty_id, quantity, price, was_fok))
        self.maker.on_trade(option, price, quantity, counterparty_id)


class Rotation:
    """Whose turn it is to receive an indivisible leftover lot.

    Without this the leftover always lands on whoever sorts first, and in a field of near-identical
    makers that is a standing advantage rather than a rounding detail.
    """

    def __init__(self) -> None:
        self._turn = 0

    def next(self, count: int) -> int:
        if count <= 0:
            return 0
        offset = self._turn % count
        self._turn += 1
        return offset


def equal_split(total: int, caps: list[int], rotation: Rotation) -> list[int]:
    """Divide `total` as evenly as the caps allow, leftovers going round-robin.

    Makers that hit their quoted size drop out and their unused share is re-divided among the rest,
    so a maker showing size 1 alongside one showing size 60 does not strand 59 lots.
    """
    allocation: list[int] = [0] * len(caps)
    active: list[int] = [index for index, cap in enumerate(caps) if cap > 0]
    while total > 0 and active:
        share: int = total // len(active)
        if share == 0:
            break
        for index in list(active):
            given: int = min(share, caps[index] - allocation[index])
            allocation[index] += given
            total -= given
            if allocation[index] >= caps[index]:
                active.remove(index)
    # Fewer lots remain than there are makers still standing, so each can take at most one more.
    if total > 0 and active:
        offset: int = rotation.next(len(active))
        for step in range(total):
            allocation[active[(offset + step) % len(active)]] += 1
    return allocation


def collect_quotes(
    accounts: dict[str, Account], option: BinaryOption, counterparty_id: int
) -> list[tuple[str, Quote]]:
    return [
        (name, account.maker.quote(option, counterparty_id))
        for name, account in accounts.items()
        if not account.bankrupt
    ]


def allocate_rfq(
    accounts: dict[str, Account],
    option: BinaryOption,
    quotes: list[tuple[str, Quote]],
    side: OrderType,
    quantity: int,
    counterparty_id: int,
    rotation: Rotation,
    limit: float | None = None,
    observer: "object | None" = None,
) -> int:
    """Route a request for quote to the best price, splitting equally and cascading down the book.

    Returns the quantity left unfilled. `limit` is the customer's reserve price if it has one; the
    recorded flow shows customers filling at 0.00 and 1.00, so it defaults to absent.
    """
    requested: int = quantity
    buying: bool = side == OrderType.BUY
    price_of = (lambda q: q.offer_price) if buying else (lambda q: q.bid_price)
    size_of = (lambda q: q.offer_quantity) if buying else (lambda q: q.bid_quantity)
    ladder = sorted(quotes, key=lambda item: price_of(item[1]) if buying else -price_of(item[1]))

    fills: dict[str, int] = {}
    position: int = 0
    while position < len(ladder) and quantity > 0:
        price: float = price_of(ladder[position][1])
        if limit is not None and ((buying and price > limit) or (not buying and price < limit)):
            break
        level = [item for item in ladder[position:] if price_of(item[1]) == price]
        position += len(level)
        caps = [max(0, size_of(quote)) for _, quote in level]
        for (name, quote), filled in zip(level, equal_split(quantity, caps, rotation)):
            if filled <= 0:
                continue
            accounts[name].apply(option, price, -filled if buying else filled, counterparty_id)
            fills[name] = fills.get(name, 0) + filled
            quantity -= filled
    if observer is not None:
        observer.on_rfq(option, side, requested, quotes, fills, counterparty_id, limit)
    return quantity


def allocate_fok(
    accounts: dict[str, Account],
    option: BinaryOption,
    order: FokOrder,
    rotation: Rotation,
    observer: "object | None" = None,
) -> list[str]:
    """Offer a fill-or-kill to every solvent maker and split it equally among those that want it."""
    shown = [name for name, account in accounts.items() if not account.bankrupt]
    acceptors = [name for name in shown if accounts[name].maker.respond_to_fok(option, order)]
    if observer is not None:
        observer.on_fok(option, order, shown, acceptors)
    if not acceptors:
        return []
    caps = [order.quantity] * len(acceptors)
    for name, filled in zip(acceptors, equal_split(order.quantity, caps, rotation)):
        if filled <= 0:
            continue
        signed: int = -filled if order.order_type == OrderType.BUY else filled
        accounts[name].apply(option, order.price, signed, order.counterparty_id, was_fok=True)
    return acceptors
