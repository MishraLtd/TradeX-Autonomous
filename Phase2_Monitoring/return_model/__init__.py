"""
TradeX Return Prediction Engine (Phase 2)
==========================================

Sits downstream of FILTER2.0 (universe filtering) and upstream of the
Phase 1 Cost Model. Produces calibrated, uncertainty-aware return
distributions per (symbol, timestamp, horizon) — never trade decisions.

See README.md for full design doc, methodology, and self-critique.
"""

__version__ = "0.1.0-experimental"
