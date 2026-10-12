"""Edge cases from spec §22, mapped to concrete tests (case numbers in comments)."""
from decimal import Decimal
from functools import partial

import pytest

from capital_feasibility.adapters.cost_adapter import price_buy_leg
from capital_feasibility.capital_state import build_capital_state
from capital_feasibility.config import CapitalFeasibilityConfig
from capital_feasibility.enums import FeasibilityStatus
from capital_feasibility.engine import evaluate_trade
from capital_feasibility.exceptions import CostModelIntegrationError
from capital_feasibility.models import AssetExposure, LiquidityContext, TradeCapitalRequest


def _price_fn(cost_engine, cost_models_module, symbol="TESTSTOCK", exchange="NSE", trade_type="DELIVERY", entry_price=Decimal("100")):
    return partial(price_buy_leg, cost_engine, cost_models_module, symbol, exchange, trade_type, entry_price)


def _run(cost_engine, cost_models_module, config, state, exposure, entry_price=Decimal("100"), qty=5, liquidity=None):
    request = TradeCapitalRequest(
        position_id="p1",
        opportunity_id="o1",
        symbol="TESTSTOCK",
        exchange="NSE",
        sector="TECHNOLOGY",
        trade_type="DELIVERY",
        entry_price=entry_price,
        requested_quantity=qty,
        liquidity=liquidity or LiquidityContext(),
    )
    price_fn = _price_fn(cost_engine, cost_models_module, entry_price=entry_price)
    return evaluate_trade(request, state, exposure, price_fn, config)


def test_case1_zero_available_capital(cost_engine, cost_models_module, config):
    # cash=0 with invested_capital=0 too (e.g. capital genuinely isn't
    # there yet, not "already fully deployed elsewhere") isolates pure
    # cash unavailability from the portfolio-allocation gates.
    state = build_capital_state(Decimal("1000"), Decimal("0"), Decimal("0"), config)
    exposure = AssetExposure(symbol="TESTSTOCK", existing_value=Decimal("0"))
    result = _run(cost_engine, cost_models_module, config, state, exposure, qty=1)
    assert result.feasible_quantity == 0
    assert result.feasibility_status == FeasibilityStatus.CAPITAL_CONSTRAINED


def test_case2_negative_available_capital(cost_engine, cost_models_module, config):
    # Already-overdrawn account: cash is negative (spec explicitly calls
    # this a meaningful state to surface, not a validation error).
    state = build_capital_state(Decimal("1000"), Decimal("-200"), Decimal("1200"), config)
    exposure = AssetExposure(symbol="TESTSTOCK", existing_value=Decimal("0"))
    result = _run(cost_engine, cost_models_module, config, state, exposure)
    assert result.feasible_quantity == 0
    assert result.capital_feasible is False
    assert any("over-committed" in w for w in result.warnings)


def test_case3_requested_quantity_zero(cost_engine, cost_models_module, config):
    state = build_capital_state(Decimal("1000"), Decimal("1000"), Decimal("0"), config)
    exposure = AssetExposure(symbol="TESTSTOCK", existing_value=Decimal("0"))
    result = _run(cost_engine, cost_models_module, config, state, exposure, qty=0)
    assert result.feasibility_status == FeasibilityStatus.FEASIBLE
    assert result.requested_quantity == 0
    assert result.feasible_quantity == 0
    assert result.capital_feasible is True


def test_case4_negative_quantity_rejected(cost_engine, cost_models_module, config):
    state = build_capital_state(Decimal("1000"), Decimal("1000"), Decimal("0"), config)
    exposure = AssetExposure(symbol="TESTSTOCK", existing_value=Decimal("0"))
    result = _run(cost_engine, cost_models_module, config, state, exposure, qty=-5)
    assert result.feasibility_status == FeasibilityStatus.REJECTED_INVALID_INPUT


def test_case5_price_zero_rejected(cost_engine, cost_models_module, config):
    state = build_capital_state(Decimal("1000"), Decimal("1000"), Decimal("0"), config)
    exposure = AssetExposure(symbol="TESTSTOCK", existing_value=Decimal("0"))
    result = _run(cost_engine, cost_models_module, config, state, exposure, entry_price=Decimal("0"))
    assert result.feasibility_status == FeasibilityStatus.REJECTED_INVALID_INPUT


def test_case6_negative_price_rejected(cost_engine, cost_models_module, config):
    state = build_capital_state(Decimal("1000"), Decimal("1000"), Decimal("0"), config)
    exposure = AssetExposure(symbol="TESTSTOCK", existing_value=Decimal("0"))
    result = _run(cost_engine, cost_models_module, config, state, exposure, entry_price=Decimal("-10"))
    assert result.feasibility_status == FeasibilityStatus.REJECTED_INVALID_INPUT


