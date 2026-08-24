import math
import random
from collections import defaultdict
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Final

AJARAI_NAME: Final[str] = "AJR"
AJARAI_UNDERLYING_ID: Final[int] = 2
FED_FUNDS_RATE_NAME: Final[str] = "FED"
FED_FUNDS_RATE_UNDERLYING_ID: Final[int] = 1
RATE_STRIKE_GRID: Final[float] = 0.25
THERIODIC_NAME: Final[str] = "THR"
THERIODIC_UNDERLYING_ID: Final[int] = 3

UNDERLYING_NAME_BY_ID: Final[dict[int, str]] = {
    AJARAI_UNDERLYING_ID: AJARAI_NAME,
    FED_FUNDS_RATE_UNDERLYING_ID: FED_FUNDS_RATE_NAME,
    THERIODIC_UNDERLYING_ID: THERIODIC_NAME,
}


@dataclass(eq=True, frozen=True, unsafe_hash=True)
class BinaryOption:
    legs: "tuple[OptionLeg, ...]"
    option_id: int
    steps_until_expiry: int
    strike: float

    def __post_init__(self) -> None:
        if self.steps_until_expiry < 0:
            raise ValueError("Steps until expiry must be non-negative")

        if not self.legs:
            raise ValueError("Binary option must have at least one leg")

        underlying_ids: list[int] = [leg.underlying_id for leg in self.legs]
        if len(underlying_ids) != len(set(underlying_ids)):
            raise ValueError("Binary option legs must reference distinct underlyings")

        if any(leg.weight == 0 for leg in self.legs):
            raise ValueError("Binary option leg weights must be non-zero")

    def __str__(self) -> str:
        terms: list[str] = []
        for index, leg in enumerate(self.legs):
            name: str = UNDERLYING_NAME_BY_ID.get(leg.underlying_id, str(leg.underlying_id))
            magnitude: float = abs(leg.weight)
            magnitude_str: str = "" if magnitude == 1 else f"{magnitude:.2f}*"
            if index == 0:
                sign: str = "-" if leg.weight < 0 else ""
            else:
                sign = " - " if leg.weight < 0 else " + "
            terms.append(f"{sign}{magnitude_str}{name}")
        observable_expression: str = "".join(terms)
        return f"{self.option_id} ({self.steps_until_expiry}d {observable_expression} >= {self.strike:.2f})"

    def advance_step(self) -> "BinaryOption":
        if self.steps_until_expiry == 0:
            return self

        return replace(self, steps_until_expiry=self.steps_until_expiry - 1)

    def contract_matches(self, other: "BinaryOption") -> bool:
        return replace(other, option_id=self.option_id) == self

    def expiry_valuation(self, value_by_underlying_id: dict[int, float]) -> float:
        return 1.0 if self.observable_value(value_by_underlying_id) >= self.strike else 0.0

    def observable_value(self, value_by_underlying_id: dict[int, float]) -> float:
        return sum(leg.weight * value_by_underlying_id[leg.underlying_id] for leg in self.legs)


@dataclass(frozen=True)
class FokOrder:
    counterparty_id: int
    option_id: int
    order_type: "OrderType"
    price: float
    quantity: int

    def __post_init__(self) -> None:
        if self.price < 0:
            raise ValueError("FOK order price must be non-negative")

        if self.quantity <= 0:
            raise ValueError("FOK order quantity must be positive")


@dataclass(frozen=True)
class MarketHistory:
    values_by_underlying_id: dict[int, tuple[float, ...]]

    def __post_init__(self) -> None:
        lengths: set[int] = {len(values) for values in self.values_by_underlying_id.values()}
        if len(lengths) > 1:
            raise ValueError("All underlyings must have the same number of historical days")

        if lengths and next(iter(lengths)) <= 0:
            raise ValueError("Market history must contain at least one day")

    @property
    def num_days(self) -> int:
        if not self.values_by_underlying_id:
            return 0
        return len(next(iter(self.values_by_underlying_id.values())))


