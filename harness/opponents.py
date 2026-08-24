"""Clones of the graders competing bots, inferred from their names and their recorded results.

Every bot prices off the same warmed-up estimator, so what is being simulated is each one's
*policy*, not who has the better model. The policies come from the names plus what
`results.txt` shows each bot actually earned:

Stalemate Quoter        0.0, 0.0, 27.0, 0.0, 0.0, 4.0 -- never once negative across six sessions.
                        Only a maker that transacts exclusively at riskless prices can do that,
                        so it bids 0.00 and offers 1.00 in size and declines every FOK. It earns
                        nothing most of the time and harvests a fortune when a large order runs
                        past everyone else's quoted size and fills against its free bid.
Fixed Width w           The stated policy: a constant half-width around its mid. Tighter is more
                        profitable and far more dangerous -- 0.05 both wins sessions and posts
                        the -102.37 in test 19.
Lattice                 A pricing name, not a risk name: quotes tight because it trusts its
                        model, sizes off margin, and lifts fill-or-kills that show real edge.
Situational Unawareness Prices correctly but is blind to its own book -- no inventory skew, no
                        position limit, fixed size, and it never takes a fill-or-kill. Modest,
                        reliably positive: 3.06, 0.07, 14.6, 22.9.
Mongoose                Predatory. Quotes very tight for size and takes almost any fill-or-kill
                        showing a positive edge, which is why it is usually the biggest loser:
                        0.3, -17.29, -6.46, -14.13, -28.07.
"""

import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)

import math

from bot import BinaryOption, FokOrder, MarketHistory, MarketMaker, OrderType, Quote


class _Base:
    """Shared plumbing: a warmed-up estimator for pricing plus margin bookkeeping."""

    half_spread = 0.10
    size = 20
    takes_fok = False
    fok_edge = 0.05
    uses_margin = True

    def __init__(self, label: str, cash: float) -> None:
        self._label = label
        self._cash = cash
        self._initial = max(cash, 1.0)
        self._model: MarketMaker | None = None
        self._position: dict[int, int] = {}

    @property
    def name(self) -> str:
        return self._label

    def warm_up(self, history: MarketHistory) -> None:
        if self._model is not None:
            self._model.warm_up(history)

    def attach(self, model: MarketMaker) -> None:
        self._model = model

    def price_option(self, option: BinaryOption) -> float:
        return self._model.price_option(option) if self._model is not None else 0.5

    def on_step_advance(self, underlyings, options) -> None:
        if self._model is not None:
            self._model.on_step_advance(underlyings, options)

    def on_trade(self, option: BinaryOption, price: float, quantity: int, counterparty_id: int) -> None:
        self._position[option.option_id] = self._position.get(option.option_id, 0) + quantity
        self._cash -= quantity * price if quantity > 0 else (-quantity) * (1.0 - price)

    def credit(self, option_id: int, payoff: float, bought: int, sold: int) -> None:
        self._cash += bought * payoff + sold * (1.0 - payoff)
        self._position.pop(option_id, None)

    def _affordable(self, margin_per_contract: float) -> int:
        if not self.uses_margin:
            return self.size
        free = max(0.0, self._cash)
        if margin_per_contract <= 1e-9:
            return self.size
        return max(0, min(self.size, int(free * 0.5 / margin_per_contract)))

    def quote(self, option: BinaryOption, counterparty_id: int) -> Quote:
        mid = self.price_option(option)
        bid = max(0, min(99, math.floor((mid - self.half_spread) * 100)))
        offer = max(bid + 1, min(100, math.ceil((mid + self.half_spread) * 100)))
        bq = max(1, self._affordable(bid / 100.0))
        oq = max(1, self._affordable(1.0 - offer / 100.0))
        return Quote(bid_price=bid / 100.0, bid_quantity=bq, offer_price=offer / 100.0, offer_quantity=oq)

    def respond_to_fok(self, option: BinaryOption, order: FokOrder) -> bool:
        if not self.takes_fok:
            return False
        theo = self.price_option(option)
        if order.order_type == OrderType.BUY:      # they buy, we sell
            edge, margin = order.price - theo, (1.0 - order.price) * order.quantity
        else:
            edge, margin = theo - order.price, order.price * order.quantity
        return edge >= self.fok_edge and margin <= max(0.0, self._cash) * 0.5


class FixedWidth(_Base):
    def __init__(self, width: float, cash: float) -> None:
        super().__init__(f"Fixed Width {width:g}", cash)
        self.half_spread = width
        self.size = 30
        self.takes_fok = True
        self.fok_edge = width


class StalemateQuoter(_Base):
    """Bids zero, offers one, never takes. Cannot lose a cent; occasionally wins a fortune."""

    def __init__(self, cash: float) -> None:
        super().__init__("Stalemate Quoter", cash)
        self.size = 60
        self.takes_fok = False

    def quote(self, option: BinaryOption, counterparty_id: int) -> Quote:
        return Quote(bid_price=0.0, bid_quantity=self.size, offer_price=1.0, offer_quantity=self.size)


class Lattice(_Base):
    def __init__(self, cash: float) -> None:
        super().__init__("Lattice", cash)
        self.half_spread = 0.04
        self.size = 30
        self.takes_fok = True
        self.fok_edge = 0.03


class SituationalUnawareness(_Base):
    """Correct prices, no awareness of its own book: fixed size, no skew, no limits, never takes."""

    def __init__(self, cash: float) -> None:
        super().__init__("Situational Unawareness", cash)
        self.half_spread = 0.12
        self.size = 12
        self.takes_fok = False
        self.uses_margin = False


class Mongoose(_Base):
    def __init__(self, cash: float) -> None:
        super().__init__("Mongoose", cash)
        self.half_spread = 0.02
        self.size = 40
        self.takes_fok = True
        self.fok_edge = 0.0


def build(name: str, cash: float) -> _Base:
    if name.startswith("Fixed Width"):
        return FixedWidth(float(name.rsplit(" ", 1)[1]), cash)
    return {"Stalemate Quoter": StalemateQuoter, "Lattice": Lattice,
            "Situational Unawareness": SituationalUnawareness, "Mongoose": Mongoose}[name](cash)
