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
# YOUR MARKET MAKER
# ============================================================================
#
# Pricing is exact -- no Monte Carlo. One step of a company's log value, from
# `MarketParameters.advance_step`, is  d log(V) = drift + rate_beta * dR + sector_beta * S + eps,
# where `dR` is the *realised* rate change. Over n steps that term telescopes to
# rate_beta * (R_n - R_0) exactly -- including the floor at zero, where a down draw leaves both the
# rate and `dR` unchanged -- so conditional on the terminal rate alone (log A_n, log T_n) is
# bivariate normal, with mean log V_0 + n * drift + rate_beta * (r - R_0), per-step variance
# v = (sector_beta * sigma_S) ** 2 + idio ** 2 and per-step covariance
# cov = sector_beta_A * sector_beta_T * sigma_S ** 2. Pricing is therefore an exact Markov-chain DP
# for the terminal rate, then one normal CDF per rate state (a quadrature for two company legs).
#
# Only (drift, rate_beta, v_A, v_T, cov) matter: the factor decomposition is neither identifiable
# from history nor needed, and the rate chain depends only on alpha_up = p_up + lam * target and
# alpha_down = p_down - lam * target. Hence the bot carries its own `_Params`. The one
# approximation is the per-step `round(value, 2)`, negligible against estimation error.

_COMPANY_UNDERLYING_IDS: Final[tuple[int, ...]] = (AJARAI_UNDERLYING_ID, THERIODIC_UNDERLYING_ID)
_DEFAULT_RATE_VALUE: Final[float] = 2.0
_EPSILON: Final[float] = 1e-9
_MIN_VARIANCE: Final[float] = 1e-12
_SQRT_TWO: Final[float] = math.sqrt(2.0)
# Ridge penalty (in squared rate levels) and ceiling for the estimated reversion strength. A
# history that genuinely spans several grid levels swamps the penalty; a flat one is pulled to zero.
_RATE_REVERSION_RIDGE: Final[float] = 8.0
_MAX_RATE_REVERSION: Final[float] = 0.35
# Prior on a company's daily log drift. Centred slightly positive rather than at zero: these are
# growing frontier labs, and across the warm-up histories the estimates sit near half a percent a
# day with roughly that much genuine dispersion once sampling noise is netted out.
_DRIFT_PRIOR_MEAN: float = 0.005
_DRIFT_PRIOR_STD_DEV: float = 0.008
# Prior on the correlation between the two companies' log returns. Both load on a common sector
# shock, so across warm-up histories the estimate sits high, near three quarters, with real but
# limited variation between markets.
_CORRELATION_PRIOR_MEAN: float = 0.75
_CORRELATION_PRIOR_STD_DEV: float = 0.20
# Prior on a company's rate sensitivity, shrunk hard because the estimate carries almost no signal:
# its cross-case spread is entirely sampling noise (sd 0.029 against a mean standard error of
# 0.037), and within one history the first half predicts the second with slope 0.08. Centred on the
# precision-weighted pooling, -0.024, tempered toward the published -0.018.
_RATE_BETA_PRIOR_MEAN: float = -0.020
_RATE_BETA_PRIOR_STD_DEV: float = 0.010


def _clamp(value: float, low: float, high: float) -> float:
    if value != value:  # NaN
        return low
    return low if value < low else (high if value > high else value)


def _clamp_probability(value: float) -> float:
    if value != value:  # NaN
        return 0.5
    return _clamp(value, 0.0, 1.0)


def _normal_cdf(z: float) -> float:
    return 0.5 * math.erfc(-z / _SQRT_TWO)


def _build_quadrature(limit: float = 7.0, num_intervals: int = 112) -> tuple[tuple[float, float], ...]:
    """Simpson nodes and weights for E[f(Z)] with Z ~ N(0, 1); weights sum to 1."""
    width: float = 2.0 * limit / num_intervals
    unnormalised: list[tuple[float, float]] = []
    total: float = 0.0
    for index in range(num_intervals + 1):
        node: float = -limit + index * width
        if index in (0, num_intervals):
            coefficient: float = 1.0
        elif index % 2 == 1:
            coefficient = 4.0
        else:
            coefficient = 2.0
        weight: float = coefficient * math.exp(-0.5 * node * node)
        unnormalised.append((node, weight))
        total += weight
    return tuple((node, weight / total) for node, weight in unnormalised)


_QUADRATURE: Final[tuple[tuple[float, float], ...]] = _build_quadrature()


@dataclass(frozen=True)
class _CompanyParams:
    drift: float
    rate_beta: float
    variance: float  # per-step variance of the log return


