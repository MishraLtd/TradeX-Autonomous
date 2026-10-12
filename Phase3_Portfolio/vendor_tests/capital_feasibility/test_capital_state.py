from decimal import Decimal

from capital_feasibility.capital_state import build_capital_state, project_post_trade_state
from capital_feasibility.config import CapitalFeasibilityConfig


def test_reserved_capital_uses_the_larger_of_absolute_and_percentage():
    config = CapitalFeasibilityConfig(minimum_cash_reserve=Decimal("50"), minimum_free_capital_ratio=Decimal("0.05"))
    # 5% of 1000 = 50, tie with the absolute floor
    assert config.reserved_capital(Decimal("1000")) == Decimal("50.00")
    # 5% of 10000 = 500, greater than the absolute floor
    assert config.reserved_capital(Decimal("10000")) == Decimal("500.00")
    # 5% of 200 = 10, less than the absolute floor -> floor wins
    assert config.reserved_capital(Decimal("200")) == Decimal("50")


def test_build_capital_state_fills_in_reserved_capital():
    config = CapitalFeasibilityConfig()
    state = build_capital_state(
        total_capital=Decimal("1000"), cash=Decimal("800"), invested_capital=Decimal("200"), config=config
    )
    assert state.reserved_capital == config.reserved_capital(Decimal("1000"))
    assert state.deployable_capital == state.cash - state.committed_capital - state.reserved_capital
    assert state.capital_utilization == Decimal("0.200000")


def test_deployable_capital_accounts_for_committed_capital():
    config = CapitalFeasibilityConfig()
    state = build_capital_state(
        total_capital=Decimal("1000"),
        cash=Decimal("800"),
        invested_capital=Decimal("200"),
        config=config,
        committed_capital=Decimal("100"),
        committed_capital_is_known=True,
    )
    assert state.uncommitted_capital == Decimal("700")
    assert state.deployable_capital == Decimal("700") - state.reserved_capital


def test_capital_utilization_none_when_total_capital_zero():
    config = CapitalFeasibilityConfig()
    state = build_capital_state(
        total_capital=Decimal("0"), cash=Decimal("0"), invested_capital=Decimal("0"), config=config
    )
    assert state.capital_utilization is None


def test_project_post_trade_state_moves_cash_into_invested_without_mutating_input():
    config = CapitalFeasibilityConfig()
    state = build_capital_state(
        total_capital=Decimal("1000"), cash=Decimal("800"), invested_capital=Decimal("200"), config=config
    )
    projected = project_post_trade_state(state, Decimal("300"))

    assert projected.cash == Decimal("500")
    assert projected.invested_capital == Decimal("500")
    # original untouched (frozen dataclass + pure function)
    assert state.cash == Decimal("800")
    assert state.invested_capital == Decimal("200")


def test_negative_deployable_capital_is_surfaced_not_clamped():
    """An account can legitimately already be over-reserved (spec §22
    Case 10) — deployable_capital must show that as negative, not 0."""
    config = CapitalFeasibilityConfig(minimum_cash_reserve=Decimal("900"))
    state = build_capital_state(
        total_capital=Decimal("1000"), cash=Decimal("800"), invested_capital=Decimal("200"), config=config
    )
    assert state.deployable_capital == Decimal("800") - Decimal("900")
    assert state.deployable_capital < 0