def test_case7_costs_greater_than_available_capital(cost_engine, cost_models_module):
    # Intraday, tiny cash, so even 1 share's execution cost dominates.
    config = CapitalFeasibilityConfig(minimum_cash_reserve=Decimal("0"), minimum_free_capital_ratio=Decimal("0"))
    state = build_capital_state(Decimal("10"), Decimal("10"), Decimal("0"), config)
    exposure = AssetExposure(symbol="TESTSTOCK", existing_value=Decimal("0"))
    result = _run(cost_engine, cost_models_module, config, state, exposure, entry_price=Decimal("9.50"), qty=1)
    # Either fully feasible (if 1 share's total cost still fits ₹10) or
    # reduced/blocked to 0 — either way the result must never claim a
    # required_capital that exceeds available cash.
    assert result.required_capital <= Decimal("10") or result.feasible_quantity == 0


def test_case8_trade_exactly_equals_available_capital(cost_engine, cost_models_module, config):
    entry_price = Decimal("100")
    state = build_capital_state(Decimal("1000"), Decimal("1000"), Decimal("0"), config)
    exposure = AssetExposure(symbol="TESTSTOCK", existing_value=Decimal("0"))
    # Find the exact max first, then request exactly that.
    result_probe = _run(cost_engine, cost_models_module, config, state, exposure, entry_price=entry_price, qty=10**6)
    exact_qty = result_probe.maximum_feasible_quantity
    result = _run(cost_engine, cost_models_module, config, state, exposure, entry_price=entry_price, qty=exact_qty)
    assert result.feasibility_status == FeasibilityStatus.FEASIBLE
    assert result.required_capital <= state.deployable_capital


def test_case9_trade_slightly_exceeds_available_capital(cost_engine, cost_models_module, config):
    entry_price = Decimal("100")
    state = build_capital_state(Decimal("1000"), Decimal("1000"), Decimal("0"), config)
    exposure = AssetExposure(symbol="TESTSTOCK", existing_value=Decimal("0"))
    result_probe = _run(cost_engine, cost_models_module, config, state, exposure, entry_price=entry_price, qty=10**6)
    one_over = result_probe.maximum_feasible_quantity + 1
    result = _run(cost_engine, cost_models_module, config, state, exposure, entry_price=entry_price, qty=one_over)
    assert result.feasibility_status == FeasibilityStatus.PARTIALLY_FEASIBLE
    assert result.feasible_quantity == result_probe.maximum_feasible_quantity


def test_case10_reserve_consumes_available_capital(cost_engine, cost_models_module):
    config = CapitalFeasibilityConfig(minimum_cash_reserve=Decimal("1000"), minimum_free_capital_ratio=Decimal("0"))
    state = build_capital_state(Decimal("1000"), Decimal("1000"), Decimal("0"), config)
    exposure = AssetExposure(symbol="TESTSTOCK", existing_value=Decimal("0"))
    result = _run(cost_engine, cost_models_module, config, state, exposure, qty=1)
    assert result.feasible_quantity == 0
    assert result.feasibility_status == FeasibilityStatus.CAPITAL_CONSTRAINED


def test_case11_existing_position_already_at_maximum_allocation(cost_engine, cost_models_module):
    config = CapitalFeasibilityConfig(max_asset_allocation_ratio=Decimal("0.40"))
    state = build_capital_state(Decimal("1000"), Decimal("1000"), Decimal("400"), config)
    exposure = AssetExposure(symbol="TESTSTOCK", existing_value=Decimal("400"))  # already at the 40% cap
    result = _run(cost_engine, cost_models_module, config, state, exposure, qty=1)
    assert result.feasible_quantity == 0
    assert result.feasibility_status == FeasibilityStatus.PORTFOLIO_CONSTRAINED


def test_case12_existing_portfolio_has_insufficient_free_capital(cost_engine, cost_models_module):
    config = CapitalFeasibilityConfig(minimum_cash_reserve=Decimal("50"), minimum_free_capital_ratio=Decimal("0"))
    state = build_capital_state(Decimal("1000"), Decimal("60"), Decimal("940"), config)
    exposure = AssetExposure(symbol="TESTSTOCK", existing_value=Decimal("0"))
    result = _run(cost_engine, cost_models_module, config, state, exposure, entry_price=Decimal("50"), qty=1)
    # deployable = 60 - 50 = 10, not enough for even 1 share at ₹50.
    assert result.feasible_quantity == 0


def test_case13_very_large_requested_quantity(cost_engine, cost_models_module, config):
    state = build_capital_state(Decimal("1000"), Decimal("1000"), Decimal("0"), config)
    exposure = AssetExposure(symbol="TESTSTOCK", existing_value=Decimal("0"))
    result = _run(cost_engine, cost_models_module, config, state, exposure, qty=1_000_000)
    assert result.feasibility_status in (
        FeasibilityStatus.PARTIALLY_FEASIBLE,
        FeasibilityStatus.CAPITAL_CONSTRAINED,
        FeasibilityStatus.PORTFOLIO_CONSTRAINED,
    )
    assert result.feasible_quantity < 1_000_000


