"""
Per Section 60 (BASELINE REQUIREMENT): never jump straight to an ML model.
This module implements, in order:

  Baseline 1 — NaiveFrequencyBaseline: unconditional historical loss /
               stop-hit frequency. No features at all.
  Baseline 2 — VolatilityMonteCarloBaseline: analytical/simulation estimate
               of P(stop before target) from a GBM with the forecast
               volatility. Uses one feature (volatility) plus trade geometry.
  Baseline 3 — CalibratedLogisticBaseline: logistic regression + isotonic/
               sigmoid calibration over the full causal feature set.
  QuantileMAEBaseline — gradient-boosted quantile regression for the MAE
               distribution (Target E).

A candidate model is only promoted to production if it beats the *previous*
tier out-of-sample on calibration + the trading-utility metrics in
Section 35 — see evaluate.py for the comparison harness.
"""
from __future__ import annotations
from dataclasses import dataclass
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.impute import SimpleImputer


# ---------------------------------------------------------------------------
# Baseline 1
# ---------------------------------------------------------------------------
@dataclass
class NaiveFrequencyBaseline:
    """P(event) = historical unconditional frequency in the training set.
    Deliberately ignores every feature — this is the floor every fancier
    model must beat."""

    p_loss_: float = None
    p_stop_hit_: float = None
    n_train_: int = 0

    def fit(self, loss: np.ndarray, stop_hit: np.ndarray) -> "NaiveFrequencyBaseline":
        self.p_loss_ = float(np.mean(loss))
        self.p_stop_hit_ = float(np.mean(stop_hit))
        self.n_train_ = len(loss)
        return self

    def predict(self, n: int) -> dict:
        return {
            "probability_of_loss": np.full(n, self.p_loss_),
            "probability_of_stop_hit": np.full(n, self.p_stop_hit_),
        }


# ---------------------------------------------------------------------------
# Baseline 2 — volatility-based Monte Carlo first-passage estimate
# ---------------------------------------------------------------------------
def simulate_stop_target_race(
    volatility_annual_pct: float,
    horizon_bars: int,
    stop_distance_pct: float,
    target_distance_pct: float,
    drift_annual_pct: float = 0.0,
    n_paths: int = 20_000,
    seed: int = 42,
) -> dict:
    """GBM Monte Carlo estimate of which barrier is hit first over
    `horizon_bars` trading days. This has no fitted parameters beyond the
    volatility forecast itself, so it is fully interpretable and requires
    zero historical trade samples — useful when `min_training_samples` is
    not yet met (Section 32/67: degrade gracefully rather than force an
    unsupported ML estimate)."""
    rng = np.random.default_rng(seed)
    dt = 1.0 / 252.0
    sigma = volatility_annual_pct / 100.0
    mu = drift_annual_pct / 100.0

    log_stop = np.log(1 - stop_distance_pct / 100.0)
    log_target = np.log(1 + target_distance_pct / 100.0)

    increments = rng.normal(
        loc=(mu - 0.5 * sigma**2) * dt,
        scale=sigma * np.sqrt(dt),
        size=(n_paths, horizon_bars),
    )
    log_paths = np.cumsum(increments, axis=1)

    stop_hit_bar = np.full(n_paths, -1)
    target_hit_bar = np.full(n_paths, -1)
    for b in range(horizon_bars):
        newly_stopped = (log_paths[:, b] <= log_stop) & (stop_hit_bar == -1)
        newly_targeted = (log_paths[:, b] >= log_target) & (target_hit_bar == -1)
        stop_hit_bar[newly_stopped] = b + 1
        target_hit_bar[newly_targeted] = b + 1

    stop_first = (stop_hit_bar != -1) & ((target_hit_bar == -1) | (stop_hit_bar <= target_hit_bar))
    target_first = (target_hit_bar != -1) & ((stop_hit_bar == -1) | (target_hit_bar < stop_hit_bar))
    neither = ~stop_first & ~target_first

    final_log_ret = log_paths[:, -1]
    final_ret_pct = (np.exp(final_log_ret) - 1) * 100.0
    mae_pct = (np.exp(np.minimum.accumulate(log_paths, axis=1).min(axis=1)) - 1) * 100.0
    # mae_pct is <= 0 (most negative excursion). To report "p90" as the
    # WORSE tail (matching the schema convention: p90 more severe than
    # p50), take the percentile of the *magnitude* and re-sign it.
    mae_magnitude = np.abs(mae_pct)

    return {
        "probability_stop_before_target": float(stop_first.mean()),
        "probability_target_before_stop": float(target_first.mean()),
        "probability_neither": float(neither.mean()),
        "expected_downside_pct": float(final_ret_pct[final_ret_pct < 0].mean()) if (final_ret_pct < 0).any() else 0.0,
        "mae_p50_pct": float(-np.percentile(mae_magnitude, 50)),
        "mae_p75_pct": float(-np.percentile(mae_magnitude, 75)),
        "mae_p90_pct": float(-np.percentile(mae_magnitude, 90)),
        "probability_of_loss": float((final_ret_pct < 0).mean()),
        "n_paths": n_paths,
    }


# ---------------------------------------------------------------------------
# Baseline 3 — calibrated logistic regression over causal features
# ---------------------------------------------------------------------------
def build_calibrated_logistic(calibration_method: str = "sigmoid") -> Pipeline:
    """Sigmoid (Platt) calibration is preferred over isotonic for small
    training sets (isotonic overfits with few hundred samples — exactly the
    regime this project starts in)."""
    base = LogisticRegression(max_iter=1000, class_weight="balanced")
    calibrated = CalibratedClassifierCV(base, method=calibration_method, cv=3)
    return Pipeline(
        steps=[
            ("impute", SimpleImputer(strategy="median")),
            ("scale", StandardScaler()),
            ("clf", calibrated),
        ]
    )


# ---------------------------------------------------------------------------
# Quantile regression baseline for the MAE distribution (Target E)
# ---------------------------------------------------------------------------
def build_quantile_mae_model(quantile: float, n_estimators: int = 150) -> Pipeline:
    reg = GradientBoostingRegressor(
        loss="quantile",
        alpha=quantile,
        n_estimators=n_estimators,
        max_depth=3,
        learning_rate=0.05,
        subsample=0.8,
        random_state=42,
    )
    return Pipeline(
        steps=[
            ("impute", SimpleImputer(strategy="median")),
            ("reg", reg),
        ]
    )


MAE_QUANTILES = (0.25, 0.50, 0.75, 0.90)
