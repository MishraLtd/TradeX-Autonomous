"""
Phase 3 against the REAL Cost Model.

Everything here runs through `cost_model.engine.CostEngine` rather than
the approximate stand-in, and covers the two things that could only be
asserted, not demonstrated, while the Cost Model was absent:

  1. the monotonicity assumption capital_feasibility's max-quantity
     binary search is built on, checked against real charges rather than
     an approximation that was monotone by construction;

  2. the `capital_feasibility.api.evaluate()` bypass — that the shipped
     public API cannot see within-batch allocations, which is why the
     pipeline calls the pure core with its own ledger-aware exposure.

Skipped automatically when cost_model is not on the path.
"""
from decimal import Decimal

import pytest

from phase3_factories import (
    make_candidate,
    make_execution_input,
    make_portfolio_state,
    make_request,
)
from phase3 import (
    AllocationStatus,
    ApproximateZerodhaCostProvider,
    Phase3Config,
    Phase3Engine,
    Phase3Status,
)
from phase3 import adapters
from phase3.costs import CostModelProvider

try:
    from cost_model import models as cost_models
    from cost_model.engine import CostEngine
    HAVE_COST_MODEL = True
except ImportError:                                  # pragma: no cover
    HAVE_COST_MODEL = False


def real_provider():
    if not HAVE_COST_MODEL:
        pytest.skip("cost_model package not installed")
    return CostModelProvider(CostEngine(), cost_models)


def real_engine(config=None):
    return Phase3Engine(cost_provider=real_provider(), config=config)


# --------------------------------------------------------------------------- #
# End to end on real charges
# --------------------------------------------------------------------------- #

def test_pipeline_runs_end_to_end_on_the_real_cost_model():
    result = real_engine().run(make_request())

    assert result.status is Phase3Status.OK
    assert len(result.instructions) == 1

    instruction = result.instructions[0]
    assert instruction.quantity > 0
    assert instruction.estimated_entry_cost > 0          # real charges, never zero
    assert instruction.required_capital == (
        instruction.position_value + instruction.estimated_entry_cost
    )


def test_instruction_cost_matches_a_fresh_pricing_of_the_same_trade():
    """The cost on the instruction must be reproducible: re-pricing the
    final quantity with the SAME liquidity context has to give the same
    number. The liquidity context is part of the input, not a hint — see
    test_missing_liquidity_data_prices_more_conservatively below."""
    from capital_feasibility.models import LiquidityContext

    provider = real_provider()
    request = make_request()
    result = Phase3Engine(cost_provider=provider).run(request)
    instruction = result.instructions[0]
    exec_input = request.execution_inputs[instruction.opportunity_id]

    repriced = provider.price_buy_leg(
        symbol=instruction.symbol,
        exchange=instruction.exchange,
        trade_type=instruction.trade_type,
        entry_price=instruction.entry_price,
        quantity=instruction.quantity,
        liquidity=LiquidityContext(
            average_daily_traded_value=exec_input.average_traded_value,
            average_daily_volume=int(exec_input.average_volume),
        ),
    )

    assert repriced.position_value == instruction.position_value
    assert repriced.execution_cost == instruction.estimated_entry_cost
    assert repriced.cost_model_version is not None       # real schedule version, not APPROX-


def test_missing_liquidity_data_prices_more_conservatively():
    """The real Cost Model widens its spread/impact assumption when it has
    no liquidity snapshot. That makes the liquidity context a required
    input rather than an optional enrichment: a trade priced without it
    looks more expensive and can be rejected on capital grounds it would
    actually clear. The pipeline therefore passes the snapshot on every
    pricing call, including the final one that stamps the instruction."""
    from capital_feasibility.models import LiquidityContext

    provider = real_provider()
    liquid = LiquidityContext(
        average_daily_traded_value=Decimal("500000000"), average_daily_volume=300000
    )

    with_data = provider.price_buy_leg("SYMA", "NSE", "DELIVERY", Decimal("50.00"), 7,
                                        liquidity=liquid)
    without_data = provider.price_buy_leg("SYMA", "NSE", "DELIVERY", Decimal("50.00"), 7)

    assert without_data.execution_cost > with_data.execution_cost


