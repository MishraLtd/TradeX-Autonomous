from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional
import numpy as np

from . import baselines, features, scoring
from .config import RiskConfig, DEFAULT_CONFIG
from .schema import RiskPrediction, DataQualityStatus, RiskModelStatus, TradeType
from .ood import OODDetector
from .exceptions import DataQualityError

MODEL_VERSION = "risk_model_v0.1.0-baseline"


def predict_risk(
    symbol: str,
    trade_type: str,
    entry_price: float,
    stop_price: float,
    target_price: float,
    horizon_bars: int,
    feature_row: dict,
    volatility_forecast_pct: Optional[float] = None,
    ood_detector: Optional[OODDetector] = None,
    config: RiskConfig = DEFAULT_CONFIG,
    training_cutoff: Optional[datetime] = None,
) -> RiskPrediction:
    """
    Phase-1 inference path. `feature_row` is a dict of the causal features
    computed by `features.build_feature_frame` for the bar immediately at
    or before entry (the caller is responsible for that alignment — this
    function does not itself look at any dataframe, precisely so it cannot
    accidentally reach into future rows).

    Uses the volatility-based Monte Carlo baseline (Baseline 2) as the
    default estimator, since it requires no historical trade-outcome
    samples — appropriate while `min_training_samples_*` has not yet been
    met by a fitted ML baseline. Swap in a fitted CalibratedLogistic /
    quantile-GBM model's outputs here once Phase 2 validation clears it.
    """
    required = ["vol_20d", "atr_14_pct"]
    missing = [k for k in required if k not in feature_row or feature_row[k] is None or (
        isinstance(feature_row[k], float) and np.isnan(feature_row[k])
    )]
    if missing:
        raise DataQualityError(f"Missing required features for risk prediction: {missing}")

    if volatility_forecast_pct is None:
        volatility_forecast_pct = float(feature_row["vol_20d"])

    stop_distance_pct = (entry_price - stop_price) / entry_price * 100.0
    target_distance_pct = (target_price - entry_price) / entry_price * 100.0
    if stop_distance_pct <= 0 or target_distance_pct <= 0:
        raise DataQualityError("stop_price must be below entry, target_price above entry")

    sim = baselines.simulate_stop_target_race(
        volatility_annual_pct=volatility_forecast_pct,
        horizon_bars=horizon_bars,
        stop_distance_pct=stop_distance_pct,
        target_distance_pct=target_distance_pct,
    )

    large_loss_probs = {}
    # Approximate P(loss > threshold) by re-running the barrier simulation
    # with the threshold as a one-sided barrier — cheap given the same
    # analytical model already fitted for the stop/target race.
    for thr in config.large_loss_thresholds_pct:
        thr_sim = baselines.simulate_stop_target_race(
            volatility_annual_pct=volatility_forecast_pct,
            horizon_bars=horizon_bars,
            stop_distance_pct=thr,
            target_distance_pct=9999,  # effectively disable the target barrier
            n_paths=4000,
        )
        large_loss_probs[f"{thr}%"] = round(thr_sim["probability_stop_before_target"], 4)

    # --- OOD / uncertainty ---
    ood_score = 0.0
    is_ood = False
    if ood_detector is not None:
        x = np.array([feature_row.get(c, np.nan) for c in features.FEATURE_COLUMNS])
        ood_score = ood_detector.normalized_score(x)
        is_ood = ood_detector.is_ood(x)

    # Uncertainty is highest when: OOD, or when we're leaning entirely on
    # the analytical baseline rather than a fitted model with validated
    # calibration. Phase 1 has no fitted model yet, so uncertainty carries
    # a fixed floor reflecting "baseline-only" status.
    baseline_only_floor = 0.35
    uncertainty = min(1.0, baseline_only_floor + 0.5 * min(ood_score, 1.0))
    confidence = 1.0 - uncertainty

    status = RiskModelStatus.OK
    if is_ood:
        status = RiskModelStatus.OUT_OF_DISTRIBUTION
    elif uncertainty > config.default_max_model_uncertainty:
        status = RiskModelStatus.UNCERTAIN

    score = scoring.compute_risk_score(
        scoring.ScoreComponents(
            probability_of_loss=sim["probability_of_loss"],
            probability_of_stop_hit=sim["probability_stop_before_target"],
            expected_downside_pct=sim["expected_downside_pct"],
            mae_p90_pct=sim["mae_p90_pct"],
            volatility_forecast_pct=volatility_forecast_pct,
            probability_of_gap_down=0.0 if trade_type == "INTRADAY" else 0.15,
            prediction_uncertainty=uncertainty,
        )
    )

    return RiskPrediction(
        symbol=symbol,
        trade_type=TradeType(trade_type),
        timestamp=datetime.now(timezone.utc),
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        holding_horizon=f"{horizon_bars}D" if trade_type == "DELIVERY" else "EOD",
        probability_of_loss=sim["probability_of_loss"],
        probability_of_stop_hit=sim["probability_stop_before_target"],
        probability_of_target_before_stop=sim["probability_target_before_stop"],
        probability_of_neither=sim["probability_neither"],
        probability_of_large_loss=large_loss_probs,
        expected_downside_pct=sim["expected_downside_pct"],
        median_downside_pct=sim["mae_p50_pct"],
        expected_mae_pct=sim["mae_p50_pct"],
        mae_p25_pct=sim["mae_p50_pct"],  # Phase 1: MC baseline reports 50/75/90 only
        mae_p50_pct=sim["mae_p50_pct"],
        mae_p75_pct=sim["mae_p75_pct"],
        mae_p90_pct=sim["mae_p90_pct"],
        volatility_forecast_pct=volatility_forecast_pct,
        tail_estimate_reliable=not is_ood,
        risk_score=score,
        prediction_confidence=round(confidence, 3),
        prediction_uncertainty=round(uncertainty, 3),
        similar_sample_count=0,  # Phase 1 baseline uses no historical trade samples
        ood_score=round(float(ood_score) if np.isfinite(ood_score) else 999.0, 3),
        is_out_of_distribution=is_ood,
        model_version=MODEL_VERSION,
        feature_version=features.FEATURE_VERSION,
        training_cutoff=training_cutoff or datetime.now(timezone.utc),
        config_version="risk_config_v1",
        data_quality_status=DataQualityStatus.VALID,
        risk_model_status=status,
    )
