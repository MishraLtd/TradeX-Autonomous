"""
PART 16 — SYSTEM OPERATING COST ALLOCATION

Deliberately a SEPARATE module from engine.py: infrastructure spend
(Kite Connect subscription, VM, DB, monitoring) is never mixed into
`CostBreakdown.total_cost`, which represents actual trading/broker/
statutory friction only. This module produces a SECOND, optional view.

    A. TRADE NET P&L          -> CostBreakdown.net_pnl  (unchanged)
    B. SYSTEM-ADJUSTED P&L    -> this module's output

Note on Kite Connect specifically (PART 17): Zerodha's own charges page
lists Kite Connect pricing as "Personal: Free" for individual/personal
non-commercial use and "Connect: ₹500/month" for the commercial API
tier. A personal single-account TradeX deployment should therefore
default `monthly_kite_connect_cost` to ₹0 — this module does NOT
hard-code that assumption; it is a parameter the caller must state
explicitly, so a future move to the commercial tier is a deliberate
config change, not a silent omission.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from .models import CostBreakdown


@dataclass
class OperatingCostConfig:
    monthly_kite_connect_cost: Decimal = Decimal("0")     # ₹0 if on the Personal (free) tier
    monthly_vm_hosting_cost: Decimal = Decimal("0")
    monthly_database_cost: Decimal = Decimal("0")
    monthly_monitoring_cost: Decimal = Decimal("0")
    expected_live_trades_per_month: Optional[int] = None  # None -> cannot allocate per-trade

    @property
    def total_monthly_cost(self) -> Decimal:
        return (self.monthly_kite_connect_cost + self.monthly_vm_hosting_cost +
                self.monthly_database_cost + self.monthly_monitoring_cost)


@dataclass
class SystemAdjustedEconomics:
    trade_net_pnl: Decimal
    allocated_operating_cost_per_trade: Optional[Decimal]
    system_adjusted_net_pnl: Optional[Decimal]
    note: str


def system_adjusted_economics(breakdown: CostBreakdown,
                               cfg: OperatingCostConfig) -> SystemAdjustedEconomics:
    if not cfg.expected_live_trades_per_month or cfg.expected_live_trades_per_month <= 0:
        return SystemAdjustedEconomics(
            trade_net_pnl=breakdown.net_pnl,
            allocated_operating_cost_per_trade=None,
            system_adjusted_net_pnl=None,
            note=("expected_live_trades_per_month not supplied — cannot allocate "
                  "operating cost per trade. Trade-level PASS/REJECT decisions "
                  "should continue to use trade_net_pnl / CostBreakdown.economic_viability, "
                  "NOT this view, since infra cost is a monthly fixed cost independent "
                  "of any single trade's economics."),
        )

    per_trade = (cfg.total_monthly_cost / cfg.expected_live_trades_per_month).quantize(Decimal("0.01"))
    adjusted = (breakdown.net_pnl - per_trade).quantize(Decimal("0.01"))
    return SystemAdjustedEconomics(
        trade_net_pnl=breakdown.net_pnl,
        allocated_operating_cost_per_trade=per_trade,
        system_adjusted_net_pnl=adjusted,
        note=(f"₹{cfg.total_monthly_cost}/month operating cost / "
              f"{cfg.expected_live_trades_per_month} expected trades = ₹{per_trade}/trade. "
              f"This is a business-economics view only — it does NOT feed back into "
              f"economic_viability, which is decided on trading friction alone."),
    )
