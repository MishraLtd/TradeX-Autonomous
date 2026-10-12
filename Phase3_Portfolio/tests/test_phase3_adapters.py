"""Unit tests for the integration layer's own logic: type bridges,
regime translation, the fallback cost provider, the ledger, and the
serialization projection."""
import json
from decimal import Decimal

import pytest

from capital_feasibility.exceptions import CostModelIntegrationError
from phase3_factories import (
    make_candidate,
    make_context,
    make_execution_input,
    make_open_position,
    make_portfolio_state,
    make_request,
)
from phase3 import (
    ApproximateZerodhaCostProvider,
    Phase3Config,
    Phase3Engine,
    result_to_dict,
)
from phase3 import adapters
from phase3.ledger import AllocationLedger
from phase3.regime import to_risk_regime, to_sizing_regime
from portfolio_manager.enums import MarketRegime
from position_sizing.enums import MarketRegime as SizingRegime


# --------------------------------------------------------------------------- #
# Regime bridge
# --------------------------------------------------------------------------- #

def test_trending_needs_confidence_before_it_becomes_directional():
    config = Phase3Config()
    assert to_sizing_regime(MarketRegime.TRENDING, 0.9, config) is SizingRegime.TRENDING_UP
    assert to_sizing_regime(MarketRegime.TRENDING, 0.2, config) is SizingRegime.SIDEWAYS
    assert to_sizing_regime(MarketRegime.TRENDING, None, config) is SizingRegime.SIDEWAYS


def test_volatile_and_unknown_map_conservatively():
    config = Phase3Config()
    assert to_sizing_regime(MarketRegime.VOLATILE, 0.9, config) is SizingRegime.HIGH_VOLATILITY
    assert to_sizing_regime(MarketRegime.UNKNOWN, 0.9, config) is SizingRegime.UNKNOWN
    assert to_risk_regime(MarketRegime.VOLATILE, config) == "HIGH_VOLATILITY"
    assert to_risk_regime("SOMETHING_NEW", config) == "UNKNOWN"


# --------------------------------------------------------------------------- #
# Type bridges
# --------------------------------------------------------------------------- #

def test_sizing_request_uses_decimal_money_and_per_share_edges():
    candidate = make_candidate()
    exec_input = make_execution_input("OPP-1", entry="100.00", stop="95.00", target="112.00")
    request = adapters.sizing_request_from_candidate(
        candidate=candidate,
        exec_input=exec_input,
        position_id="pos-1",
        capital_available=Decimal("950.00"),
        market_context=make_context(),
        config=Phase3Config(),
    )

    assert isinstance(request.capital_available_for_sizing, Decimal)
    assert request.expected_upside == Decimal("12.00")   # target - entry, per share
    assert request.expected_downside == Decimal("5.00")  # entry - stop, per share
    assert request.probability_of_loss == Decimal("1") - request.probability_of_profit


def test_per_trade_cost_basis_refuses_to_invent_a_per_share_number():
    config = Phase3Config(candidate_cost_basis="PER_TRADE")
    request = adapters.sizing_request_from_candidate(
        candidate=make_candidate(),
        exec_input=make_execution_input(),
        position_id="pos-1",
        capital_available=Decimal("1000"),
        market_context=make_context(),
        config=config,
    )
    assert request.expected_total_cost is None


def test_explicit_cost_override_wins():
    request = adapters.sizing_request_from_candidate(
        candidate=make_candidate(),
        exec_input=make_execution_input(expected_cost_per_share=Decimal("0.93")),
        position_id="pos-1",
        capital_available=Decimal("1000"),
        market_context=make_context(),
        config=Phase3Config(),
    )
    assert request.expected_total_cost == Decimal("0.93")


def test_proposed_risk_position_is_marked_and_unmarked_at_the_right_time():
    proposed = adapters.proposed_risk_position(make_candidate(), make_execution_input(), 10)
    assert proposed.is_proposed is True
    # A proposed trade has no mark yet: no fabricated unrealized P&L.
    assert proposed.entry_price == proposed.current_price
    assert proposed.unrealized_pnl == 0.0


def test_exposure_includes_within_batch_allocations():
    state = make_portfolio_state(positions=[make_open_position(symbol="SYMA", sector="TECH", quantity=2,
                                                              current_price=50.0)])
    exposure = adapters.asset_exposure(
        state, "SYMA", "TECH",
        extra_symbol_value=Decimal("100"),
        extra_sector_value=Decimal("250"),
    )
    assert exposure.existing_value == Decimal("200")          # 2 x 50 already open + 100 this batch
    assert exposure.existing_sector_value == Decimal("350")


