from decimal import Decimal
from functools import partial

import pytest

from capital_feasibility.adapters.cost_adapter import price_buy_leg
from capital_feasibility.capital_state import build_capital_state
from capital_feasibility.config import CapitalFeasibilityConfig
from capital_feasibility.max_quantity import find_max_feasible_quantity
from capital_feasibility.models import AssetExposure, LiquidityContext


def _price_fn(cost_engine, cost_models_module, entry_price, exchange="NSE", trade_type="DELIVERY"):
    return partial(
        price_buy_leg,
        cost_engine,
        cost_models_module,
        "TESTSTOCK",
        exchange,
        trade_type,
        entry_price,
    )


def test_max_quantity_matches_naive_cash_estimate_when_unconstrained(cost_engine, cost_models_module):
    # Allocation ratios deliberately widened to 1.0 so the ONLY binding
    # constraint left is raw cash/reserve — isolates the cash-side search.
    config = CapitalFeasibilityConfig(
        minimum_cash_reserve=Decimal("0"),
        minimum_free_capital_ratio=Decimal("0"),
        max_trade_capital_ratio=Decimal("1.0"),
        max_asset_allocation_ratio=Decimal("1.0"),
        max_sector_allocation_ratio=Decimal("1.0"),
        max_portfolio_allocation_ratio=Decimal("1.0"),
    )
    entry_price = Decimal("100")
    state = build_capital_state(Decimal("100000"), Decimal("100000"), Decimal("0"), config)
    exposure = AssetExposure(symbol="TESTSTOCK", existing_value=Decimal("0"))

    max_qty, requirement, gates = find_max_feasible_quantity(
        price_fn=_price_fn(cost_engine, cost_models_module, entry_price),
        state=state,
        exposure=exposure,
        entry_price=entry_price,
        liquidity=LiquidityContext(),
        config=config,
    )

    assert max_qty > 0
    assert requirement.quantity == max_qty
    assert all(g.passed for g in gates)
    # One more share must NOT be affordable (tight upper edge, spec §13).
    over = price_buy_leg(cost_engine, cost_models_module, "TESTSTOCK", "NSE", "DELIVERY", entry_price, max_qty + 1)
    assert over.required_capital > state.deployable_capital


def test_max_quantity_is_monotonic_in_available_capital(cost_engine, cost_models_module, config):
    entry_price = Decimal("50")
    exposure = AssetExposure(symbol="TESTSTOCK", existing_value=Decimal("0"))

    smaller_state = build_capital_state(Decimal("1000"), Decimal("1000"), Decimal("0"), config)
    larger_state = build_capital_state(Decimal("5000"), Decimal("5000"), Decimal("0"), config)

    max_qty_small, _, _ = find_max_feasible_quantity(
        price_fn=_price_fn(cost_engine, cost_models_module, entry_price),
        state=smaller_state,
        exposure=exposure,
        entry_price=entry_price,
        liquidity=LiquidityContext(),
        config=config,
    )
    max_qty_large, _, _ = find_max_feasible_quantity(
        price_fn=_price_fn(cost_engine, cost_models_module, entry_price),
        state=larger_state,
        exposure=exposure,
        entry_price=entry_price,
        liquidity=LiquidityContext(),
        config=config,
    )
    assert max_qty_large >= max_qty_small


def test_max_quantity_zero_when_no_deployable_capital(cost_engine, cost_models_module, config):
    entry_price = Decimal("100")
    state = build_capital_state(Decimal("1000"), Decimal("0"), Decimal("1000"), config)
    exposure = AssetExposure(symbol="TESTSTOCK", existing_value=Decimal("0"))

    max_qty, requirement, gates = find_max_feasible_quantity(
        price_fn=_price_fn(cost_engine, cost_models_module, entry_price),
        state=state,
        exposure=exposure,
        entry_price=entry_price,
        liquidity=LiquidityContext(),
        config=config,
    )
    assert max_qty == 0
    assert requirement.quantity == 0
    assert requirement.required_capital == Decimal("0")


def test_max_quantity_respects_lot_size(cost_engine, cost_models_module, config):
    entry_price = Decimal("100")
    state = build_capital_state(Decimal("1000"), Decimal("1000"), Decimal("0"), config)
    exposure = AssetExposure(symbol="TESTSTOCK", existing_value=Decimal("0"))

    max_qty, _, _ = find_max_feasible_quantity(
        price_fn=_price_fn(cost_engine, cost_models_module, entry_price),
        state=state,
        exposure=exposure,
        entry_price=entry_price,
        liquidity=LiquidityContext(),
        config=config,
        lot_size=7,
    )
    assert max_qty % 7 == 0


def test_max_quantity_capped_by_liquidity_participation_limit(cost_engine, cost_models_module, config):
    entry_price = Decimal("10")
    state = build_capital_state(Decimal("1000000"), Decimal("1000000"), Decimal("0"), config)
    exposure = AssetExposure(symbol="TESTSTOCK", existing_value=Decimal("0"))
    liquidity = LiquidityContext(average_daily_traded_value=Decimal("10000"))
    # 2% participation limit of ₹10,000 = ₹200 -> at ₹10/share, max 20 shares
    max_qty, requirement, gates = find_max_feasible_quantity(
        price_fn=_price_fn(cost_engine, cost_models_module, entry_price),
        state=state,
        exposure=exposure,
        entry_price=entry_price,
        liquidity=liquidity,
        config=config,
    )
    assert max_qty <= 20
    assert all(g.passed for g in gates)


def test_max_quantity_capped_by_asset_allocation_limit(cost_engine, cost_models_module, config):
    entry_price = Decimal("10")
    total_capital = Decimal("1000")
    state = build_capital_state(total_capital, total_capital, Decimal("0"), config)
    # max_asset_allocation_ratio default 0.40 -> ₹400 ceiling on this symbol
    exposure = AssetExposure(symbol="TESTSTOCK", existing_value=Decimal("0"))

    max_qty, requirement, _ = find_max_feasible_quantity(
        price_fn=_price_fn(cost_engine, cost_models_module, entry_price),
        state=state,
        exposure=exposure,
        entry_price=entry_price,
        liquidity=LiquidityContext(),
        config=config,
    )
    assert requirement.position_value <= config.max_asset_allocation_ratio * total_capital
