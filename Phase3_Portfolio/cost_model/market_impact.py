"""
PART 7 — MARKET IMPACT

For a ₹1,000-₹100,000 order in a liquid large-cap NSE stock, market
impact is usually negligible — but "usually negligible" is not the same
as "zero", and the model must behave sensibly for illiquid small-caps
too. We use a simple, defensible square-root participation model
(the standard first-order approximation used across execution-cost
literature: impact scales roughly with the square root of participation
rate, not linearly), scaled by a volatility multiplier when available.

    impact_bps = K * volatility_multiplier * sqrt(participation_rate) * 10000

Where:
    participation_rate = order_value / avg_daily_traded_value
    K                   = a conservative, configurable constant (bps
                           per unit of sqrt(participation) at 1x
                           volatility) — NOT a fitted/precise
                           coefficient; this is intentionally a
                           conservative round number, not fake
                           precision (PART 24 / PART 26).

When avg_daily_traded_value is unavailable, the model falls back to a
configurable conservative flat assumption rather than assuming zero
impact — a stock with NO liquidity data available is treated as if it
were relatively illiquid, not as if it were infinitely liquid.
"""
from __future__ import annotations

from decimal import Decimal

from .models import LiquiditySnapshot

# K: bps of impact at 100% participation and 1x normal volatility, before
# the sqrt scaling is applied. Deliberately conservative/round.
IMPACT_K_BPS = Decimal("40")

# Used when avg_daily_traded_value is missing entirely.
NO_LIQUIDITY_DATA_FLAT_BPS = Decimal("10")

# Applied when recent_volatility_pct is not supplied — neutral (1x).
DEFAULT_VOLATILITY_MULTIPLIER = Decimal("1.0")

# Reference "normal" daily volatility (%) used to scale the multiplier;
# a stock trading at e.g. 2x this ATR% gets ~2x the impact assumption.
REFERENCE_VOLATILITY_PCT = Decimal("1.5")

# Impact is floored/ceilinged to keep the model from producing either
# a fake-precise near-zero number or an absurdly large one on bad inputs.
MIN_IMPACT_BPS = Decimal("0.5")
MAX_IMPACT_BPS = Decimal("250")   # 2.5% hard ceiling — beyond this, flag for manual review


def _isqrt_decimal(x: Decimal) -> Decimal:
    """Decimal-safe sqrt via Newton's method (avoids float contamination
    of financial figures)."""
    if x <= 0:
        return Decimal("0")
    guess = x
    for _ in range(60):
        guess = (guess + x / guess) / 2
    return guess


def estimate_market_impact(turnover: Decimal, liquidity: LiquiditySnapshot) -> tuple:
    """Returns (impact_cost_rupees, tier, explanation)."""
    if turnover == 0:
        return Decimal("0.00"), "LEVEL_0", "Zero turnover — no impact to estimate."

    if liquidity.avg_daily_traded_value is None or liquidity.avg_daily_traded_value <= 0:
        bps = NO_LIQUIDITY_DATA_FLAT_BPS
        cost = (turnover * bps / Decimal("10000")).quantize(Decimal("0.01"))
        return cost, "NO_LIQUIDITY_DATA_FALLBACK", (
            f"No average-daily-traded-value data — used flat conservative "
            f"assumption of {bps}bps rather than assuming zero impact."
        )

    participation = turnover / liquidity.avg_daily_traded_value

    if liquidity.recent_volatility_pct is not None and liquidity.recent_volatility_pct > 0:
        vol_multiplier = liquidity.recent_volatility_pct / REFERENCE_VOLATILITY_PCT
    else:
        vol_multiplier = DEFAULT_VOLATILITY_MULTIPLIER

    sqrt_participation = _isqrt_decimal(participation)
    bps = IMPACT_K_BPS * vol_multiplier * sqrt_participation

    clamped_note = ""
    if bps < MIN_IMPACT_BPS:
        clamped_note = f"; floored from {bps:.4f}bps to {MIN_IMPACT_BPS}bps"
        bps = MIN_IMPACT_BPS
    elif bps > MAX_IMPACT_BPS:
        clamped_note = f"; capped from {bps:.4f}bps to {MAX_IMPACT_BPS}bps (flag for manual review)"
        bps = MAX_IMPACT_BPS

    cost = (turnover * bps / Decimal("10000")).quantize(Decimal("0.01"))
    explanation = (
        f"participation={participation*100:.4f}% of ADV, vol_multiplier={vol_multiplier:.2f}, "
        f"impact={bps:.4f}bps{clamped_note}"
    )
    tier = "LIQUIDITY_ADJUSTED_SQRT_MODEL"
    return cost, tier, explanation
