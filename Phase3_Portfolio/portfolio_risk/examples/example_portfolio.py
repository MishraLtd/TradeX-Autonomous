"""
example_portfolio.py
-----------------------
Runnable example: builds a realistic small-capital Indian equity portfolio,
runs a full Portfolio Risk assessment, prints the JSON report, then runs a
new-trade pre-check ("what happens if I add this trade?").

Run with:
    python -m portfolio_risk.examples.example_portfolio
"""

import json
import numpy as np

from portfolio_risk import PortfolioRiskEngine, PortfolioState, Position


def synthetic_returns(n=90, vol=0.02, drift=0.0004, seed=0):
    """
    Stand-in for real historical return data. In production this would come
    from FILTER2.0 / the market data layer, not be generated synthetically.
    """
    rng = np.random.default_rng(seed)
    return list(rng.normal(drift, vol, n))


def build_example_portfolio() -> PortfolioState:
    positions = [
        Position(
            symbol="TCS", quantity=2, entry_price=3550, current_price=3610,
            sector="IT", market_cap_class="LARGE_CAP", stop_loss_price=3450,
            volatility=0.21, beta=0.85, avg_traded_value=8_00_00_000,
            return_history=synthetic_returns(seed=1),
        ),
        Position(
            symbol="INFY", quantity=3, entry_price=1520, current_price=1560,
            sector="IT", market_cap_class="LARGE_CAP", stop_loss_price=1470,
            volatility=0.24, beta=0.95, avg_traded_value=6_00_00_000,
            return_history=synthetic_returns(seed=2),
        ),
        Position(
            symbol="HDFCBANK", quantity=3, entry_price=1620, current_price=1600,
            sector="BANKING", market_cap_class="LARGE_CAP", stop_loss_price=1560,
            volatility=0.19, beta=1.05, avg_traded_value=9_00_00_000,
            return_history=synthetic_returns(seed=3),
        ),
        Position(
            symbol="SUNPHARMA", quantity=4, entry_price=1180, current_price=1210,
            sector="PHARMA", market_cap_class="LARGE_CAP", stop_loss_price=1130,
            volatility=0.23, beta=0.60, avg_traded_value=3_00_00_000,
            return_history=synthetic_returns(seed=4, drift=0.0002),
        ),
    ]

    return PortfolioState(
        total_capital=100_000,
        available_cash=20_000,
        positions=positions,
        peak_portfolio_value=105_000,
        current_portfolio_value=99_000,
        daily_pnl=-800,
        cumulative_pnl=3200,
        market_regime="NEUTRAL",
    )


def main():
    engine = PortfolioRiskEngine()
    portfolio = build_example_portfolio()

    report = engine.assess(portfolio)
    print("=" * 70)
    print("FULL PORTFOLIO RISK ASSESSMENT")
    print("=" * 70)
    print(json.dumps(report, indent=2, default=str))

    print("\n" + "=" * 70)
    print("NEW TRADE PRE-CHECK: proposing a large additional IT-sector position")
    print("=" * 70)
    proposed = Position(
        symbol="WIPRO", quantity=40, entry_price=480, current_price=480,
        sector="IT", stop_loss_price=455, is_proposed=True,
        volatility=0.26, beta=1.0, avg_traded_value=4_00_00_000,
        return_history=synthetic_returns(seed=5),
    )
    impact = engine.evaluate_new_trade(portfolio, [proposed])
    print(f"Before score: {impact.before_score}  ({impact.before_risk_class})")
    print(f"After score:  {impact.after_score}  ({impact.after_risk_class})")
    print(f"Score change: {impact.score_change:+.2f}")
    print(f"New breaches caused by this trade: {impact.new_breaches_caused_by_trade}")
    print(f"Risk contribution of new position: {impact.risk_contribution_of_new_position_pct:.2%}")
    print(f"Recommendation: {impact.final_recommendation}")

    print("\n" + impact.after_full_report["decision_explanation"])


if __name__ == "__main__":
    main()
