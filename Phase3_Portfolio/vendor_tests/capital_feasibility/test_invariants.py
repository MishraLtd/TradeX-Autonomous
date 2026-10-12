"""
Property-style invariants (spec §33), checked across a spread of
randomized-but-seeded inputs rather than a single example each.
"""
import random
from decimal import Decimal
from functools import partial

import pytest

from capital_feasibility.adapters.cost_adapter import price_buy_leg
from capital_feasibility.capital_state import build_capital_state
from capital_feasibility.config import CapitalFeasibilityConfig
from capital_feasibility.engine import evaluate_trade
from capital_feasibility.models import AssetExposure, TradeCapitalRequest


def _make_case(rng, cost_engine, cost_models_module):
    config = CapitalFeasibilityConfig()
    total_capital = Decimal(rng.randint(200, 20000))
    cash = Decimal(rng.randint(0, int(total_capital)))
    invested = total_capital - cash
    state = build_capital_state(total_capital, cash, invested, config)
    exposure = AssetExposure(symbol="TESTSTOCK", existing_value=Decimal(rng.randint(0, int(invested))) if invested > 0 else Decimal("0"))
    entry_price = Decimal(rng.randint(5, 2000))
    requested_qty = rng.randint(0, 500)

    request = TradeCapitalRequest(
        position_id="p1", opportunity_id="o1", symbol="TESTSTOCK", exchange="NSE",
        sector="TECHNOLOGY", trade_type=rng.choice(["DELIVERY", "INTRADAY"]),
        entry_price=entry_price, requested_quantity=requested_qty,
    )
    price_fn = partial(price_buy_leg, cost_engine, cost_models_module, "TESTSTOCK", "NSE", request.trade_type, entry_price)
    result = evaluate_trade(request, state, exposure, price_fn, config)
    return state, config, result


def test_invariant_feasible_quantity_never_exceeds_requested(cost_engine, cost_models_module):
    rng = random.Random(42)
    for _ in range(60):
        _, _, result = _make_case(rng, cost_engine, cost_models_module)
        assert result.feasible_quantity <= result.requested_quantity


def test_invariant_required_capital_at_feasible_qty_fits_deployable_when_feasible(cost_engine, cost_models_module):
    rng = random.Random(43)
    for _ in range(60):
        state, config, result = _make_case(rng, cost_engine, cost_models_module)
        if result.feasible_quantity > 0:
            assert result.required_capital <= state.deployable_capital


def test_invariant_remaining_capital_respects_reserve_when_feasible(cost_engine, cost_models_module):
    rng = random.Random(44)
    for _ in range(60):
        state, config, result = _make_case(rng, cost_engine, cost_models_module)
        if result.capital_feasible and result.feasible_quantity > 0:
            # cash - required_capital - reserve should be >= 0 at the
            # accepted quantity (Gate 2's exact condition).
            assert result.capital_reserve_remaining >= Decimal("0") - Decimal("0.01")


def test_invariant_max_feasible_quantity_monotonic_in_available_capital(cost_engine, cost_models_module, config):
    entry_price = Decimal("50")
    exposure = AssetExposure(symbol="TESTSTOCK", existing_value=Decimal("0"))
    request = TradeCapitalRequest(
        position_id="p1", opportunity_id="o1", symbol="TESTSTOCK", exchange="NSE",
        sector="TECHNOLOGY", trade_type="DELIVERY", entry_price=entry_price, requested_quantity=10_000,
    )
    price_fn = partial(price_buy_leg, cost_engine, cost_models_module, "TESTSTOCK", "NSE", "DELIVERY", entry_price)

    prior_max = -1
    for cash in (Decimal("0"), Decimal("500"), Decimal("1000"), Decimal("5000"), Decimal("20000")):
        state = build_capital_state(cash if cash > 0 else Decimal("1"), cash, Decimal("0"), config)
        result = evaluate_trade(request, state, exposure, price_fn, config)
        assert result.maximum_feasible_quantity >= prior_max
        prior_max = result.maximum_feasible_quantity


def test_invariant_status_and_boolean_agree(cost_engine, cost_models_module):
    from capital_feasibility.enums import FeasibilityStatus
    rng = random.Random(45)
    for _ in range(60):
        _, _, result = _make_case(rng, cost_engine, cost_models_module)
        expected_feasible = result.feasibility_status in (
            FeasibilityStatus.FEASIBLE, FeasibilityStatus.PARTIALLY_FEASIBLE
        )
        assert result.capital_feasible == expected_feasible


def test_invariant_never_raises_on_random_valid_inputs(cost_engine, cost_models_module):
    rng = random.Random(46)
    for _ in range(100):
        _make_case(rng, cost_engine, cost_models_module)  # must not raise
