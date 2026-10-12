"""
Unit tests for the TradeX Portfolio Risk Model.

Covers the 16 scenarios required by spec section 22:
  1. Empty portfolio
  2. Single-position portfolio
  3. Well-diversified portfolio
  4. Highly concentrated portfolio
  5. Highly correlated portfolio
  6. Single-sector portfolio
  7. Insufficient historical data
  8. Missing price data
  9. Very small capital
  10. Large number of positions
  11. Large aggregate stop-loss exposure
  12. Proposed trade causes risk-limit breach
  13. Proposed trade improves diversification
  14. Portfolio under severe drawdown
  15. Covariance matrix is singular
  16. Invalid input values

Run with:  python -m pytest portfolio_risk/tests/ -v
"""

import math
import numpy as np
import pytest

from portfolio_risk.models import PortfolioState, Position, Direction
from portfolio_risk.portfolio_risk_engine import PortfolioRiskEngine
from portfolio_risk.config import RiskConfig


def make_history(n=90, vol=0.02, drift=0.0003, seed=0):
    rng = np.random.default_rng(seed)
    return list(rng.normal(drift, vol, n))


def make_correlated_histories(n_assets, n_obs=90, base_seed=100, corr=0.9, vol=0.02):
    """Generate n_assets return series with approx pairwise correlation `corr`."""
    rng = np.random.default_rng(base_seed)
    common = rng.normal(0, vol, n_obs)
    series = []
    for i in range(n_assets):
        idio = rng.normal(0, vol, n_obs)
        combined = math.sqrt(corr) * common + math.sqrt(1 - corr) * idio
        series.append(list(combined))
    return series


@pytest.fixture
def engine():
    return PortfolioRiskEngine()


# --------------------------------------------------------------------------
# Test 1: Empty portfolio
# --------------------------------------------------------------------------
def test_empty_portfolio(engine):
    portfolio = PortfolioState(total_capital=10000, available_cash=10000, positions=[])
    report = engine.assess(portfolio)
    assert report["portfolio_risk_score"] == 0.0
    assert report["risk_class"] == "LOW"
    assert report["risk_status"] == "ACCEPT"


# --------------------------------------------------------------------------
# Test 2: Single-position portfolio
# --------------------------------------------------------------------------
def test_single_position_portfolio(engine):
    pos = Position(symbol="TCS", quantity=2, entry_price=3500, current_price=3550,
                   sector="IT", stop_loss_price=3400, volatility=0.2,
                   return_history=make_history(seed=1))
    portfolio = PortfolioState(total_capital=10000, available_cash=2900, positions=[pos])
    report = engine.assess(portfolio)
    # A single position necessarily accounts for 100% of INVESTED capital,
    # even though its share of total equity (cash + invested) is smaller.
    assert report["concentration"]["largest_position_symbol"] == "TCS"
    assert report["concentration"]["top2_weight"] == report["concentration"]["largest_position_weight"]
    assert report["risk_limits"]["position_concentration"] == "BREACH"
    # correlation must gracefully report nothing meaningful for N=1
    assert report["correlation"]["average_correlation"] == 0.0


# --------------------------------------------------------------------------
# Test 3: Well-diversified portfolio
# --------------------------------------------------------------------------
def test_well_diversified_portfolio(engine):
    sectors = ["IT", "BANKING", "PHARMA", "FMCG", "AUTO", "ENERGY"]
    positions = []
    for i, sector in enumerate(sectors):
        positions.append(Position(
            symbol=f"SYM{i}", quantity=10, entry_price=100, current_price=101,
            sector=sector, stop_loss_price=95, volatility=0.18, beta=1.0,
            avg_traded_value=10_00_00_000,
            return_history=make_history(seed=10 + i, vol=0.015),
        ))
    portfolio = PortfolioState(total_capital=100000, available_cash=40000, positions=positions,
                                market_regime="NEUTRAL")
    report = engine.assess(portfolio)
    assert report["concentration"]["largest_position_weight"] < 0.20
    assert report["risk_limits"]["position_concentration"] == "PASS"
    assert report["risk_class"] in ("LOW", "MODERATE")


