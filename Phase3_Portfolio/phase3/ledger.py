"""
Within-batch allocation ledger.

The single most important thing the integration layer adds over calling
the four components in sequence: candidates in one batch are NOT
independent. Each allocation consumes cash, adds symbol/sector exposure,
and changes the portfolio the risk model sees. Evaluating candidate #3
against the pre-batch state would let a ₹1,000 account "afford" five
₹400 trades.

The ledger is the running state that makes each candidate see the
portfolio as it will actually be when its turn comes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional

from capital_feasibility.capital_state import build_capital_state
from capital_feasibility.models import AccountCapitalState
from portfolio_risk.models import Position as RiskPosition

from .adapters import d


@dataclass
class AllocationLedger:
    """Mutable, run-scoped. Never mutates any caller object."""

    portfolio_state: object          # portfolio_manager.PortfolioState
    config: object                   # Phase3Config
    initial_committed_capital: Optional[Decimal] = None

    allocated_capital: Decimal = Decimal("0")
    symbol_value: Dict[str, Decimal] = field(default_factory=dict)
    sector_value: Dict[str, Decimal] = field(default_factory=dict)
    accepted_positions: List[RiskPosition] = field(default_factory=list)

    @property
    def committed_capital_is_known(self) -> bool:
        return self.initial_committed_capital is not None

    def account_state(self) -> AccountCapitalState:
        """Capital state as of RIGHT NOW in the batch: the caller's
        committed capital (if known) plus everything allocated so far in
        this run. Reserve is recomputed by capital_feasibility, not here."""
        base = self.initial_committed_capital or Decimal("0")
        return build_capital_state(
            total_capital=d(self.portfolio_state.total_equity),
            cash=d(self.portfolio_state.cash),
            invested_capital=d(self.portfolio_state.invested_capital),
            config=self.config.feasibility,
            committed_capital=base + self.allocated_capital,
            committed_capital_is_known=self.committed_capital_is_known,
        )

    def extra_symbol_value(self, symbol: str) -> Decimal:
        return self.symbol_value.get(symbol, Decimal("0"))

    def extra_sector_value(self, sector: Optional[str]) -> Decimal:
        if sector is None:
            return Decimal("0")
        return self.sector_value.get(sector, Decimal("0"))

    def commit(
        self,
        symbol: str,
        sector: Optional[str],
        position_value: Decimal,
        required_capital: Decimal,
        risk_position: RiskPosition,
    ) -> None:
        """Record an allocation.

        `required_capital` (value + entry cost) is what leaves the cash
        balance; `position_value` is what shows up as exposure. Using one
        number for both would either under-reserve cash or overstate
        concentration, so both are tracked.
        """
        self.allocated_capital += required_capital
        self.symbol_value[symbol] = self.symbol_value.get(symbol, Decimal("0")) + position_value
        if sector is not None:
            self.sector_value[sector] = self.sector_value.get(sector, Decimal("0")) + position_value
        self.accepted_positions.append(risk_position)
