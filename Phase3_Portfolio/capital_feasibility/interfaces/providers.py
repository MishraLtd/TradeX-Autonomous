"""
PART — PORTFOLIO MANAGER PROVIDER ADAPTER (spec §15, §27, §28)

portfolio_manager.interfaces.providers.CapitalFeasibilityProvider is an
ABC placeholder the Portfolio Manager package already ships, with one
method:

    check_feasibility(decision: PortfolioDecision, sized: Dict[str, int]) -> Dict[str, Any]

This module implements that ABC as a thin batch adapter over
`evaluate_trade()` / `api.evaluate()`, so a caller that already has a
PortfolioDecision (selected candidates) and a `{opportunity_id: quantity}`
map from Position Sizing can get one CapitalFeasibilityResult per
candidate without hand-rolling the loop. It does NOT replace the richer
typed `evaluate()` entry point in api.py — that remains the primary
interface (spec §31); this exists purely to satisfy the contract the
Portfolio Manager package already defined for "the next Phase 3
component" (spec §33 in that package's own docs).

This also directly answers spec §15 (multiple candidate trades): each
candidate is evaluated independently against the SAME starting
`account_state`, so the caller can see which are individually feasible
and how much capital each would consume — full multi-candidate capital
sequencing/optimization (which candidates to actually take together) is
explicitly left to the Portfolio Manager / a future allocator (spec §15:
"do not build a complex optimizer unless the repository already requires
one"), not decided inside this provider.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Optional

from ..adapters.cost_adapter import price_buy_leg
from ..adapters.portfolio_adapter import account_state_from_portfolio, asset_exposure_from_portfolio
from ..config import CapitalFeasibilityConfig, DEFAULT_CONFIG
from ..engine import evaluate_trade
from ..models import LiquidityContext, TradeCapitalRequest

try:
    from portfolio_manager.interfaces.providers import CapitalFeasibilityProvider as _BaseProvider
except ImportError:  # pragma: no cover - portfolio_manager is an optional runtime dependency
    class _BaseProvider:  # type: ignore[no-redef]
        """Fallback base when portfolio_manager isn't installed in this
        environment (e.g. running capital_feasibility's own unit tests in
        isolation) — preserves the same method shape without a hard
        import dependency."""

        def check_feasibility(self, decision, sized):  # noqa: D401
            raise NotImplementedError


class PortfolioManagerCapitalFeasibilityProvider(_BaseProvider):
    def __init__(
        self,
        cost_engine,
        cost_models_module,
        config: Optional[CapitalFeasibilityConfig] = None,
        sector_lookup: Optional[Dict[str, str]] = None,
        lot_size: int = 1,
        committed_capital: Optional[Decimal] = None,
    ):
        """
        sector_lookup: optional {symbol: sector} map, since
        PortfolioDecision/PortfolioCandidate objects are looked up by
        opportunity_id and the candidate's own `sector` field is not
        threaded through PortfolioDecision — callers that have it should
        pass it here; otherwise sector-concentration (Gate 4) degrades to
        a no-op per candidate, exactly as it does when asset_exposure's
        sector is None elsewhere in this package.
        """
        self.cost_engine = cost_engine
        self.cost_models_module = cost_models_module
        self.config = config or DEFAULT_CONFIG
        self.sector_lookup = sector_lookup or {}
        self.lot_size = lot_size
        self.committed_capital = committed_capital

    def check_feasibility(
        self,
        decision,       # portfolio_manager.decision.PortfolioDecision
        sized: Dict[str, int],   # {opportunity_id: recommended_quantity} from Position Sizing
        candidates_by_id: Optional[Dict[str, Any]] = None,  # {opportunity_id: PortfolioCandidate}, for price/exchange/trade_type
        portfolio_state=None,    # portfolio_manager.portfolio_state.PortfolioState
    ) -> Dict[str, Any]:
        if portfolio_state is None:
            raise ValueError(
                "PortfolioManagerCapitalFeasibilityProvider.check_feasibility requires "
                "portfolio_state (not part of the base ABC signature, but required to "
                "compute capital state) — pass it as a keyword argument."
            )
        candidates_by_id = candidates_by_id or {}

        account_state = account_state_from_portfolio(
            portfolio_state=portfolio_state,
            config=self.config,
            committed_capital=self.committed_capital,
        )

        results: Dict[str, Any] = {}
        for opportunity_id in decision.selected_candidates:
            requested_qty = sized.get(opportunity_id, 0)
            candidate = candidates_by_id.get(opportunity_id)
            if candidate is None:
                results[opportunity_id] = {
                    "error": "MISSING_CANDIDATE_DETAIL",
                    "detail": f"No candidate detail supplied for {opportunity_id}; cannot price the trade.",
                }
                continue

            sector = self.sector_lookup.get(candidate.symbol, getattr(candidate, "sector", None))

            entry_price_raw = getattr(candidate, "entry_price", None)
            entry_price = Decimal(str(entry_price_raw)) if entry_price_raw is not None else Decimal("0")

            trade_request = TradeCapitalRequest(
                position_id=opportunity_id,
                opportunity_id=opportunity_id,
                symbol=candidate.symbol,
                exchange=candidate.exchange,
                sector=sector,
                trade_type=candidate.trade_type.value,
                entry_price=entry_price,
                requested_quantity=requested_qty,
                lot_size=self.lot_size,
                liquidity=LiquidityContext(),
            )

            exposure = asset_exposure_from_portfolio(
                portfolio_state=portfolio_state, symbol=candidate.symbol, sector=sector
            )

            def _price_fn(qty, _entry_price=trade_request.entry_price, _symbol=candidate.symbol,
                          _exchange=candidate.exchange, _trade_type=candidate.trade_type.value):
                return price_buy_leg(
                    self.cost_engine,
                    self.cost_models_module,
                    _symbol,
                    _exchange,
                    _trade_type,
                    _entry_price,
                    qty,
                    available_capital=account_state.deployable_capital,
                )

            result = evaluate_trade(
                request=trade_request,
                account_state=account_state,
                exposure=exposure,
                price_fn=_price_fn,
                config=self.config,
            )
            results[opportunity_id] = result

        return results
