"""
End-to-end Phase 3 demo at TradeX's real starting scale (~Rs 1,000).

Run:  python demo_phase3.py

Prices through the real Cost Model when `cost_model` is on the path, and
falls back to the labelled ApproximateZerodhaCostProvider stand-in when
it is not. The banner says which one produced the numbers.
"""
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from phase3 import (
    ApproximateZerodhaCostProvider,
    ExecutionInput,
    Phase3Engine,
    Phase3Request,
    result_to_dict,
)
from portfolio_manager.candidate import PortfolioCandidate
from portfolio_manager.enums import MarketRegime, Strategy, TradeType
from portfolio_manager.portfolio_state import MarketContext, PortfolioState

NOW = datetime(2026, 9, 16, 4, 30, tzinfo=timezone.utc)


def candidate(oid, symbol, sector, score, price_note=""):
    return PortfolioCandidate(
        opportunity_id=oid,
        symbol=symbol,
        exchange="NSE",
        trade_type=TradeType.DELIVERY,
        strategy=Strategy.MOMENTUM,
        sector=sector,
        industry=None,
        opportunity_score=score,
        predicted_return=0.045,
        expected_net_return=0.032,
        expected_net_profit=9.5,
        probability_of_profit=0.58,
        expected_risk=0.021,
        expected_downside=0.018,
        confidence=0.74,
        expected_holding_period=4,
        economic_viability=True,
        expected_total_cost=0.55,          # per share, round trip
        liquidity_score=0.81,
        regime=MarketRegime.TRENDING,
        regime_compatibility=0.8,
        signal_timestamp=NOW - timedelta(minutes=3),
        model_versions={"return_model": "2.1.0", "cost_model": "1.4.0"},
    )


def execution_input(oid, price, stop, target, adv, seed=0):
    rng = random.Random(seed)
    history = [rng.gauss(0.0008, 0.018) for _ in range(90)]
    return ExecutionInput(
        opportunity_id=oid,
        entry_price=Decimal(price),
        stop_loss=Decimal(stop),
        target=Decimal(target),
        atr_pct=Decimal("0.016"),
        volatility_annualized=0.27,
        average_traded_value=Decimal(adv),
        average_volume=Decimal("450000"),
        lot_size=1,
        return_history=history,
    )


def cost_provider():
    """Real Cost Model if available, labelled stand-in otherwise."""
    try:
        from cost_model import models as cost_models
        from cost_model.engine import CostEngine

        from phase3.costs import CostModelProvider

        return CostModelProvider(CostEngine(), cost_models), "cost_model.CostEngine (real)"
    except ImportError:
        return ApproximateZerodhaCostProvider(), "ApproximateZerodhaCostProvider (STAND-IN)"


def main():
    portfolio = PortfolioState(
        portfolio_id="TRADEX-LIVE-1",
        timestamp=NOW,
        total_equity=1000.0,
        cash=1000.0,
        invested_capital=0.0,
        open_positions=[],
        current_regime=MarketRegime.TRENDING,
    )

    context = MarketContext(as_of=NOW, market_regime=MarketRegime.TRENDING, regime_confidence=0.72)

    candidates = [
        candidate("OPP-1", "IDEA", "TELECOM", 0.82),
        candidate("OPP-2", "SUZLON", "ENERGY", 0.78),
        candidate("OPP-3", "YESBANK", "FINANCIALS", 0.71),
    ]

    execution_inputs = {
        "OPP-1": execution_input("OPP-1", "7.85", "7.42", "8.60", "820000000", seed=11),
        "OPP-2": execution_input("OPP-2", "62.40", "59.90", "67.50", "1450000000", seed=29),
        "OPP-3": execution_input("OPP-3", "21.15", "20.30", "22.90", "990000000", seed=47),
    }

    provider, provider_name = cost_provider()
    print(f"cost source       : {provider_name}\n")
    engine = Phase3Engine(cost_provider=provider)
    result = engine.run(
        Phase3Request(
            portfolio_state=portfolio,
            candidates=candidates,
            market_context=context,
            execution_inputs=execution_inputs,
            committed_capital=Decimal("0"),
        )
    )

    print(f"status            : {result.status.value}")
    print(f"explanation       : {result.explanation}")
    print(f"risk before/after : {result.risk_before.get('risk_status')} "
          f"({result.risk_before.get('portfolio_risk_score'):.1f}) -> "
          f"{result.risk_after.get('risk_status')} "
          f"({result.risk_after.get('portfolio_risk_score'):.1f})")
    if result.capital:
        print(f"deployable start  : Rs {result.capital.deployable_at_start}")
        print(f"allocated         : Rs {result.capital.capital_allocated}")
        print(f"deployable left   : Rs {result.capital.deployable_remaining}")

    print("\n--- instructions -------------------------------------------")
    for ins in result.instructions:
        print(
            f"{ins.symbol:<10} qty={ins.quantity:<4} value=Rs {ins.position_value:<9} "
            f"cost=Rs {ins.estimated_entry_cost:<7} status={ins.allocation_status.value}"
        )
        print(f"           {ins.explanation}")

    print("\n--- outcomes -----------------------------------------------")
    for out in result.outcomes:
        print(f"{out.symbol:<10} {out.status.value:<28} [{out.terminal_stage.value}] {out.reason[:90]}")

    payload = result_to_dict(result)
    print(f"\nserialized keys   : {sorted(payload)}")


if __name__ == "__main__":
    main()
