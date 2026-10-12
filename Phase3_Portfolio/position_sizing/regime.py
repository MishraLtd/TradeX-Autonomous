from decimal import Decimal

from .config import SizingConfig
from .enums import MarketRegime


def calculate_regime_adjustment(market_regime: MarketRegime, config: SizingConfig) -> Decimal:
    """Regime multiplier bounded to [0, 1.0] by design — a regime can only
    REDUCE size, never amplify it (DESIGN.md Part 9/11). Position Sizing does
    not decide whether trading is permitted; it only scales size."""
    key = market_regime.value if hasattr(market_regime, "value") else str(market_regime)
    mult = config.regime_multipliers.get(key, config.regime_multipliers["UNKNOWN"])
    return max(Decimal("0"), min(Decimal("1"), mult))
