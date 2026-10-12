"""
Integration tests (spec §32):
  Position Sizing -> Capital Feasibility
  Portfolio Manager -> Capital Feasibility
  Cost Model -> Capital Feasibility
using the REAL classes from all three uploaded packages, not mocks.
"""
from decimal import Decimal
from functools import partial

import pytest

from capital_feasibility import api
from capital_feasibility.adapters.cost_adapter import price_buy_leg
from capital_feasibility.adapters.portfolio_adapter import (
    account_state_from_portfolio,
    asset_exposure_from_portfolio,
)
from capital_feasibility.adapters.position_sizing_adapter import (
    to_position_sizing_constraint,
    trade_request_from_position_sizing,
)
from capital_feasibility.config import CapitalFeasibilityConfig
from capital_feasibility.enums import FeasibilityStatus
from capital_feasibility.engine import evaluate_trade

from position_sizing.models import CapitalFeasibilityConstraint

from factories import make_portfolio_position, make_portfolio_state, make_sizing_request, make_sizing_result


def test_full_pipeline_feasible_small_account(cost_engine, cost_models_module, config):
    sizing_request = make_sizing_request(
        entry_price=Decimal("100.00"), capital_available_for_sizing=Decimal("1000.00")
    )
    sizing_result = make_sizing_result(recommended_quantity=3, symbol="TESTSTOCK")
    portfolio_state = make_portfolio_state(total_equity=1000.0, cash=1000.0, invested_capital=0.0)

    result = api.evaluate(
        sizing_request=sizing_request,
        sizing_result=sizing_result,
        portfolio_state=portfolio_state,
        cost_engine=cost_engine,
        cost_models_module=cost_models_module,
        sector="TECHNOLOGY",
        config=config,
    )

    assert result.feasibility_status == FeasibilityStatus.FEASIBLE
    assert result.capital_feasible is True
    assert result.feasible_quantity == 3
    assert result.requested_quantity == 3
    assert result.required_capital > result.requested_position_value  # costs are added, never absorbed silently


def test_full_pipeline_reduces_oversized_request(cost_engine, cost_models_module, config):
    sizing_request = make_sizing_request(
        entry_price=Decimal("100.00"), capital_available_for_sizing=Decimal("1000.00")
    )
    # Position Sizing itself would normally cap this via its own capital
    # cap, but simulate a caller that skipped that (e.g. a stale
    # capital_available_for_sizing) to prove Capital Feasibility is the
    # real backstop, not merely a rubber stamp on Position Sizing's math.
    sizing_result = make_sizing_result(recommended_quantity=50, symbol="TESTSTOCK")
    portfolio_state = make_portfolio_state(total_equity=1000.0, cash=1000.0, invested_capital=0.0)

    result = api.evaluate(
        sizing_request=sizing_request,
        sizing_result=sizing_result,
        portfolio_state=portfolio_state,
        cost_engine=cost_engine,
        cost_models_module=cost_models_module,
        sector="TECHNOLOGY",
        config=config,
    )

    assert result.feasibility_status == FeasibilityStatus.PARTIALLY_FEASIBLE
    assert 0 < result.feasible_quantity < result.requested_quantity
    assert result.feasible_quantity <= result.maximum_feasible_quantity


def test_full_pipeline_blocked_by_existing_sector_concentration(cost_engine, cost_models_module):
    config = CapitalFeasibilityConfig(max_sector_allocation_ratio=Decimal("0.30"))
    sizing_request = make_sizing_request(entry_price=Decimal("100.00"))
    sizing_result = make_sizing_result(recommended_quantity=5, symbol="TESTSTOCK")

    # ₹280 already in the TECHNOLOGY sector out of ₹1000 total capital;
    # sector cap is 30% = ₹300, leaving only ₹20 of headroom (<1 share).
    existing_position = make_portfolio_position(
        symbol="OTHERTECH", sector="TECHNOLOGY", entry_price=280.0, current_price=280.0, quantity=1
    )
    portfolio_state = make_portfolio_state(
        total_equity=1000.0, cash=720.0, invested_capital=280.0, open_positions=[existing_position]
    )

    result = api.evaluate(
        sizing_request=sizing_request,
        sizing_result=sizing_result,
        portfolio_state=portfolio_state,
        cost_engine=cost_engine,
        cost_models_module=cost_models_module,
        sector="TECHNOLOGY",
        config=config,
    )

    assert result.feasible_quantity == 0
    assert result.feasibility_status == FeasibilityStatus.PORTFOLIO_CONSTRAINED
    from capital_feasibility.enums import BlockingConstraint
    assert BlockingConstraint.MAX_SECTOR_ALLOCATION in result.blocking_constraints


def test_full_pipeline_blocked_by_cash_reserve(cost_engine, cost_models_module):
    config = CapitalFeasibilityConfig(minimum_cash_reserve=Decimal("900"), minimum_free_capital_ratio=Decimal("0"))
    sizing_request = make_sizing_request(entry_price=Decimal("100.00"))
    sizing_result = make_sizing_result(recommended_quantity=2, symbol="TESTSTOCK")
    portfolio_state = make_portfolio_state(total_equity=1000.0, cash=1000.0, invested_capital=0.0)

    result = api.evaluate(
        sizing_request=sizing_request,
        sizing_result=sizing_result,
        portfolio_state=portfolio_state,
        cost_engine=cost_engine,
        cost_models_module=cost_models_module,
        sector="TECHNOLOGY",
        config=config,
    )

    assert result.feasible_quantity == 0
    assert result.feasibility_status == FeasibilityStatus.CAPITAL_CONSTRAINED


def test_result_round_trips_into_position_sizing_constraint(cost_engine, cost_models_module, config):
    sizing_request = make_sizing_request(entry_price=Decimal("100.00"))
    sizing_result = make_sizing_result(recommended_quantity=3, symbol="TESTSTOCK")
    portfolio_state = make_portfolio_state(total_equity=1000.0, cash=1000.0, invested_capital=0.0)

    result = api.evaluate(
        sizing_request=sizing_request,
        sizing_result=sizing_result,
        portfolio_state=portfolio_state,
        cost_engine=cost_engine,
        cost_models_module=cost_models_module,
        sector="TECHNOLOGY",
        config=config,
    )

    constraint_kwargs = to_position_sizing_constraint(result)
    # Must construct position_sizing's REAL frozen dataclass without error —
    # proves the two packages' interfaces genuinely line up field-for-field.
    constraint = CapitalFeasibilityConstraint(**constraint_kwargs)
    assert constraint.max_affordable_quantity == result.max_affordable_quantity
    assert constraint.available_capital == result.available_capital
