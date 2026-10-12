"""
PART 4 — EXACT COST ENGINE

    Trade Input
      -> Instrument / Segment Resolver         (TradeInput itself + validator)
      -> Applicable Fee Schedule Resolver       (fee_schedule.get_schedule)
      -> Brokerage / STT / Exchange / SEBI /
         GST / Stamp / DP Calculators           (calculators.py, per leg)
      -> Spread / Slippage Model                (slippage.py)
      -> Market Impact Model                    (market_impact.py)
      -> Total Cost -> Gross P&L -> Net P&L ->
         Net Return -> Economic Viability Test  (this module + economics.py)

Every step is independently testable (each calculator/model module has
its own unit tests) and this module only composes them — it contains no
rate numbers or thresholds of its own.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from .models import (
    TradeInput, CostBreakdown, CostComponent, Side, Scenario, TradeType,
)
from .fee_schedule import get_schedule
from . import calculators as calc
from . import slippage as slip
from . import market_impact as impact
from . import breakeven as be
from .economics import evaluate_viability, minimum_required_gross_return_pct
from .config import GateConfig, DEFAULT_GATE_CONFIG
from .validator import validate_trade_input

# Scenario multipliers applied to the EXECUTION-cost components only
# (slippage + spread + impact). Statutory/broker charges are deterministic
# and do not vary by scenario — only execution quality does (PART 14).
_SCENARIO_MULTIPLIERS = {
    Scenario.OPTIMISTIC: Decimal("0.5"),
    Scenario.BASE: Decimal("1.0"),
    Scenario.CONSERVATIVE: Decimal("2.0"),
}


class CostEngine:
    def __init__(self, gate: GateConfig = DEFAULT_GATE_CONFIG):
        self.gate = gate

    # ------------------------------------------------------------------
    def _leg_cost(self, schedule, turnover: Decimal, side: Side,
                   trade: TradeInput):
        brokerage = calc.brokerage_for_leg(schedule, turnover)
        stt = calc.stt_for_leg(schedule, turnover, side)
        exch = calc.exchange_txn_charge_for_leg(schedule, turnover)
        ipft = calc.ipft_charge_for_leg(schedule, turnover)
        sebi = calc.sebi_charge_for_leg(schedule, turnover)
        gst = calc.gst_for_leg(schedule, brokerage, sebi, exch, ipft)
        stamp = calc.stamp_duty_for_leg(schedule, turnover, side)
        dp = calc.dp_charge_for_leg(
            schedule, side, trade.trade_type,
            shares_from_demat_holdings=bool(trade.shares_from_demat_holdings),
        )
        total = brokerage + stt + exch + ipft + sebi + gst + stamp + dp
        parts = {
            "brokerage": brokerage, "stt": stt, "exchange_charges": exch,
            "ipft_charges": ipft, "sebi_charges": sebi, "gst": gst,
            "stamp_duty": stamp, "dp_charges": dp,
        }
        return total, parts

    # ------------------------------------------------------------------
    def price_trade(
        self,
        trade: TradeInput,
        scenario: Scenario = Scenario.BASE,
        calibrated_slippage_bps: Decimal = None,
        gate: GateConfig = None,
    ) -> CostBreakdown:
        """
        Prices a full ROUND TRIP (buy leg + sell leg) if trade.exit_price
        is set; if not, prices the BUY leg alone and reports gross/net
        P&L as None-equivalent (0) — used for pre-trade entry-only costing.
        """
        validate_trade_input(trade)
        gate = gate or self.gate

        as_of = (trade.entry_timestamp.date() if trade.entry_timestamp else date.today())
        schedule = get_schedule(trade.exchange, trade.trade_type, as_of)

        audit: list[str] = [
            f"Fee schedule resolved: {schedule.version_id} (effective_from={schedule.effective_from})"
        ]

        buy_turnover = trade.turnover
        buy_total, buy_parts = self._leg_cost(schedule, buy_turnover, Side.BUY, trade)
        audit.append(f"BUY leg turnover=₹{buy_turnover} statutory/broker cost=₹{buy_total}")

        components = [
            CostComponent(f"buy_{k}", v, schedule.version_id, "Zerodha charges page",
                           "https://zerodha.com/charges", Side.BUY)
            for k, v in buy_parts.items()
        ]

        sell_total = Decimal("0.00")
        sell_parts = {k: Decimal("0.00") for k in buy_parts}
        gross_pnl = Decimal("0.00")
        sell_turnover = Decimal("0.00")

        if trade.exit_price is not None:
            sell_turnover = (trade.exit_price * trade.quantity).quantize(Decimal("0.01"))
            sell_total, sell_parts = self._leg_cost(schedule, sell_turnover, Side.SELL, trade)
            gross_pnl = (trade.exit_price - trade.entry_price) * trade.quantity
            audit.append(f"SELL leg turnover=₹{sell_turnover} statutory/broker cost=₹{sell_total}")
            components += [
                CostComponent(f"sell_{k}", v, schedule.version_id, "Zerodha charges page",
                               "https://zerodha.com/charges", Side.SELL)
                for k, v in sell_parts.items()
            ]

        statutory_total = {
            k: buy_parts[k] + sell_parts[k] for k in buy_parts
        }
        total_statutory_broker_cost = buy_total + sell_total

        # -------------------- execution friction --------------------
        reference_turnover = buy_turnover if trade.exit_price is None else (buy_turnover + sell_turnover)

        spread_cost, spread_note = slip.estimate_spread_cost(buy_turnover, trade.liquidity)
        slippage_cost, slip_tier, slip_note = slip.estimate_slippage(
            buy_turnover, trade.order_type, trade.trade_type, trade.liquidity,
            calibrated_bps=calibrated_slippage_bps,
        )
        impact_cost, impact_tier, impact_note = impact.estimate_market_impact(
            buy_turnover, trade.liquidity
        )
        if trade.exit_price is not None:
            sell_spread_cost, _ = slip.estimate_spread_cost(sell_turnover, trade.liquidity)
            sell_slippage_cost, _, _ = slip.estimate_slippage(
                sell_turnover, trade.order_type, trade.trade_type, trade.liquidity,
                calibrated_bps=calibrated_slippage_bps,
            )
            sell_impact_cost, _, _ = impact.estimate_market_impact(sell_turnover, trade.liquidity)
            spread_cost += sell_spread_cost
            slippage_cost += sell_slippage_cost
            impact_cost += sell_impact_cost

        mult = _SCENARIO_MULTIPLIERS[scenario]
        spread_cost = (spread_cost * mult).quantize(Decimal("0.01"))
        slippage_cost = (slippage_cost * mult).quantize(Decimal("0.01"))
        impact_cost = (impact_cost * mult).quantize(Decimal("0.01"))

        audit.append(f"Slippage[{slip_tier}] (x{mult} for {scenario.value}): ₹{slippage_cost} — {slip_note}")
        audit.append(f"Spread cost (x{mult}): ₹{spread_cost} — {spread_note}")
        audit.append(f"Market impact[{impact_tier}] (x{mult}): ₹{impact_cost} — {impact_note}")

        total_execution_cost = spread_cost + slippage_cost + impact_cost
        total_cost = total_statutory_broker_cost + total_execution_cost

        net_pnl = gross_pnl - total_cost

        gross_return_pct = (
            (gross_pnl / buy_turnover * 100).quantize(Decimal("0.0001"))
            if buy_turnover else Decimal("0")
        )
        net_return_pct = (
            (net_pnl / buy_turnover * 100).quantize(Decimal("0.0001"))
            if buy_turnover else Decimal("0")
        )

        cost_as_pct_of_capital = (
            (total_cost / trade.available_capital * 100).quantize(Decimal("0.0001"))
            if trade.available_capital else None
        )
        cost_as_pct_of_position = (
            (total_cost / buy_turnover * 100).quantize(Decimal("0.0001"))
            if buy_turnover else Decimal("0")
        )
        cost_as_pct_of_gross_profit = (
            (total_cost / gross_pnl * 100).quantize(Decimal("0.0001"))
            if gross_pnl and gross_pnl > 0 else None
        )

        # -------------------- break-even --------------------
        break_even_price = be.solve_break_even_price(trade, buy_total, Decimal("0"))
        break_even_move_pct = (
            (break_even_price - trade.entry_price) / trade.entry_price * 100
        ).quantize(Decimal("0.0001"))

        friction_pct = (total_cost / buy_turnover * 100) if buy_turnover else Decimal("0")
        min_required_return = minimum_required_gross_return_pct(friction_pct, gate=gate)

        viability, reasons = evaluate_viability(
            net_pnl=net_pnl,
            net_return_pct=net_return_pct,
            total_cost=total_cost,
            gross_pnl=gross_pnl,
            break_even_move_pct=abs(break_even_move_pct),
            slippage_tier=slip_tier,
            market_impact_tier=impact_tier,
            position_value=buy_turnover,
            gate=gate,
        )

        return CostBreakdown(
            trade=trade,
            scenario=scenario,
            gross_pnl=gross_pnl,
            brokerage=statutory_total["brokerage"],
            stt=statutory_total["stt"],
            exchange_charges=statutory_total["exchange_charges"],
            ipft_charges=statutory_total["ipft_charges"],
            sebi_charges=statutory_total["sebi_charges"],
            gst=statutory_total["gst"],
            stamp_duty=statutory_total["stamp_duty"],
            dp_charges=statutory_total["dp_charges"],
            slippage_cost=slippage_cost,
            spread_cost=spread_cost,
            market_impact_cost=impact_cost,
            total_statutory_broker_cost=total_statutory_broker_cost,
            total_execution_cost=total_execution_cost,
            total_cost=total_cost,
            net_pnl=net_pnl,
            gross_return_pct=gross_return_pct,
            net_return_pct=net_return_pct,
            cost_as_pct_of_capital=cost_as_pct_of_capital,
            cost_as_pct_of_position=cost_as_pct_of_position,
            cost_as_pct_of_gross_profit=cost_as_pct_of_gross_profit,
            break_even_move_pct=break_even_move_pct,
            break_even_price=break_even_price,
            minimum_expected_return_required_pct=min_required_return,
            economic_viability=viability,
            rejection_reasons=reasons,
            components=components,
            audit_trail=audit,
            fee_schedule_version=schedule.version_id,
        )