# --------------------------------------------------------------------------
# Test 4: Highly concentrated portfolio
# --------------------------------------------------------------------------
def test_highly_concentrated_portfolio(engine):
    positions = [
        Position(symbol="BIGONE", quantity=100, entry_price=100, current_price=100,
                 sector="IT", stop_loss_price=90, return_history=make_history(seed=20)),
        Position(symbol="SMALL", quantity=1, entry_price=100, current_price=100,
                 sector="AUTO", stop_loss_price=90, return_history=make_history(seed=21)),
    ]
    portfolio = PortfolioState(total_capital=11000, available_cash=900, positions=positions)
    report = engine.assess(portfolio)
    assert report["concentration"]["largest_position_weight"] > 0.9
    assert report["risk_limits"]["position_concentration"] == "BREACH"
    assert report["risk_status"] in ("REJECT_NEW_POSITION", "REDUCE_EXPOSURE", "EMERGENCY_REDUCTION")


# --------------------------------------------------------------------------
# Test 5: Highly correlated portfolio
# --------------------------------------------------------------------------
def test_highly_correlated_portfolio(engine):
    hist = make_correlated_histories(4, n_obs=90, corr=0.92)
    positions = [
        Position(symbol=f"CORR{i}", quantity=5, entry_price=100, current_price=101,
                 sector=f"SEC{i}", stop_loss_price=95, return_history=hist[i])
        for i in range(4)
    ]
    portfolio = PortfolioState(total_capital=20000, available_cash=15000, positions=positions)
    report = engine.assess(portfolio)
    assert report["correlation"]["average_correlation"] > 0.6
    assert report["risk_limits"]["average_correlation"] in ("WARNING", "BREACH")


# --------------------------------------------------------------------------
# Test 6: Single-sector portfolio
# --------------------------------------------------------------------------
def test_single_sector_portfolio(engine):
    positions = [
        Position(symbol=f"IT{i}", quantity=5, entry_price=100, current_price=100,
                 sector="IT", stop_loss_price=95, return_history=make_history(seed=30 + i))
        for i in range(5)
    ]
    portfolio = PortfolioState(total_capital=10000, available_cash=2000, positions=positions)
    report = engine.assess(portfolio)
    # Only one sector is present, so it must hold 100% of the invested capital.
    assert len(report["sector"]["weight_by_sector"]) == 1
    assert report["sector"]["largest_sector"] == "IT"
    assert report["risk_limits"]["sector_concentration"] == "BREACH"


# --------------------------------------------------------------------------
# Test 7: Insufficient historical data
# --------------------------------------------------------------------------
def test_insufficient_historical_data(engine):
    positions = [
        Position(symbol="A", quantity=5, entry_price=100, current_price=100, sector="IT",
                 stop_loss_price=95, return_history=[0.01, -0.005]),  # only 2 obs
        Position(symbol="B", quantity=5, entry_price=100, current_price=100, sector="AUTO",
                 stop_loss_price=95, return_history=[0.02, 0.01]),
    ]
    portfolio = PortfolioState(total_capital=5000, available_cash=4000, positions=positions)
    report = engine.assess(portfolio)
    assert report["data_quality"]["status"] != "VALID"
    # Must not crash and must still produce a score
    assert isinstance(report["portfolio_risk_score"], float)
    assert not math.isnan(report["portfolio_risk_score"])


