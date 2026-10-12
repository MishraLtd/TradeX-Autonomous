from decimal import Decimal

from capital_feasibility.capital_state import build_capital_state
from capital_feasibility.config import CapitalFeasibilityConfig
from capital_feasibility.enums import BlockingConstraint
from capital_feasibility.gates import (
    check_gate1_basic_capital,
    check_gate2_capital_reserve,
    check_gate3_position_allocation,
    check_gate4_portfolio_exposure,
    check_gate5_execution_capital,
    check_gate6_liquidity,
)
from capital_feasibility.models import LiquidityContext, TradeCapitalRequirement


def make_requirement(qty, price, cost=Decimal("0")):
    position_value = Decimal(qty) * price
    return TradeCapitalRequirement(
        quantity=qty,
        position_value=position_value,
        execution_cost=cost,
        required_capital=position_value + cost,
    )


def test_gate1_passes_when_cash_covers_required_capital():
    config = CapitalFeasibilityConfig()
    state = build_capital_state(Decimal("1000"), Decimal("800"), Decimal("200"), config)
    req = make_requirement(5, Decimal("100"))  # 500 required
    result = check_gate1_basic_capital(req, state, Decimal("100"))
    assert result.passed
    assert result.constraint == BlockingConstraint.NONE


def test_gate1_fails_when_cash_insufficient():
    config = CapitalFeasibilityConfig()
    state = build_capital_state(Decimal("1000"), Decimal("100"), Decimal("900"), config)
    req = make_requirement(5, Decimal("100"))  # 500 required, only 100 cash
    result = check_gate1_basic_capital(req, state, Decimal("100"))
    assert not result.passed
    assert result.constraint == BlockingConstraint.INSUFFICIENT_CASH
    assert result.max_quantity_allowed == 1


def test_gate2_fails_when_reserve_would_be_breached():
    config = CapitalFeasibilityConfig(minimum_cash_reserve=Decimal("300"), minimum_free_capital_ratio=Decimal("0"))
    state = build_capital_state(Decimal("1000"), Decimal("500"), Decimal("500"), config)
    # cash=500, reserve=300 -> deployable=200
    req = make_requirement(3, Decimal("100"))  # 300 required > 200 deployable
    result = check_gate2_capital_reserve(req, state, Decimal("100"))
    assert not result.passed
    assert result.constraint == BlockingConstraint.MINIMUM_CASH_RESERVE
    assert result.max_quantity_allowed == 2


def test_gate3_fails_on_trade_capital_ratio():
    config = CapitalFeasibilityConfig(max_trade_capital_ratio=Decimal("0.30"), max_asset_allocation_ratio=Decimal("0.90"))
    # total capital 1000 -> max trade value 300
    result = check_gate3_position_allocation(
        position_value=Decimal("400"),
        existing_asset_value=Decimal("0"),
        total_capital=Decimal("1000"),
        entry_price=Decimal("100"),
        config=config,
    )
    assert not result.passed
    assert result.constraint == BlockingConstraint.MAX_TRADE_CAPITAL_RATIO


def test_gate3_fails_on_asset_allocation_even_within_trade_ratio():
    config = CapitalFeasibilityConfig(max_trade_capital_ratio=Decimal("0.90"), max_asset_allocation_ratio=Decimal("0.40"))
    # existing 350 + new 100 = 450 > 400 (40% of 1000)
    result = check_gate3_position_allocation(
        position_value=Decimal("100"),
        existing_asset_value=Decimal("350"),
        total_capital=Decimal("1000"),
        entry_price=Decimal("100"),
        config=config,
    )
    assert not result.passed
    assert result.constraint == BlockingConstraint.MAX_ASSET_ALLOCATION


def test_gate4_sector_concentration_blocks_even_with_cash_available():
    """The canonical example from spec §9: cash is sufficient but sector
    exposure is already near the limit."""
    config = CapitalFeasibilityConfig(max_sector_allocation_ratio=Decimal("0.50"), max_portfolio_allocation_ratio=Decimal("0.99"))
    result = check_gate4_portfolio_exposure(
        position_value=Decimal("200"),
        existing_sector_value=Decimal("450"),  # 45% of 1000
        existing_invested_capital=Decimal("450"),
        total_capital=Decimal("1000"),
        entry_price=Decimal("100"),
        config=config,
    )
    assert not result.passed
    assert result.constraint == BlockingConstraint.MAX_SECTOR_ALLOCATION


def test_gate4_skips_sector_check_when_sector_unknown():
    config = CapitalFeasibilityConfig(max_portfolio_allocation_ratio=Decimal("0.99"))
    result = check_gate4_portfolio_exposure(
        position_value=Decimal("100"),
        existing_sector_value=None,
        existing_invested_capital=Decimal("0"),
        total_capital=Decimal("1000"),
        entry_price=Decimal("100"),
        config=config,
    )
    assert result.passed


def test_gate5_reflects_actual_cost_at_evaluated_quantity():
    config = CapitalFeasibilityConfig()
    state = build_capital_state(Decimal("1000"), Decimal("500"), Decimal("500"), config)
    # deployable = cash(500) - reserve(max(50, 5% of 1000)=50) = 450
    req = make_requirement(5, Decimal("100"), cost=Decimal("2"))  # required_capital = 502
    result = check_gate5_execution_capital(req, state, Decimal("100"))
    assert not result.passed  # 502 > 450 deployable -> correctly fails
    assert result.constraint == BlockingConstraint.EXECUTION_COST_INFEASIBLE

    # A smaller quantity whose required_capital fits within deployable passes.
    req_small = make_requirement(4, Decimal("100"), cost=Decimal("2"))  # required_capital = 402
    result_small = check_gate5_execution_capital(req_small, state, Decimal("100"))
    assert result_small.passed


def test_gate6_no_op_without_liquidity_data():
    config = CapitalFeasibilityConfig()
    result = check_gate6_liquidity(1000, Decimal("100"), LiquidityContext(), config)
    assert result.passed
    assert "no-op" in result.detail


def test_gate6_blocks_oversized_participation():
    config = CapitalFeasibilityConfig(liquidity_participation_limit=Decimal("0.02"))
    liquidity = LiquidityContext(average_daily_traded_value=Decimal("10000"))
    # max participation value = 200; requesting 300 worth
    result = check_gate6_liquidity(3, Decimal("100"), liquidity, config)
    assert not result.passed
    assert result.constraint == BlockingConstraint.LIQUIDITY_PARTICIPATION_LIMIT
    assert result.max_quantity_allowed == 2
