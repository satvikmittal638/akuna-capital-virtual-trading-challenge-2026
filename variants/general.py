"""Build a GENERAL bot: `bot.py` plus only what survived field-wide scrutiny.

No fingerprint table, no case keys, no per-case constants. Every session runs the same code, so
this is a candidate for round 2 as much as round 1.

What the overfit builds taught, and what happened to each lesson:

* **Momentum drift blend** -- REJECTED. It was the core of both the TC-5 and TC-6 builds, and
  `cross_case.py` scores it against real settlement on all 16 scored cases: mean Brier 0.1114 for
  base against 0.1117 for the best trend variant, and every variant worse on mean |bias|. It helped
  precisely on the two cases it was tuned to (4: 0.1252 -> 0.1150, 5: 0.0958 -> 0.0822) and badly
  hurt others (9: 0.1228 -> 0.2544). HANDOFF sec.7 already said why: the generator's true drift is
  ~0.001, so momentum over a 15-40 day history is mostly noise, and extrapolating it fits noise.
* **Flat 0.01/0.99 quoting, and undercutting a rival's width** -- REJECTED as general. Both were
  decisive (TC-5 +33.97, TC-6 +13.55) and both are *priced against a specific rival*: the first
  needs a 0.00/1.00 maker and no real competitor, the second needs to know the rival quotes 0.25.
  Neither is knowable in an unseen session.
* **Side-asymmetric penny step** -- KEPT, as the one change here. See below.

Measured field-wide over 944 quotes on all 16 scored cases:
  `_unopposed()` fires on **0.3%** of them (3 of 944), so the penny step it gates is dead code --
  while **74.6%** of quotes pin to a boundary (bid 0.00 on 46%, offer 1.00 on 29%), which is where
  that step would apply. Two things follow. The mechanism has never actually been tried at scale,
  and `_UNOPPOSED_EDGE` / `_UNOPPOSED_SIZE_MULTIPLE` / `_MIN_QUOTE_SAMPLE` are inert -- so the
  round-2 risk HANDOFF sec.8 attributes to them does not exist.

    python3.13 variants/general.py
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import build as build_mod

GENERAL_LAYER = '''

# GENERAL LAYER -- no per-case anything. Repairs one mechanism the 16.30 build already has.

_BaseMarketMaker = MarketMaker


class MarketMaker(_BaseMarketMaker):  # type: ignore[no-redef]
    """The 16.30 maker, with the competitive-width search actually working.

    The base bot means to do this already: `_unopposed()` is supposed to notice that no rival ever
    prices better and respond by charging more (`_UNOPPOSED_EDGE`) in more size. Measured over 944
    quotes on all 16 scored cases it fires on **3** of them. It compares `_quotes_won`, a count of
    TRADES incremented in `_record_trade`, against `_quotes_shown - 1`, a count of QUOTES: different
    events, so one unfilled auction breaks it permanently and a fill-or-kill inflates it for free.

    Two repairs, and nothing else:

    1. Count auctions, not trades. An auction opens on `quote` and resolves on the next one, won if
       any non-fill-or-kill trade arrived in between.
    2. Respond continuously instead of in one 0.45 step. Widen after an auction won, cut back hard
       after one lost. That is a search for the widest price the field still leaves us, which is the
       only quantity that matters and the one thing a session actually reveals. Where the only rival
       quotes 0.00/1.00 it walks out to the boundary; where a rival quotes 0.25 it settles just
       inside; where a rival quotes 0.05 it stays tight. No constant knows which -- the session does.

    Size rides the same signal, because it has to: a quote near the boundary risks about a penny a
    lot, so the margin budget stops binding and the lot cap alone decides how much of an order is
    left to whoever quotes behind us.
    """

    _EDGE_STEP: Final[float] = 0.12
    _EDGE_DECAY: Final[float] = 0.5
    _MAX_EDGE_BONUS: Final[float] = 0.45
    _EDGE_SIZE_GAIN: Final[float] = 30.0

    def __init__(self, underlying_initial_state: list[Underlying],
                 option_initial_state: list[BinaryOption], cash_balance: float) -> None:
        super().__init__(underlying_initial_state, option_initial_state, cash_balance)
        # A small prior, not a hardcode: it says "assume a little pricing power until the field
        # says otherwise", and one lost auction halves it, two reduce it to noise.
        self._edge_bonus: float = 0.10
        self._auction_open: bool = False
        self._auction_won: bool = False
        self._base_quote_size: int = self._max_quote_size
        self._base_position_cap: float = self._contract_position_cap

    def _resolve_auction(self) -> None:
        if not self._auction_open:
            return
        if self._auction_won:
            # Geometric probing, multiplicative back-off. Additive steps need nine straight wins to
            # saturate and a short session may only hold fourteen auctions, so most of it would be
            # spent under-charging a field that was never going to compete.
            self._edge_bonus = min(max(self._edge_bonus * 2.0, self._EDGE_STEP), self._MAX_EDGE_BONUS)
        else:
            self._edge_bonus *= self._EDGE_DECAY
        self._auction_open = False
        multiple: float = 1.0 + self._EDGE_SIZE_GAIN * self._edge_bonus
        self._max_quote_size = max(1, int(self._base_quote_size * multiple))
        self._contract_position_cap = max(2.0, self._base_position_cap * multiple)

    def quote(self, option: BinaryOption, counterparty_id: int) -> Quote:
        try:
            self._resolve_auction()
            self._auction_open = True
            self._auction_won = False
        except Exception:
            pass
        quote: Quote = super().quote(option, counterparty_id)
        try:
            # A quote resting exactly on 0.00 or 1.00 does not beat a rival standing there, it ties
            # it and the order splits; one penny inside takes the whole thing. The value is
            # side-asymmetric, which an earlier ungated attempt missed: bidding 0.01 rather than
            # 0.00 is worth `0.5p - 0.01` so it pays above p = 0.02, offering 0.99 rather than 1.00
            # is worth `0.49 - 0.5p` so it pays below p = 0.98. That version used 0.01/0.99 and so
            # fired straight through the band where the offer step is negative.
            theo: float = self.price_option(option)
            bid: float = quote.bid_price
            offer: float = quote.offer_price
            if bid <= 0.0 and theo > 0.02:
                bid = 0.01
            if offer >= 1.0 and theo < 0.98:
                offer = 0.99
            if bid >= offer or (bid == quote.bid_price and offer == quote.offer_price):
                return quote
            return Quote(bid_price=bid, bid_quantity=quote.bid_quantity,
                         offer_price=offer, offer_quantity=quote.offer_quantity)
        except Exception:
            return quote

    def on_trade(self, option: BinaryOption, price: float, quantity: int, counterparty_id: int) -> None:
        try:
            # A fill-or-kill fill is not an auction won; it must not credit the open auction.
            is_fok: bool = any(
                pending_id == option.option_id and abs(pending_price - price) <= _EPSILON
                for pending_id, pending_price, _, _ in self._pending_fok
            )
            if not is_fok:
                self._auction_won = True
        except Exception:
            pass
        super().on_trade(option, price, quantity, counterparty_id)

    def _adverse_selection_edge(self) -> float:
        return super()._adverse_selection_edge() + self._edge_bonus
'''


def build(tag: str = "general") -> str:
    with open(build_mod.BOT_PATH) as handle:
        base = handle.read()
    with open(build_mod.TEMPLATE_PATH) as handle:
        template = handle.read()
    cut = base.index(build_mod.BANNER)
    if base[:cut] != template[:cut]:
        raise RuntimeError("bot.py header no longer matches template.py")

    room = build_mod.LINE_LIMIT - GENERAL_LAYER.count("\n")
    for _ in range(4):
        source = build_mod.compact(base, room).rstrip("\n") + "\n" + GENERAL_LAYER.rstrip("\n") + "\n"
        overshoot = source.count("\n") - build_mod.LINE_LIMIT
        if overshoot <= 0:
            break
        room -= overshoot
    else:
        raise RuntimeError("could not fit the file inside the line limit")

    os.makedirs(build_mod.OUT_DIR, exist_ok=True)
    path = os.path.join(build_mod.OUT_DIR, f"{tag}.py")
    with open(path, "w") as handle:
        handle.write(source)
    return path


if __name__ == "__main__":
    written = build()
    with open(written) as handle:
        text = handle.read()
    print(f"wrote {written}  {text.count(chr(10))} lines  "
          f"maxcol {max(len(line) for line in text.split(chr(10)))}")
    print("no fingerprint table:", "_GENOME_TABLE" not in text and "_CASE_KEYS" not in text)
