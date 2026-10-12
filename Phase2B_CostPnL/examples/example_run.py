"""
Example usage of the TradeX Cost Model.

1. PART 2 — capital sweep: shows how cost burden changes across
   ₹500 .. ₹1,00,000 for a fixed small predicted move.
2. PART 25 — a full TRADE ECONOMIC REPORT for a single trade.
3. PART 9 — a break-even table.
4. PART 14 — scenario analysis.
"""
import sys
import os
from decimal import Decimal
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from cost_model.models import (
    TradeInput, Exchange, Segment, ProductType, TradeType, Side, OrderType,
    LiquiditySnapshot,
)
from cost_model.engine import CostEngine
from cost_model.breakeven import break_even_table
from cost_model.scenarios import run_scenarios


def make_trade(capital: Decimal, price: Decimal, move_pct: Decimal, trade_type=TradeType.DELIVERY):
    qty = int(capital / price)
    if qty < 1:
        qty = 1
    exit_price = (price * (1 + move_pct / 100)).quantize(Decimal("0.01"))
    return TradeInput(
        symbol="DEMO",
        exchange=Exchange.NSE,
        segment=Segment.EQUITY,
        product_type=ProductType.CNC if trade_type == TradeType.DELIVERY else ProductType.MIS,
        trade_type=trade_type,
        side=Side.BUY,
        quantity=qty,
        entry_price=price,
        exit_price=exit_price,
        entry_timestamp=datetime(2026, 9, 10, 9, 30),
        available_capital=capital,
        shares_from_demat_holdings=(trade_type == TradeType.DELIVERY),
    )


def capital_sweep():
    engine = CostEngine()
    capitals = [500, 750, 1000, 1500, 2000, 5000, 10000, 25000, 50000, 100000]
    price = Decimal("250")
    move_pct = Decimal("1.5")  # a plausible predicted swing move

    print("\n=== PART 2 — Cost burden across capital levels ===")
    print(f"{'Capital':>10} | {'Qty':>4} | {'Cost/Capital %':>15} | {'Net Return %':>12} | Viability")
    for cap in capitals:
        trade = make_trade(Decimal(cap), price, move_pct)
        bd = engine.price_trade(trade)
        cost_pct = bd.cost_as_pct_of_capital if bd.cost_as_pct_of_capital is not None else Decimal("0")
        print(f"{'₹'+str(cap):>10} | {trade.quantity:>4} | {cost_pct:>14.2f}% | "
              f"{bd.net_return_pct:>11.2f}% | {bd.economic_viability.value}")


def full_trade_report():
    engine = CostEngine()
    liquidity = LiquiditySnapshot(
        best_bid=Decimal("249.90"), best_ask=Decimal("250.10"),
        avg_daily_traded_value=Decimal("50000000"), recent_volatility_pct=Decimal("2.0"),
    )
    trade = TradeInput(
        symbol="DEMO", exchange=Exchange.NSE, segment=Segment.EQUITY,
        product_type=ProductType.CNC, trade_type=TradeType.DELIVERY,
        side=Side.BUY, quantity=4, entry_price=Decimal("250.00"),
        exit_price=Decimal("258.00"), order_type=OrderType.LIMIT,
        entry_timestamp=datetime(2026, 9, 10, 9, 30),
        available_capital=Decimal("1000"),
        shares_from_demat_holdings=True,
    )
    trade.liquidity = liquidity
    bd = engine.price_trade(trade)

    print("\n=== PART 25 — TRADE ECONOMIC REPORT ===")
    print(f"Symbol: {trade.symbol}")
    print(f"Trade Type: {trade.trade_type.value}")
    print(f"Entry: ₹{trade.entry_price}   Expected Exit: ₹{trade.exit_price}")
    print(f"Quantity: {trade.quantity}   Capital Used: ₹{trade.turnover}")
    print(f"\nExpected Gross P&L: ₹{bd.gross_pnl}")
    print(f"\nBrokerage: ₹{bd.brokerage}")
    print(f"STT: ₹{bd.stt}")
    print(f"Exchange Charges: ₹{bd.exchange_charges}")
    print(f"IPFT Charges: ₹{bd.ipft_charges}")
    print(f"SEBI Charges: ₹{bd.sebi_charges}")
    print(f"GST: ₹{bd.gst}")
    print(f"Stamp Duty: ₹{bd.stamp_duty}")
    print(f"DP Charges: ₹{bd.dp_charges}")
    print(f"\nExpected Slippage: ₹{bd.slippage_cost}")
    print(f"Expected Spread Cost: ₹{bd.spread_cost}")
    print(f"Expected Market Impact: ₹{bd.market_impact_cost}")
    print(f"\nTOTAL EXPECTED COST: ₹{bd.total_cost}")
    print(f"\nEXPECTED NET P&L: ₹{bd.net_pnl}")
    print(f"EXPECTED NET RETURN: {bd.net_return_pct}%")
    print(f"\nBREAK-EVEN MOVE: {bd.break_even_move_pct}%  (price ₹{bd.break_even_price})")
    print(f"\nCOST / CAPITAL: {bd.cost_as_pct_of_capital}%")
    print(f"COST / GROSS PROFIT: {bd.cost_as_pct_of_gross_profit}%")

    scenarios = run_scenarios(engine, trade)
    print(f"\nOptimistic Net P&L: ₹{scenarios.breakdowns.__getitem__(__import__('cost_model.models', fromlist=['Scenario']).Scenario.OPTIMISTIC).net_pnl}")
    print(f"Base Net P&L: ₹{bd.net_pnl}")
    print(f"Conservative Net P&L: ₹{scenarios.breakdowns.__getitem__(__import__('cost_model.models', fromlist=['Scenario']).Scenario.CONSERVATIVE).net_pnl}")

    print(f"\nECONOMIC DECISION: {bd.economic_viability.value}")
    if bd.rejection_reasons:
        print("REJECTION REASON(S):")
        for r in bd.rejection_reasons:
            print(f"  - {r}")

    print("\n--- Audit trail ---")
    for line in bd.audit_trail:
        print(f"  * {line}")


def break_even_examples():
    engine = CostEngine()
    print("\n=== PART 9 — Break-even table (Entry ₹100, Qty 5, Capital ₹500) ===")
    trade = TradeInput(
        symbol="DEMO", exchange=Exchange.NSE, segment=Segment.EQUITY,
        product_type=ProductType.CNC, trade_type=TradeType.DELIVERY,
        side=Side.BUY, quantity=5, entry_price=Decimal("100"),
        entry_timestamp=datetime(2026, 9, 10, 9, 30),
        shares_from_demat_holdings=True,
    )
    from cost_model.fee_schedule import get_schedule
    schedule = get_schedule(trade.exchange, trade.trade_type, trade.entry_timestamp.date())
    buy_total, _ = engine._leg_cost(schedule, trade.turnover, Side.BUY, trade)
    table = break_even_table(trade, buy_total)
    for target, price in table.items():
        print(f"  Net P&L target ₹{target:>3} -> exit price ₹{price}")


if __name__ == "__main__":
    capital_sweep()
    full_trade_report()
    break_even_examples()
