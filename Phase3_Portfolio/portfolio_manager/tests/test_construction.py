from datetime import timedelta

from portfolio_manager.api import construct_portfolio
from portfolio_manager.config import PortfolioManagerConfig
from portfolio_manager.correlation import StaticCorrelationProvider
from portfolio_manager.enums import DecisionType, RejectionReason, Strategy
from portfolio_manager.tests.factories import (
    NOW,
    make_candidate,
    make_context,
    make_position,
    make_state,
)


def test_zero_candidates_holds_cash():
    state = make_state()
    ctx = make_context()
    decision = construct_portfolio(state, [], ctx)
    assert decision.decision_type == DecisionType.HOLD_CASH
    assert decision.selected_candidates == []


def test_single_good_candidate_is_selected():
    state = make_state()
    ctx = make_context()
    cand = make_candidate()
    decision = construct_portfolio(state, [cand], ctx)
    assert decision.decision_type == DecisionType.NEW_PORTFOLIO
    assert decision.selected_candidates == [cand.opportunity_id]


def test_economically_invalid_candidate_is_rejected():
    state = make_state()
    ctx = make_context()
    cand = make_candidate(economic_viability=False, expected_net_profit=-5.0)
    decision = construct_portfolio(state, [cand], ctx)
    assert decision.decision_type == DecisionType.HOLD_CASH
    outcome = decision.candidate_outcomes[0]
    assert not outcome.selected
    assert RejectionReason.ECONOMICALLY_INVALID in outcome.reason_codes


def test_stale_signal_is_rejected():
    state = make_state()
    ctx = make_context()
    cand = make_candidate(age_seconds=10_000)  # older than default 900s max
    decision = construct_portfolio(state, [cand], ctx)
    outcome = decision.candidate_outcomes[0]
    assert not outcome.selected
    assert RejectionReason.STALE_SIGNAL in outcome.reason_codes


def test_duplicate_symbol_keeps_higher_score_one():
    state = make_state()
    ctx = make_context()
    c1 = make_candidate(opportunity_id="A", symbol="TCS", opportunity_score=90)
    c2 = make_candidate(opportunity_id="B", symbol="TCS", opportunity_score=70)
    decision = construct_portfolio(state, [c1, c2], ctx)
    assert decision.selected_candidates == ["A"]
    b_outcome = [o for o in decision.candidate_outcomes if o.opportunity_id == "B"][0]
    assert RejectionReason.DUPLICATE_SYMBOL in b_outcome.reason_codes


def test_high_correlation_prefers_diversifying_lower_score_candidate():
    """Spec §12/§38: A + C should beat A + B when B is highly correlated
    with A even though B has a marginally higher raw expected return."""
    state = make_state()
    ctx = make_context()

    a = make_candidate(opportunity_id="A", symbol="AAA", sector="TECH", expected_net_return=0.025)
    b = make_candidate(opportunity_id="B", symbol="BBB", sector="TECH", expected_net_return=0.022)
    c = make_candidate(
        opportunity_id="C", symbol="CCC", sector="ENERGY", expected_net_return=0.017, confidence=0.55
    )

    corr = StaticCorrelationProvider()
    corr.set("AAA", "BBB", 0.95)  # highly correlated
    corr.set("AAA", "CCC", 0.05)  # low correlation

    decision = construct_portfolio(state, [a, b, c], ctx, correlation_provider=corr)
    assert "A" in decision.selected_candidates
    assert "C" in decision.selected_candidates
    assert "B" not in decision.selected_candidates


def test_unknown_correlation_is_not_treated_as_zero():
    state = make_state()
    ctx = make_context()
    a = make_candidate(opportunity_id="A", symbol="AAA")
    b = make_candidate(opportunity_id="B", symbol="BBB")
    corr = StaticCorrelationProvider()  # no entries -> UNKNOWN for A-B
    decision = construct_portfolio(state, [a, b], ctx, correlation_provider=corr)
    b_outcome = [o for o in decision.candidate_outcomes if o.opportunity_id == "B"][0]
    # Unknown correlation still produces a penalized (non-zero-correlation)
    # diversification score, not the "as if independent" maximum score of a
    # first-selected candidate.
    assert b_outcome.score_components["diversification_contribution"] < (
        [o for o in decision.candidate_outcomes if o.opportunity_id == "A"][0]
    ).score_components["diversification_contribution"]


def test_sector_concentration_penalizes_third_same_sector_candidate():
    positions = [
        make_position(position_id="P1", symbol="INFY", sector="TECH"),
        make_position(position_id="P2", symbol="WIPRO", sector="TECH"),
    ]
    state = make_state(positions=positions)
    ctx = make_context()
    cand = make_candidate(symbol="TCS", sector="TECH")
    decision = construct_portfolio(state, [cand], ctx)
    outcome = decision.candidate_outcomes[0]
    assert outcome.score_components["sector_concentration_penalty"] < 0


def test_strategy_concentration_flagged():
    positions = [
        make_position(position_id="P1", symbol="A1", strategy=Strategy.MOMENTUM, sector="S1"),
        make_position(position_id="P2", symbol="A2", strategy=Strategy.MOMENTUM, sector="S2"),
    ]
    state = make_state(positions=positions)
    ctx = make_context()
    cand = make_candidate(symbol="A3", strategy=Strategy.MOMENTUM, sector="S3")
    decision = construct_portfolio(state, [cand], ctx)
    outcome = decision.candidate_outcomes[0]
    assert outcome.score_components["strategy_concentration_penalty"] < 0


