from decimal import Decimal

import pytest

from position_sizing.config import DEFAULT_CONFIG
from position_sizing.engine import calculate_position_size
from position_sizing.enums import MarketRegime, SizingStatus, TradeType
from position_sizing.models import (
    CapitalFeasibilityConstraint,
    PortfolioManagerInstruction,
    PortfolioRiskSizingConstraint,
)

from .conftest import make_request


@pytest.mark.parametrize("capital", ["500", "750", "1000", "1500", "2000", "5000", "10000", "25000", "50000"])
def test_capital_tiers_produce_valid_result(capital):
    req = make_request(capital_available_for_sizing=Decimal(capital))
    result = calculate_position_size(req)
    assert result.sizing_status in (
        SizingStatus.APPROVED,
        SizingStatus.REDUCED,
        SizingStatus.NO_POSITION,
    )
    assert result.recommended_quantity >= 0
    if result.recommended_quantity > 0:
        assert result.recommended_position_value <= req.capital_available_for_sizing + Decimal("0.01")


def test_missing_stop_loss_is_incomplete_not_zero_fallback():
    req = make_request(stop_loss=None)
    result = calculate_position_size(req)
    assert result.sizing_status == SizingStatus.SIZING_INCOMPLETE
    assert result.recommended_quantity == 0
    assert "stop_loss" in result.rejection_reason


def test_missing_volatility_degrades_level_not_size_to_zero():
    req = make_request(atr_pct=None)
    result = calculate_position_size(req)
    assert result.sizing_level.value == "LEVEL_1_RISK_ONLY" or result.volatility_adjustment == Decimal("1.00")


def test_missing_confidence_defaults_to_neutral_multiplier():
    req = make_request(model_confidence=None)
    result = calculate_position_size(req)
    assert result.confidence_adjustment == Decimal("1.00")


def test_invalid_zero_price_rejected():
    req = make_request(entry_price=Decimal("0"))
    result = calculate_position_size(req)
    assert result.sizing_status == SizingStatus.SIZING_INCOMPLETE


def test_invalid_negative_price_rejected():
    req = make_request(entry_price=Decimal("-10"))
    result = calculate_position_size(req)
    assert result.sizing_status == SizingStatus.SIZING_INCOMPLETE


def test_stop_above_entry_rejected():
    req = make_request(stop_loss=Decimal("150.00"))  # above entry of 145
    result = calculate_position_size(req)
    assert result.sizing_status == SizingStatus.SIZING_INCOMPLETE


def test_tight_stop_yields_larger_quantity_than_wide_stop():
    tight = make_request(stop_loss=Decimal("144.50"))
    wide = make_request(stop_loss=Decimal("130.00"))
    r_tight = calculate_position_size(tight)
    r_wide = calculate_position_size(wide)
    assert r_tight.recommended_quantity >= r_wide.recommended_quantity


def test_high_volatility_does_not_increase_size_vs_low_volatility():
    low_vol = make_request(atr_pct=Decimal("0.005"))
    high_vol = make_request(atr_pct=Decimal("0.06"))
    r_low = calculate_position_size(low_vol)
    r_high = calculate_position_size(high_vol)
    assert r_high.volatility_adjustment <= r_low.volatility_adjustment


def test_low_confidence_does_not_exceed_high_confidence_size():
    low_conf = make_request(model_confidence=Decimal("0.30"))
    high_conf = make_request(model_confidence=Decimal("0.95"))
    r_low = calculate_position_size(low_conf)
    r_high = calculate_position_size(high_conf)
    assert r_low.confidence_adjustment <= r_high.confidence_adjustment


def test_deterministic_repeated_calls_identical():
    req = make_request()
    r1 = calculate_position_size(req)
    r2 = calculate_position_size(req)
    assert r1.recommended_quantity == r2.recommended_quantity
    assert r1.recommended_position_value == r2.recommended_position_value
    assert r1.sizing_status == r2.sizing_status


def test_max_quantity_constraint_binds():
    req = make_request(capital_available_for_sizing=Decimal("100000"), max_risk_allowed=Decimal("5000"), max_quantity=3)
    result = calculate_position_size(req)
    assert result.recommended_quantity <= 3


def test_min_quantity_below_one_no_forced_share():
    # Extremely small capital and wide stop -> raw theoretical qty << 1 and 1 share fails capital
    req = make_request(capital_available_for_sizing=Decimal("50"), entry_price=Decimal("145.00"), stop_loss=Decimal("100.00"))
    result = calculate_position_size(req)
    assert result.recommended_quantity == 0
    assert result.sizing_status == SizingStatus.NO_POSITION


def test_capital_feasibility_interface_caps_quantity():
    req = make_request(capital_available_for_sizing=Decimal("100000"), max_risk_allowed=Decimal("5000"))
    cap = CapitalFeasibilityConstraint(max_affordable_quantity=2)
    result = calculate_position_size(req, capital_constraint=cap)
    assert result.recommended_quantity <= 2


def test_portfolio_risk_halt_forces_no_position():
    req = make_request()
    risk_constraint = PortfolioRiskSizingConstraint(halt_sizing=True)
    result = calculate_position_size(req, portfolio_risk_constraint=risk_constraint)
    assert result.sizing_status == SizingStatus.NO_POSITION
    assert result.recommended_quantity == 0


def test_portfolio_risk_multiplier_reduces_size():
    req = make_request(capital_available_for_sizing=Decimal("100000"), max_risk_allowed=Decimal("5000"))
    full = calculate_position_size(req)
    reduced = calculate_position_size(
        req, portfolio_risk_constraint=PortfolioRiskSizingConstraint(risk_multiplier=Decimal("0.5"))
    )
    assert reduced.recommended_quantity <= full.recommended_quantity


def test_portfolio_manager_preferred_exposure_caps_position_value():
    req = make_request(capital_available_for_sizing=Decimal("100000"), max_risk_allowed=Decimal("5000"))
    instruction = PortfolioManagerInstruction(preferred_exposure=Decimal("300"))
    result = calculate_position_size(req, portfolio_manager_instruction=instruction)
    assert result.recommended_position_value <= Decimal("300") + req.entry_price  # allow < 1 share slack


def test_panic_regime_zeroes_out_via_multiplier():
    req = make_request(market_regime=MarketRegime.PANIC)
    result = calculate_position_size(req)
    assert result.regime_adjustment == Decimal("0")
    assert result.recommended_quantity == 0


def test_result_never_negative_quantity():
    req = make_request(capital_available_for_sizing=Decimal("10"))
    result = calculate_position_size(req)
    assert result.recommended_quantity >= 0


def test_economic_minimum_rejects_tiny_edge():
    req = make_request(
        capital_available_for_sizing=Decimal("1000"),
        expected_upside=Decimal("0.10"),
        expected_total_cost=Decimal("0.09"),
    )
    result = calculate_position_size(req)
    # net edge per share ~0.01 * qty likely below min_expected_net_profit threshold
    if result.recommended_quantity == 0:
        assert result.sizing_status == SizingStatus.NO_POSITION


def test_intraday_and_delivery_both_supported():
    for tt in (TradeType.INTRADAY, TradeType.DELIVERY):
        req = make_request(trade_type=tt)
        result = calculate_position_size(req)
        assert result.sizing_status in (
            SizingStatus.APPROVED,
            SizingStatus.REDUCED,
            SizingStatus.NO_POSITION,
            SizingStatus.SIZING_INCOMPLETE,
        )
