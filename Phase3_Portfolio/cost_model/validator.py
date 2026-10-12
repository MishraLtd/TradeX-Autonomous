"""
PART 18 — SAFETY AGAINST BAD DATA

Validates a TradeInput before any calculator touches it. Missing
exchange/segment/product, invalid quantity/price, or an exit price on
the wrong side of the entry for a stated direction all FAIL CLOSED here
rather than silently propagating as ₹0 costs or nonsense P&L downstream.
"""
from __future__ import annotations

from decimal import Decimal

from .models import TradeInput
from .exceptions import CostModelValidationError


def validate_trade_input(trade: TradeInput) -> None:
    if trade.quantity is None or trade.quantity <= 0:
        raise CostModelValidationError("quantity", f"quantity must be a positive integer, got {trade.quantity!r}")

    if trade.entry_price is None or trade.entry_price <= 0:
        raise CostModelValidationError("entry_price", f"entry_price must be positive, got {trade.entry_price!r}")

    if trade.exit_price is not None and trade.exit_price <= 0:
        raise CostModelValidationError("exit_price", f"exit_price must be positive, got {trade.exit_price!r}")

    if trade.exchange is None:
        raise CostModelValidationError("exchange", "exchange is required")

    if trade.segment is None:
        raise CostModelValidationError("segment", "segment is required")

    if trade.product_type is None:
        raise CostModelValidationError("product_type", "product_type is required")

    if trade.trade_type is None:
        raise CostModelValidationError("trade_type", "trade_type is required")

    turnover = trade.entry_price * trade.quantity
    if turnover <= 0:
        raise CostModelValidationError("turnover", f"computed turnover is non-positive: {turnover}")
    # sanity ceiling — catches fat-fingered inputs (e.g. price in paise
    # instead of rupees, or a quantity typo) rather than silently costing
    # an absurd trade.
    if turnover > Decimal("100000000"):  # ₹10 crore
        raise CostModelValidationError(
            "turnover", f"computed turnover ₹{turnover} exceeds sanity ceiling for this "
                        f"low-capital system — check inputs for a units/decimal error"
        )

    if trade.available_capital is not None and trade.available_capital < 0:
        raise CostModelValidationError("available_capital", "available_capital cannot be negative")

    liq = trade.liquidity
    if liq.best_bid is not None and liq.best_ask is not None:
        if liq.best_bid <= 0 or liq.best_ask <= 0:
            raise CostModelValidationError("liquidity", "best_bid/best_ask must be positive when supplied")
        if liq.best_ask < liq.best_bid:
            raise CostModelValidationError("liquidity", "best_ask cannot be less than best_bid")

    if liq.avg_daily_traded_value is not None and liq.avg_daily_traded_value < 0:
        raise CostModelValidationError("liquidity", "avg_daily_traded_value cannot be negative")
