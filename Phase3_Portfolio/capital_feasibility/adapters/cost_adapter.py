"""
PART — TRADE CAPITAL REQUIREMENT (Cost Model bridge)

The Cost Model (`cost_model.engine.CostEngine`) is the sole source of
truth for transaction costs (spec §12/§29) — this module never computes
brokerage/STT/slippage itself. `CostEngine.price_trade()` already
supports buy-leg-only pricing when `TradeInput.exit_price` is left
unset (see cost_model/engine.py docstring), which is exactly the
"capital required to ENTER this position" question Capital Feasibility
needs to answer — a full round-trip price would double-count a sell
leg that hasn't happened yet.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from ..exceptions import CostModelIntegrationError
from ..models import LiquidityContext, TradeCapitalRequirement


def price_buy_leg(
    cost_engine,
    cost_models,  # the cost_model package's `models` module (Exchange/Segment/ProductType/...)
    symbol: str,
    exchange: str,
    trade_type: str,
    entry_price: Decimal,
    quantity: int,
    liquidity: Optional[LiquidityContext] = None,
    same_day_cnc_square_off: bool = False,
    available_capital: Optional[Decimal] = None,
) -> TradeCapitalRequirement:
    """Prices the BUY leg only (no exit_price) for `quantity` shares and
    returns the resulting TradeCapitalRequirement. Raises
    CostModelIntegrationError (never returns a silently-zero cost) if the
    Cost Model rejects the input or cannot price it — Capital Feasibility
    must fail closed on a costing failure, not assume ₹0 friction."""
    if quantity <= 0:
        return TradeCapitalRequirement(
            quantity=0,
            position_value=Decimal("0"),
            execution_cost=Decimal("0"),
            required_capital=Decimal("0"),
            cost_model_version=None,
        )

    try:
        exchange_enum = cost_models.Exchange(exchange)
    except ValueError as exc:
        raise CostModelIntegrationError(f"unknown exchange {exchange!r}") from exc

    try:
        trade_type_enum = cost_models.TradeType(trade_type)
    except ValueError as exc:
        raise CostModelIntegrationError(f"unknown trade_type {trade_type!r}") from exc

    product_type = (
        cost_models.ProductType.CNC
        if trade_type_enum == cost_models.TradeType.DELIVERY
        else cost_models.ProductType.MIS
    )

    liq = liquidity or LiquidityContext()
    liquidity_snapshot = cost_models.LiquiditySnapshot(
        avg_daily_traded_value=liq.average_daily_traded_value,
        avg_daily_volume=liq.average_daily_volume,
    )

    trade_input = cost_models.TradeInput(
        symbol=symbol,
        exchange=exchange_enum,
        segment=cost_models.Segment.EQUITY,
        product_type=product_type,
        trade_type=trade_type_enum,
        side=cost_models.Side.BUY,
        quantity=quantity,
        entry_price=entry_price,
        order_type=cost_models.OrderType.MARKET,
        liquidity=liquidity_snapshot,
        same_day_cnc_square_off=same_day_cnc_square_off,
        available_capital=available_capital,
        # shares_from_demat_holdings intentionally left None: this is a
        # BUY leg, DP charges are a delivery-SELL-specific concern and
        # do not apply here.
    )

    try:
        breakdown = cost_engine.price_trade(trade_input)
    except Exception as exc:  # noqa: BLE001 - re-raised as our own type, never swallowed
        raise CostModelIntegrationError(
            f"Cost Model could not price {quantity} x {symbol} @ {entry_price}: {exc}"
        ) from exc

    position_value = trade_input.turnover
    execution_cost = breakdown.total_cost  # buy-leg-only total_cost since exit_price is None
    required_capital = (position_value + execution_cost).quantize(Decimal("0.01"))

    return TradeCapitalRequirement(
        quantity=quantity,
        position_value=position_value,
        execution_cost=execution_cost,
        required_capital=required_capital,
        cost_model_version=breakdown.fee_schedule_version,
    )