@dataclass(frozen=True)
class MarketParameters:
    ajarai_drift: float
    ajarai_idio_std_dev: float
    ajarai_rate_beta: float
    ajarai_sector_beta: float
    rate_down_probability: float
    rate_reversion_strength: float
    rate_up_probability: float
    sector_std_dev: float
    theriodic_drift: float
    theriodic_idio_std_dev: float
    theriodic_rate_beta: float
    theriodic_sector_beta: float

    rate_step: float = 0.25
    rate_target: float = 2.0

    def __post_init__(self) -> None:
        if self.rate_step <= 0:
            raise ValueError("Rate step must be positive")

        if self.rate_up_probability <= 0 or self.rate_down_probability <= 0:
            raise ValueError("Rate up/down probabilities must both be positive")

        if self.rate_up_probability + self.rate_down_probability > 1:
            raise ValueError("Rate up/down probabilities must not sum to more than 1")

        if self.rate_target < 0:
            raise ValueError("Rate target must be non-negative")

        if not (0 <= self.rate_reversion_strength <= 1):
            raise ValueError("Rate reversion strength must be between 0 and 1")

        if self.ajarai_idio_std_dev < 0 or self.theriodic_idio_std_dev < 0 or self.sector_std_dev < 0:
            raise ValueError("Standard deviations must be non-negative")

    def advance_company_value(
        self,
        current_value: float,
        rate_change: float,
        sector_shock: float,
        *,
        drift: float,
        rate_beta: float,
        sector_beta: float,
        idio_std_dev: float,
    ) -> float:
        idiosyncratic_shock: float = random.gauss(mu=0.0, sigma=idio_std_dev)
        log_return: float = drift + (rate_beta * rate_change) + (sector_beta * sector_shock) + idiosyncratic_shock
        return round(current_value * math.exp(log_return), 2)

    def advance_rate(self, rate_value: float) -> float:
        up_probability, down_probability = self.tilted_rate_probabilities(rate_value)
        draw: float = random.random()
        if draw < up_probability:
            return self.next_rate_value(rate_value, 1)

        if draw < up_probability + down_probability:
            return self.next_rate_value(rate_value, -1)

        return rate_value

    def advance_step(self, value_by_underlying_id: dict[int, float]) -> dict[int, float]:
        current_rate_value: float = value_by_underlying_id[FED_FUNDS_RATE_UNDERLYING_ID]
        rate_value: float = self.advance_rate(current_rate_value)
        rate_change: float = round(rate_value - current_rate_value, 2)
        sector_shock: float = random.gauss(mu=0.0, sigma=self.sector_std_dev)
        return {
            FED_FUNDS_RATE_UNDERLYING_ID: rate_value,
            AJARAI_UNDERLYING_ID: self.advance_company_value(
                value_by_underlying_id[AJARAI_UNDERLYING_ID],
                rate_change,
                sector_shock,
                drift=self.ajarai_drift,
                rate_beta=self.ajarai_rate_beta,
                sector_beta=self.ajarai_sector_beta,
                idio_std_dev=self.ajarai_idio_std_dev,
            ),
            THERIODIC_UNDERLYING_ID: self.advance_company_value(
                value_by_underlying_id[THERIODIC_UNDERLYING_ID],
                rate_change,
                sector_shock,
                drift=self.theriodic_drift,
                rate_beta=self.theriodic_rate_beta,
                sector_beta=self.theriodic_sector_beta,
                idio_std_dev=self.theriodic_idio_std_dev,
            ),
        }

    def next_rate_value(self, rate_value: float, num_grid_steps: int) -> float:
        return max(round(rate_value + num_grid_steps * self.rate_step, 2), 0.0)

    def tilted_rate_probabilities(self, rate_value: float) -> tuple[float, float]:
        tilt: float = self.rate_reversion_strength * (self.rate_target - rate_value)
        up_probability: float = min(max(self.rate_up_probability + tilt, 0.0), 1.0)
        down_probability: float = min(max(self.rate_down_probability - tilt, 0.0), 1.0 - up_probability)
        return up_probability, down_probability


