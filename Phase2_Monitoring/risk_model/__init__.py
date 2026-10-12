"""TradeX Risk Prediction Model — Phase 1 (baseline layer).

See /docs/ARCHITECTURE.md (delivered alongside this package) for the full
design. This package intentionally ships ONLY the baseline + infrastructure
layer first (naive baseline -> volatility baseline -> calibrated logistic
regression + quantile GBM for MAE), per the project's own requirement
(Section 60): never jump straight to a complex ML model.
"""

__version__ = "0.1.0"