@dataclass(frozen=True)
class _Params:
    rate_up_intercept: float  # up(R) = clamp(alpha_up - lam * R, 0, 1)
    rate_down_intercept: float  # down(R) = clamp(alpha_down + lam * R, 0, 1 - up)
    rate_reversion: float
    rate_step: float
    company: dict[int, _CompanyParams]
    covariance: float  # per-step covariance of the two company log returns
    # Posterior covariance of the OLS mean estimates, expressed as the multiples of the
    # residual covariance matrix given by inv(X'X) for the design X = [1, dR]: (m00, m01, m11).
    # All zero when the parameters are known exactly (the THEO path).
    mean_uncertainty: tuple[float, float, float] = (0.0, 0.0, 0.0)

    @classmethod
    def from_market_parameters(cls, parameters: MarketParameters) -> "_Params":
        reversion: float = parameters.rate_reversion_strength
        target: float = parameters.rate_target
        sector_variance: float = parameters.sector_std_dev**2
        return cls(
            rate_up_intercept=parameters.rate_up_probability + reversion * target,
            rate_down_intercept=parameters.rate_down_probability - reversion * target,
            rate_reversion=reversion,
            rate_step=parameters.rate_step,
            company={
                AJARAI_UNDERLYING_ID: _CompanyParams(
                    drift=parameters.ajarai_drift,
                    rate_beta=parameters.ajarai_rate_beta,
                    variance=(parameters.ajarai_sector_beta**2) * sector_variance + parameters.ajarai_idio_std_dev**2,
                ),
                THERIODIC_UNDERLYING_ID: _CompanyParams(
                    drift=parameters.theriodic_drift,
                    rate_beta=parameters.theriodic_rate_beta,
                    variance=(parameters.theriodic_sector_beta**2) * sector_variance
                    + parameters.theriodic_idio_std_dev**2,
                ),
            },
            covariance=parameters.ajarai_sector_beta * parameters.theriodic_sector_beta * sector_variance,
        )

    def company_params(self, underlying_id: int) -> _CompanyParams:
        return self.company.get(underlying_id, _CompanyParams(drift=0.0, rate_beta=0.0, variance=1e-4))

    def effective_steps(self, num_steps: int, rate_change: float) -> float:
        """Diffusion steps plus mean-estimation error, in the same units.

        Integrating a normal CDF over a normally distributed mean gives another normal CDF, so
        estimation error in (drift, rate_beta) enters exactly as extra variance: for the mean
        m = n * drift + rate_beta * dR, var(m) = [n, dR] inv(X'X) [n, dR]' times the residual
        covariance -- the same multiple for both companies and for their covariance.
        """
        m00, m01, m11 = self.mean_uncertainty
        extra: float = (
            num_steps * num_steps * m00 + 2.0 * num_steps * rate_change * m01 + rate_change * rate_change * m11
        )
        return num_steps + _clamp(extra, 0.0, 4.0 * num_steps)

    def transition(self, rate_value: float) -> tuple[float, float]:
        """Mirrors `MarketParameters.tilted_rate_probabilities`, in intercept form."""
        up_probability: float = _clamp(self.rate_up_intercept - self.rate_reversion * rate_value, 0.0, 1.0)
        down_probability: float = _clamp(
            self.rate_down_intercept + self.rate_reversion * rate_value, 0.0, 1.0 - up_probability
        )
        return up_probability, down_probability

    def drift_standard_error(self, underlying_id: int) -> float:
        return math.sqrt(max(self.mean_uncertainty[0] * self.company_params(underlying_id).variance, 0.0))

    def rate_beta_standard_error(self, underlying_id: int) -> float:
        return math.sqrt(max(self.mean_uncertainty[2] * self.company_params(underlying_id).variance, 0.0))

    def with_company_shift(self, underlying_id: int, drift_multiple: float, beta_multiple: float) -> "_Params":
        """Push one company's drift and rate beta by multiples of their own standard errors."""
        company: _CompanyParams = self.company_params(underlying_id)
        shifted: _CompanyParams = replace(
            company,
            drift=company.drift + drift_multiple * self.drift_standard_error(underlying_id),
            rate_beta=company.rate_beta + beta_multiple * self.rate_beta_standard_error(underlying_id),
        )
        return replace(self, company={**self.company, underlying_id: shifted})

    def with_rate_shift(self, up_shift: float, down_shift: float) -> "_Params":
        return replace(
            self,
            rate_up_intercept=self.rate_up_intercept + up_shift,
            rate_down_intercept=self.rate_down_intercept + down_shift,
        )

    def with_variance_scale(self, factor: float) -> "_Params":
        return replace(
            self,
            company={
                underlying_id: replace(params, variance=params.variance * factor)
                for underlying_id, params in self.company.items()
            },
            covariance=self.covariance * factor,
        )


def _default_params() -> _Params:
    """Deliberately vague priors for the case where `warm_up` never ran or was unusable."""
    return _Params(
        rate_up_intercept=0.15 + 0.10 * _DEFAULT_RATE_VALUE,
        rate_down_intercept=0.15 - 0.10 * _DEFAULT_RATE_VALUE,
        rate_reversion=0.10,
        rate_step=RATE_STRIKE_GRID,
        company={
            underlying_id: _CompanyParams(drift=0.0, rate_beta=-0.05, variance=9e-4)
            for underlying_id in _COMPANY_UNDERLYING_IDS
        },
        covariance=4.5e-4,
        mean_uncertainty=(0.125, 0.0, 280.0),
    )


def _rate_distribution(params: _Params, rate_value: float, num_steps: int) -> dict[float, float]:
    """Exact distribution of the rate after `num_steps`, on the grid the exchange rounds to."""
    distribution: dict[float, float] = {rate_value: 1.0}
    for _ in range(num_steps):
        successor: dict[float, float] = defaultdict(float)
        for value, probability in distribution.items():
            up_probability, down_probability = params.transition(value)
            stay_probability: float = max(0.0, 1.0 - up_probability - down_probability)
            if up_probability > 0.0:
                successor[max(round(value + params.rate_step, 2), 0.0)] += probability * up_probability
            if down_probability > 0.0:
                successor[max(round(value - params.rate_step, 2), 0.0)] += probability * down_probability
            if stay_probability > 0.0:
                successor[value] += probability * stay_probability
        distribution = dict(successor)
    return distribution


def _single_leg_probability(weight: float, mean: float, std_dev: float, strike: float) -> float:
    """P(weight * exp(X) >= strike) for X ~ N(mean, std_dev ** 2)."""
    threshold: float = strike / weight
    if weight > 0.0:
        if threshold <= 0.0:
            return 1.0
        return _normal_cdf((mean - math.log(threshold)) / std_dev)

    if threshold <= 0.0:
        return 0.0
    return _normal_cdf((math.log(threshold) - mean) / std_dev)


def _pair_probability(
    first: tuple[float, float, float],
    second: tuple[float, float, float],
    correlation: float,
    strike: float,
) -> float:
    """P(w1 * exp(X) + w2 * exp(Y) >= strike) for jointly normal (X, Y).

    Each of `first`/`second` is (weight, mean, std_dev).
    """
    weight_x, mean_x, std_dev_x = first
    weight_y, mean_y, std_dev_y = second

    # The spreads actually traded ("does AJR beat THR?") reduce to a log ratio, which is exact.
    if abs(strike) <= _EPSILON and weight_x * weight_y < 0.0:
        difference_variance: float = (
            std_dev_x * std_dev_x + std_dev_y * std_dev_y - 2.0 * correlation * std_dev_x * std_dev_y
        )
        difference_std_dev: float = math.sqrt(max(difference_variance, _MIN_VARIANCE))
        if weight_x > 0.0:
            cutoff: float = math.log(-weight_y / weight_x)
            return _normal_cdf((mean_x - mean_y - cutoff) / difference_std_dev)

        cutoff = math.log(-weight_x / weight_y)
        return _normal_cdf((mean_y - mean_x - cutoff) / difference_std_dev)

    # Otherwise integrate over X and use the fact that the condition on Y is monotone.
    conditional_std_dev: float = std_dev_y * math.sqrt(max(1.0 - correlation * correlation, 0.0))
    total: float = 0.0
    for node, node_weight in _QUADRATURE:
        residual: float = strike - weight_x * math.exp(mean_x + std_dev_x * node)
        conditional_mean: float = mean_y + correlation * std_dev_y * node
        # exp(Y) is positive, so a non-positive threshold settles the leg outright: certain when
        # the Y leg is long, impossible when it is short.
        threshold: float = residual / weight_y
        if threshold <= 0.0:
            total += node_weight if weight_y > 0.0 else 0.0
            continue
        cutoff: float = math.log(threshold)
        excess: float = (conditional_mean - cutoff) if weight_y > 0.0 else (cutoff - conditional_mean)
        if conditional_std_dev <= 0.0:
            total += node_weight if excess >= 0.0 else 0.0
        else:
            total += node_weight * _normal_cdf(excess / conditional_std_dev)
    return total


