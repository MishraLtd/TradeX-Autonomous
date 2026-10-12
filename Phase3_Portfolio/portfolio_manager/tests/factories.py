"""Factory helpers to build test candidates/positions/state concisely."""

from datetime import datetime, timedelta, timezone

from portfolio_manager.candidate import PortfolioCandidate
from portfolio_manager.enums import MarketRegime, Strategy, TradeType
from portfolio_manager.portfolio_state import MarketContext, PortfolioPosition, PortfolioState

NOW = datetime(2026, 9, 14, 10, 0, 0, tzinfo=timezone.utc)


def make_context(as_of=NOW, regime=MarketRegime.TRENDING, regime_confidence=0.8) -> MarketContext:
    return MarketContext(as_of=as_of, market_regime=regime, regime_confidence=regime_confidence)


def make_candidate(
    opportunity_id="OPP-1",
    symbol="TCS",
    sector="TECHNOLOGY",
    strategy=Strategy.MOMENTUM,
    trade_type=TradeType.DELIVERY,
    opportunity_score=80.0,
    expected_net_return=0.02,
    expected_net_profit=20.0,
    probability_of_profit=0.6,
    expected_risk=0.01,
    expected_downside=0.01,
    confidence=0.7,
    expected_holding_period=3.0,
    economic_viability=True,
    expected_total_cost=2.0,
    liquidity_score=0.8,
    regime=MarketRegime.TRENDING,
    regime_compatibility=0.7,
    signal_timestamp=None,
    age_seconds=60,
    industry="IT_SERVICES",
    exchange="NSE",
) -> PortfolioCandidate:
    ts = signal_timestamp or (NOW - timedelta(seconds=age_seconds))
    return PortfolioCandidate(
        opportunity_id=opportunity_id,
        symbol=symbol,
        exchange=exchange,
        trade_type=trade_type,
        strategy=strategy,
        sector=sector,
        industry=industry,
        opportunity_score=opportunity_score,
        predicted_return=expected_net_return,
        expected_net_return=expected_net_return,
        expected_net_profit=expected_net_profit,
        probability_of_profit=probability_of_profit,
        expected_risk=expected_risk,
        expected_downside=expected_downside,
        confidence=confidence,
        expected_holding_period=expected_holding_period,
        economic_viability=economic_viability,
        expected_total_cost=expected_total_cost,
        liquidity_score=liquidity_score,
        regime=regime,
        regime_compatibility=regime_compatibility,
        signal_timestamp=ts,
        model_versions={"return_model_version": "r1", "risk_model_version": "k1"},
    )


def make_position(
    position_id="POS-1",
    symbol="INFY",
    sector="TECHNOLOGY",
    strategy=Strategy.MOMENTUM,
    trade_type=TradeType.DELIVERY,
    expected_remaining_return=0.01,
    expected_risk=0.01,
    confidence=0.6,
    opportunity_score=70.0,
    entry_price=1500.0,
    current_price=1520.0,
    quantity=1,
) -> PortfolioPosition:
    return PortfolioPosition(
        position_id=position_id,
        symbol=symbol,
        exchange="NSE",
        trade_type=trade_type,
        strategy=strategy,
        sector=sector,
        entry_price=entry_price,
        current_price=current_price,
        quantity=quantity,
        entry_time=NOW - timedelta(days=1),
        expected_return=0.015,
        expected_remaining_return=expected_remaining_return,
        expected_risk=expected_risk,
        opportunity_score=opportunity_score,
        confidence=confidence,
        market_regime=MarketRegime.TRENDING,
        model_version="r1",
    )


def make_state(positions=None, total_equity=1000.0, cash=1000.0) -> PortfolioState:
    positions = positions or []
    invested = sum(p.entry_price * p.quantity for p in positions)
    return PortfolioState(
        portfolio_id="PF-TEST",
        timestamp=NOW,
        total_equity=total_equity,
        cash=cash - invested,
        invested_capital=invested,
        open_positions=positions,
        current_regime=MarketRegime.TRENDING,
    )