def test_case14_missing_optional_portfolio_data_sector_unknown(cost_engine, cost_models_module, config):
    state = build_capital_state(Decimal("1000"), Decimal("1000"), Decimal("0"), config)
    exposure = AssetExposure(symbol="TESTSTOCK", existing_value=Decimal("0"), sector=None, existing_sector_value=None)
    result = _run(cost_engine, cost_models_module, config, state, exposure, qty=1)
    assert any("Sector exposure unknown" in w for w in result.warnings)
    # Missing sector data degrades Gate 4's sector check, it never blocks outright.
    assert result.feasibility_status == FeasibilityStatus.FEASIBLE


def test_case15_missing_cost_model_output_fails_closed(cost_engine, cost_models_module, config):
    state = build_capital_state(Decimal("1000"), Decimal("1000"), Decimal("0"), config)
    exposure = AssetExposure(symbol="TESTSTOCK", existing_value=Decimal("0"))
    request = TradeCapitalRequest(
        position_id="p1", opportunity_id="o1", symbol="TESTSTOCK", exchange="NSE",
        sector="TECHNOLOGY", trade_type="DELIVERY", entry_price=Decimal("100"), requested_quantity=5,
    )

    def broken_price_fn(qty):
        raise CostModelIntegrationError("simulated Cost Model outage")

    with pytest.raises(CostModelIntegrationError):
        evaluate_trade(request, state, exposure, broken_price_fn, config)


def test_case16_invalid_numeric_values_rejected(cost_engine, cost_models_module, config):
    state = build_capital_state(Decimal("1000"), Decimal("1000"), Decimal("0"), config)
    exposure = AssetExposure(symbol="TESTSTOCK", existing_value=Decimal("0"))
    result = _run(cost_engine, cost_models_module, config, state, exposure, entry_price=Decimal("-1"), qty=5)
    assert result.feasibility_status == FeasibilityStatus.REJECTED_INVALID_INPUT


def test_case17_missing_total_capital_rejected(cost_engine, cost_models_module, config):
    from capital_feasibility.models import AccountCapitalState

    broken_state = AccountCapitalState(
        total_capital=None, cash=Decimal("1000"), invested_capital=Decimal("0"), reserved_capital=Decimal("0")
    )
    exposure = AssetExposure(symbol="TESTSTOCK", existing_value=Decimal("0"))
    request = TradeCapitalRequest(
        position_id="p1", opportunity_id="o1", symbol="TESTSTOCK", exchange="NSE",
        sector="TECHNOLOGY", trade_type="DELIVERY", entry_price=Decimal("100"), requested_quantity=5,
    )
    price_fn = _price_fn(cost_engine, cost_models_module)
    result = evaluate_trade(request, broken_state, exposure, price_fn, config)
    assert result.feasibility_status == FeasibilityStatus.REJECTED_INVALID_INPUT


def test_case18_integer_quantity_rounding_never_exceeds_capital(cost_engine, cost_models_module, config):
    entry_price = Decimal("33.33")
    state = build_capital_state(Decimal("1000"), Decimal("1000"), Decimal("0"), config)
    exposure = AssetExposure(symbol="TESTSTOCK", existing_value=Decimal("0"))
    result = _run(cost_engine, cost_models_module, config, state, exposure, entry_price=entry_price, qty=1_000)
    assert result.required_capital <= state.deployable_capital or result.feasible_quantity == 0
    assert isinstance(result.feasible_quantity, int)


def test_case19_lot_size_constraint_respected(cost_engine, cost_models_module, config):
    entry_price = Decimal("100")
    state = build_capital_state(Decimal("1000"), Decimal("1000"), Decimal("0"), config)
    exposure = AssetExposure(symbol="TESTSTOCK", existing_value=Decimal("0"))
    request = TradeCapitalRequest(
        position_id="p1", opportunity_id="o1", symbol="TESTSTOCK", exchange="NSE",
        sector="TECHNOLOGY", trade_type="DELIVERY", entry_price=entry_price,
        requested_quantity=1_000, lot_size=25,
    )
    price_fn = _price_fn(cost_engine, cost_models_module, entry_price=entry_price)
    result = evaluate_trade(request, state, exposure, price_fn, config)
    assert result.feasible_quantity % 25 == 0
    assert result.maximum_feasible_quantity % 25 == 0


def test_case20_liquidity_constraint_binds_before_capital(cost_engine, cost_models_module, config):
    entry_price = Decimal("10")
    state = build_capital_state(Decimal("1000000"), Decimal("1000000"), Decimal("0"), config)
    exposure = AssetExposure(symbol="TESTSTOCK", existing_value=Decimal("0"))
    liquidity = LiquidityContext(average_daily_traded_value=Decimal("5000"))  # thin stock
    result = _run(
        cost_engine, cost_models_module, config, state, exposure,
        entry_price=entry_price, qty=1000, liquidity=liquidity,
    )
    assert result.feasibility_status == FeasibilityStatus.PARTIALLY_FEASIBLE
    from capital_feasibility.enums import BlockingConstraint
    assert BlockingConstraint.LIQUIDITY_PARTICIPATION_LIMIT in result.blocking_constraints