# --------------------------------------------------------------------------
# Test 8: Missing price data
# --------------------------------------------------------------------------
def test_missing_price_data(engine):
    positions = [
        Position(symbol="GOOD", quantity=5, entry_price=100, current_price=105, sector="IT",
                 stop_loss_price=95, return_history=make_history(seed=40)),
        Position(symbol="BAD", quantity=5, entry_price=100, current_price=None, sector="AUTO",
                 stop_loss_price=95, return_history=make_history(seed=41)),
        Position(symbol="BAD2", quantity=5, entry_price=0, current_price=100, sector="AUTO",
                 stop_loss_price=95, return_history=make_history(seed=42)),
    ]
    portfolio = PortfolioState(total_capital=5000, available_cash=4000, positions=positions)
    report = engine.assess(portfolio)
    # invalid positions should be excluded, not crash
    assert "GOOD" in report["concentration"]["largest_position_symbol"] or report["concentration"]["largest_position_symbol"] == "GOOD"
    assert report["data_quality"]["status"] != "VALID"


# --------------------------------------------------------------------------
# Test 9: Very small capital
# --------------------------------------------------------------------------
def test_very_small_capital(engine):
    positions = [
        Position(symbol="ONLY", quantity=1, entry_price=800, current_price=810, sector="IT",
                 stop_loss_price=750, return_history=make_history(seed=50)),
    ]
    portfolio = PortfolioState(total_capital=1000, available_cash=200, positions=positions)
    report = engine.assess(portfolio)
    assert report["capital"]["capital_tier"] == "MICRO"
    assert isinstance(report["portfolio_risk_score"], float)
    # concentration risk still reported at full severity, only annotated
    assert report["concentration"]["largest_position_weight"] > 0.5


# --------------------------------------------------------------------------
# Test 10: Large number of positions
# --------------------------------------------------------------------------
def test_large_number_of_positions(engine):
    sectors = ["IT", "BANKING", "PHARMA", "FMCG", "AUTO", "ENERGY", "METAL", "REALTY"]
    positions = [
        Position(symbol=f"S{i}", quantity=2, entry_price=100, current_price=100,
                 sector=sectors[i % len(sectors)], stop_loss_price=95,
                 return_history=make_history(seed=60 + i))
        for i in range(20)
    ]
    portfolio = PortfolioState(total_capital=50000, available_cash=10000, positions=positions)
    report = engine.assess(portfolio)
    assert report["risk_limits"]["position_count"] == "BREACH"


# --------------------------------------------------------------------------
# Test 11: Large aggregate stop-loss exposure
# --------------------------------------------------------------------------
def test_large_aggregate_stop_loss_exposure(engine):
    positions = [
        Position(symbol=f"WIDE{i}", quantity=10, entry_price=100, current_price=100,
                 sector="IT", stop_loss_price=70,  # 30% stop distance
                 return_history=make_history(seed=70 + i))
        for i in range(3)
    ]
    portfolio = PortfolioState(total_capital=5000, available_cash=2000, positions=positions)
    report = engine.assess(portfolio)
    assert report["risk_limits"]["aggregate_stop_loss_risk"] == "BREACH"


# --------------------------------------------------------------------------
# Test 12: Proposed trade causes risk-limit breach
# --------------------------------------------------------------------------
def test_proposed_trade_causes_breach(engine):
    existing = [
        Position(symbol="EXIST", quantity=5, entry_price=100, current_price=100, sector="IT",
                 stop_loss_price=95, return_history=make_history(seed=80)),
    ]
    portfolio = PortfolioState(total_capital=10000, available_cash=9500, positions=existing)
    proposed = Position(symbol="HUGE", quantity=80, entry_price=100, current_price=100,
                        sector="IT", stop_loss_price=95, is_proposed=True,
                        return_history=make_history(seed=81))
    impact = engine.evaluate_new_trade(portfolio, [proposed])
    assert len(impact.new_breaches_caused_by_trade) > 0
    assert impact.after_score > impact.before_score


