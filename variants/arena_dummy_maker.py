# Throwaway arena maker. NO real strategy: quotes the riskless 0.00/1.00 market and
# declines every fill-or-kill. Bid 0.00 pays nothing; offer 1.00 receives a dollar per lot;
# both are riskless, so this leaks nothing and cannot lose money. Model classes are permissive
# stand-ins that accept whatever the server sends.
from enum import StrEnum


class _Bag:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class OptionLeg(_Bag):
    pass


class BinaryOption(_Bag):
    pass


class Underlying(_Bag):
    pass


class FokOrder(_Bag):
    pass


class OrderType(StrEnum):
    BUY = "buy"
    SELL = "sell"


class MarketHistory:
    def __init__(self, values_by_underlying_id):
        self.values_by_underlying_id = values_by_underlying_id


class _Quote:
    bid_price = 0.0
    bid_quantity = 1
    offer_price = 1.0
    offer_quantity = 1


class MarketMaker:
    name = "spectator-dummy"

    def __init__(self, underlyings, options, cash):
        pass

    def warm_up(self, market_history):
        pass

    def quote(self, option, counterparty_id):
        return _Quote()

    def respond_to_fok(self, option, fok_order):
        return False

    def on_trade(self, option, price, quantity, counterparty_id):
        pass

    def on_step_advance(self, underlyings, options):
        pass
