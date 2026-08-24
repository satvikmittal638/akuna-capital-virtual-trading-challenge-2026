"""A general fill-or-kill acceptance rule: judge on return over risk, with a floor and a ceiling.

The base hurdle is `_MIN_FOK_EDGE + 0.75 * uncertainty`, roughly 0.12 of absolute probability. That
is the wrong unit. For a buy at price p the money at risk is p and the expected gain is q - p, so
what matters is `(q - p) / p`; for a sell at p the risk is 1 - p and the gain is p - q, so it is
`(p - q) / (1 - p)`. An absolute hurdle of 0.12 makes every contract whose whole price is under
0.12 unacceptable no matter how good it looks, and simultaneously waves through an expensive
near-certainty that risks 0.99 to make 0.01.

So: require a fixed return on the margin actually risked, and bound that margin at both ends.

  * `_TAKE_MULTIPLE` 1.5 -- a 50% expected return on risk. Buying needs `q >= 1.5 p`, selling needs
    `(1 - q) >= 1.5 (1 - p)`.
  * `_TAKE_FLOOR` 0.05 -- nothing super cheap. An earlier version of this had only a ceiling, so it
    bought 0.01 lottery tickets, and the terms recovery (which fails its own control, so treat it as
    weak) put those at a loss.
  * `_TAKE_CEILING` 0.40 -- nothing that ties up most of a ten dollar book for a cent of upside.
"""

from __future__ import annotations

TAKE_LAYER = '''

    _TAKE_MULTIPLE: Final[float] = 1.5
    _TAKE_FLOOR: Final[float] = 0.05
    _TAKE_CEILING: Final[float] = 0.40

    def respond_to_fok(self, option: BinaryOption, fok_order: FokOrder) -> bool:
        try:
            if self._worth_taking(option, fok_order):
                return True
        except Exception:
            pass
        return super().respond_to_fok(option, fok_order)

    def _worth_taking(self, option: BinaryOption, fok_order: FokOrder) -> bool:
        """Accept on return over the margin risked, not on an absolute edge."""
        price: float = fok_order.price
        buying: bool = self._buys_on_fok(fok_order.order_type)
        risk: float = price if buying else 1.0 - price
        if risk < self._TAKE_FLOOR or risk > self._TAKE_CEILING:
            return False

        theo: float = self.price_option(option)
        gain: float = (theo - price) if buying else (price - theo)
        if gain < (self._TAKE_MULTIPLE - 1.0) * risk:
            return False

        margin: float = risk * fok_order.quantity
        if margin > self._available_margin() * self._FOK_MARGIN_FRACTION:
            return False
        current: float = float(self.position.option_quantity_by_option_id.get(option.option_id, 0))
        direction: int = 1 if buying else -1
        projected: float = current + direction * fok_order.quantity
        if abs(projected) > self._contract_position_cap and projected * direction > 0.0:
            return False

        self._pending_fok.append((fok_order.option_id, price, direction, fok_order.quantity))
        return True
'''

BARE_HEAD = '''

# GENERAL LAYER -- fill-or-kill acceptance judged on return over risk. No per-case anything.

_BaseMarketMaker = MarketMaker


class MarketMaker(_BaseMarketMaker):  # type: ignore[no-redef]
    """The 16.30 maker, with one change: which fill-or-kill orders are worth taking."""
'''