def test_batch_capital_invariant_holds_with_real_costs():
    candidates = [make_candidate(f"OPP-{i}", f"SYM{i}", sector=f"SECTOR{i}") for i in range(1, 6)]
    result = real_engine().run(make_request(candidates=candidates))

    total = sum(i.required_capital for i in result.instructions)
    assert total == result.capital.capital_allocated
    assert total <= result.capital.deployable_at_start


def test_real_costs_exceed_the_approximate_stand_in():
    """Sanity check on the stand-in's honesty: it is labelled approximate,
    and it should not be quietly cheaper in a way that would have let a
    trade look affordable that the real schedule rejects."""
    real = real_provider()
    approx = ApproximateZerodhaCostProvider()

    for qty in (1, 5, 25, 100):
        r = real.price_buy_leg("SYMA", "NSE", "DELIVERY", Decimal("62.40"), qty)
        a = approx.price_buy_leg("SYMA", "NSE", "DELIVERY", Decimal("62.40"), qty)
        assert r.position_value == a.position_value
        assert r.execution_cost >= a.execution_cost


# --------------------------------------------------------------------------- #
# The monotonicity assumption, on real charges
# --------------------------------------------------------------------------- #

def test_real_required_capital_is_monotonic_in_quantity():
    """capital_feasibility/max_quantity.py binary-searches over quantity
    and documents this assumption (spec §33). The fixed components of the
    real schedule — the intraday brokerage cap especially — are exactly
    where it could break, so it is checked here against real numbers."""
    provider = real_provider()

    for trade_type, price in (("DELIVERY", Decimal("37.50")), ("INTRADAY", Decimal("37.50"))):
        previous = Decimal("-1")
        for qty in range(1, 400):
            requirement = provider.price_buy_leg("SYMA", "NSE", trade_type, price, qty)
            assert requirement.required_capital > previous, (
                f"{trade_type} required_capital fell at qty={qty}"
            )
            previous = requirement.required_capital


def test_real_cost_failure_abandons_the_batch():
    provider = real_provider()
    candidates = [make_candidate("OPP-1", "SYMA", sector="TECH", exchange="MCX")]
    result = Phase3Engine(cost_provider=provider).run(make_request(candidates=candidates))

    assert result.status is Phase3Status.PIPELINE_ERROR
    assert result.instructions == []


# --------------------------------------------------------------------------- #
# The api.evaluate() bypass, demonstrated
# --------------------------------------------------------------------------- #

def _sizing_request_for(candidate, exec_input, capital):
    from phase3_factories import make_context
    return adapters.sizing_request_from_candidate(
        candidate=candidate,
        exec_input=exec_input,
        position_id="pos-x",
        capital_available=capital,
        market_context=make_context(),
        config=Phase3Config(),
    )


