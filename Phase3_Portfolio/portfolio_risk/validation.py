"""
validation.py
-------------
Validation and sanitation layer. Per spec section 18, the model must never
crash on incomplete/invalid data — it must validate, flag, degrade
gracefully, and produce the safest possible decision.

`validate_portfolio` returns a cleaned copy of the portfolio (invalid
positions removed, NaNs handled) plus a DataQualityReport describing what
was dropped or downgraded.
"""

from __future__ import annotations

import math
from copy import deepcopy
from typing import List, Tuple

from .models import PortfolioState, Position, DataQualityReport, DataQualityState


def _is_bad_number(x) -> bool:
    if x is None:
        return True
    try:
        return math.isnan(float(x)) or math.isinf(float(x))
    except (TypeError, ValueError):
        return True


def sanitize_return_history(history: List[float]) -> List[float]:
    """Drop NaN/inf/None observations while preserving order."""
    if not history:
        return []
    return [float(x) for x in history if not _is_bad_number(x)]


def validate_position(pos: Position, report: DataQualityReport) -> Tuple[Position, bool]:
    """
    Returns (cleaned_position, keep_flag). If keep_flag is False the
    position is unusable (e.g. zero/negative price) and must be excluded
    from all calculations, with the exclusion logged in the report.
    """
    pos = deepcopy(pos)

    if pos.quantity is None or _is_bad_number(pos.quantity) or pos.quantity == 0:
        report.downgrade(DataQualityState.DEGRADED, f"{pos.symbol}: invalid/zero quantity — excluded")
        return pos, False

    if pos.entry_price is None or _is_bad_number(pos.entry_price) or pos.entry_price <= 0:
        report.downgrade(DataQualityState.DEGRADED, f"{pos.symbol}: invalid entry price — excluded")
        return pos, False

    if pos.current_price is None or _is_bad_number(pos.current_price) or pos.current_price <= 0:
        report.downgrade(DataQualityState.DEGRADED, f"{pos.symbol}: invalid/stale current price — excluded")
        return pos, False

    if pos.stop_loss_price is not None and _is_bad_number(pos.stop_loss_price):
        report.downgrade(DataQualityState.LIMITED_DATA, f"{pos.symbol}: invalid stop-loss price — treated as absent")
        pos.stop_loss_price = None

    if pos.sector is None or (isinstance(pos.sector, str) and pos.sector.strip() == ""):
        report.downgrade(DataQualityState.LIMITED_DATA, f"{pos.symbol}: missing sector — grouped as 'UNKNOWN'")
        pos.sector = "UNKNOWN"

    cleaned_history = sanitize_return_history(pos.return_history)
    if len(cleaned_history) != len(pos.return_history):
        report.downgrade(
            DataQualityState.LIMITED_DATA,
            f"{pos.symbol}: dropped {len(pos.return_history) - len(cleaned_history)} invalid return observations",
        )
    pos.return_history = cleaned_history

    if pos.volatility is not None and (_is_bad_number(pos.volatility) or pos.volatility < 0):
        report.downgrade(DataQualityState.LIMITED_DATA, f"{pos.symbol}: invalid volatility — treated as unavailable")
        pos.volatility = None

    if pos.avg_traded_value is not None and (_is_bad_number(pos.avg_traded_value) or pos.avg_traded_value < 0):
        pos.avg_traded_value = None

    return pos, True


def validate_portfolio(portfolio: PortfolioState) -> Tuple[PortfolioState, DataQualityReport]:
    report = DataQualityReport()
    portfolio = deepcopy(portfolio)

    if portfolio.total_capital is None or _is_bad_number(portfolio.total_capital) or portfolio.total_capital < 0:
        report.downgrade(DataQualityState.UNAVAILABLE, "total_capital missing/invalid — cannot assess portfolio risk")
        portfolio.total_capital = 0.0

    if portfolio.available_cash is None or _is_bad_number(portfolio.available_cash):
        report.downgrade(DataQualityState.LIMITED_DATA, "available_cash missing — assumed 0")
        portfolio.available_cash = 0.0

    cleaned_positions: List[Position] = []
    seen_symbols = {}
    for pos in portfolio.positions:
        cleaned, keep = validate_position(pos, report)
        if not keep:
            continue
        key = (cleaned.symbol, cleaned.is_proposed)
        if key in seen_symbols:
            report.downgrade(
                DataQualityState.DEGRADED,
                f"{cleaned.symbol}: duplicate position rows detected — merged by summing quantity",
            )
            existing = seen_symbols[key]
            total_qty = existing.quantity + cleaned.quantity
            if total_qty != 0:
                existing.entry_price = (
                    existing.entry_price * existing.quantity + cleaned.entry_price * cleaned.quantity
                ) / total_qty
            existing.quantity = total_qty
            continue
        seen_symbols[key] = cleaned
        cleaned_positions.append(cleaned)

    portfolio.positions = cleaned_positions

    if portfolio.total_capital == 0 and not portfolio.positions:
        report.downgrade(DataQualityState.UNAVAILABLE, "empty portfolio and zero capital")

    return portfolio, report
