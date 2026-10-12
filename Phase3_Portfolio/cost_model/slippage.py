"""
PART 6 — SLIPPAGE MODEL

Deliberately kept separate from spread cost (this module) and market
impact (market_impact.py) so the three never double-count the same
friction — see PART 25 self-critique question #2 ("where could this
accidentally double-count costs?"):

    spread_cost        -> priced directly from the live quote when
                           available (see `estimate_spread_cost`)
    slippage_cost       -> a residual "everything else" friction
                           (latency, price-time priority, order-type
                           urgency) that is NOT derived from the quote
    market_impact_cost  -> priced in market_impact.py from order size
                           relative to average daily traded value

If a live quote is NOT available, spread_cost is reported as ₹0 but
slippage_cost is raised to a higher fixed assumption that is meant to
STAND IN for the missing spread information — so the "no data" case is
never cheaper than the "we have data" case.

Tiering (PART 6):
    LEVEL 1  fixed bps assumption — used when no live quote exists
    LEVEL 2  spread-derived — used when a live quote exists (handled by
             estimate_spread_cost, called by the engine directly)
    LEVEL 4  historically calibrated — caller supplies an empirically
             measured slippage-in-bps figure; floored at the LEVEL-1
             minimum so a mis-calibrated "0 bps" history can never zero
             out slippage entirely.
(LEVEL 3, the liquidity/participation adjustment, lives in
market_impact.py since that is specifically an order-size-vs-liquidity
effect, not a bid/ask effect.)
"""
from __future__ import annotations

from decimal import Decimal

from .models import LiquiditySnapshot, OrderType, TradeType

# Conservative, configurable ASSUMPTIONS (not statutory facts — must be
# periodically reviewed against real fills via reconciliation.py).
NO_QUOTE_MARKET_ORDER_BPS = Decimal("15")     # 0.15%, no live quote, market order
NO_QUOTE_LIMIT_ORDER_BPS = Decimal("5")       # 0.05%, no live quote, limit order (on fill)
WITH_QUOTE_RESIDUAL_BPS = Decimal("2")        # 0.02% latency/priority residual when spread IS priced
INTRADAY_URGENCY_EXTRA_BPS = Decimal("5")     # same-day square-off urgency add-on
ABSOLUTE_FLOOR_BPS = Decimal("2")             # slippage is never assumed to be below this


def estimate_spread_cost(turnover: Decimal, liquidity: LiquiditySnapshot) -> tuple:
    """Half the quoted bid-ask spread, applied to turnover — a
    marketable order should on average cross roughly half the spread
    from mid. Returns (cost, explanation). ₹0 if no live quote."""
    if liquidity.best_bid is None or liquidity.best_ask is None:
        return Decimal("0.00"), ("No live quote available — spread cost not separately priced "
                                  "(folded into the higher no-quote slippage assumption instead).")
    mid = (liquidity.best_bid + liquidity.best_ask) / 2
    if mid <= 0:
        return Decimal("0.00"), "Non-positive mid price — spread cost skipped."
    spread_bps = (liquidity.best_ask - liquidity.best_bid) / mid * Decimal("10000")
    half_spread_bps = spread_bps / 2
    cost = (turnover * half_spread_bps / Decimal("10000")).quantize(Decimal("0.01"))
    return cost, f"half of quoted spread ({spread_bps:.2f}bps) = {half_spread_bps:.2f}bps"


def estimate_slippage(
    turnover: Decimal,
    order_type: OrderType,
    trade_type: TradeType,
    liquidity: LiquiditySnapshot,
    calibrated_bps: Decimal = None,
) -> tuple:
    """Returns (slippage_cost_rupees, tier_used, explanation)."""
    if turnover == 0:
        return Decimal("0.00"), "LEVEL_0", "Zero turnover — no slippage to estimate."

    have_quote = liquidity.best_bid is not None and liquidity.best_ask is not None

    if calibrated_bps is not None:
        floor = WITH_QUOTE_RESIDUAL_BPS if have_quote else NO_QUOTE_MARKET_ORDER_BPS
        bps = max(calibrated_bps, floor)
        cost = (turnover * bps / Decimal("10000")).quantize(Decimal("0.01"))
        return cost, "LEVEL_4_CALIBRATED", (
            f"Calibrated slippage {calibrated_bps}bps, floored at {floor}bps -> used {bps}bps."
        )

    if have_quote:
        bps = WITH_QUOTE_RESIDUAL_BPS
        tier = "LEVEL_2_RESIDUAL"
        note = f"live quote present; residual (non-spread) friction assumption {bps}bps"
    else:
        bps = NO_QUOTE_MARKET_ORDER_BPS if order_type == OrderType.MARKET else NO_QUOTE_LIMIT_ORDER_BPS
        tier = "LEVEL_1_FIXED"
        note = f"no live quote; fixed conservative assumption {bps}bps for {order_type.value} order"

    if trade_type == TradeType.INTRADAY:
        bps += INTRADAY_URGENCY_EXTRA_BPS
        note += f"; +{INTRADAY_URGENCY_EXTRA_BPS}bps same-day square-off urgency"

    if bps < ABSOLUTE_FLOOR_BPS:
        note += f"; raised from {bps}bps to absolute floor {ABSOLUTE_FLOOR_BPS}bps"
        bps = ABSOLUTE_FLOOR_BPS

    cost = (turnover * bps / Decimal("10000")).quantize(Decimal("0.01"))
    return cost, tier, note
