"""
portfolio_risk_engine.py
---------------------------
Top-level orchestrator. This is the single entry point the rest of TradeX
(Portfolio Manager, Position Sizing, Capital Feasibility) should call.

    from portfolio_risk.portfolio_risk_engine import PortfolioRiskEngine
    engine = PortfolioRiskEngine(config)
    report = engine.assess(portfolio_state)              # dict, spec section 19 schema
    impact = engine.evaluate_new_trade(portfolio_state, [proposed_position])

Wires together every engine in the package, in the order:
  validate -> concentration -> sector -> correlation -> covariance/volatility
  -> downside -> VaR -> ES -> drawdown -> capital utilization -> liquidity
  -> stop-loss -> stress test -> marginal risk -> scoring -> risk budget
  -> risk limits -> recommendation
"""

from __future__ import annotations

from typing import Dict, List, Optional

from .config import RiskConfig, DEFAULT_CONFIG
from .models import PortfolioState, Position, DataQualityReport
from .validation import validate_portfolio

from .concentration_engine import concentration_risk
from .sector_risk_engine import sector_concentration_risk
from .correlation_engine import correlation_risk, correlation_score
from .covariance_engine import estimate_covariance, portfolio_volatility, volatility_score
from .downside_risk_engine import downside_risk
from .var_engine import compute_var, var_score
from .expected_shortfall_engine import compute_expected_shortfall
from .drawdown_engine import drawdown_risk
from .capital_utilization_engine import capital_utilization_risk
from .liquidity_risk_engine import liquidity_risk
from .stop_loss_risk_engine import stop_loss_risk
from .stress_test_engine import run_stress_tests
from .marginal_risk_engine import marginal_risk_contribution
from .scoring_engine import compute_portfolio_risk_score
from .risk_budget_engine import compute_risk_budget
from .risk_limit_engine import evaluate_limits
from .recommendation_engine import build_recommendation
from .trade_impact_engine import evaluate_trade_impact, TradeImpactResult


