"""
PART — CAPITAL STATE CALCULATION

Builds the fully-populated AccountCapitalState (with reserved_capital
filled in from config) and projects the post-trade state. Pure,
deterministic, no I/O (spec §25).
"""
from __future__ import annotations

from decimal import Decimal

from .config import CapitalFeasibilityConfig
from .models import AccountCapitalState


def build_capital_state(
    total_capital: Decimal,
    cash: Decimal,
    invested_capital: Decimal,
    config: CapitalFeasibilityConfig,
    committed_capital: Decimal = Decimal("0"),
    committed_capital_is_known: bool = True,
) -> AccountCapitalState:
    reserved = config.reserved_capital(total_capital)
    return AccountCapitalState(
        total_capital=total_capital,
        cash=cash,
        invested_capital=invested_capital,
        committed_capital=committed_capital,
        committed_capital_is_known=committed_capital_is_known,
        reserved_capital=reserved,
    )


def project_post_trade_state(
    state: AccountCapitalState, required_capital: Decimal
) -> AccountCapitalState:
    """Returns the AccountCapitalState AS IF this trade's required
    capital had already been deducted from cash and added to invested
    capital (spec §7, §14). Does not mutate `state` — pure projection."""
    return AccountCapitalState(
        total_capital=state.total_capital,
        cash=state.cash - required_capital,
        invested_capital=state.invested_capital + required_capital,
        committed_capital=state.committed_capital,
        committed_capital_is_known=state.committed_capital_is_known,
        reserved_capital=state.reserved_capital,
    )
