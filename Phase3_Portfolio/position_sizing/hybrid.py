from decimal import Decimal

from .config import SizingConfig


def calculate_hybrid_size(
    base_risk_size: Decimal,
    confidence_mult: Decimal,
    volatility_mult: Decimal,
    regime_mult: Decimal,
    portfolio_risk_mult: Decimal,
    config: SizingConfig,
) -> dict:
    """Combine bounded multipliers, then bound the COMBINED product too, to
    prevent multiplicative compounding blowup (DESIGN.md Part 9/17)."""
    raw_combined = confidence_mult * volatility_mult * regime_mult * portfolio_risk_mult
    combined_mult = max(config.min_combined_mult, min(config.max_combined_mult, raw_combined))

    size_after_adjustments = base_risk_size * combined_mult

    return {
        "raw_combined_mult": raw_combined,
        "combined_mult": combined_mult,
        "size_after_adjustments_raw": size_after_adjustments,
    }
