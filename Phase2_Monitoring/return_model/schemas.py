"""
Strict I/O schemas. These are the contracts every other TradeX phase
(Opportunity Engine, Portfolio Manager, Backtester) codes against.
Changing a field is a breaking change and requires a schema version bump.
"""

from __future__ import annotations
from datetime import datetime
from enum import Enum
from typing import Optional, Dict, List

from pydantic import BaseModel, Field, field_validator


class PredictionStatus(str, Enum):
    VALID = "VALID"
    LOW_TRUST = "PREDICTION_LOW_TRUST"
    UNAVAILABLE = "PREDICTION_UNAVAILABLE"


class ModelStatus(str, Enum):
    EXPERIMENTAL = "EXPERIMENTAL"
    VALIDATED = "VALIDATED"
    PAPER = "PAPER"
    PRODUCTION = "PRODUCTION"
    RETIRED = "RETIRED"


class QuantileEstimate(BaseModel):
    q10: Optional[float] = None
    q25: Optional[float] = None
    q50: Optional[float] = None
    q75: Optional[float] = None
    q90: Optional[float] = None


class ThresholdProbabilities(BaseModel):
    """P(return > threshold) for each configured threshold, in decimal (0-1)."""
    probs: Dict[float, float] = Field(default_factory=dict)  # {0.25: 0.671, ...}


class FeatureContribution(BaseModel):
    name: str
    contribution_pct: float  # signed, in return-% units, per Sec 27 example


class RawPrediction(BaseModel):
    """Output of the ML layer BEFORE cost-model integration (Sec 18/38)."""
    symbol: str
    prediction_timestamp: datetime
    horizon: str
    model_id: str
    model_version: str
    data_version: str

    expected_gross_return_pct: float
    probability_positive: float
    threshold_probs: ThresholdProbabilities
    expected_mfe_pct: float
    expected_mae_pct: float
    quantiles_pct: QuantileEstimate
    prediction_interval_pct: tuple[float, float]
    confidence: float  # 0-1, calibrated

    top_contributors: List[FeatureContribution] = Field(default_factory=list)
    is_out_of_distribution: bool = False
    status: PredictionStatus = PredictionStatus.VALID
    rejection_reason: Optional[str] = None

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, v: float) -> float:
        return max(0.0, min(1.0, v))


class NetPrediction(BaseModel):
    """RawPrediction + Cost Model output = the final contract other
    phases consume (Sec 38 output contract)."""
    raw: RawPrediction
    expected_total_friction_pct: float
    expected_net_return_pct: float
    cost_model_version: str
    economically_viable: bool
    status: PredictionStatus


class FeatureVectorRecord(BaseModel):
    """A single row of the prediction_features table."""
    symbol: str
    timestamp: datetime
    feature_version: str
    features: Dict[str, float]
    universe_eligible: bool


class ModelRegistryEntry(BaseModel):
    model_id: str
    model_version: str
    horizon: str
    training_start: datetime
    training_end: datetime
    feature_version: str
    target_definition: str
    hyperparameters: Dict[str, float | int | str | bool]
    training_metrics: Dict[str, float]
    validation_metrics: Dict[str, float]
    test_metrics: Dict[str, float]
    calibration_metrics: Dict[str, float]
    data_version: str
    status: ModelStatus
    created_at: datetime
