"""
TradeX Phase 3 — Capital Management, wired end to end.

    Portfolio Manager -> Position Sizing -> Capital Feasibility -> Portfolio Risk
                              ^                     |
                              +---------------------+
                          (verified affordability re-sizes the trade)

Public surface:

    from phase3 import Phase3Engine, Phase3Request, ExecutionInput, run_phase3
    from phase3.costs import CostModelProvider, ApproximateZerodhaCostProvider

    engine = Phase3Engine(cost_provider=CostModelProvider(cost_engine, cost_models))
    result = engine.run(Phase3Request(
        portfolio_state=portfolio_state,      # portfolio_manager.PortfolioState
        candidates=candidates,                 # List[PortfolioCandidate]
        market_context=market_context,
        execution_inputs={opp_id: ExecutionInput(...)},
        committed_capital=Decimal("0"),
    ))

    for instruction in result.instructions:   # -> Execution Engine -> Zerodha Kite
        ...

Everything upstream of the Portfolio Manager (Market Data, FILTER2.0,
Feature Engine, Return/Risk/Regime/Opportunity/Cost models) and
everything downstream of the instruction list (Execution Engine,
Position Manager, Trade Journal) is out of scope here by design.
"""
from .config import DEFAULT_CONFIG, Phase3Config
from .costs import ApproximateZerodhaCostProvider, CostModelProvider, CostProvider
from .enums import AllocationStatus, Phase3Status, Stage
from .models import (
    CandidateOutcome,
    CapitalSummary,
    ExecutionInput,
    Phase3Request,
    Phase3Result,
    TradeInstruction,
)
from .pipeline import Phase3Engine, run_phase3
from .serialization import result_to_dict

__version__ = "1.0.0"

__all__ = [
    "Phase3Engine",
    "run_phase3",
    "Phase3Request",
    "Phase3Result",
    "Phase3Config",
    "DEFAULT_CONFIG",
    "ExecutionInput",
    "TradeInstruction",
    "CandidateOutcome",
    "CapitalSummary",
    "AllocationStatus",
    "Phase3Status",
    "Stage",
    "CostProvider",
    "CostModelProvider",
    "ApproximateZerodhaCostProvider",
    "result_to_dict",
    "__version__",
]
