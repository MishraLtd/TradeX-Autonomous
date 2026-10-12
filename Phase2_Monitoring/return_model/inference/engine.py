"""
Live Inference Engine — Sections 37, 38.

Assembles a single symbol/timestamp/horizon prediction from the fitted
model bundle. Applies rejection conditions (Section 29/46) BEFORE
returning anything to the caller — a rejected prediction never reaches
the Cost Model with fabricated numbers.
"""

from __future__ import annotations
from datetime import datetime, timezone
import numpy as np
import pandas as pd

from ..schemas import RawPrediction, QuantileEstimate, ThresholdProbabilities, PredictionStatus, FeatureContribution
from ..config import EngineConfig
from .ood import OODDetector


class ModelBundle:
    """Everything needed to go from a feature row to a RawPrediction:
    point-return model, probability-positive model, threshold-probability
    models, quantile model, conformal interval wrapper, OOD detector.
    Assembled once per (model_id, model_version, horizon) at training time
    and reused at inference time."""

    def __init__(
        self,
        model_id: str,
        model_version: str,
        data_version: str,
        point_model,
        prob_positive_model,
        threshold_models: dict[float, object],   # {threshold_pct: fitted classifier}
        quantile_model,                           # QuantileGBM
        conformal,                                 # ConformalIntervals (fit+calibrated)
        ood_detector: OODDetector,
        mfe_model=None,
        mae_model=None,
    ):
        self.model_id = model_id
        self.model_version = model_version
        self.data_version = data_version
        self.point_model = point_model
        self.prob_positive_model = prob_positive_model
        self.threshold_models = threshold_models
        self.quantile_model = quantile_model
        self.conformal = conformal
        self.ood_detector = ood_detector
        self.mfe_model = mfe_model
        self.mae_model = mae_model


def predict_one(
    bundle: ModelBundle,
    symbol: str,
    horizon: str,
    feature_row: pd.DataFrame,  # single-row DataFrame
    config: EngineConfig,
    n_train_observations: int,
    conformal_alpha: float = 0.20,
) -> RawPrediction:
    now = datetime.now(timezone.utc)

    # --- Rejection conditions (Section 29/46), checked BEFORE any model call
    if feature_row.isna().all(axis=1).iloc[0]:
        return _unavailable(symbol, horizon, bundle, now, "all features missing/NaN for this row")
    if n_train_observations < config.min_train_observations:
        return _unavailable(symbol, horizon, bundle, now,
                             f"insufficient training history ({n_train_observations} < {config.min_train_observations})")

    is_ood = bool(bundle.ood_detector.is_out_of_distribution(feature_row)[0])

    expected_gross = float(bundle.point_model.predict(feature_row)[0])
    prob_positive = float(np.clip(bundle.prob_positive_model.predict_proba(feature_row.fillna(0.0))[0][1], 0, 1)) \
        if hasattr(bundle.prob_positive_model, "predict_proba") else 0.5

    threshold_probs = {}
    for thr, clf in bundle.threshold_models.items():
        p = float(np.clip(clf.predict_proba(feature_row.fillna(0.0))[0][1], 0, 1)) if hasattr(clf, "predict_proba") else np.nan
        threshold_probs[thr] = p

    q = bundle.quantile_model.predict(feature_row)
    quantiles = QuantileEstimate(
        q10=float(q["q10"].iloc[0]) if "q10" in q else None,
        q25=float(q["q25"].iloc[0]) if "q25" in q else None,
        q50=float(q["q50"].iloc[0]) if "q50" in q else None,
        q75=float(q["q75"].iloc[0]) if "q75" in q else None,
        q90=float(q["q90"].iloc[0]) if "q90" in q else None,
    )

    lo, hi = bundle.conformal.predict_interval(feature_row, alpha=conformal_alpha)
    lo, hi = float(lo[0]), float(hi[0])
    confidence = float(bundle.conformal.confidence_score(feature_row, alpha=conformal_alpha)[0])

    mfe = float(bundle.mfe_model.predict(feature_row)[0]) if bundle.mfe_model is not None else float("nan")
    mae = float(bundle.mae_model.predict(feature_row)[0]) if bundle.mae_model is not None else float("nan")

    status = PredictionStatus.VALID
    reason = None
    normalized_width = (hi - lo) / 2.0 / max(abs(expected_gross), 1e-6) if expected_gross != 0 else 1.0
    if is_ood or confidence < (1 - config.max_uncertainty_for_trust):
        status = PredictionStatus.LOW_TRUST
        reason = "out_of_distribution" if is_ood else "excessive_uncertainty"

    return RawPrediction(
        symbol=symbol, prediction_timestamp=now, horizon=horizon,
        model_id=bundle.model_id, model_version=bundle.model_version,
        data_version=bundle.data_version,
        expected_gross_return_pct=expected_gross,
        probability_positive=prob_positive,
        threshold_probs=ThresholdProbabilities(probs=threshold_probs),
        expected_mfe_pct=mfe, expected_mae_pct=mae,
        quantiles_pct=quantiles,
        prediction_interval_pct=(lo, hi),
        confidence=confidence,
        top_contributors=[],  # filled separately via explainability module if requested
        is_out_of_distribution=is_ood,
        status=status,
        rejection_reason=reason,
    )


def _unavailable(symbol, horizon, bundle, now, reason) -> RawPrediction:
    return RawPrediction(
        symbol=symbol, prediction_timestamp=now, horizon=horizon,
        model_id=bundle.model_id, model_version=bundle.model_version,
        data_version=bundle.data_version,
        expected_gross_return_pct=0.0, probability_positive=0.5,
        threshold_probs=ThresholdProbabilities(probs={}),
        expected_mfe_pct=float("nan"), expected_mae_pct=float("nan"),
        quantiles_pct=QuantileEstimate(),
        prediction_interval_pct=(float("nan"), float("nan")),
        confidence=0.0, top_contributors=[],
        is_out_of_distribution=False,
        status=PredictionStatus.UNAVAILABLE,
        rejection_reason=reason,
    )
