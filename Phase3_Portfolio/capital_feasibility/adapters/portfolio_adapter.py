"""
PART — PORTFOLIO MANAGER BRIDGE

portfolio_manager.portfolio_state.PortfolioState is the authoritative
source for cash/invested_capital/open_positions (spec §28) — this module
never re-derives those figures, it only converts them into the types
Capital Feasibility computes with.

`PortfolioState` uses `float` throughout (a documented existing choice in
that package); `capital_feasibility` uses `Decimal` throughout (matching
cost_model and position_sizing). This adapter is the SINGLE place that
boundary is crossed, via `Decimal(str(x))` — never a bare `Decimal(x)` —
to avoid importing binary float noise into money math (the same pattern
`cost_model.models.TradeInput.__post_init__` already uses).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from ..config import CapitalFeasibilityConfig
from ..capital_state import build_capital_state
from ..models import AccountCapitalState, AssetExposure


def _d(x) -> Decimal:
    return Decimal(str(x))


def account_state_from_portfolio(
    portfolio_state,  # portfolio_manager.portfolio_state.PortfolioState
    config: CapitalFeasibilityConfig,
    committed_capital: Optional[Decimal] = None,
) -> AccountCapitalState:
    """`committed_capital` (capital already earmarked for OTHER pending
    trades not yet reflected in `invested_capital`) is not a field on
    PortfolioState today. If the caller doesn't supply it, it defaults to
    0 and `committed_capital_is_known=False` is recorded — spec §38:
    unknown must never look identical to a verified zero to the caller,
    even though the arithmetic treats it as 0 in the absence of better
    information."""
    known = committed_capital is not None
    committed = committed_capital if known else Decimal("0")

    return build_capital_state(
        total_capital=_d(portfolio_state.total_equity),
        cash=_d(portfolio_state.cash),
        invested_capital=_d(portfolio_state.invested_capital),
        config=config,
        committed_capital=committed,
        committed_capital_is_known=known,
    )


def asset_exposure_from_portfolio(
    portfolio_state,  # portfolio_manager.portfolio_state.PortfolioState
    symbol: str,
    sector: Optional[str],
) -> AssetExposure:
    """Market value (current_price * quantity), not entry-cost value —
    exposure/concentration limits are about how much of the portfolio is
    presently riding on a name, which moves with price, not the
    historical entry cost (spec §9)."""
    existing_value = Decimal("0")
    sector_value: Optional[Decimal] = Decimal("0") if sector is not None else None

    for pos in portfolio_state.open_positions:
        market_value = _d(pos.current_price) * pos.quantity
        if pos.symbol == symbol:
            existing_value += market_value
        if sector is not None and pos.sector == sector:
            sector_value += market_value

    return AssetExposure(
        symbol=symbol,
        existing_value=existing_value,
        sector=sector,
        existing_sector_value=sector_value,
    )
