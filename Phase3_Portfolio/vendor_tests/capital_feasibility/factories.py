from datetime import datetime, timezone
from decimal import Decimal

from cost_model import models as cost_models
from cost_model.engine import CostEngine

from portfolio_manager.enums import MarketRegime as PMMarketRegime
from portfolio_manager.enums import PositionStatus, Strategy, TradeType as PMTradeType
from portfolio_manager.portfolio_state import PortfolioPosition, PortfolioState

from position_sizing.enums import MarketRegime as PSMarketRegime
from position_sizing.enums import TradeType as PSTradeType
from position_sizing.models import PositionSizeResult, PositionSizingRequest
from position_sizing.enums import SizingConfidence, SizingLevel, SizingMethod, SizingStatus

from capital_feasibility.config import CapitalFeasibilityConfig
from capital_feasibility.models import AssetExposure, LiquidityContext, TradeCapitalRequest

NOW = datetime(2026, 9, 15, 10, 0, 0, tzinfo=timezone.utc)


def make_portfolio_position(
    position_id="POS-1",
    symbol="INFY",
    sector="TECHNOLOGY",
    entry_price=1500.0,
    current_price=1520.0,
    quantity=1,
    trade_type=PMTradeType.DELIVERY,
) -> PortfolioPosition:
    return PortfolioPosition(
        position_id=position_id,
        symbol=symbol,
        exchange="NSE",
        trade_type=trade_type,
        strategy=Strategy.MOMENTUM,
        sector=sector,
        entry_price=entry_price,
        current_price=current_price,
        quantity=quantity,
        entry_time=NOW,
        expected_return=0.02,
        expected_remaining_return=0.01,
        expected_risk=0.01,
        opportunity_score=70.0,
        confidence=0.7,
        market_regime=PMMarketRegime.TRENDING,
        model_version="r1",
        position_status=PositionStatus.OPEN,
    )


def make_portfolio_state(
    total_equity=1000.0,
    cash=1000.0,
    invested_capital=0.0,
    open_positions=None,
) -> PortfolioState:
    return PortfolioState(
        portfolio_id="PF-1",
        timestamp=NOW,
        total_equity=total_equity,
        cash=cash,
        invested_capital=invested_capital,
        open_positions=open_positions or [],
    )


def make_sizing_request(**overrides) -> PositionSizingRequest:
    defaults = dict(
        position_id="pos-1",
        opportunity_id="opp-1",
        symbol="TESTSTOCK",
        exchange="NSE",
        segment="EQ",
        trade_type=PSTradeType.DELIVERY,
        strategy="momentum",
        market_regime=PSMarketRegime.SIDEWAYS,
        entry_price=Decimal("145.00"),
        stop_loss=Decimal("141.00"),
        capital_available_for_sizing=Decimal("1000.00"),
        average_traded_value=Decimal("50000000"),
        average_volume=Decimal("400000"),
        model_versions={"position_sizing": "1.0.0"},
    )
    defaults.update(overrides)
    return PositionSizingRequest(**defaults)


def make_sizing_result(**overrides) -> PositionSizeResult:
    defaults = dict(
        symbol="TESTSTOCK",
        recommended_quantity=5,
        minimum_quantity=1,
        maximum_quantity=10,
        recommended_position_value=Decimal("725.00"),
        recommended_position_pct=Decimal("0.725"),
        risk_per_share=Decimal("4.00"),
        estimated_trade_risk=Decimal("20.00"),
        risk_pct=Decimal("0.02"),
        expected_gross_profit=Decimal("25.00"),
        expected_net_profit=Decimal("23.00"),
        expected_net_return=Decimal("0.032"),
        capital_efficiency=Decimal("0.032"),
        risk_efficiency=Decimal("1.15"),
        sizing_method=SizingMethod.RISK_BASED_HYBRID,
        sizing_level=SizingLevel.LEVEL_2_RISK_VOL,
        sizing_components={},
        size_adjustments={},
        binding_constraints=[],
        sizing_status=SizingStatus.APPROVED,
        rejection_reason=None,
        confidence_adjustment=None,
        volatility_adjustment=None,
        liquidity_adjustment=None,
        regime_adjustment=None,
        portfolio_adjustment=None,
        sizing_confidence=SizingConfidence.MEDIUM,
        model_versions={"position_sizing": "1.0.0"},
        calculation_timestamp=NOW,
        audit_id="audit-1",
    )
    defaults.update(overrides)
    return PositionSizeResult(**defaults)


def make_trade_request(**overrides) -> TradeCapitalRequest:
    defaults = dict(
        position_id="pos-1",
        opportunity_id="opp-1",
        symbol="TESTSTOCK",
        exchange="NSE",
        sector="TECHNOLOGY",
        trade_type="DELIVERY",
        entry_price=Decimal("145.00"),
        requested_quantity=5,
        lot_size=1,
        liquidity=LiquidityContext(),
    )
    defaults.update(overrides)
    return TradeCapitalRequest(**defaults)


def make_asset_exposure(**overrides) -> AssetExposure:
    defaults = dict(
        symbol="TESTSTOCK",
        existing_value=Decimal("0"),
        sector="TECHNOLOGY",
        existing_sector_value=Decimal("0"),
    )
    defaults.update(overrides)
    return AssetExposure(**defaults)
