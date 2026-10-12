"""
Worked example (spec §59, §60): five illustrative candidates, ranked, with
full explanations. Run: python -m opportunity_model.example

These input numbers are illustrative only (per spec §59) - NOT validated
production inputs.
"""

from __future__ import annotations

from opportunity_model.testing_helpers import make_candidate, NOW
from opportunity_model.ranking import rank_opportunities
from opportunity_model.explainability import explain_opportunity
from opportunity_model.schemas import MarketRegimeLabel


def build_candidates():
    return [
        make_candidate(
            symbol="CAND_A", expected_return_pct=2.4, probability_of_profit=0.71,
            expected_downside_pct=0.9, expected_mae_pct=1.3, probability_stop_loss=0.22,
            cost_ratio=0.146,  # -> net ~2.05%
            regime=MarketRegimeLabel.TRENDING_UP,
            regime_compatibility=0.85, holding_period_days=2, capital_required=280,
        ),
        make_candidate(
            symbol="CAND_B", expected_return_pct=3.8, probability_of_profit=0.58,
            expected_downside_pct=2.5, expected_mae_pct=4.2, probability_stop_loss=0.42,
            cost_ratio=0.21,  # -> net ~3.0%
            regime_compatibility=0.6, holding_period_days=5, capital_required=350,
        ),
        make_candidate(
            symbol="CAND_C", expected_return_pct=1.7, probability_of_profit=0.79,
            expected_downside_pct=0.7, expected_mae_pct=0.95, probability_stop_loss=0.14,
            cost_ratio=0.147,  # -> net ~1.45%
            regime_compatibility=0.8, holding_period_days=1, capital_required=250,
        ),
        make_candidate(
            symbol="CAND_D", expected_return_pct=2.9, probability_of_profit=0.65,
            expected_downside_pct=1.8, expected_mae_pct=2.6, probability_stop_loss=0.35,
            cost_ratio=0.414,  # -> net ~1.70%
            regime_compatibility=0.55, holding_period_days=3, capital_required=320,
        ),
        make_candidate(
            symbol="CAND_E", expected_return_pct=2.0, probability_of_profit=0.73,
            expected_downside_pct=1.0, expected_mae_pct=1.4, probability_stop_loss=0.24,
            cost_ratio=0.15,  # -> net ~1.70%
            regime_compatibility=0.25,  # poor regime compatibility
            holding_period_days=2, capital_required=260,
        ),
    ]


def main():
    candidates = build_candidates()
    result = rank_opportunities(candidates, now=NOW)

    print("=" * 78)
    print(f"TradeX Opportunity Engine — cycle result "
          f"({result.total_candidates} candidates, {result.eligible_count} eligible)")
    print("=" * 78)

    if result.no_trade:
        print(f"\nNO TRADE — {result.no_trade_reason}\n")
    else:
        for assessment in result.top_opportunities:
            print(explain_opportunity(assessment, total_candidates=result.eligible_count))
            print("-" * 78)

    print("\nFull candidate ledger (including rejected):\n")
    for a in result.all_assessments:
        score = a.opportunity_score if a.opportunity_score is not None else "N/A"
        reasons = ", ".join(r.value for r in a.rejection_reasons) if a.rejection_reasons else "-"
        print(f"{a.symbol:10s} status={a.eligibility_status.value:10s} score={score!s:6s} reasons={reasons}")


if __name__ == "__main__":
    main()