def _price_with_params(params: _Params, value_by_underlying_id: dict[int, float], option: BinaryOption) -> float:
    num_steps: int = option.steps_until_expiry
    if num_steps <= 0:
        return option.expiry_valuation(value_by_underlying_id)

    rate_weight: float = 0.0
    company_legs: list[OptionLeg] = []
    for leg in option.legs:
        if leg.underlying_id == FED_FUNDS_RATE_UNDERLYING_ID:
            rate_weight += leg.weight
        elif value_by_underlying_id.get(leg.underlying_id, 0.0) > 0.0:
            company_legs.append(leg)
        # A company worth zero stays at zero, so such a leg simply drops out.

    initial_rate: float = value_by_underlying_id.get(FED_FUNDS_RATE_UNDERLYING_ID, _DEFAULT_RATE_VALUE)
    rate_matters: bool = rate_weight != 0.0 or any(
        params.company_params(leg.underlying_id).rate_beta != 0.0 for leg in company_legs
    )
    distribution: dict[float, float] = (
        _rate_distribution(params, initial_rate, num_steps) if rate_matters else {initial_rate: 1.0}
    )

    total: float = 0.0
    for rate_value, rate_probability in distribution.items():
        if rate_probability <= 0.0:
            continue

        residual_strike: float = option.strike - rate_weight * rate_value
        if not company_legs:
            if residual_strike <= _EPSILON:
                total += rate_probability
            continue

        rate_change: float = rate_value - initial_rate
        steps: float = params.effective_steps(num_steps, rate_change)
        moments: list[tuple[float, float, float]] = []
        for leg in company_legs:
            company: _CompanyParams = params.company_params(leg.underlying_id)
            mean: float = (
                math.log(value_by_underlying_id[leg.underlying_id])
                + num_steps * company.drift
                + company.rate_beta * rate_change
            )
            moments.append((leg.weight, mean, math.sqrt(max(steps * company.variance, _MIN_VARIANCE))))

        if len(moments) == 1:
            weight, mean, std_dev = moments[0]
            total += rate_probability * _single_leg_probability(weight, mean, std_dev, residual_strike)
            continue

        std_dev_product: float = moments[0][2] * moments[1][2]
        correlation: float = (
            _clamp(steps * params.covariance / std_dev_product, -0.999999, 0.999999) if std_dev_product > 0.0 else 0.0
        )
        total += rate_probability * _pair_probability(moments[0], moments[1], correlation, residual_strike)
    return _clamp_probability(total)


def _indicator_moments(samples: list[tuple[float, float]]) -> tuple[int, float, float, float, float]:
    """Sums for regressing a 0/1 move indicator on the rate level it was observed at.

    Returns (n, sum of indicators, sum of levels, centred cross product, centred level spread).
    """
    count: int = len(samples)
    if count == 0:
        return 0, 0.0, 0.0, 0.0, 0.0
    indicator_sum: float = sum(indicator for indicator, _ in samples)
    level_sum: float = sum(level for _, level in samples)
    cross: float = sum(indicator * level for indicator, level in samples) - indicator_sum * level_sum / count
    spread: float = sum(level * level for _, level in samples) - level_sum * level_sum / count
    return count, indicator_sum, level_sum, cross, spread


