"""Factories for Phase 3 integration tests.

Plain functions rather than fixtures so any test module can import them
directly, matching the convention the capital_feasibility suite already
uses (and for the same reason: several packages here name their test
directory `tests`).
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from phase3 import ExecutionInput, Phase3Request
from portfolio_manager.candidate import PortfolioCandidate
from portfolio_manager.enums import MarketRegime, Strategy, TradeType
from portfolio_manager.portfolio_state import MarketContext, PortfolioPosition, PortfolioState

NOW = datetime(2026, 9, 16, 4, 30, tzinfo=timezone.utc)


def make_candidate(opportunity_id="OPP-1", symbol="TESTCO", sector="TECH", **overrides):
    defaults = dict(
        opportunity_id=opportunity_id,
        symbol=symbol,
        exchange="NSE",
        trade_type=TradeType.DELIVERY,
        strategy=Strategy.MOMENTUM,
        sector=sector,
        industry=None,
        opportunity_score=0.80,
        predicted_return=0.045,
        expected_net_return=0.030,
        expected_net_profit=8.0,
        probability_of_profit=0.58,
        expected_risk=0.020,
        expected_downside=0.018,
        confidence=0.75,
        expected_holding_period=4,
        economic_viability=True,
        expected_total_cost=0.40,
        liquidity_score=0.80,
        regime=MarketRegime.TRENDING,
        regime_compatibility=0.80,
        signal_timestamp=NOW - timedelta(minutes=5),
        model_versions={"return_model": "2.1.0"},
    )
    defaults.update(overrides)
    return PortfolioCandidate(**defaults)


def make_execution_input(opportunity_id="OPP-1", entry="50.00", stop="47.50", target="56.00",
                         seed=7, **overrides):
    rng = random.Random(seed)
    defaults = dict(
        opportunity_id=opportunity_id,
        entry_price=Decimal(entry),
        stop_loss=Decimal(stop),
        target=Decimal(target),
        atr_pct=Decimal("0.016"),
        volatility_annualized=0.26,
        average_traded_value=Decimal("500000000"),
        average_volume=Decimal("300000"),
        lot_size=1,
        return_history=[rng.gauss(0.0007, 0.017) for _ in range(90)],
    )
    defaults.update(overrides)
    return ExecutionInput(**defaults)


def make_portfolio_state(cash=1000.0, total_equity=1000.0, invested=0.0, positions=None, **overrides):
    defaults = dict(
        portfolio_id="TEST-PF",
        timestamp=NOW,
        total_equity=total_equity,
        cash=cash,
        invested_capital=invested,
        open_positions=positions or [],
        current_regime=MarketRegime.TRENDING,
    )
    defaults.update(overrides)
    return PortfolioState(**defaults)


def make_open_position(position_id="POS-1", symbol="HELDCO", sector="TECH", quantity=5,
                       entry_price=40.0, current_price=42.0):
    return PortfolioPosition(
        position_id=position_id,
        symbol=symbol,
        exchange="NSE",
        trade_type=TradeType.DELIVERY,
        strategy=Strategy.MOMENTUM,
        sector=sector,
        entry_price=entry_price,
        current_price=current_price,
        quantity=quantity,
        entry_time=NOW - timedelta(days=2),
        expected_return=0.03,
        expected_remaining_return=0.02,
        expected_risk=0.02,
        opportunity_score=0.7,
        confidence=0.7,
        market_regime=MarketRegime.TRENDING,
        model_version="1.0.0",
    )


def make_context(regime=MarketRegime.TRENDING, confidence=0.72):
    return MarketContext(as_of=NOW, market_regime=regime, regime_confidence=confidence)


def make_request(candidates=None, execution_inputs=None, portfolio_state=None,
                 committed_capital=Decimal("0"), **overrides):
    candidates = candidates if candidates is not None else [make_candidate()]
    if execution_inputs is None:
        execution_inputs = {
            c.opportunity_id: make_execution_input(c.opportunity_id, seed=abs(hash(c.symbol)) % 1000)
            for c in candidates
        }
    defaults = dict(
        portfolio_state=portfolio_state or make_portfolio_state(),
        candidates=candidates,
        market_context=make_context(),
        execution_inputs=execution_inputs,
        committed_capital=committed_capital,
    )
    defaults.update(overrides)
    return Phase3Request(**defaults)