def test_shipped_api_cannot_see_within_batch_exposure():
    """The concrete case behind the bypass.

    Two candidates in the same sector. Trade 1 is allocated but not yet
    filled, so it is not in PortfolioState.open_positions. Evaluating
    trade 2 the way `capital_feasibility.api.evaluate()` does — exposure
    read from open_positions alone — sees an empty sector. The pipeline's
    ledger-aware exposure sees trade 1's value, and the sector gate binds.
    """
    if not HAVE_COST_MODEL:
        pytest.skip("cost_model package not installed")

    from capital_feasibility.adapters.portfolio_adapter import asset_exposure_from_portfolio
    from capital_feasibility.capital_state import build_capital_state
    from capital_feasibility.engine import evaluate_trade

    config = Phase3Config()
    provider = real_provider()
    portfolio = make_portfolio_state(cash=1000.0, total_equity=1000.0)

    candidate = make_candidate("OPP-2", "SYMB", sector="TECH")
    exec_input = make_execution_input("OPP-2", entry="50.00", stop="49.00", target="56.00")
    sizing_request = _sizing_request_for(candidate, exec_input, Decimal("950"))

    # Trade 1: 7 x SYMA @ 50 = Rs 350 of TECH exposure, allocated earlier
    # in this same batch and not yet reflected in the portfolio.
    already_allocated = Decimal("350")

    account_state = build_capital_state(
        total_capital=Decimal("1000"),
        cash=Decimal("1000"),
        invested_capital=Decimal("0"),
        config=config.feasibility,
        committed_capital=already_allocated,
    )

    request = adapters.trade_capital_request(sizing_request, 7, "TECH", exec_input)

    def price_fn(qty):
        return provider.price_buy_leg(
            symbol=request.symbol, exchange=request.exchange,
            trade_type=request.trade_type, entry_price=request.entry_price,
            quantity=qty, liquidity=request.liquidity,
        )

    # What the shipped API sees: exposure from open_positions only.
    api_style_exposure = asset_exposure_from_portfolio(portfolio, "SYMB", "TECH")
    api_style = evaluate_trade(request, account_state, api_style_exposure, price_fn, config.feasibility)

    # What the pipeline sees: the same, plus this batch's allocations.
    ledger_exposure = adapters.asset_exposure(
        portfolio, "SYMB", "TECH",
        extra_symbol_value=Decimal("0"),
        extra_sector_value=already_allocated,
    )
    ledger_style = evaluate_trade(request, account_state, ledger_exposure, price_fn, config.feasibility)

    assert api_style_exposure.existing_sector_value == Decimal("0")
    assert ledger_exposure.existing_sector_value == already_allocated

    # The ledger-aware evaluation is strictly more constrained.
    assert ledger_style.maximum_feasible_quantity < api_style.maximum_feasible_quantity
    assert ledger_style.post_trade_sector_exposure_ratio > api_style.post_trade_sector_exposure_ratio


def test_pipeline_keeps_a_same_sector_batch_inside_the_sector_limit():
    """The same failure mode, through the whole pipeline: several
    candidates in one sector must not each pass the sector gate
    independently and leave the book over the limit."""
    config = Phase3Config()
    config.apply_risk_position_cap = False       # isolate the capital-side gate

    candidates = [make_candidate(f"OPP-{i}", f"SYM{i}", sector="TECH") for i in range(1, 6)]
    execution_inputs = {
        f"OPP-{i}": make_execution_input(f"OPP-{i}", entry="50.00", stop="49.00",
                                         target="56.00", seed=100 + i)
        for i in range(1, 6)
    }

    result = real_engine(config).run(
        make_request(candidates=candidates, execution_inputs=execution_inputs)
    )

    tech_value = sum(i.position_value for i in result.instructions if i.sector == "TECH")
    limit = config.feasibility.max_sector_allocation_ratio * Decimal("1000")
    assert tech_value <= limit, f"sector exposure {tech_value} exceeded limit {limit}"


def test_allocated_reduced_is_attributed_when_the_sector_gate_binds():
    """A trade cut by the within-batch sector gate must be reported as
    reduced by CAPITAL_FEASIBILITY, not silently shrunk."""
    config = Phase3Config()
    config.apply_risk_position_cap = False

    candidates = [make_candidate(f"OPP-{i}", f"SYM{i}", sector="TECH") for i in range(1, 6)]
    execution_inputs = {
        f"OPP-{i}": make_execution_input(f"OPP-{i}", entry="50.00", stop="49.00",
                                         target="56.00", seed=200 + i)
        for i in range(1, 6)
    }

    result = real_engine(config).run(
        make_request(candidates=candidates, execution_inputs=execution_inputs)
    )

    for outcome in result.outcomes:
        if outcome.instruction is None:
            continue
        if outcome.final_quantity < outcome.requested_quantity:
            assert outcome.instruction.allocation_status is AllocationStatus.ALLOCATED_REDUCED
            assert outcome.instruction.reduced_by_stage is not None
