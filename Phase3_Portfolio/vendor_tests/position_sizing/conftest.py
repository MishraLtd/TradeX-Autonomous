from decimal import Decimal

import pytest

from position_sizing.enums import MarketRegime, TradeType
from position_sizing.models import PositionSizingRequest


def make_request(**overrides) -> PositionSizingRequest:
    defaults = dict(
        position_id="pos-1",
        opportunity_id="opp-1",
        symbol="TESTSTOCK",
        exchange="NSE",
        segment="EQ",
        trade_type=TradeType.DELIVERY,
        strategy="momentum",
        market_regime=MarketRegime.SIDEWAYS,
        entry_price=Decimal("145.00"),
        stop_loss=Decimal("141.00"),
        target=Decimal("152.00"),
        expected_exit_price=Decimal("150.00"),
        expected_net_return=Decimal("0.024"),
        probability_of_profit=Decimal("0.68"),
        probability_of_loss=Decimal("0.32"),
        expected_upside=Decimal("5.00"),
        expected_downside=Decimal("4.00"),
        atr=Decimal("2.10"),
        atr_pct=Decimal("0.0145"),
        model_confidence=Decimal("0.81"),
        expected_total_cost=Decimal("0.35"),
        average_traded_value=Decimal("50000000"),
        average_volume=Decimal("400000"),
        capital_available_for_sizing=Decimal("1000.00"),
        model_versions={"return_model": "0.3.0", "cost_model": "1.0.0"},
    )
    defaults.update(overrides)
    return PositionSizingRequest(**defaults)


@pytest.fixture
def base_request():
    return make_request()
