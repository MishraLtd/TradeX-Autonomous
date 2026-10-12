"""
Feature / data quality control — Section 7.

Produces a FeatureValidationReport rather than silently cleaning data.
Downstream code decides whether to drop, flag, or reject based on it.
"""

from __future__ import annotations
from dataclasses import dataclass, field
import numpy as np
import pandas as pd


@dataclass
class FeatureValidationReport:
    n_rows: int
    n_cols: int
    missing_pct: dict = field(default_factory=dict)
    zero_or_negative_price_rows: int = 0
    duplicate_timestamps: int = 0
    stale_value_cols: list = field(default_factory=list)     # unchanged for N bars
    abnormal_volume_rows: int = 0
    infinite_value_cols: list = field(default_factory=list)
    highly_correlated_pairs: list = field(default_factory=list)  # [(colA, colB, corr)]
    passed: bool = True
    issues: list = field(default_factory=list)


def validate_ohlcv(df: pd.DataFrame) -> FeatureValidationReport:
    report = FeatureValidationReport(n_rows=len(df), n_cols=df.shape[1])

    dup = df.index.duplicated().sum()
    report.duplicate_timestamps = int(dup)
    if dup:
        report.issues.append(f"{dup} duplicate timestamps")

    bad_px = ((df[["open", "high", "low", "close"]] <= 0).any(axis=1)).sum()
    report.zero_or_negative_price_rows = int(bad_px)
    if bad_px:
        report.issues.append(f"{bad_px} rows with zero/negative OHLC")

    if "volume" in df:
        vol = df["volume"]
        med = vol.rolling(20, min_periods=5).median()
        abnormal = (vol > med * 20).sum()  # >20x rolling median = suspect
        report.abnormal_volume_rows = int(abnormal)
        if abnormal:
            report.issues.append(f"{abnormal} rows with abnormal volume spikes (>20x rolling median)")

    for col in ("close",):
        if col in df:
            unchanged_run = (df[col].diff() == 0).astype(int)
            stale = unchanged_run.groupby((unchanged_run != unchanged_run.shift()).cumsum()).cumsum()
            if (stale >= 5).any():
                report.stale_value_cols.append(col)
                report.issues.append(f"'{col}' unchanged for >=5 consecutive bars somewhere in series")

    report.passed = len(report.issues) == 0
    return report


def validate_feature_matrix(X: pd.DataFrame, corr_threshold: float = 0.97) -> FeatureValidationReport:
    report = FeatureValidationReport(n_rows=len(X), n_cols=X.shape[1])

    miss = X.isna().mean().to_dict()
    report.missing_pct = {k: round(v, 4) for k, v in miss.items() if v > 0}
    high_missing = [k for k, v in miss.items() if v > 0.3]
    if high_missing:
        report.issues.append(f"columns with >30% missing: {high_missing}")

    inf_cols = [c for c in X.columns if np.isinf(X[c].to_numpy(dtype="float64", na_value=0)).any()]
    report.infinite_value_cols = inf_cols
    if inf_cols:
        report.issues.append(f"infinite values in: {inf_cols}")

    numeric = X.select_dtypes(include=[np.number])
    if numeric.shape[1] > 1:
        corr = numeric.corr().abs()
        pairs = []
        cols = corr.columns
        for i in range(len(cols)):
            for j in range(i + 1, len(cols)):
                c = corr.iloc[i, j]
                if pd.notna(c) and c >= corr_threshold:
                    pairs.append((cols[i], cols[j], round(float(c), 4)))
        report.highly_correlated_pairs = pairs
        if pairs:
            report.issues.append(f"{len(pairs)} highly correlated (>={corr_threshold}) feature pairs — consider dropping redundant ones")

    report.passed = len(report.issues) == 0
    return report