# --------------------------------------------------------------------------- #
# Ledger
# --------------------------------------------------------------------------- #

def test_ledger_tracks_capital_and_exposure_separately():
    config = Phase3Config()
    state = make_portfolio_state(cash=1000.0, total_equity=1000.0)
    ledger = AllocationLedger(portfolio_state=state, config=config,
                              initial_committed_capital=Decimal("0"))

    before = ledger.account_state().deployable_capital
    ledger.commit(
        symbol="SYMA", sector="TECH",
        position_value=Decimal("300"), required_capital=Decimal("300.45"),
        risk_position=adapters.proposed_risk_position(make_candidate(), make_execution_input(), 6),
    )
    after = ledger.account_state().deployable_capital

    assert before - after == Decimal("300.45")           # cash moves by required capital
    assert ledger.extra_symbol_value("SYMA") == Decimal("300")   # exposure by position value
    assert ledger.extra_sector_value("TECH") == Decimal("300")
    assert len(ledger.accepted_positions) == 1


def test_unknown_committed_capital_flows_through_to_the_capital_state():
    ledger = AllocationLedger(portfolio_state=make_portfolio_state(), config=Phase3Config(),
                              initial_committed_capital=None)
    assert ledger.account_state().committed_capital_is_known is False


# --------------------------------------------------------------------------- #
# Fallback cost provider
# --------------------------------------------------------------------------- #

def test_delivery_buy_leg_carries_stt_intraday_does_not():
    provider = ApproximateZerodhaCostProvider(slippage_bps=Decimal("0"), impact_coefficient=Decimal("0"))
    delivery = provider.price_buy_leg("SYMA", "NSE", "DELIVERY", Decimal("100"), 10)
    intraday = provider.price_buy_leg("SYMA", "NSE", "INTRADAY", Decimal("100"), 10)

    assert delivery.position_value == Decimal("1000.00")
    assert delivery.execution_cost > intraday.execution_cost
    assert delivery.required_capital == delivery.position_value + delivery.execution_cost


def test_intraday_brokerage_is_capped():
    provider = ApproximateZerodhaCostProvider(slippage_bps=Decimal("0"), impact_coefficient=Decimal("0"))
    big = provider.price_buy_leg("SYMA", "NSE", "INTRADAY", Decimal("1000"), 1000)  # Rs 10,00,000
    # 0.03% of 10 lakh is Rs 300, so the Rs 20 cap must bind.
    assert big.execution_cost < Decimal("300")


def test_cost_provider_fails_closed_on_bad_input():
    provider = ApproximateZerodhaCostProvider()
    with pytest.raises(CostModelIntegrationError):
        provider.price_buy_leg("SYMA", "NSE", "DELIVERY", Decimal("0"), 10)
    with pytest.raises(CostModelIntegrationError):
        provider.price_buy_leg("SYMA", "MCX", "DELIVERY", Decimal("100"), 10)
    with pytest.raises(CostModelIntegrationError):
        provider.price_buy_leg("SYMA", "NSE", "SWING", Decimal("100"), 10)


def test_zero_quantity_prices_to_zero_without_raising():
    requirement = ApproximateZerodhaCostProvider().price_buy_leg(
        "SYMA", "NSE", "DELIVERY", Decimal("100"), 0
    )
    assert requirement.required_capital == Decimal("0")


def test_cost_is_monotonic_in_quantity():
    """capital_feasibility's max-quantity binary search depends on this."""
    provider = ApproximateZerodhaCostProvider()
    previous = Decimal("-1")
    for qty in range(1, 40):
        requirement = provider.price_buy_leg("SYMA", "NSE", "DELIVERY", Decimal("37.5"), qty)
        assert requirement.required_capital > previous
        previous = requirement.required_capital


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #

def test_result_serializes_to_json():
    engine = Phase3Engine(cost_provider=ApproximateZerodhaCostProvider())
    result = engine.run(make_request())
    payload = result_to_dict(result)

    encoded = json.dumps(payload)          # must not raise on Decimal/Enum/datetime
    assert "instructions" in json.loads(encoded)
    assert payload["risk_summary"]["status_before"]


def test_full_risk_reports_are_opt_in():
    engine = Phase3Engine(cost_provider=ApproximateZerodhaCostProvider())
    result = engine.run(make_request())

    assert "risk_before" not in result_to_dict(result)
    assert "risk_before" in result_to_dict(result, include_risk_reports=True)
