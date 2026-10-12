"""
Risk score (Section 21).

This is NOT a black-box 0-100 number. It is a documented weighted sum of
normalized risk components, each rescaled onto a common [0,1] "danger"
scale before combining, so the weights are comparable. It exists purely as
a single sortable/thresholdable summary for the Opportunity Engine — the
Portfolio Manager and Risk Engine should always have access to the raw
underlying components (probability_of_loss, expected_downside, etc.) too,
per Section 22 ("do not collapse everything into one metric too early").

Normalization choices and why:
  - probability_of_loss, probability_of_stop_hit: already in [0,1].
  - expected_downside_pct, mae_p90_pct: scaled by a configurable reference
    magnitude (default 3% and 5% respectively) representing "large" moves
    for a small-cap/short-horizon Indian equity trade; capped at 1.0.
  - volatility_forecast_pct: scaled against a reference annualized vol
    (default 60%, roughly the top decile for liquid NSE mid/small caps).
  - probability_of_gap_down (delivery only): already in [0,1].
  - prediction_uncertainty: already in [0,1]; included so an uncertain
    prediction pushes the score toward "more dangerous", never "safer" —
    i.e. the model is conservative under its own doubt (Section 17-18).

Weights sum to 1.0 and are versioned; changing them requires bumping
SCORING_VERSION so historical risk_score values remain interpretable
against the formula that produced them (Section 54).
"""
from __future__ import annotations
from dataclasses import dataclass

SCORING_VERSION = "risk_scoring_v1"

WEIGHTS = {
    "probability_of_loss": 0.20,
    "probability_of_stop_hit": 0.15,
    "expected_downside": 0.15,
    "tail_mae_p90": 0.15,
    "volatility": 0.10,
    "gap_risk": 0.10,
    "uncertainty": 0.15,
}
assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9

REFERENCE_DOWNSIDE_PCT = 3.0
REFERENCE_MAE_P90_PCT = 5.0
REFERENCE_VOLATILITY_PCT = 60.0


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


@dataclass
class ScoreComponents:
    probability_of_loss: float
    probability_of_stop_hit: float
    expected_downside_pct: float  # negative or zero
    mae_p90_pct: float            # negative or zero
    volatility_forecast_pct: float
    probability_of_gap_down: float = 0.0
    prediction_uncertainty: float = 0.0


def compute_risk_score(c: ScoreComponents) -> float:
    normalized = {
        "probability_of_loss": _clip01(c.probability_of_loss),
        "probability_of_stop_hit": _clip01(c.probability_of_stop_hit),
        "expected_downside": _clip01(abs(c.expected_downside_pct) / REFERENCE_DOWNSIDE_PCT),
        "tail_mae_p90": _clip01(abs(c.mae_p90_pct) / REFERENCE_MAE_P90_PCT),
        "volatility": _clip01(c.volatility_forecast_pct / REFERENCE_VOLATILITY_PCT),
        "gap_risk": _clip01(c.probability_of_gap_down),
        "uncertainty": _clip01(c.prediction_uncertainty),
    }
    score01 = sum(WEIGHTS[k] * normalized[k] for k in WEIGHTS)
    return round(100.0 * _clip01(score01), 2)