@dataclass(frozen=True)
class OptionLeg:
    underlying_id: int
    weight: float


class OrderType(StrEnum):
    BUY = "buy"
    SELL = "sell"


class Position:
    def __init__(self) -> None:
        self.option_quantity_by_option_id: dict[int, int] = defaultdict(int)

    def add_option_quantity(self, option_id: int, quantity: int) -> None:
        self.option_quantity_by_option_id[option_id] += quantity


@dataclass(frozen=True)
class Quote:
    bid_price: float
    bid_quantity: int
    offer_price: float
    offer_quantity: int

    def __post_init__(self) -> None:
        if self.bid_quantity <= 0 or self.offer_quantity <= 0:
            raise ValueError("Quote quantities must be positive")

        if not (0.0 <= self.bid_price <= 1.0 and 0.0 <= self.offer_price <= 1.0):
            raise ValueError("Quote prices must be between 0 and 1")

        if self.bid_price >= self.offer_price:
            raise ValueError("Quote bid price must be less than offer price")

        if any(abs(round(price * 100) - price * 100) > 1e-6 for price in (self.bid_price, self.offer_price)):
            raise ValueError("Quote prices must be in whole pennies (multiples of 0.01)")


@dataclass(frozen=True)
class Underlying:
    name: str
    underlying_id: int
    value: float

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, Underlying):
            return False
        return self.underlying_id == other.underlying_id


# ============================================================================
# YOUR MARKET MAKER -- fill in the six stubbed methods below
# ============================================================================


