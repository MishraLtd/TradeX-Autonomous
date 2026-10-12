"""
Convenience builder for OpportunityCandidate, used by tests and the
worked example. NOT part of the public production API - keeps the
schema's required-field strictness (a deliberate fail-closed design
choice) from making every test/example verbose and repetitive.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from .schemas import (
    OpportunityCandidate,
    ReturnPrediction,
    RiskPrediction,
    RegimePrediction,
    CostPrediction,
    CostScenario,
    LiquidityInfo,
    PortfolioContext,
    TradeType,
    MarketRegimeLabel,
)

NOW = datetime(2026, 9, 14, 9, 30, tzinfo=timezone.utc)


def D(x) -> Decimal:
    return Decimal(str(x))


def make_candidate(
    symbol: str = "TESTSTOCK",
    *,
    expected_return_pct=2.0,
    probability_of_profit=0.70,
    return_p25_pct=None,
    expected_downside_pct=0.8,
    expected_mae_pct=1.2,
    probability_stop_loss=0.25,
    tail_risk_pct=None,
    regime=MarketRegimeLabel.TRENDING_UP,
    regime_probability=0.75,
    regime_compatibility=0.8,
    gross_return_pct=None,
    cost_ratio=0.20,
    net_return_pct=None,
    economic_viability=True,
    break_even_move_pct=0.3,
    capital_required=300,
    available_capital=1000,
    holding_period_days=2,
    prediction_confidence=0.75,
    prediction_uncertainty=0.25,
    model_calibration_quality=0.7,
    average_traded_value=200000,
    relative_order_size=0.005,
    spread_pct=0.15,
    optimistic_net_return_pct=None,
    conservative_net_return_pct=None,
    data_age_seconds=10,
    trade_type=TradeType.DELIVERY,
    strategy="momentum",
) -> OpportunityCandidate:
    gross = D(gross_return_pct) if gross_return_pct is not None else D(expected_return_pct)
    net = D(net_return_pct) if net_return_pct is not None else (gross * (D(1) - D(cost_ratio)))

    base_scenario = CostScenario(
        expected_total_cost_pct=gross - net if gross >= net else D(0),
        expected_net_return_pct=net,
        cost_ratio=D(cost_ratio),
        break_even_move_pct=D(break_even_move_pct),
        economic_viability=economic_viability,
    )
    optimistic = None
    if optimistic_net_return_pct is not None:
        optimistic = base_scenario.model_copy(update={"expected_net_return_pct": D(optimistic_net_return_pct)})
    conservative = None
    if conservative_net_return_pct is not None:
        conservative = base_scenario.model_copy(update={"expected_net_return_pct": D(conservative_net_return_pct)})

    entry_price = D(100)
    exit_price = entry_price * (D(1) + gross / D(100))
    position_size = max(1, int(D(capital_required) / entry_price))

    return OpportunityCandidate(
        symbol=symbol,
        trade_type=trade_type,
        strategy=strategy,
        timestamp=NOW,
        entry_price=entry_price,
        expected_exit_price=exit_price,
        position_size=position_size,
        capital_required=D(capital_required),
        holding_period_days=D(holding_period_days),
        prediction_confidence=D(prediction_confidence),
        prediction_uncertainty=D(prediction_uncertainty),
        model_calibration_quality=D(model_calibration_quality),
        return_prediction=ReturnPrediction(
            expected_return_pct=gross,
            probability_of_profit=D(probability_of_profit),
            return_p25_pct=D(return_p25_pct) if return_p25_pct is not None else None,
            model_version="return-model-mock-1.0",
            generated_at=NOW,
        ),
        risk_prediction=RiskPrediction(
            expected_downside_pct=D(expected_downside_pct),
            expected_max_adverse_excursion_pct=D(expected_mae_pct),
            probability_stop_loss=D(probability_stop_loss),
            tail_risk_pct=D(tail_risk_pct) if tail_risk_pct is not None else None,
            model_version="risk-model-mock-1.0",
            generated_at=NOW,
        ),
        regime_prediction=RegimePrediction(
            regime=regime,
            regime_probability=D(regime_probability),
            regime_compatibility=D(regime_compatibility),
            model_version="regime-model-mock-1.0",
            generated_at=NOW,
        ),
        cost_prediction=CostPrediction(
            expected_gross_return_pct=gross,
            base=base_scenario,
            optimistic=optimistic,
            conservative=conservative,
            model_version="cost-model-mock-1.0",
            generated_at=NOW,
        ),
        liquidity=LiquidityInfo(
            average_traded_value=D(average_traded_value),
            relative_order_size=D(relative_order_size),
            spread_pct=D(spread_pct),
        ),
        portfolio_context=PortfolioContext(
            available_capital=D(available_capital),
            current_exposure=D(0),
            number_of_open_positions=0,
        ),
        data_generated_at=NOW.replace(microsecond=0),
    )
