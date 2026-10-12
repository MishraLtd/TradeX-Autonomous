from decimal import Decimal

import pytest

from position_sizing.config import DEFAULT_CONFIG
from position_sizing.confidence import calculate_confidence_adjustment
from position_sizing.exceptions import InvalidRequestError, MissingCriticalInputError
from position_sizing.kelly import calculate_kelly_size
from position_sizing.risk_based import calculate_risk_based_size, compute_risk_per_share
from position_sizing.rounding import round_quantity
from position_sizing.volatility import calculate_volatility_adjustment


def test_compute_risk_per_share_basic():
    assert compute_risk_per_share(Decimal("100"), Decimal("98")) == Decimal("2")


def test_compute_risk_per_share_missing_stop_raises():
    with pytest.raises(MissingCriticalInputError):
        compute_risk_per_share(Decimal("100"), None)


def test_compute_risk_per_share_invalid_raises():
    with pytest.raises(InvalidRequestError):
        compute_risk_per_share(Decimal("100"), Decimal("100"))


def test_risk_based_example_from_spec():
    # Entry 100, stop 98 -> risk/share 2; allowed loss ~ 1% of 1000 = 10 -> qty ~5 (before cost buffer)
    size, diag = calculate_risk_based_size(
        Decimal("100"), Decimal("98"), Decimal("0"), Decimal("1000"), Decimal("10"), DEFAULT_CONFIG
    )
    assert size == Decimal("5")


def test_volatility_adjustment_bounded():
    low = calculate_volatility_adjustment(Decimal("0.0001"), DEFAULT_CONFIG)
    high = calculate_volatility_adjustment(Decimal("1.0"), DEFAULT_CONFIG)
    assert low <= DEFAULT_CONFIG.max_vol_mult
    assert high >= DEFAULT_CONFIG.min_vol_mult


def test_volatility_adjustment_none_is_neutral():
    assert calculate_volatility_adjustment(None, DEFAULT_CONFIG) == Decimal("1.00")


def test_confidence_adjustment_bounds_and_monotonic():
    c_low = calculate_confidence_adjustment(Decimal("0.1"), DEFAULT_CONFIG)
    c_mid = calculate_confidence_adjustment(Decimal("0.65"), DEFAULT_CONFIG)
    c_high = calculate_confidence_adjustment(Decimal("0.99"), DEFAULT_CONFIG)
    assert c_low == DEFAULT_CONFIG.min_confidence_mult
    assert c_low <= c_mid <= c_high
    assert c_high <= DEFAULT_CONFIG.max_confidence_mult


def test_confidence_extreme_never_exceeds_cap():
    c = calculate_confidence_adjustment(Decimal("1.0"), DEFAULT_CONFIG)
    assert c == DEFAULT_CONFIG.max_confidence_mult


def test_round_quantity_down_default():
    assert round_quantity(Decimal("4.9"), mode="DOWN") == 4


def test_round_quantity_never_negative():
    assert round_quantity(Decimal("-3"), mode="DOWN") == 0


def test_round_quantity_respects_max():
    assert round_quantity(Decimal("10"), max_quantity=3, mode="DOWN") == 3


def test_kelly_hard_capped():
    diag = calculate_kelly_size(
        probability_of_profit=Decimal("0.95"),
        probability_of_loss=Decimal("0.05"),
        expected_upside=Decimal("50"),
        expected_downside=Decimal("1"),
        capital_available_for_sizing=Decimal("1000"),
        entry_price=Decimal("100"),
        config=DEFAULT_CONFIG,
    )
    assert diag is not None
    assert diag["capped_kelly_fraction"] <= DEFAULT_CONFIG.kelly_hard_cap


def test_kelly_missing_inputs_returns_none():
    assert calculate_kelly_size(None, None, None, None, Decimal("1000"), Decimal("100"), DEFAULT_CONFIG) is None