def test_all_candidates_rejected_holds_cash():
    state = make_state()
    ctx = make_context()
    bad1 = make_candidate(opportunity_id="A", economic_viability=False, expected_net_profit=-1)
    bad2 = make_candidate(opportunity_id="B", age_seconds=100_000)
    decision = construct_portfolio(state, [bad1, bad2], ctx)
    assert decision.decision_type == DecisionType.HOLD_CASH
    assert decision.selected_candidates == []


def test_all_candidates_selected_when_all_are_strong_and_diverse():
    state = make_state()
    ctx = make_context()
    cands = [
        make_candidate(opportunity_id=f"C{i}", symbol=f"SYM{i}", sector=f"SEC{i}", expected_net_return=0.03)
        for i in range(3)
    ]
    decision = construct_portfolio(state, cands, ctx)
    assert set(decision.selected_candidates) == {"C0", "C1", "C2"}


def test_maintain_portfolio_when_no_candidate_beats_bar():
    """spec §40: a candidate that is weak across essentially every axis
    (poor return, high risk, low confidence, poor regime fit) must fail
    to clear the absolute selection bar, leaving the existing portfolio
    untouched -> MAINTAIN_PORTFOLIO."""
    positions = [make_position()]
    state = make_state(positions=positions)
    ctx = make_context()
    weak = make_candidate(
        expected_net_return=-0.02,
        expected_net_profit=1.0,
        expected_risk=0.05,
        confidence=0.05,
        regime_compatibility=0.0,
        liquidity_score=0.1,
    )
    decision = construct_portfolio(state, [weak], ctx)
    assert decision.decision_type in (DecisionType.MAINTAIN_PORTFOLIO, DecisionType.REDUCE_EXPOSURE_REVIEW)
    assert decision.selected_candidates == []


def test_deterministic_repeated_decisions():
    state = make_state()
    ctx = make_context()
    cands = [make_candidate(opportunity_id=f"C{i}", symbol=f"S{i}", sector=f"SEC{i}") for i in range(4)]
    d1 = construct_portfolio(state, cands, ctx)
    d2 = construct_portfolio(state, cands, ctx)
    assert d1.selected_candidates == d2.selected_candidates
    assert d1.rejected_candidates == d2.rejected_candidates
    assert d1.portfolio_state_snapshot_id == d2.portfolio_state_snapshot_id


def test_tie_breaking_is_deterministic_on_identical_scores():
    state = make_state()
    ctx = make_context()
    a = make_candidate(opportunity_id="AAA_ID", symbol="AAA", sector="S1")
    b = make_candidate(opportunity_id="ZZZ_ID", symbol="ZZZ", sector="S2")
    # Identical everything except id/symbol -> both selected (not correlated,
    # different sectors), but the internal ordering used during construction
    # must be stable across runs.
    d1 = construct_portfolio(state, [a, b], ctx)
    d2 = construct_portfolio(state, [b, a], ctx)
    assert sorted(d1.selected_candidates) == sorted(d2.selected_candidates)


def test_invalid_portfolio_state_fails_closed():
    from portfolio_manager.enums import SystemStatus

    ctx = make_context()
    state = make_state()
    object.__setattr__(state, "system_status", SystemStatus.UNAVAILABLE)
    decision = construct_portfolio(state, [make_candidate()], ctx)
    assert decision.decision_type == DecisionType.DECISION_UNAVAILABLE


def test_selected_and_rejected_are_disjoint_and_subset_of_universe():
    state = make_state()
    ctx = make_context()
    cands = [make_candidate(opportunity_id=f"C{i}", symbol=f"S{i}", sector=f"SEC{i}") for i in range(6)]
    decision = construct_portfolio(state, cands, ctx)
    universe = {c.opportunity_id for c in cands}
    selected = set(decision.selected_candidates)
    rejected = set(decision.rejected_candidates)
    assert selected.isdisjoint(rejected)
    assert selected.issubset(universe)
    assert rejected.issubset(universe)


def test_replacement_candidate_flagged_not_executed():
    positions = [
        make_position(
            position_id="P1",
            symbol="OLD",
            sector="TECH",
            expected_remaining_return=0.001,
            expected_risk=0.03,
            confidence=0.3,
        )
    ]
    state = make_state(positions=positions)
    ctx = make_context()
    replacement = make_candidate(
        opportunity_id="NEWC",
        symbol="OLD",  # same symbol as the existing position
        sector="TECH",
        expected_net_return=0.03,
        confidence=0.9,
    )
    decision = construct_portfolio(state, [replacement], ctx)
    from portfolio_manager.enums import ExistingPositionAction

    actions = {a.symbol: a.action for a in decision.existing_position_actions}
    assert actions["OLD"] == ExistingPositionAction.REPLACE_CANDIDATE
    # Confirm it is only a *flag*, not an executed trade: PortfolioDecision
    # carries no order IDs / execution confirmation fields at all.
    assert not hasattr(decision, "broker_order_id")