class PortfolioRiskEngine:
    def __init__(self, config: Optional[RiskConfig] = None):
        self.config = config or DEFAULT_CONFIG

    # ------------------------------------------------------------------
    def assess(self, portfolio: PortfolioState, equity_history: Optional[List[float]] = None) -> Dict:
        """Run the full portfolio risk assessment and return the spec
        section 19 output schema as a plain dict."""

        cfg = self.config
        clean_portfolio, report = validate_portfolio(portfolio)
        limits = cfg.limits_for_regime(clean_portfolio.market_regime)

        equity = clean_portfolio.portfolio_equity()
        total_capital = clean_portfolio.total_capital
        open_positions = clean_portfolio.open_positions()

        # ---- Empty-portfolio short circuit --------------------------------
        if not open_positions or equity <= 0:
            report.downgrade(report.status, "empty or zero-equity portfolio — returning minimal-risk baseline")
            return self._empty_report(clean_portfolio, report)

        # ---- Concentration -------------------------------------------------
        conc = concentration_risk(open_positions, equity, limits.max_position_weight, limits.warn_position_weight)

        # ---- Sector ----------------------------------------------------------
        sector = sector_concentration_risk(open_positions, equity, limits.max_sector_weight, limits.warn_sector_weight)

        # ---- Correlation -------------------------------------------------
        corr = correlation_risk(open_positions, cfg.min_history_days, cfg.correlation_high_threshold, report)
        corr_score = correlation_score(corr.average_correlation, corr.maximum_correlation,
                                        limits.warn_avg_correlation, limits.max_avg_correlation)

        # ---- Covariance / volatility ---------------------------------------
        cov = estimate_covariance(open_positions, cfg.min_history_days, cfg.covariance_shrinkage, report)
        cov = portfolio_volatility(cov, open_positions, equity)
        vol_score = volatility_score(cov.portfolio_volatility_annualized, limits.warn_portfolio_volatility,
                                      limits.max_portfolio_volatility)

        # ---- Downside --------------------------------------------------------
        downside = downside_risk(open_positions, equity, cfg.min_history_days, report)

        # ---- VaR ------------------------------------------------------------
        var_result = compute_var(open_positions, equity, cfg.var_confidence_levels, cfg.min_history_days,
                                  cfg.var_horizon_days, cov.portfolio_volatility_daily, report)
        var_95 = var_result.historical_var.get(0.95, var_result.parametric_var.get(0.95, 0.0))
        v_score = var_score(var_95, limits.warn_var_95_pct, limits.max_var_95_pct)

        # ---- Expected Shortfall ----------------------------------------------
        es_result = compute_expected_shortfall(open_positions, equity, cfg.var_confidence_levels,
                                                cfg.min_history_days, report)
        es_95 = es_result.es.get(0.95, 0.0)

        # ---- Drawdown --------------------------------------------------------
        dd = drawdown_risk(clean_portfolio, cfg.drawdown_states, equity_history, report=report)

        # ---- Capital utilization ----------------------------------------------
        cap_util = capital_utilization_risk(clean_portfolio, limits.max_invested_capital_pct,
                                             limits.warn_invested_capital_pct, limits.min_cash_buffer_pct)

        # ---- Liquidity -----------------------------------------------------
        liq = liquidity_risk(open_positions, limits.warn_liquidity_ratio, limits.max_liquidity_ratio, report)

        # ---- Stop-loss -------------------------------------------------------
        stop = stop_loss_risk(open_positions, total_capital, limits.warn_aggregate_stop_risk_pct,
                               limits.max_aggregate_stop_risk_pct, corr.high_correlation_pairs, report)

        # ---- Stress tests ------------------------------------------------------
        stress = run_stress_tests(
            open_positions, equity, sector.weight_by_sector, corr.high_correlation_pairs,
            liq.illiquid_positions, downside.downside_deviation, cfg.stress_scenarios, report,
        )

        # ---- Marginal risk contribution ----------------------------------------
        marginal = marginal_risk_contribution(open_positions, equity, cov)

        # ---- Composite score ---------------------------------------------------
        sub_scores = {
            "concentration": conc.concentration_score,
            "sector": sector.sector_concentration_score,
            "correlation": corr_score,
            "volatility": vol_score,
            "downside": downside.downside_score,
            "tail_risk": max(v_score, self._es_score(es_95, limits.warn_es_95_pct, limits.max_es_95_pct)),
            "drawdown": dd.drawdown_score,
            "capital_utilization": cap_util.capital_utilization_score,
            "stop_loss": stop.stop_loss_score,
            "stress_test": stress.stress_score,
        }
        scoring = compute_portfolio_risk_score(cfg, sub_scores, total_capital, len(open_positions))

        # ---- Risk budget --------------------------------------------------------
        budget = compute_risk_budget(cfg, sub_scores)

        # ---- Risk limits ----------------------------------------------------------
        worst_liquidity = max([r for r in liq.ratios.values() if r is not None], default=0.0)
        limit_report = evaluate_limits(
            limits=limits,
            largest_position_weight=conc.largest_position_weight,
            largest_sector_weight=sector.largest_sector_weight,
            aggregate_stop_risk_pct=stop.aggregate_risk_pct_of_capital,
            portfolio_volatility=cov.portfolio_volatility_annualized,
            var_95_pct=var_95,
            es_95_pct=es_95,
            num_positions=len(open_positions),
            avg_correlation=corr.average_correlation,
            max_pairwise_correlation=corr.maximum_correlation,
            invested_capital_pct=cap_util.invested_pct,
            cash_pct=cap_util.cash_pct,
            current_drawdown=dd.current_drawdown or 0.0,
            worst_stress_loss_pct=abs(min(stress.worst_loss_pct, 0.0)),
            worst_liquidity_ratio=worst_liquidity,
        )

        # ---- Recommendation / decision -----------------------------------------
        recommendation = build_recommendation(limit_report, scoring, dd, marginal)

        return self._build_schema(
            clean_portfolio, total_capital, equity, conc, sector, corr, corr_score, cov, downside,
            var_result, var_95, es_result, es_95, dd, cap_util, liq, stop, stress,
            marginal, scoring, budget, limit_report, recommendation, report,
        )

    # ------------------------------------------------------------------
    def evaluate_new_trade(self, portfolio: PortfolioState, proposed_positions: List[Position]) -> TradeImpactResult:
        return evaluate_trade_impact(portfolio, proposed_positions, self.assess)

    # ------------------------------------------------------------------
    @staticmethod
    def _es_score(es_95: float, warn: float, cap: float) -> float:
        if es_95 <= warn * 0.5:
            return (es_95 / (warn * 0.5)) * 25 if warn > 0 else 0.0
        elif es_95 <= cap:
            span = max(cap - warn * 0.5, 1e-9)
            return 25 + (es_95 - warn * 0.5) / span * 50
        else:
            span = max(cap, 1e-9)
            return max(0.0, min(100.0, 75 + min((es_95 - cap) / span, 1.0) * 25))

    # ------------------------------------------------------------------
    def _empty_report(self, portfolio: PortfolioState, report: DataQualityReport) -> Dict:
        return {
            "portfolio_risk_score": 0.0,
            "risk_class": "LOW",
            "risk_status": "ACCEPT",
            "capital": {
                "total_capital": portfolio.total_capital,
                "invested_capital": 0.0,
                "available_cash": portfolio.available_cash,
                "capital_utilization": 0.0,
            },
            "concentration": {"largest_position_weight": 0.0, "sector_concentration": 0.0, "hhi": 0.0},
            "correlation": {"average_correlation": 0.0, "maximum_correlation": 0.0, "high_correlation_pairs": []},
            "volatility": {"portfolio_volatility": 0.0},
            "tail_risk": {"var_95": 0.0, "expected_shortfall_95": 0.0},
            "drawdown": {"current_drawdown": 0.0, "max_drawdown": 0.0},
            "stop_loss": {"aggregate_risk": 0.0},
            "stress_tests": {},
            "risk_limits": {},
            "risk_drivers": ["Portfolio is empty or has zero equity — no risk to assess."],
            "recommendations": ["No open positions. Portfolio risk gate has nothing to evaluate."],
            "marginal_risk": {"capital_weight": {}, "risk_contribution_pct": {}},
            "risk_budget": {},
            "data_quality": {"status": report.status.value, "notes": report.notes},
        }

    # ------------------------------------------------------------------
    def _build_schema(
        self, portfolio, total_capital, equity, conc, sector, corr, corr_score, cov, downside,
        var_result, var_95, es_result, es_95, dd, cap_util, liq, stop, stress,
        marginal, scoring, budget, limit_report, recommendation, report,
    ) -> Dict:
        return {
            "portfolio_risk_score": round(scoring.portfolio_risk_score, 2),
            "risk_class": scoring.risk_class,
            "risk_status": recommendation.status.value,

            "capital": {
                "total_capital": total_capital,
                "invested_capital": portfolio.invested_capital(),
                "available_cash": portfolio.available_cash,
                "capital_utilization": round(cap_util.invested_pct, 4),
                "capital_tier": scoring.capital_tier,
            },

            "concentration": {
                "largest_position_weight": round(conc.largest_position_weight, 4),
                "largest_position_symbol": conc.largest_position_symbol,
                "top2_weight": round(conc.top2_weight, 4),
                "top5_weight": round(conc.top5_weight, 4),
                "hhi": round(conc.hhi, 4),
                "score": round(conc.concentration_score, 2),
            },

            "sector": {
                "capital_by_sector": sector.capital_by_sector,
                "weight_by_sector": {k: round(v, 4) for k, v in sector.weight_by_sector.items()},
                "largest_sector": sector.largest_sector,
                "largest_sector_weight": round(sector.largest_sector_weight, 4),
                "sector_hhi": round(sector.sector_hhi, 4),
                "score": round(sector.sector_concentration_score, 2),
            },

            "correlation": {
                "average_correlation": round(corr.average_correlation, 4),
                "maximum_correlation": round(corr.maximum_correlation, 4),
                "max_pair": corr.max_pair,
                "high_correlation_pairs": corr.high_correlation_pairs,
                "symbols_excluded": corr.symbols_excluded,
                "score": round(corr_score, 2),
            },

            "volatility": {
                "portfolio_volatility_daily": round(cov.portfolio_volatility_daily, 4),
                "portfolio_volatility_annualized": round(cov.portfolio_volatility_annualized, 4),
                "method": cov.method,
            },

            "downside_risk": {
                "downside_deviation_daily": round(downside.downside_deviation, 4),
                "expected_loss_daily": round(downside.expected_loss, 4),
                "max_observed_adverse_move": round(downside.max_observed_adverse_move, 4),
                "observations_used": downside.observations_used,
            },

            "tail_risk": {
                "var": {str(k): round(v, 4) for k, v in var_result.historical_var.items()},
                "var_parametric": {str(k): round(v, 4) for k, v in var_result.parametric_var.items()},
                "var_method": var_result.method_used,
                "var_95": round(var_95, 4),
                "expected_shortfall": {str(k): round(v, 4) for k, v in es_result.es.items()},
                "expected_shortfall_95": round(es_95, 4),
            },

            "drawdown": {
                "current_drawdown": round(dd.current_drawdown, 4) if dd.current_drawdown is not None else None,
                "max_drawdown": round(dd.max_drawdown, 4) if dd.max_drawdown is not None else None,
                "rolling_drawdown": round(dd.rolling_drawdown, 4) if dd.rolling_drawdown is not None else None,
                "state": dd.state,
            },

            "stop_loss": {
                "per_position_risk": stop.per_position_risk,
                "positions_missing_stop": stop.positions_missing_stop,
                "aggregate_risk_amount": round(stop.aggregate_risk_amount, 2),
                "aggregate_risk_pct_of_capital": round(stop.aggregate_risk_pct_of_capital, 4),
                "largest_single_symbol": stop.largest_single_symbol,
                "correlated_cluster_risk_pct": round(stop.correlated_cluster_risk_pct, 4),
            },

            "liquidity": {
                "ratios": {k: (round(v, 4) if v is not None else None) for k, v in liq.ratios.items()},
                "illiquid_positions": liq.illiquid_positions,
                "worst_symbol": liq.worst_symbol,
                "worst_ratio": round(liq.worst_ratio, 4),
            },

            "stress_tests": {k: round(v, 4) for k, v in stress.scenario_losses_pct.items()},
            "worst_stress_scenario": stress.worst_scenario,

            "marginal_risk": {
                "capital_weight": {k: round(v, 4) for k, v in marginal.capital_weight.items()},
                "risk_contribution_pct": {k: round(v, 4) for k, v in marginal.risk_contribution_pct.items()},
                "disproportionate_symbols": marginal.disproportionate_symbols,
            },

            "risk_budget": {
                "total_budget": budget.total_budget,
                "category_budget": {k: round(v, 2) for k, v in budget.category_budget.items()},
                "category_consumed": {k: round(v, 2) for k, v in budget.category_consumed.items()},
                "category_remaining": {k: round(v, 2) for k, v in budget.category_remaining.items()},
                "total_remaining": round(budget.total_remaining, 2),
            },

            "risk_limits": {k: v.status.value for k, v in limit_report.checks.items()},
            "risk_limit_detail": {
                k: {"value": round(v.value, 4), "warn": v.warn_threshold, "max": v.max_threshold, "status": v.status.value}
                for k, v in limit_report.checks.items()
            },

            "risk_drivers": recommendation.primary_drivers,
            "recommendations": recommendation.recommendations,
            "decision_explanation": recommendation.explanation,

            "sub_scores": {k: round(v, 2) for k, v in scoring.sub_scores.items()},
            "classification_notes": scoring.classification_notes,

            "data_quality": {"status": report.status.value, "notes": report.notes},
        }
