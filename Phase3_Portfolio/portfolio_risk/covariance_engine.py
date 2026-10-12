"""
covariance_engine.py
----------------------
Covariance estimation and portfolio volatility (spec section 5.D).

sigma_p = sqrt(w^T * Sigma * w)

Handles:
  - insufficient history -> falls back to a diagonal covariance built from
    each position's supplied `volatility` field (if available), which is
    strictly less informative but numerically stable and non-fabricated.
  - singular / near-singular covariance -> shrinkage toward the diagonal
    (Ledoit-Wolf-style, simplified) to guarantee positive semi-definiteness.
  - small portfolios (1-2 names) -> degenerate but well-defined result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

from .models import Position, DataQualityReport, DataQualityState


@dataclass
class CovarianceResult:
    symbols: List[str] = field(default_factory=list)
    covariance_matrix: Optional[np.ndarray] = None  # daily, fractional-return units
    portfolio_volatility_daily: float = 0.0
    portfolio_volatility_annualized: float = 0.0
    method: str = "UNAVAILABLE"  # SAMPLE | SHRUNK | DIAGONAL_FALLBACK | UNAVAILABLE


TRADING_DAYS_PER_YEAR = 252


def _shrink_to_diagonal(cov: np.ndarray, shrinkage: float) -> np.ndarray:
    diag = np.diag(np.diag(cov))
    return (1 - shrinkage) * cov + shrinkage * diag


def _nearest_psd(cov: np.ndarray) -> np.ndarray:
    """Clip negative eigenvalues to a small positive epsilon to guarantee PSD."""
    sym = (cov + cov.T) / 2
    eigvals, eigvecs = np.linalg.eigh(sym)
    eigvals_clipped = np.clip(eigvals, 1e-10, None)
    return eigvecs @ np.diag(eigvals_clipped) @ eigvecs.T


def estimate_covariance(
    positions: List[Position],
    min_history_days: int,
    shrinkage: float,
    report: DataQualityReport,
) -> CovarianceResult:
    open_positions = [p for p in positions if not p.is_proposed]
    if not open_positions:
        return CovarianceResult()

    symbols = [p.symbol for p in open_positions]
    usable = {p.symbol: p for p in open_positions if len(p.return_history) >= min_history_days}

    if len(usable) >= 2:
        min_len = min(len(p.return_history) for p in usable.values())
        data = np.vstack([np.array(p.return_history[-min_len:], dtype=float) for p in usable.values()])
        used_symbols = list(usable.keys())

        cov = np.cov(data, ddof=1)
        if cov.ndim == 0:
            cov = np.array([[float(cov)]])

        # Detect near-singularity
        try:
            cond = np.linalg.cond(cov)
        except np.linalg.LinAlgError:
            cond = np.inf

        method = "SAMPLE"
        if not np.isfinite(cond) or cond > 1e8:
            report.downgrade(
                DataQualityState.DEGRADED,
                "covariance: near-singular sample covariance matrix — applying shrinkage",
            )
            cov = _shrink_to_diagonal(cov, max(shrinkage, 0.25))
            cov = _nearest_psd(cov)
            method = "SHRUNK"
        elif shrinkage > 0:
            cov = _shrink_to_diagonal(cov, shrinkage)
            method = "SHRUNK"

        if min_len < 60:
            report.downgrade(
                DataQualityState.LIMITED_DATA,
                f"covariance: only {min_len} observations used — estimate is noisy",
            )

        # Build a full symbol-aligned matrix; unusable symbols get zero
        # covariance with everything (their risk is simply not modeled),
        # which is flagged rather than silently assumed to be zero risk.
        missing = [s for s in symbols if s not in used_symbols]
        if missing:
            report.downgrade(
                DataQualityState.LIMITED_DATA,
                f"covariance: {missing} lack sufficient history and are excluded from the covariance matrix "
                f"(their volatility is NOT modeled as zero-risk in the score — see volatility fallback)",
            )

        full_cov = np.zeros((len(symbols), len(symbols)))
        idx = {s: i for i, s in enumerate(symbols)}
        for i, si in enumerate(used_symbols):
            for j, sj in enumerate(used_symbols):
                full_cov[idx[si], idx[sj]] = cov[i, j]

        # For missing symbols, fall back to their own supplied volatility on
        # the diagonal (zero correlation assumed with the rest, explicitly
        # flagged as a limitation).
        for s in missing:
            pos = next(p for p in open_positions if p.symbol == s)
            if pos.volatility:
                daily_var = (pos.volatility ** 2) / TRADING_DAYS_PER_YEAR
                full_cov[idx[s], idx[s]] = daily_var
            else:
                full_cov[idx[s], idx[s]] = 0.0
                report.downgrade(
                    DataQualityState.DEGRADED,
                    f"covariance: {s} has no return history AND no volatility estimate — "
                    f"treated as zero variance, which UNDERSTATES risk",
                )

        return CovarianceResult(symbols=symbols, covariance_matrix=full_cov,
                                 method=method)

    # Fallback: diagonal covariance built purely from supplied volatilities.
    report.downgrade(
        DataQualityState.INSUFFICIENT_HISTORY,
        "covariance: fewer than 2 positions have sufficient return history — "
        "falling back to a diagonal covariance from supplied volatility (correlations assumed 0, "
        "which UNDERSTATES true portfolio risk if positions are actually correlated)",
    )
    diag_vars = []
    for p in open_positions:
        if p.volatility:
            diag_vars.append((p.volatility ** 2) / TRADING_DAYS_PER_YEAR)
        else:
            diag_vars.append(0.0)
    cov = np.diag(diag_vars)
    method = "DIAGONAL_FALLBACK" if any(diag_vars) else "UNAVAILABLE"
    return CovarianceResult(symbols=symbols, covariance_matrix=cov, method=method)


def portfolio_volatility(
    cov_result: CovarianceResult,
    positions: List[Position],
    portfolio_equity: float,
) -> CovarianceResult:
    if cov_result.covariance_matrix is None or portfolio_equity <= 0 or not cov_result.symbols:
        return cov_result

    open_positions = {p.symbol: p for p in positions if not p.is_proposed}
    weights = np.array(
        [max(open_positions[s].market_value, 0.0) / portfolio_equity if s in open_positions else 0.0
         for s in cov_result.symbols]
    )

    sigma = cov_result.covariance_matrix
    variance = float(weights @ sigma @ weights)
    variance = max(variance, 0.0)
    daily_vol = float(np.sqrt(variance))
    annualized_vol = daily_vol * np.sqrt(TRADING_DAYS_PER_YEAR)

    cov_result.portfolio_volatility_daily = daily_vol
    cov_result.portfolio_volatility_annualized = annualized_vol
    return cov_result


def volatility_score(annualized_vol: float, warn: float, cap: float) -> float:
    if annualized_vol <= warn * 0.5:
        return (annualized_vol / (warn * 0.5)) * 25 if warn > 0 else 0.0
    elif annualized_vol <= cap:
        span = max(cap - warn * 0.5, 1e-9)
        return 25 + (annualized_vol - warn * 0.5) / span * 50
    else:
        span = max(cap, 1e-9)
        return max(0.0, min(100.0, 75 + min((annualized_vol - cap) / span, 1.0) * 25))
