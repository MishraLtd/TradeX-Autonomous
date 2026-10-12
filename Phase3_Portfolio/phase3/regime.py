"""
Regime vocabulary bridge.

Three packages, three independently-specified regime vocabularies:

  portfolio_manager.MarketRegime : TRENDING | RANGING | VOLATILE | UNKNOWN
  position_sizing.MarketRegime   : TRENDING_UP | TRENDING_DOWN | SIDEWAYS |
                                   HIGH_VOLATILITY | LOW_VOLATILITY | PANIC | UNKNOWN
  portfolio_risk (plain strings) : STRONG_BULLISH | BULLISH | NEUTRAL |
                                   BEARISH | STRONG_BEARISH | HIGH_VOLATILITY | UNKNOWN

Translating between them is a genuine integration decision, so it lives
here, in one place, with the rule written down — rather than being done
inline at three different call sites with three different guesses.

Rule: never invent information the source label does not contain.
PM's TRENDING has no direction, so it only becomes TRENDING_UP when the
caller's own regime_confidence clears the configured threshold; below
that it degrades to SIDEWAYS. Nothing ever maps to PANIC or
STRONG_BEARISH from a PM label, because PM has no label that means that —
a caller who has that information should pass it explicitly.
"""
from __future__ import annotations

from position_sizing.enums import MarketRegime as SizingRegime


def _label(regime) -> str:
    return regime.value if hasattr(regime, "value") else str(regime)


def to_sizing_regime(pm_regime, regime_confidence: float, config) -> SizingRegime:
    label = _label(pm_regime)
    mapped = config.regime_to_sizing.get(label, "UNKNOWN")

    if mapped == "TRENDING_UP" and (
        regime_confidence is None or regime_confidence < config.directional_regime_confidence
    ):
        # Direction is not established well enough to size as a trend.
        mapped = "SIDEWAYS"

    try:
        return SizingRegime(mapped)
    except ValueError:
        return SizingRegime.UNKNOWN


def to_risk_regime(pm_regime, config) -> str:
    label = _label(pm_regime)
    mapped = config.regime_to_risk.get(label)
    if mapped is None:
        # An unrecognized label must not silently inherit NEUTRAL's looser
        # limits — portfolio_risk treats UNKNOWN conservatively (0.90x).
        return "UNKNOWN"
    return mapped
