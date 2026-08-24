"""Local clones of the live-arena bots, modelled from spectated order books.

Built by watching `wss://.../ws` with the throwaway `spectator-dummy` maker (which quotes the
riskless 0.00/1.00 and declines every FOK, so it leaks nothing) and reading the books the arena
broadcasts to every client. `variants/parse_arena.py` reduces a capture to per-maker statistics;
the parameters below are those statistics, not guesses.

What is modelled is each bot's *policy* -- half-spread, size, boundary habit, FOK appetite -- not
its pricing. As in `harness/opponents.py`, every clone prices off the same warmed-up estimator the
bot uses, so a local match compares quoting behaviour on a level model, which is the whole point.

Observed nature (medians over the first capture; refined from the full log):

  HelloWorld    the winner. Very wide (half ~0.15, out to 0.50), moderate size, quotes the
                boundary ~half the time. Trades little and rarely on the wrong side -- it lets
                informed flow walk past and collects the crossers.
  3 Rings       tight (half ~0.045) but stacks large size, especially on the safe side; high
                volume (36 trades) and a strong second. A size-and-tightness predator.
  Ar4yu         floods size (up to 1000), boundary-heavy (~40% of quotes), medium width. Wins on
                spillover the way a Stalemate does, but also competes on price.
  quantifyer    medium-wide (half ~0.09), medium size. Middle of the pack.
  Lattice       very tight (half 0.025), size 1, top volume -- trusts its model, takes FOKs.
  Fixed Width   the stated policy, half 0.025, size 1; picked off by informed flow, usually last.
"""

import os as _os
import sys as _sys

_HARNESS = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "harness")
if _HARNESS not in _sys.path:
    _sys.path.insert(0, _HARNESS)

import math

from bot import BinaryOption, FokOrder, OrderType, Quote
from opponents import _Base


class _Arena(_Base):
    """A `_Base` clone with independent bid/offer size and an optional boundary habit.

    The arena books show sizes that differ sharply between the two sides (a huge bid, a small
    offer). That is what a maker does when one side is near a price boundary and therefore nearly
    riskless: `_affordable` already caps the risky side by cash, so giving a large nominal `size`
    reproduces the asymmetry on its own. `boundary` widens further out toward 0.00/1.00 when the
    price is already extreme, matching the bots that sit on the boundary a large share of the time.
    """

    boundary = 0.0  # extra half-spread applied as the price approaches 0 or 1

    def quote(self, option: BinaryOption, counterparty_id: int) -> Quote:
        mid = self.price_option(option)
        extremity = 1.0 - 4.0 * mid * (1.0 - mid)  # 0 at the money, 1 at a boundary
        half = self.half_spread + self.boundary * extremity
        bid = max(0, min(99, math.floor((mid - half) * 100)))
        offer = max(bid + 1, min(100, math.ceil((mid + half) * 100)))
        bq = max(1, self._affordable(bid / 100.0))
        oq = max(1, self._affordable(1.0 - offer / 100.0))
        return Quote(bid_price=bid / 100.0, bid_quantity=bq, offer_price=offer / 100.0, offer_quantity=oq)


class HelloWorld(_Arena):
    def __init__(self, cash: float) -> None:
        super().__init__("HelloWorld", cash)
        self.half_spread = 0.13   # median 0.125; ADAPTIVE, ranges 0.025-0.50
        self.boundary = 0.10
        self.size = 10
        self.takes_fok = True
        self.fok_edge = 0.12  # wide: only lifts a FOK showing a big edge


class ThreeRings(_Arena):
    def __init__(self, cash: float) -> None:
        super().__init__("3 Rings", cash)
        # The arena's dominant winner: +23 avg PnL, 0.94 avg points across 3 matches. Tight base
        # width but ADAPTIVE (observed 0.005-0.48), large size, ~45 trades/match.
        self.half_spread = 0.045
        self.size = 200  # large; _affordable strands the risky side, leaving the safe-side stacks
        self.takes_fok = True
        self.fok_edge = 0.04


class Ar4yu(_Arena):
    def __init__(self, cash: float) -> None:
        super().__init__("Ar4yu", cash)
        # A size-1000 boundary flooder: median half-spread 0.5, quotes 0.00/1.00 ~77% of the time.
        # Wins on spillover like a Stalemate, occasionally competes on price (range down to 0.015).
        self.half_spread = 0.42
        self.boundary = 0.08
        self.size = 600
        self.takes_fok = True
        self.fok_edge = 0.06


class Quantifyer(_Arena):
    def __init__(self, cash: float) -> None:
        super().__init__("quantifyer", cash)
        self.half_spread = 0.09
        self.size = 20
        self.takes_fok = True
        self.fok_edge = 0.08


class LatticeArena(_Arena):
    def __init__(self, cash: float) -> None:
        super().__init__("Lattice", cash)
        self.half_spread = 0.025
        self.size = 6
        self.takes_fok = True
        self.fok_edge = 0.03


class FixedWidthArena(_Arena):
    def __init__(self, cash: float, width: float = 0.05) -> None:
        super().__init__(f"Fixed Width {width:g}", cash)
        self.half_spread = width / 2.0
        self.size = 3
        self.takes_fok = True
        self.fok_edge = width / 2.0


ROSTER = {
    "HelloWorld": HelloWorld,
    "3 Rings": ThreeRings,
    "Ar4yu": Ar4yu,
    "quantifyer": Quantifyer,
    "Lattice": LatticeArena,
    "Fixed Width 0.05": lambda cash: FixedWidthArena(cash, 0.05),
}


def build(name: str, cash: float) -> _Arena:
    return ROSTER[name](cash)


def full_field(cash: float) -> list:
    return [factory(cash) for factory in ROSTER.values()]