def _estimate_rate_process(
    rates: tuple[float, ...], rate_changes: list[float]
) -> tuple[float, float, float, float, float]:
    """Return (rate_step, alpha_up, alpha_down, reversion, probability_std_error).

    The true up/down probabilities are linear in the rate level (before clamping), so the
    intercept form is recovered by least squares on the move indicators, sharing one slope:
    up(R) = alpha_up - lam * R and down(R) = alpha_down + lam * R. A down move at a zero rate
    is indistinguishable from no move, so those rows are dropped from the down regression only.
    """
    magnitudes: list[float] = sorted(abs(change) for change in rate_changes if abs(change) > _EPSILON)
    rate_step: float = magnitudes[len(magnitudes) // 2] if magnitudes else RATE_STRIKE_GRID
    if rate_step <= 0.0:
        rate_step = RATE_STRIKE_GRID

    ups: list[tuple[float, float]] = [
        (1.0 if change > _EPSILON else 0.0, rates[index]) for index, change in enumerate(rate_changes)
    ]
    downs: list[tuple[float, float]] = [
        (1.0 if change < -_EPSILON else 0.0, rates[index])
        for index, change in enumerate(rate_changes)
        if rates[index] > _EPSILON
    ]
    up_count, up_sum, up_level_sum, up_cross, up_spread = _indicator_moments(ups)
    down_count, down_sum, down_level_sum, down_cross, down_spread = _indicator_moments(downs)
    # Ridge, not plain least squares. A ten-day warm-up with the rate on two neighbouring grid
    # points leaves almost no variation to regress against and the raw slope explodes, implying one
    # grid step swings the up probability by tens of points. The penalty pulls it toward a local
    # random walk -- what a flat sample evidences -- and fades once the history spans levels.
    denominator: float = up_spread + down_spread + _RATE_REVERSION_RIDGE
    reversion: float = _clamp((down_cross - up_cross) / denominator, 0.0, _MAX_RATE_REVERSION)

    # Laplace-style nudge on the intercepts so a run without any up (or down) move does not
    # freeze that direction out of the chain entirely.
    up_frequency: float = up_sum / up_count
    alpha_up: float = (up_sum + reversion * up_level_sum) / up_count + (up_sum + 1.0) / (up_count + 4.0) - up_frequency
    if down_count > 0:
        down_frequency: float = down_sum / down_count
        alpha_down: float = (
            (down_sum - reversion * down_level_sum) / down_count
            + (down_sum + 1.0) / (down_count + 4.0)
            - down_frequency
        )
    else:
        # No informative observation: assume symmetry at the reversion target.
        alpha_down = alpha_up - 2.0 * reversion * _DEFAULT_RATE_VALUE

    standard_error: float = math.sqrt(max(up_frequency * (1.0 - up_frequency), 0.01) / up_count)
    if down_count > 0:
        down_frequency = down_sum / down_count
        standard_error = max(standard_error, math.sqrt(max(down_frequency * (1.0 - down_frequency), 0.01) / down_count))
    return rate_step, alpha_up, alpha_down, reversion, _clamp(standard_error, 0.002, 0.25)


def _estimate_company_process(
    values_by_underlying_id: dict[int, tuple[float, ...]], rate_changes: list[float]
) -> tuple[dict[int, _CompanyParams], float, tuple[float, float, float], float] | None:
    """OLS of each company's log return on [1, dR], sharing one design matrix.

    Returns (company params, per-step covariance, inv(X'X) as (m00, m01, m11), relative vol error).
    """
    series: dict[int, tuple[float, ...]] = {
        underlying_id: tuple(values_by_underlying_id.get(underlying_id, ()))
        for underlying_id in _COMPANY_UNDERLYING_IDS
    }
    if any(len(values) != len(rate_changes) + 1 for values in series.values()):
        return None

    design: list[float] = []
    log_returns: dict[int, list[float]] = {underlying_id: [] for underlying_id in _COMPANY_UNDERLYING_IDS}
    for index, change in enumerate(rate_changes):
        if any(series[uid][index] <= 0.0 or series[uid][index + 1] <= 0.0 for uid in _COMPANY_UNDERLYING_IDS):
            continue
        design.append(change)
        for underlying_id in _COMPANY_UNDERLYING_IDS:
            log_returns[underlying_id].append(
                math.log(series[underlying_id][index + 1] / series[underlying_id][index])
            )

    count: int = len(design)
    if count < 4:
        return None

    change_sum: float = sum(design)
    change_square_sum: float = sum(change * change for change in design)
    determinant: float = count * change_square_sum - change_sum * change_sum
    degrees_of_freedom: int = max(count - 2, 1)
    variance_inflation: float = degrees_of_freedom / (degrees_of_freedom - 2.0) if degrees_of_freedom > 4 else 2.0

    residuals: dict[int, list[float]] = {}
    company: dict[int, _CompanyParams] = {}
    for underlying_id in _COMPANY_UNDERLYING_IDS:
        observations: list[float] = log_returns[underlying_id]
        return_sum: float = sum(observations)
        cross_sum: float = sum(change * value for change, value in zip(design, observations))
        if determinant > 1e-18:
            rate_beta: float = (count * cross_sum - change_sum * return_sum) / determinant
            drift: float = (return_sum - rate_beta * change_sum) / count
        else:  # the rate never moved, so the beta is unidentified
            rate_beta = 0.0
            drift = return_sum / count
        residuals[underlying_id] = [value - drift - rate_beta * change for change, value in zip(design, observations)]
        variance: float = max(
            sum(residual * residual for residual in residuals[underlying_id]) / degrees_of_freedom, _MIN_VARIANCE
        )
        company[underlying_id] = _CompanyParams(drift=drift, rate_beta=rate_beta, variance=variance)

    ajarai_residuals, theriodic_residuals = residuals[AJARAI_UNDERLYING_ID], residuals[THERIODIC_UNDERLYING_ID]
    raw_covariance: float = sum(a * t for a, t in zip(ajarai_residuals, theriodic_residuals)) / degrees_of_freedom
    variance_product: float = math.sqrt(
        company[AJARAI_UNDERLYING_ID].variance * company[THERIODIC_UNDERLYING_ID].variance
    )
    correlation: float = _clamp(raw_covariance / variance_product, -0.999, 0.999) if variance_product > 0.0 else 0.0

    # Shrink the correlation toward the level this market structurally runs at: both companies load
    # on one sector shock, the sample estimate off a few dozen days is noisy, and the error goes
    # straight into spread pricing, where var(difference) = v_A + v_T - 2 cov is acutely sensitive.
    correlation_error: float = (1.0 - correlation * correlation) / math.sqrt(max(count - 1, 1))
    prior_correlation_variance: float = _CORRELATION_PRIOR_STD_DEV**2
    correlation_weight: float = prior_correlation_variance / (
        prior_correlation_variance + correlation_error * correlation_error
    )
    correlation = _clamp(
        _CORRELATION_PRIOR_MEAN + (correlation - _CORRELATION_PRIOR_MEAN) * correlation_weight, -0.999, 0.999
    )

    # The variances are themselves estimated from as few as eight residuals, so the predictive law
    # is a Student t, not a normal -- too thin in the tails, pushing every probability further from
    # a half than it belongs. Matching the t's variance, nu / (nu - 2), restores it, across the
    # whole matrix or correlation is deflated with it.
    company = {
        underlying_id: replace(params, variance=params.variance * variance_inflation)
        for underlying_id, params in company.items()
    }
    covariance: float = correlation * math.sqrt(
        company[AJARAI_UNDERLYING_ID].variance * company[THERIODIC_UNDERLYING_ID].variance
    )

    if determinant > 1e-18:
        mean_uncertainty: tuple[float, float, float] = (
            change_square_sum / determinant,
            -change_sum / determinant,
            count / determinant,
        )
    else:
        average_variance: float = 0.5 * sum(params.variance for params in company.values())
        mean_uncertainty = (1.0 / count, 0.0, 0.25 / max(average_variance, _MIN_VARIANCE))

    # Shrink the drift and the rate beta, each toward its own prior with its own standard error.
    # Twenty days of a company rising sixty percent implies two and a half percent a day, which
    # taken literally extrapolates a lucky run into every price. A plain Bayesian prior leaves a
    # long history nearly untouched and pulls a short trending one back hard.
    drift_prior_variance: float = _DRIFT_PRIOR_STD_DEV**2
    beta_prior_variance: float = _RATE_BETA_PRIOR_STD_DEV**2
    drift_weight_sum: float = 0.0
    beta_weight_sum: float = 0.0
    for underlying_id, params in company.items():
        drift_error: float = mean_uncertainty[0] * params.variance
        beta_error: float = mean_uncertainty[2] * params.variance
        drift_weight: float = (
            drift_prior_variance / (drift_prior_variance + drift_error) if drift_error > _MIN_VARIANCE else 1.0
        )
        beta_weight: float = (
            beta_prior_variance / (beta_prior_variance + beta_error) if beta_error > _MIN_VARIANCE else 1.0
        )
        company[underlying_id] = replace(
            params,
            drift=_DRIFT_PRIOR_MEAN + (params.drift - _DRIFT_PRIOR_MEAN) * drift_weight,
            rate_beta=_RATE_BETA_PRIOR_MEAN + (params.rate_beta - _RATE_BETA_PRIOR_MEAN) * beta_weight,
        )
        drift_weight_sum += drift_weight
        beta_weight_sum += beta_weight
    # Both posterior variances shrink with their estimate, so `_uncertainty` stays consistent.
    companies: int = max(len(company), 1)
    mean_uncertainty = (
        mean_uncertainty[0] * drift_weight_sum / companies,
        mean_uncertainty[1],
        mean_uncertainty[2] * beta_weight_sum / companies,
    )

    return company, covariance, mean_uncertainty, _clamp(math.sqrt(1.0 / (2.0 * degrees_of_freedom)), 0.02, 0.5)


@dataclass
class _CounterpartyStats:
    markout_count: int = 0
    markout_sum: float = 0.0
    markout_square_sum: float = 0.0

    def markout_lower_bound(self, confidence_multiple: float) -> float:
        """Optimistic bound on profit per contract against this counterparty.

        The *upper* end of the interval for our own profit, so a counterparty is judged toxic only
        when the losses are too consistent to be luck.
        """
        if self.markout_count < 2:
            return 0.0
        mean: float = self.markout_sum / self.markout_count
        variance: float = max(self.markout_square_sum / self.markout_count - mean * mean, 0.0)
        standard_error: float = math.sqrt(variance / self.markout_count)
        return mean + confidence_multiple * standard_error


class MarketMaker:
    # The floor width before uncertainty, and the dominant term -- the uncertainty piece adds under
    # a penny at the median, so this is about 85% of the width shown. Split because `_half_spread`
    # also sets the fill-or-kill return-on-margin hurdle, and one constant would move both at once
    # so no result could be attributed. The quote side is cut; the fill-or-kill side is held.
    _QUOTE_BASE_HALF_SPREAD: Final[float] = 0.03
    _FOK_BASE_HALF_SPREAD: Final[float] = 0.05
    _MIN_HALF_SPREAD: Final[float] = 0.01
    # Capped well inside what a fixed-width competitor quotes: past roughly a dime the quote
    # stops winning any flow, so extra width buys protection nobody is paying for.
    _MAX_HALF_SPREAD: Final[float] = 0.15
    # Winning a trade is itself bad news -- a counterparty lifts our offer exactly when our own
    # pricing error made it look cheap -- so these sit above zero, and below one because simple
    # fixed-width makers profit here, which says the flow is mostly benign. The quote side is the
    # lower: a log shows an eleven-lot request lost with our bid 0.109 under fair against a
    # competitor's 0.05, beaten on width not size. The fill-or-kill hurdle deliberately holds.
    _QUOTE_UNCERTAINTY_MULTIPLIER: Final[float] = 0.40
    _FOK_UNCERTAINTY_MULTIPLIER: Final[float] = 0.75
    _MIN_FOK_EDGE: Final[float] = 0.005
    _CASH_BUFFER_FRACTION: Final[float] = 0.05
    _QUOTE_MARGIN_FRACTION: Final[float] = 0.50
    # Both as fractions of capacity: how many contracts of typical margin the balance supports. The
    # quote cap was 0.30 -- six lots on ten dollars, against orders running to eleven -- so whenever
    # our price was best but capped, the remainder walked down the ladder to the next maker at a far
    # better price for them. The margin test below still binds, so this frees size mainly on cheap
    # and rich contracts, where a contract posts little margin.
    _QUOTE_SIZE_FRACTION: Final[float] = 0.60
    _POSITION_CAP_FRACTION: Final[float] = 0.75
    _FOK_MARGIN_FRACTION: Final[float] = 0.30
    _MAX_TOXICITY_EDGE: Final[float] = 0.25
    _MAX_TOTAL_HALF_SPREAD: Final[float] = 0.50
    _MIN_TOXICITY_TRADES: Final[int] = 12
    _TOXICITY_CONFIDENCE: Final[float] = 2.0
    _CACHE_LIMIT: Final[int] = 4096

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

        self._initial_cash: float = max(cash_balance, 1.0)
        self._params: _Params = _default_params()
        self._vol_relative_error: float = 0.5
        self._rate_probability_error: float = 0.08

        # Every trade costs its maximum loss in cash and only expiries pay it back, so sizes are
        # margin-driven. Limits are expressed against capacity -- contracts of typical (half a
        # dollar) margin the balance supports -- since absolute caps mean nothing at the wrong scale.
        capacity: float = self._initial_cash / 0.5
        self._max_quote_size: int = max(1, int(self._QUOTE_SIZE_FRACTION * capacity))
        self._contract_position_cap: float = max(2.0, self._POSITION_CAP_FRACTION * capacity)
        self._exposure_scale: float = max(2.0, self._POSITION_CAP_FRACTION * capacity)

        self._price_cache: dict[tuple[BinaryOption, tuple[tuple[int, float], ...]], float] = {}
        self._uncertainty_cache: dict[tuple[BinaryOption, tuple[tuple[int, float], ...]], float] = {}
        self._exposure: dict[int, float] | None = None
        self._exposure_key: tuple[Any, ...] | None = None

        self._traded_contracts: dict[int, BinaryOption] = {}
        self._open_trades: dict[int, list[tuple[int, int, float]]] = defaultdict(list)
        self._recent_trades: list[tuple[int, int, int, float]] = []
        self._counterparty_stats: dict[int, _CounterpartyStats] = {}
        self._trade_counter: int = 0

        # `FokOrder.order_type` is read as the counterparty's side, so accepting a BUY means
        # selling, the same convention the RFQ pipeline uses; `_match_pending_fok` checks that
        # against each resulting fill. Entries are (option_id, price, direction, quantity).
        self._pending_fok: list[tuple[int, float, int, int]] = []
        self._fok_side_flipped: bool = False
        self._fok_agreements: int = 0
        self._fok_disagreements: int = 0

    def on_step_advance(self, new_underlying_state: list[Underlying], new_option_state: list[BinaryOption]) -> None:
        previous_values: dict[int, float] = self._read_values()
        self.underlying_state = new_underlying_state
        self.active_option_state = new_option_state
        try:
            self._update_markouts(self._settle_expired(previous_values))
        except Exception:
            pass
        self._price_cache.clear()
        self._uncertainty_cache.clear()
        self._exposure = None
        self._exposure_key = None
        self._pending_fok.clear()

    def on_trade(self, option: BinaryOption, price: float, quantity: int, counterparty_id: int) -> None:
        self.position.add_option_quantity(option.option_id, quantity)
        try:
            self._record_trade(option, price, quantity, counterparty_id)
        except Exception:
            pass

    @property
    def name(self) -> str:
        return "Telescoping Theo"

    def price_option(self, option: BinaryOption) -> float:
        try:
            values, signature = self._value_state()
            return self._theo(option, values, signature)
        except Exception:
            return 0.5

    def price_option_from_parameters(self, market_parameters: MarketParameters, option: BinaryOption) -> float:
        try:
            values, _ = self._value_state()
            return _clamp_probability(
                _price_with_params(_Params.from_market_parameters(market_parameters), values, option)
            )
        except Exception:
            return 0.5

    def quote(self, option: BinaryOption, counterparty_id: int) -> Quote:
        try:
            return self._build_quote(option, counterparty_id)
        except Exception:
            # Bidding at zero and offering at one are both riskless, so this is always safe.
            return Quote(bid_price=0.0, bid_quantity=1, offer_price=1.0, offer_quantity=1)

    def respond_to_fok(self, option: BinaryOption, fok_order: FokOrder) -> bool:
        try:
            return self._evaluate_fok(option, fok_order)
        except Exception:
            return False

    def warm_up(self, market_history: MarketHistory) -> None:
        try:
            self._estimate_parameters(market_history)
        except Exception:
            pass
        self._price_cache.clear()
        self._uncertainty_cache.clear()

    # ------------------------------------------------------------------ pricing

    def _read_values(self) -> dict[int, float]:
        return {underlying.underlying_id: underlying.value for underlying in self.underlying_state}

    def _value_state(self) -> tuple[dict[int, float], tuple[tuple[int, float], ...]]:
        values: dict[int, float] = self._read_values()
        return values, tuple(sorted(values.items()))

    def _theo(self, option: BinaryOption, values: dict[int, float], signature: tuple[tuple[int, float], ...]) -> float:
        key = (option, signature)
        cached: float | None = self._price_cache.get(key)
        if cached is None:
            if len(self._price_cache) > self._CACHE_LIMIT:
                self._price_cache.clear()
            cached = _clamp_probability(_price_with_params(self._params, values, option))
            self._price_cache[key] = cached
        return cached

    def _uncertainty(
        self,
        option: BinaryOption,
        values: dict[int, float],
        signature: tuple[tuple[int, float], ...],
        theo: float,
    ) -> float:
        """How far the price could be from the truth, given how the parameters were estimated.

        Each source is pushed by one standard error and the price moves combined in quadrature,
        the errors being largely independent. Drift and beta error also widen the mid through
        `_Params.effective_steps`, which makes the mid the posterior mean -- a separate thing from
        how far that mean can sit from the truth.
        """
        key = (option, signature)
        cached: float | None = self._uncertainty_cache.get(key)
        if cached is not None:
            return cached

        params: _Params = self._params
        vol_scale: float = (1.0 + self._vol_relative_error) ** 2
        error: float = self._rate_probability_error
        # Up and down probability error are separate sources: moving both adversely at once assumes
        # the two estimates always conspire, which massively overstated short-dated rate options.
        sources: list[tuple[_Params, _Params]] = [
            (params.with_variance_scale(vol_scale), params.with_variance_scale(1.0 / vol_scale)),
            (params.with_rate_shift(error, 0.0), params.with_rate_shift(-error, 0.0)),
            (params.with_rate_shift(0.0, error), params.with_rate_shift(0.0, -error)),
        ]
        # One source per company per parameter: a leg's own drift error is independent of the
        # other company's, so pushing them together would double count on a single-leg option.
        for leg in option.legs:
            if leg.underlying_id == FED_FUNDS_RATE_UNDERLYING_ID:
                continue
            for drift_step, beta_step in ((1.0, 0.0), (0.0, 1.0)):
                sources.append(
                    (
                        params.with_company_shift(leg.underlying_id, drift_step, beta_step),
                        params.with_company_shift(leg.underlying_id, -drift_step, -beta_step),
                    )
                )

        total: float = 0.0
        for variants in sources:
            worst: float = max(abs(_price_with_params(v, values, option) - theo) for v in variants)
            total += worst * worst
        uncertainty: float = math.sqrt(total)

        if len(self._uncertainty_cache) > self._CACHE_LIMIT:
            self._uncertainty_cache.clear()
        self._uncertainty_cache[key] = uncertainty
        return uncertainty

    def _reference_prices(self, option: BinaryOption, values: dict[int, float], theo: float) -> tuple[float, float]:
        """The prices a bid must stay under and an offer must stay over.

        Whether a zero-step option settles on today's values or after one more move is not
        specified, so bracket both readings rather than hand away what looks like free money.
        """
        if option.steps_until_expiry != 0:
            return theo, theo
        try:
            forward: float = _clamp_probability(
                _price_with_params(self._params, values, replace(option, steps_until_expiry=1))
            )
        except Exception:
            return theo, theo
        return min(theo, forward), max(theo, forward)

    def _estimate_parameters(self, market_history: MarketHistory) -> None:
        values_by_underlying_id: dict[int, tuple[float, ...]] = market_history.values_by_underlying_id
        rates: tuple[float, ...] = tuple(values_by_underlying_id.get(FED_FUNDS_RATE_UNDERLYING_ID, ()))
        if len(rates) < 3:
            return

        rate_changes: list[float] = [round(rates[index] - rates[index - 1], 2) for index in range(1, len(rates))]
        rate_step, alpha_up, alpha_down, reversion, probability_error = _estimate_rate_process(rates, rate_changes)
        estimated = _estimate_company_process(values_by_underlying_id, rate_changes)
        company: dict[int, _CompanyParams] = self._params.company
        covariance: float = self._params.covariance
        mean_uncertainty: tuple[float, float, float] = self._params.mean_uncertainty
        if estimated is not None:
            company, covariance, mean_uncertainty, self._vol_relative_error = estimated

        self._params = _Params(
            rate_up_intercept=alpha_up,
            rate_down_intercept=alpha_down,
            rate_reversion=reversion,
            rate_step=rate_step,
            company=company,
            covariance=covariance,
            mean_uncertainty=mean_uncertainty,
        )
        self._rate_probability_error = probability_error

    # ------------------------------------------------------------------ quoting

    def _build_quote(self, option: BinaryOption, counterparty_id: int) -> Quote:
        values, signature = self._value_state()
        theo: float = self._theo(option, values, signature)
        uncertainty: float = self._uncertainty(option, values, signature, theo)
        low_reference, high_reference = self._reference_prices(option, values, theo)
        half_spread: float = self._half_spread(
            uncertainty, counterparty_id, self._QUOTE_BASE_HALF_SPREAD, self._QUOTE_UNCERTAINTY_MULTIPLIER
        )
        skew: float = self._inventory_skew(option, theo, half_spread, values, signature)

        # The skew only modulates how aggressive each side is; never bid above or offer below
        # fair value, so that every fill is expected to make money and inventory is managed
        # purely through relative aggressiveness.
        bid_price: float = min(low_reference - half_spread + skew, low_reference)
        offer_price: float = max(high_reference + half_spread + skew, high_reference)
        bid_pennies: int = int(_clamp(math.floor(bid_price * 100.0 + 1e-9), 0.0, 99.0))
        offer_pennies: int = int(_clamp(math.ceil(offer_price * 100.0 - 1e-9), float(bid_pennies + 1), 100.0))

        budget: float = self._available_margin() * self._QUOTE_MARGIN_FRACTION
        size_factor: float = (1.0 / (1.0 + 6.0 * uncertainty)) * self._counterparty_size_factor(counterparty_id)
        bid_quantity: int = self._side_quantity(option, bid_pennies / 100.0, 1, budget, size_factor)
        offer_quantity: int = self._side_quantity(option, 1.0 - offer_pennies / 100.0, -1, budget, size_factor)
        if bid_quantity <= 0:  # a quantity of zero is illegal, but a bid of zero costs nothing
            bid_pennies, bid_quantity = 0, 1
        if offer_quantity <= 0:
            offer_pennies, offer_quantity = 100, 1
        if offer_pennies <= bid_pennies:
            bid_pennies = offer_pennies - 1

        return Quote(
            bid_price=bid_pennies / 100.0,
            bid_quantity=bid_quantity,
            offer_price=offer_pennies / 100.0,
            offer_quantity=offer_quantity,
        )

    def _half_spread(self, uncertainty: float, counterparty_id: int, base: float, multiplier: float) -> float:
        model_spread: float = _clamp(base + multiplier * uncertainty, self._MIN_HALF_SPREAD, self._MAX_HALF_SPREAD)
        # Toxicity is allowed past the model cap: against a counterparty that keeps winning, the
        # only real defence is a spread so wide that it amounts to declining to trade.
        return min(model_spread + self._toxicity_edge(counterparty_id), self._MAX_TOTAL_HALF_SPREAD)

    def _inventory_skew(
        self,
        option: BinaryOption,
        theo: float,
        half_spread: float,
        values: dict[int, float],
        signature: tuple[tuple[int, float], ...],
    ) -> float:
        exposure: dict[int, float] = self._underlying_exposure(values, signature)
        sensitivity: float = 4.0 * theo * (1.0 - theo)  # cheap proxy for how at-the-money it is
        aggregate: float = 0.0
        for leg in option.legs:
            aggregate += (1.0 if leg.weight > 0.0 else -1.0) * exposure.get(leg.underlying_id, 0.0)

        contract_position: float = float(self.position.option_quantity_by_option_id.get(option.option_id, 0))
        raw: float = (
            0.8 * aggregate * sensitivity / self._exposure_scale
            + 0.6 * contract_position / self._contract_position_cap
        )
        return -half_spread * _clamp(raw, -1.5, 1.5)

    def _underlying_exposure(
        self, values: dict[int, float], signature: tuple[tuple[int, float], ...]
    ) -> dict[int, float]:
        key: tuple[Any, ...] = (signature, self._trade_counter)
        if self._exposure is not None and self._exposure_key == key:
            return self._exposure

        exposure: dict[int, float] = defaultdict(float)
        for option in self.active_option_state:
            quantity: int = self.position.option_quantity_by_option_id.get(option.option_id, 0)
            if not quantity:
                continue
            price: float = self._theo(option, values, signature)
            sensitivity: float = 4.0 * price * (1.0 - price)
            for leg in option.legs:
                exposure[leg.underlying_id] += quantity * sensitivity * (1.0 if leg.weight > 0.0 else -1.0)

        self._exposure = exposure
        self._exposure_key = key
        return exposure

    def _side_quantity(
        self, option: BinaryOption, margin_per_contract: float, direction: int, budget: float, size_factor: float
    ) -> int:
        if margin_per_contract <= _EPSILON:  # riskless side, only position limits apply
            quantity: float = float(self._max_quote_size)
        else:
            quantity = budget / margin_per_contract
        quantity = min(quantity, float(self._max_quote_size)) * max(size_factor, 0.0)
        current: float = float(self.position.option_quantity_by_option_id.get(option.option_id, 0))
        room: float = self._contract_position_cap - direction * current
        return int(max(0.0, min(quantity, room)))

    def _available_margin(self) -> float:
        return max(0.0, self.cash_balance - self._CASH_BUFFER_FRACTION * self._initial_cash)

    # ------------------------------------------------------------------ fill-or-kill

    def _evaluate_fok(self, option: BinaryOption, fok_order: FokOrder) -> bool:
        values, signature = self._value_state()
        theo: float = self._theo(option, values, signature)
        uncertainty: float = self._uncertainty(option, values, signature, theo)
        low_reference, high_reference = self._reference_prices(option, values, theo)
        # The fill-or-kill path reads its own multiplier throughout: this width also sets the
        # return-on-margin hurdle below, so leaving it on the quote constant would keep the two
        # coupled through the back door.
        half_spread: float = self._half_spread(
            uncertainty, fok_order.counterparty_id, self._FOK_BASE_HALF_SPREAD, self._FOK_UNCERTAINTY_MULTIPLIER
        )
        skew: float = self._inventory_skew(option, theo, half_spread, values, signature)

        buying: bool = self._buys_on_fok(fok_order.order_type)
        direction: int = 1 if buying else -1
        quantity: float = float(fok_order.quantity)
        if buying:
            edge: float = low_reference - fok_order.price
            margin: float = max(fok_order.price, 0.0) * quantity
        else:
            edge = fok_order.price - high_reference
            margin = max(1.0 - fok_order.price, 0.0) * quantity

        required: float = (
            self._MIN_FOK_EDGE
            + self._FOK_UNCERTAINTY_MULTIPLIER * uncertainty
            + self._toxicity_edge(fok_order.counterparty_id)
        )
        required = max(0.5 * self._MIN_FOK_EDGE, required - direction * skew)
        if edge < required:
            return False

        # A positive edge is not enough when margin is scarce: eight deep in-the-money contracts at
        # 0.99 for a cent apiece is one percent on half the balance, and that margin is worth more
        # quoting ordinary flow. So the trade must also clear a return on the margin it consumes.
        margin_per_contract: float = margin / quantity
        if margin_per_contract > _EPSILON:
            utilisation: float = _clamp(1.0 - self._available_margin() / self._initial_cash, 0.0, 1.0)
            hurdle: float = half_spread * (0.5 + 1.5 * utilisation)
            if edge / margin_per_contract < hurdle:
                return False

        # Accepting can be allocated the whole order, so it has to be affordable in full.
        if margin > self._available_margin() * self._FOK_MARGIN_FRACTION:
            return False
        current: float = float(self.position.option_quantity_by_option_id.get(option.option_id, 0))
        projected: float = current + direction * quantity
        if abs(projected) > self._contract_position_cap and projected * direction > 0.0:
            return False

        self._pending_fok.append((fok_order.option_id, fok_order.price, direction, fok_order.quantity))
        return True

    def _buys_on_fok(self, order_type: OrderType) -> bool:
        counterparty_buys: bool = order_type == OrderType.BUY
        if self._fok_side_flipped:
            counterparty_buys = not counterparty_buys
        return not counterparty_buys

    def _match_pending_fok(self, option_id: int, price: float, quantity: int) -> None:
        """Confirm the side convention against the fill an accepted fill-or-kill produced.

        Contract and price alone cannot separate that fill from an ordinary request-for-quote fill
        on the same contract at the same penny, so the accepted quantity is part of the key. An
        order is allocated at most in full, so requiring no more never rejects a genuine match.
        """
        for index, (pending_id, pending_price, expected_direction, pending_quantity) in enumerate(self._pending_fok):
            if pending_id != option_id or abs(pending_price - price) > _EPSILON:
                continue
            if abs(quantity) > pending_quantity:
                continue
            del self._pending_fok[index]
            if (1 if quantity > 0 else -1) == expected_direction:
                self._fok_agreements += 1
            else:
                self._fok_disagreements += 1
            # Only a convention that was wrong from the very first trade is worth overturning. Once
            # any fill has agreed, the reading is settled and later noise must not flip it -- an
            # inverted reading would have the bot buying every order it meant to sell.
            if self._fok_disagreements >= 3 and self._fok_agreements == 0:
                self._fok_side_flipped = not self._fok_side_flipped
                self._fok_agreements = 0
                self._fok_disagreements = 0
            return

    # ------------------------------------------------------------------ cash and inventory

    def _record_trade(self, option: BinaryOption, price: float, quantity: int, counterparty_id: int) -> None:
        self._trade_counter += 1
        self._traded_contracts[option.option_id] = option
        # Shadow the exchange: each trade costs its maximum loss, clamped so the shadow is never
        # more optimistic than the real balance.
        if quantity >= 0:
            self.cash_balance -= quantity * max(price, 0.0)
        else:
            self.cash_balance -= (-quantity) * max(1.0 - price, 0.0)
        self._open_trades[option.option_id].append((counterparty_id, quantity, price))
        self._recent_trades.append((option.option_id, counterparty_id, quantity, price))
        self._match_pending_fok(option.option_id, price, quantity)

    def _update_markouts(self, settled_payoffs: dict[int, float]) -> None:
        """Score each of the day's trades against where the option stands one step later."""
        if not self._recent_trades:
            return

        values, signature = self._value_state()
        active: dict[int, BinaryOption] = {option.option_id: option for option in self.active_option_state}
        for option_id, counterparty_id, quantity, price in self._recent_trades:
            option: BinaryOption | None = active.get(option_id)
            if option is not None:
                reference: float = self._theo(option, values, signature)
            elif option_id in settled_payoffs:
                reference = settled_payoffs[option_id]
            else:
                continue
            stats: _CounterpartyStats = self._counterparty_stats.setdefault(counterparty_id, _CounterpartyStats())
            per_contract: float = (1.0 if quantity > 0 else -1.0) * (reference - price)
            stats.markout_count += 1
            stats.markout_sum += per_contract
            stats.markout_square_sum += per_contract * per_contract
        self._recent_trades.clear()

    def _settle_expired(self, previous_values: dict[int, float]) -> dict[int, float]:
        settled_payoffs: dict[int, float] = {}
        active_ids: set[int] = {option.option_id for option in self.active_option_state}
        current_values: dict[int, float] = self._read_values()
        for option_id in list(self._traded_contracts):
            if option_id in active_ids:
                continue

            contract: BinaryOption = self._traded_contracts.pop(option_id)
            trades: list[tuple[int, int, float]] = self._open_trades.pop(option_id, [])
            self.position.option_quantity_by_option_id.pop(option_id, None)
            try:
                payoff: float = contract.expiry_valuation(current_values)
            except KeyError:
                continue
            settled_payoffs[option_id] = payoff

            try:
                previous_payoff: float = contract.expiry_valuation(previous_values)
            except KeyError:
                previous_payoff = payoff

            # Each trade reserved its own maximum loss, so each is released separately -- netting
            # would make a profitable round trip destroy cash. Settlement may use the values from
            # before or after this advance; crediting the smaller only makes the bot more cautious.
            # Crediting in full is exactly correct and was tried: the freed margin raises every
            # quote size, and the grader fell 15.1 -> 14.3. Under-margined is not the defect it looks.
            for _, traded_quantity, _ in trades:
                if traded_quantity > 0:
                    self.cash_balance += traded_quantity * min(payoff, previous_payoff)
                else:
                    self.cash_balance += (-traded_quantity) * min(1.0 - payoff, 1.0 - previous_payoff)
        return settled_payoffs

    def _toxicity_edge(self, counterparty_id: int) -> float:
        loss_per_contract: float = -self._counterparty_pnl_per_contract(counterparty_id)
        if loss_per_contract <= 0.0:
            return 0.0
        # Charge more than the observed loss: a counterparty that has beaten us by this much per
        # contract is presumed to keep doing so, and only a wider quote than that stops it.
        return min(self._MAX_TOXICITY_EDGE, 1.5 * loss_per_contract)

    def _counterparty_size_factor(self, counterparty_id: int) -> float:
        loss_per_contract: float = -self._counterparty_pnl_per_contract(counterparty_id)
        if loss_per_contract <= 0.0:
            return 1.0
        return 1.0 / (1.0 + 25.0 * loss_per_contract)

    def _counterparty_pnl_per_contract(self, counterparty_id: int) -> float:
        """Profit per contract against this counterparty, from one-day markouts.

        Markouts rather than settled payoffs: a binary payoff is a coin flip worth about 0.5 per
        contract in noise, which drowns an information edge of a few pennies, whereas the next
        day's theo is a conditional expectation and moves far less. Even so the mean alone misjudges
        ordinary counterparties often enough to cost more than it saves, so this returns the
        optimistic end of the interval -- only a consistent loser looks toxic.
        """
        stats: _CounterpartyStats | None = self._counterparty_stats.get(counterparty_id)
        if stats is None or stats.markout_count < self._MIN_TOXICITY_TRADES:
            return 0.0
        return stats.markout_lower_bound(self._TOXICITY_CONFIDENCE)
