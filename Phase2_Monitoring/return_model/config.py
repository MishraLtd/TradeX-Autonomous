"""
Central configuration for the Return Prediction Engine.

NOTHING here is a magic number pulled from thin air — every threshold
is meant to be re-derived empirically per Section 6/20/11 of the spec
before PRODUCTION promotion. Defaults below are reasonable starting
points for NSE cash-equity, low-capital (~₹1,000) trading and MUST be
validated against real data before being trusted.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Sequence


class Horizon(str, Enum):
    # Intraday (bars are wall-clock minutes from session open)
    INTRA_30M = "intraday_30m"
    INTRA_60M = "intraday_60m"
    INTRA_120M = "intraday_120m"
    INTRA_EOD = "intraday_eod"
    # Delivery / swing (trading days, not calendar days)
    SWING_1D = "swing_1d"
    SWING_2D = "swing_2d"
    SWING_3D = "swing_3d"
    SWING_5D = "swing_5d"
    SWING_10D = "swing_10d"

    @property
    def is_intraday(self) -> bool:
        return self.value.startswith("intraday")


INTRADAY_HORIZONS: Sequence[Horizon] = (
    Horizon.INTRA_30M, Horizon.INTRA_60M, Horizon.INTRA_120M, Horizon.INTRA_EOD,
)
SWING_HORIZONS: Sequence[Horizon] = (
    Horizon.SWING_1D, Horizon.SWING_2D, Horizon.SWING_3D,
    Horizon.SWING_5D, Horizon.SWING_10D,
)

# Overlap (in bars/days) used for purging/embargo per horizon. Must be
# >= the label's forward window so overlapping labels don't leak across
# train/validation/test splits (Section 23).
LABEL_OVERLAP = {
    Horizon.INTRA_30M: 30, Horizon.INTRA_60M: 60, Horizon.INTRA_120M: 120,
    Horizon.INTRA_EOD: 375,  # approx NSE session minutes
    Horizon.SWING_1D: 1, Horizon.SWING_2D: 2, Horizon.SWING_3D: 3,
    Horizon.SWING_5D: 5, Horizon.SWING_10D: 10,
}
EMBARGO_EXTRA_BARS = {  # extra buffer beyond the overlap, conservative
    Horizon.INTRA_30M: 5, Horizon.INTRA_60M: 10, Horizon.INTRA_120M: 15,
    Horizon.INTRA_EOD: 15,
    Horizon.SWING_1D: 1, Horizon.SWING_2D: 1, Horizon.SWING_3D: 2,
    Horizon.SWING_5D: 2, Horizon.SWING_10D: 3,
}

# Candidate probability-of-exceeding thresholds. These are STARTING
# candidates, not final — Section 11 requires deriving them from
# realized volatility/cost data per horizon before use. Kept here as
# the configurable surface the validation stage sweeps over.
CANDIDATE_RETURN_THRESHOLDS_PCT: Sequence[float] = (0.25, 0.50, 1.00, 2.00)

QUANTILES: Sequence[float] = (0.10, 0.25, 0.50, 0.75, 0.90)

CAPITAL_LEVELS_INR: Sequence[int] = (500, 750, 1000, 1500, 2000, 5000, 10000, 25000)


@dataclass(frozen=True)
class DataVersion:
    """Every dataset build gets a content-addressable-ish version tag."""
    feature_version: str
    target_version: str
    universe_version: str  # ties back to FILTER2.0's own versioning
    data_snapshot_date: str  # YYYY-MM-DD, last date of data included

    @property
    def tag(self) -> str:
        return f"feat={self.feature_version}|tgt={self.target_version}|uni={self.universe_version}|asof={self.data_snapshot_date}"


@dataclass
class EngineConfig:
    horizons: Sequence[Horizon] = field(default_factory=lambda: (*INTRADAY_HORIZONS, *SWING_HORIZONS))
    quantiles: Sequence[float] = QUANTILES
    thresholds_pct: Sequence[float] = CANDIDATE_RETURN_THRESHOLDS_PCT
    min_history_bars: int = 250          # min bars before a stock enters training (Sec 26/29)
    min_train_observations: int = 2000   # below this -> PREDICTION_UNAVAILABLE
    random_seed: int = 42
    max_uncertainty_for_trust: float = 0.65  # normalized width -> LOW_TRUST above this (Sec 28/29)
