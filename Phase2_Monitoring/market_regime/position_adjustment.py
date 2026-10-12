"""
§31/§32/§54 - the Regime Model RECOMMENDS a position multiplier and a
gating decision (ALLOW/REDUCE/RESTRICT/BLOCK). It never places, sizes, or
blocks an order directly; the Portfolio Manager and Risk Engine remain the
authority (§54) and may be MORE conservative than this output but should
never be LESS conservative than a BLOCK from here.

Base multipliers below are again explicitly-flagged PRIORS (§31: "DO NOT
hard-code these values... must be calibrated using historical risk-adjusted
performance"), kept in one small, easily-replaced table.
"""

from __future__ import annotations
from dataclasses import dataclass

from .enums import RegimeLabel, StressLevel, TradePermission
from .config import ConfidenceConfig

BASE_POSITION_MULTIPLIER_PRIOR = {
    RegimeLabel.TRENDING_UP: 1.00,
    RegimeLabel.TRENDING_DOWN: 0.50,   # long-only book by default per TradeX v1 scope
    RegimeLabel.SIDEWAYS: 0.70,
    RegimeLabel.HIGH_VOLATILITY: 0.50,
    RegimeLabel.LOW_VOLATILITY: 0.85,
    RegimeLabel.PANIC: 0.00,
    RegimeLabel.UNKNOWN: 0.00,
}

BASE_GATING_PRIOR = {
    RegimeLabel.TRENDING_UP: TradePermission.ALLOW,
    RegimeLabel.TRENDING_DOWN: TradePermission.REDUCE,
    RegimeLabel.SIDEWAYS: TradePermission.ALLOW,
    RegimeLabel.HIGH_VOLATILITY: TradePermission.REDUCE,
    RegimeLabel.LOW_VOLATILITY: TradePermission.ALLOW,
    RegimeLabel.PANIC: TradePermission.BLOCK,
    RegimeLabel.UNKNOWN: TradePermission.RESTRICT,
}

_PERMISSION_SEVERITY = {
    TradePermission.ALLOW: 0,
    TradePermission.REDUCE: 1,
    TradePermission.RESTRICT: 2,
    TradePermission.BLOCK: 3,
}


def _most_restrictive(a: TradePermission, b: TradePermission) -> TradePermission:
    return a if _PERMISSION_SEVERITY[a] >= _PERMISSION_SEVERITY[b] else b


def compute_position_multiplier_and_gating(
    regime: RegimeLabel,
    confidence: float,
    stability: float,
    stress_level: StressLevel,
    data_freshness_ok: bool,
    confidence_cfg: ConfidenceConfig = None,
):
    """Returns (position_multiplier: float, trade_permission: TradePermission).

    Rules, in order of precedence (most conservative always wins - §44):
    1. Stale/missing data -> BLOCK, multiplier 0 regardless of label.
    2. PANIC or SEVERE stress -> at least RESTRICT even if label lags behind.
    3. Low confidence or low stability scales the multiplier down further
       and can escalate gating by one severity level (§29).
    """
    confidence_cfg = confidence_cfg or ConfidenceConfig()

    if not data_freshness_ok:
        return 0.0, TradePermission.BLOCK

    base_mult = BASE_POSITION_MULTIPLIER_PRIOR.get(regime, 0.0)
    permission = BASE_GATING_PRIOR.get(regime, TradePermission.RESTRICT)

    if stress_level == StressLevel.PANIC:
        permission = _most_restrictive(permission, TradePermission.BLOCK)
        base_mult = 0.0
    elif stress_level == StressLevel.SEVERE:
        permission = _most_restrictive(permission, TradePermission.RESTRICT)
        base_mult = min(base_mult, 0.3)

    # confidence/stability scaling - continuous, not a step function, so
    # small confidence changes don't cause discontinuous position jumps
    conf_factor = max(0.0, min(1.0, confidence))
    stab_factor = max(0.0, min(1.0, stability))
    scaled_mult = base_mult * (0.3 + 0.7 * conf_factor) * (0.3 + 0.7 * stab_factor)

    if confidence < confidence_cfg.low:
        permission = _most_restrictive(permission, TradePermission.RESTRICT)
    elif confidence < confidence_cfg.cautious:
        permission = _most_restrictive(permission, TradePermission.REDUCE)

    return max(0.0, min(1.0, scaled_mult)), permission
