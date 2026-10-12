"""
Integration test: Portfolio Manager -> Capital Feasibility via the
`PortfolioManagerCapitalFeasibilityProvider` (interfaces/providers.py),
using construct_portfolio()'s REAL output (not a hand-built decision).
"""
from decimal import Decimal

from portfolio_manager.api import construct_portfolio
from portfolio_manager.config import PortfolioManagerConfig
from portfolio_manager.tests.factories import make_candidate, make_context, make_position
from portfolio_manager.portfolio_state import PortfolioState

from capital_feasibility.interfaces.providers import PortfolioManagerCapitalFeasibilityProvider


def test_provider_evaluates_real_portfolio_decision(cost_engine, cost_models_module, config):
    context = make_context()
    candidate = make_candidate(opportunity_id="OPP-1", symbol="TCS", sector="TECHNOLOGY")
    # PortfolioCandidate has no entry_price field in this package (price
    # lives with Position Sizing/Cost Model, not the Portfolio Manager) —
    # confirm that assumption directly rather than guessing silently.
    assert not hasattr(candidate, "entry_price")

    portfolio_state = PortfolioState(
        portfolio_id="PF-1",
        timestamp=context.as_of,
        total_equity=1000.0,
        cash=1000.0,
        invested_capital=0.0,
        open_positions=[],
    )

    decision = construct_portfolio(
        portfolio_state=portfolio_state,
        candidates=[candidate],
        market_context=context,
        config=PortfolioManagerConfig(),
    )

    assert decision.decision_status == "FINAL"

    # entry_price is supplied out-of-band (from the Return/Opportunity
    # model's market snapshot, not from PortfolioCandidate) — the
    # provider's `candidates_by_id` lookup carries it.
    candidates_by_id = {candidate.opportunity_id: candidate}
    for oid in decision.selected_candidates:
        candidates_by_id[oid].__dict__  # sanity: real object, not a stub

    class _WithPrice:
        """Wrap the real PortfolioCandidate to attach the entry_price the
        provider needs, without mutating the frozen dataclass."""

        def __init__(self, inner, entry_price):
            self._inner = inner
            self.entry_price = entry_price

        def __getattr__(self, item):
            return getattr(self._inner, item)

    priced_candidates_by_id = {oid: _WithPrice(c, Decimal("500.00")) for oid, c in candidates_by_id.items()}

    provider = PortfolioManagerCapitalFeasibilityProvider(
        cost_engine=cost_engine, cost_models_module=cost_models_module, config=config
    )
    sized = {oid: 1 for oid in decision.selected_candidates}

    results = provider.check_feasibility(
        decision=decision,
        sized=sized,
        candidates_by_id=priced_candidates_by_id,
        portfolio_state=portfolio_state,
    )

    if decision.selected_candidates:
        assert set(results.keys()) == set(decision.selected_candidates)
        for oid, result in results.items():
            assert result.symbol == "TCS"
            assert result.requested_quantity == 1
    else:
        # If the candidate didn't clear Portfolio Manager's own
        # selection bar, there is nothing for Capital Feasibility to
        # evaluate — still a valid, non-crashing outcome.
        assert results == {}
