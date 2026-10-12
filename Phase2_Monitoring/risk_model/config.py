"""
All thresholds live here, are documented, and are versioned via
CONFIG_VERSION. Nothing in the rest of the package should hardcode a
threshold inline (Section 55: NO HIDDEN CONSTANTS).
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal

CONFIG_VERSION = "risk_config_v1"

TradeType = Literal["INTRADAY", "DELIVERY"]
Regime = Literal[
    "TRENDING_UP", "TRENDING_DOWN", "SIDEWAYS",
    "HIGH_VOLATILITY", "LOW_VOLATILITY", "PANIC", "RECOVERY", "UNKNOWN",
]


@dataclass(frozen=True)
class RiskConfig:
    # --- large-loss probability thresholds (Section 4 / Target F) ---
    large_loss_thresholds_pct: tuple = (0.25, 0.50, 1.0, 2.0, 3.0)

    # --- minimum data support before the model is allowed to speak ---
    min_training_samples_intraday: int = 300
    min_training_samples_delivery: int = 250
    min_similar_state_samples: int = 30  # for local/conditional estimates

    # --- OOD / novelty ---
    ood_mahalanobis_chi2_percentile: float = 0.995  # flag beyond this quantile
    # of the chi-square distribution with df = n_features

    # --- calibration monitoring ---
    calibration_bins: int = 10
    calibration_degradation_ece_threshold: float = 0.08  # abs. expected
    # calibration error beyond which RISK_MODEL_CALIBRATION_DEGRADED fires

    # --- purge / embargo for walk-forward CV (Section 34) ---
    embargo_bars: int = 5  # extra bars purged after the max horizon,
    # to absorb serial correlation beyond the label window itself

    # --- risk gates (Section 49) — defaults only, must be tuned via
    # historical analysis + risk-of-ruin study before production use ---
    default_max_probability_of_loss: float = 0.55
    default_max_probability_stop_hit: float = 0.45
    default_max_expected_downside_pct: float = 2.5
    default_max_mae_p90_pct: float = 4.0
    default_max_model_uncertainty: float = 0.5  # on a 0-1 uncertainty scale

    # --- capital constraints (Section 25) ---
    default_total_capital_rupees: float = 1000.0
    default_reserve_fraction: float = 0.25

    # --- horizons ---
    intraday_horizon_minutes: int = 375  # ~9:15 to 15:30 IST full session
    delivery_horizon_days_default: int = 5
    delivery_horizon_days_max: int = 10

    # --- volatility estimation windows (trading days, for delivery;
    # trading bars for intraday features are handled at the feature layer) ---
    vol_windows_days: tuple = (5, 10, 20)


DEFAULT_CONFIG = RiskConfig()