class MarketMaker:
    def __init__(
        self,
        underlying_initial_state: list[Underlying],
        option_initial_state: list[BinaryOption],
        cash_balance: float,
    ) -> None:
        self.underlying_state: list[Underlying] = underlying_initial_state
        self.active_option_state: list[BinaryOption] = option_initial_state
        self.cash_balance: float = cash_balance
        self.position: Position = Position()
        self.estimated_params: MarketParameters | None = None

    def on_step_advance(self, new_underlying_state: list[Underlying], new_option_state: list[BinaryOption]) -> None:
        self.underlying_state = new_underlying_state
        self.active_option_state = new_option_state

    def on_trade(self, option: BinaryOption, price: float, quantity: int, counterparty_id: int) -> None:
        self.position.add_option_quantity(option.option_id, quantity)
        
        # Keep track of local cash impact
        max_loss = quantity * price if quantity > 0 else abs(quantity) * (1.0 - price)
        self.cash_balance -= max_loss

    @property
    def name(self) -> str: 
        return "Gemini_Quant_MM"

    def price_option(self, option: BinaryOption) -> float: 
        if self.estimated_params is None:
            # Fallback if warm_up was somehow bypassed
            return 0.50
        return self.price_option_from_parameters(self.estimated_params, option)

    def price_option_from_parameters(
        self, market_parameters: MarketParameters, option: BinaryOption
    ) -> float:
        # Monte Carlo Simulation to price the option
        N_SIMULATIONS = 1000
        payoff_sum = 0.0
        
        # Prepare starting state mapping
        start_state = {u.underlying_id: u.value for u in self.underlying_state}
        
        for _ in range(N_SIMULATIONS):
            current_state = start_state.copy()
            for _ in range(option.steps_until_expiry):
                current_state = market_parameters.advance_step(current_state)
            
            payoff_sum += option.expiry_valuation(current_state)
            
        return payoff_sum / N_SIMULATIONS

    def quote(self, option: BinaryOption, counterparty_id: int) -> Quote: 
        theoretical_price = self.price_option(option)
        
        # Apply a conservative edge/spread
        spread = 0.05
        
        # Calculate unconstrained prices
        raw_bid = theoretical_price - spread
        raw_offer = theoretical_price + spread
        
        # Round and enforce strict [0.01, 0.99] bounds
        bid_price = max(0.01, min(0.98, round(raw_bid, 2)))
        offer_price = max(bid_price + 0.01, min(0.99, round(raw_offer, 2)))
        
        # Risk limits: Don't risk more than 5% of available cash on a single quote
        max_capital_to_risk = max(1.0, self.cash_balance * 0.05)
        
        bid_qty = max(1, int(max_capital_to_risk / bid_price))
        offer_qty = max(1, int(max_capital_to_risk / (1.0 - offer_price)))
        
        return Quote(
            bid_price=bid_price,
            bid_quantity=bid_qty,
            offer_price=offer_price,
            offer_quantity=offer_qty
        )

    def respond_to_fok(self, option: BinaryOption, fok_order: FokOrder) -> bool: 
        theoretical_price = self.price_option(option)
        edge_required = 0.02
        
        if fok_order.order_type == OrderType.BUY:
            # We are selling to the counterparty
            max_loss_if_traded = fok_order.quantity * (1.0 - fok_order.price)
            if max_loss_if_traded > self.cash_balance * 0.10:
                return False  # Trade is too large, risk of bankruptcy
            
            # Trade if they are willing to pay above our theoretical + edge
            return fok_order.price >= (theoretical_price + edge_required)
            
        elif fok_order.order_type == OrderType.SELL:
            # We are buying from the counterparty
            max_loss_if_traded = fok_order.quantity * fok_order.price
            if max_loss_if_traded > self.cash_balance * 0.10:
                return False
            
            # Trade if they are willing to sell below our theoretical - edge
            return fok_order.price <= (theoretical_price - edge_required)
            
        return False

    def warm_up(self, market_history: MarketHistory) -> None:
        fed_hist = market_history.values_by_underlying_id[FED_FUNDS_RATE_UNDERLYING_ID]
        ajr_hist = market_history.values_by_underlying_id[AJARAI_UNDERLYING_ID]
        thr_hist = market_history.values_by_underlying_id[THERIODIC_UNDERLYING_ID]
        
        n_days = market_history.num_days
        
        # 1. Estimate Rate Probabilities
        up_moves, down_moves = 0, 0
        for i in range(1, n_days):
            diff = fed_hist[i] - fed_hist[i-1]
            if diff > 0:
                up_moves += 1
            elif diff < 0:
                down_moves += 1
                
        total_rate_moves = n_days - 1
        rate_up_prob = up_moves / total_rate_moves if total_rate_moves > 0 else 0.2
        rate_down_prob = down_moves / total_rate_moves if total_rate_moves > 0 else 0.2
        
        # Ensure probs don't sum to > 1 and are bounded
        rate_up_prob = max(0.01, min(0.98, rate_up_prob))
        rate_down_prob = max(0.01, min(0.99 - rate_up_prob, rate_down_prob))
        
        # 2. Estimate Drift and Volatility (Naive Statistical estimation, bundling betas)
        def estimate_drift_vol(history: tuple[float, ...]) -> tuple[float, float]:
            if len(history) < 2:
                return 0.0, 0.05
            
            log_returns = [math.log(history[i]/history[i-1]) for i in range(1, len(history))]
            mean = sum(log_returns) / len(log_returns)
            variance = sum((r - mean)**2 for r in log_returns) / len(log_returns)
            return mean, math.sqrt(variance)

        ajr_drift, ajr_vol = estimate_drift_vol(ajr_hist)
        thr_drift, thr_vol = estimate_drift_vol(thr_hist)

        # Build estimated parameters
        self.estimated_params = MarketParameters(
            ajarai_drift=ajr_drift,
            ajarai_idio_std_dev=ajr_vol,
            ajarai_rate_beta=0.0,    # Simplified: effect bundled into drift/vol
            ajarai_sector_beta=0.0,  # Simplified: effect bundled into drift/vol
            rate_down_probability=rate_down_prob,
            rate_reversion_strength=0.05, 
            rate_up_probability=rate_up_prob,
            sector_std_dev=0.0,      # Simplified
            theriodic_drift=thr_drift,
            theriodic_idio_std_dev=thr_vol,
            theriodic_rate_beta=0.0,
            theriodic_sector_beta=0.0
        )