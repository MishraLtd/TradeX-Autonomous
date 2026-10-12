from __future__ import annotations
from datetime import datetime
from enum import Enum
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field


class DataQualityStatus(str, Enum):
    VALID = "VALID"
    DEGRADED = "DEGRADED"
    DATA_INCOMPLETE = "DATA_INCOMPLETE"
    STALE = "STALE"


class RiskModelStatus(str, Enum):
    OK = "OK"
    UNCERTAIN = "RISK_PREDICTION_UNCERTAIN"
    OUT_OF_DISTRIBUTION = "OUT_OF_DISTRIBUTION"
    MODEL_STALE = "MODEL_STALE"
    REJECT = "REJECT"


class TradeType(str, Enum):
    INTRADAY = "INTRADAY"
    DELIVERY = "DELIVERY"


class RiskPrediction(BaseModel):
    # --- identity ---
    symbol: str
    exchange: str = "NSE"
    trade_type: TradeType
    timestamp: datetime
    entry_price: float
    stop_price: float
    target_price: float
    holding_horizon: str  # e.g. "EOD" or "5D"

    # --- core probabilities ---
    probability_of_loss: float = Field(ge=0, le=1)
    probability_of_stop_hit: float = Field(ge=0, le=1)
    probability_of_target_before_stop: float = Field(ge=0, le=1)
    probability_of_neither: float = Field(ge=0, le=1, default=0.0)
    probability_of_large_loss: dict = Field(default_factory=dict)  # {"1.0%": 0.08, ...}

    # --- downside magnitude ---
    expected_downside_pct: float
    median_downside_pct: float
    expected_mae_pct: float
    mae_p25_pct: float
    mae_p50_pct: float
    mae_p75_pct: float
    mae_p90_pct: float
    expected_drawdown_pct: Optional[float] = None

    # --- volatility & tail ---
    volatility_forecast_pct: float
    tail_risk_p95_loss_pct: Optional[float] = None
    tail_risk_p99_loss_pct: Optional[float] = None
    tail_estimate_reliable: bool = True  # False => TAIL_ESTIMATE_UNRELIABLE

    # --- gap risk (delivery only; None for intraday) ---
    probability_of_gap_down: Optional[float] = None
    expected_gap_down_pct: Optional[float] = None
    probability_gap_exceeds_stop: Optional[float] = None

    # --- timing ---
    expected_time_to_stop: Optional[str] = None
    expected_time_to_target: Optional[str] = None

    # --- risk score (see scoring.py for the transparent formula) ---
    risk_score: float = Field(ge=0, le=100)

    # --- confidence / uncertainty (Section 17-19) ---
    prediction_confidence: float = Field(ge=0, le=1)
    prediction_uncertainty: float = Field(ge=0, le=1)
    similar_sample_count: int
    ood_score: float
    is_out_of_distribution: bool

    # --- regime context ---
    regime: str = "UNKNOWN"
    risk_in_current_regime: Optional[float] = None
    regime_confidence: Optional[float] = None

    # --- versioning & governance (Section 54) ---
    model_version: str
    feature_version: str
    training_cutoff: datetime
    config_version: str

    # --- status ---
    data_quality_status: DataQualityStatus
    risk_model_status: RiskModelStatus

    model_config = ConfigDict(use_enum_values=True)