# --------------------------------------------------------------------------
# Test 13: Proposed trade improves diversification
# --------------------------------------------------------------------------
def test_proposed_trade_improves_diversification(engine):
    existing = [
        Position(symbol=f"IT{i}", quantity=8, entry_price=100, current_price=100, sector="IT",
                 stop_loss_price=95, return_history=make_history(seed=90 + i))
        for i in range(3)
    ]
    portfolio = PortfolioState(total_capital=20000, available_cash=4000, positions=existing)
    proposed = Position(symbol="PHARMA1", quantity=2, entry_price=100, current_price=100,
                        sector="PHARMA", stop_loss_price=95, is_proposed=True,
                        return_history=make_history(seed=95, drift=-0.0002))
    impact = engine.evaluate_new_trade(portfolio, [proposed])
    # Adding a small uncorrelated new-sector position at modest size should
    # not increase sector concentration score above where it already was.
    assert impact.after_full_report["sector"]["largest_sector_weight"] <= impact.before_full_report["sector"]["largest_sector_weight"]


# --------------------------------------------------------------------------
# Test 14: Portfolio under severe drawdown
# --------------------------------------------------------------------------
def test_severe_drawdown(engine):
    positions = [
        Position(symbol="A", quantity=5, entry_price=100, current_price=90, sector="IT",
                 stop_loss_price=80, return_history=make_history(seed=100)),
    ]
    portfolio = PortfolioState(
        total_capital=10000, available_cash=9500, positions=positions,
        peak_portfolio_value=12000, current_portfolio_value=9000,
    )
    report = engine.assess(portfolio)
    assert report["drawdown"]["state"] in ("HIGH", "CRITICAL")
    assert report["risk_status"] in ("REDUCE_EXPOSURE", "EMERGENCY_REDUCTION", "REJECT_NEW_POSITION")


# --------------------------------------------------------------------------
# Test 15: Covariance matrix is singular
# --------------------------------------------------------------------------
def test_singular_covariance_matrix(engine):
    # Two positions with IDENTICAL return histories -> perfectly collinear -> singular covariance
    identical_hist = make_history(seed=110, n=90)
    positions = [
        Position(symbol="DUP1", quantity=5, entry_price=100, current_price=100, sector="IT",
                 stop_loss_price=95, return_history=list(identical_hist)),
        Position(symbol="DUP2", quantity=5, entry_price=100, current_price=100, sector="IT",
                 stop_loss_price=95, return_history=list(identical_hist)),
    ]
    portfolio = PortfolioState(total_capital=10000, available_cash=9000, positions=positions)
    # Must not raise
    report = engine.assess(portfolio)
    assert isinstance(report["volatility"]["portfolio_volatility_annualized"], float)
    assert not math.isnan(report["volatility"]["portfolio_volatility_annualized"])


# --------------------------------------------------------------------------
# Test 16: Invalid input values
# --------------------------------------------------------------------------
def test_invalid_input_values(engine):
    positions = [
        Position(symbol="NEG", quantity=-5, entry_price=100, current_price=100, sector="IT",
                 stop_loss_price=95, return_history=make_history(seed=120)),
        Position(symbol="NANQ", quantity=float("nan"), entry_price=100, current_price=100,
                 sector="IT", stop_loss_price=95, return_history=make_history(seed=121)),
        Position(symbol="OK", quantity=5, entry_price=100, current_price=101, sector="IT",
                 stop_loss_price=95, return_history=make_history(seed=122)),
    ]
    portfolio = PortfolioState(total_capital=float("nan"), available_cash=5000, positions=positions)
    # Must not raise
    report = engine.assess(portfolio)
    assert isinstance(report["portfolio_risk_score"], float)
    assert not math.isnan(report["portfolio_risk_score"])


# --------------------------------------------------------------------------
# Additional determinism check
# --------------------------------------------------------------------------
def test_deterministic_output(engine):
    positions = [
        Position(symbol="TCS", quantity=5, entry_price=3500, current_price=3600, sector="IT",
                 stop_loss_price=3400, return_history=make_history(seed=1)),
    ]
    portfolio = PortfolioState(total_capital=20000, available_cash=15000, positions=positions)
    r1 = engine.assess(portfolio)
    r2 = engine.assess(portfolio)
    assert r1["portfolio_risk_score"] == r2["portfolio_risk_score"]
