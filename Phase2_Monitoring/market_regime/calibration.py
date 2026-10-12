"""
§28 calibration (Platt scaling / isotonic regression) requires a labeled
historical dataset of (predicted_score, realized_regime) pairs spanning
multiple market eras (§26). We don't have that data in this environment,
so this module ships an honest identity pass-through plus the exact
interface a real calibrator will implement - swap `IdentityCalibrator` for
`PlattCalibrator`/`IsotonicCalibrator` once fit() has real data, with zero
changes needed in inference.py.
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from typing import List, Tuple


class BaseCalibrator(ABC):
    @abstractmethod
    def calibrate(self, raw_score: float) -> float:
        """Map a raw heuristic support score/margin to a calibrated
        probability in [0,1]."""

    @abstractmethod
    def fit(self, pairs: List[Tuple[float, int]]) -> None:
        """pairs: list of (raw_score, was_correct: 0/1) from historical
        walk-forward evaluation (§15)."""


class IdentityCalibrator(BaseCalibrator):
    """No-op: returns the input unchanged, clipped to [0,1]. This is what
    heuristic_confidence() in rule_based.py already produces, so using this
    calibrator is equivalent to declaring 'we have not calibrated yet' -
    which is the truthful state (see config.py CONFIG_VERSION)."""

    def calibrate(self, raw_score: float) -> float:
        return max(0.0, min(1.0, raw_score))

    def fit(self, pairs) -> None:
        raise NotImplementedError(
            "IdentityCalibrator does not fit. Implement PlattCalibrator or "
            "IsotonicCalibrator once you have (score, outcome) history from "
            "walk-forward backtests, then swap it in inference.RegimeEngine."
        )


DEFAULT_CALIBRATOR = IdentityCalibrator()
