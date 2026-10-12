"""
Phase 3 end-to-end orchestration.

    PortfolioDecision            (portfolio_manager)
            |  selected candidates, in PM's own priority order
            v
    pre-trade portfolio risk     (portfolio_risk.assess)
            |  risk_multiplier / halt / max position value
            v
    Position Sizing              (position_sizing.calculate_position_size)
            |  recommended_quantity
            v
    Capital Feasibility          (capital_feasibility.evaluate_trade)
            |  feasible_quantity  --(if smaller)--> re-size once, re-verify
            v
    post-trade portfolio risk    (portfolio_risk.evaluate_new_trade)
            |  no new breach? --(if breach)--> retry smaller, else reject
            v
    TradeInstruction  -> ledger (cash, exposure, working portfolio)
            |
            v
    next candidate sees the updated portfolio

Design rules this file enforces:

1. Sequential, ledger-aware. Candidate N is evaluated against the
   portfolio as it will be after candidates 1..N-1 are filled.
2. Fail closed. A cost-pricing failure aborts the run with
   PIPELINE_ERROR and emits NO instructions — a partially-funded batch
   priced on a broken cost model is worse than no batch.
3. No component's verdict is second-guessed. Phase 3 shrinks a quantity
   or drops a trade; it never raises a size above what a component
   allowed, and never re-derives a number a component already produced.
4. Every rejection records WHICH stage rejected it and keeps that
   stage's own result object for audit.
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from capital_feasibility.engine import evaluate_trade
from capital_feasibility.enums import FeasibilityStatus
from capital_feasibility.exceptions import CostModelIntegrationError
from portfolio_manager import __version__ as pm_version
from portfolio_manager.api import construct_portfolio
from portfolio_manager.enums import DecisionType
from portfolio_risk.portfolio_risk_engine import PortfolioRiskEngine
from position_sizing.engine import calculate_position_size
from position_sizing.enums import SizingStatus
from position_sizing.models import CapitalFeasibilityConstraint

from . import adapters
from .config import DEFAULT_CONFIG, Phase3Config
from .costs import CostProvider
from .enums import AllocationStatus, Phase3Status, Stage
from .ledger import AllocationLedger
from .models import (
    CandidateOutcome,
    CapitalSummary,
    ExecutionInput,
    Phase3Request,
    Phase3Result,
    TradeInstruction,
)


def _floor_to_lot(quantity: int, lot_size: int) -> int:
    if lot_size <= 1:
        return max(0, quantity)
    return (max(0, quantity) // lot_size) * lot_size


class Phase3Engine:
    """Holds the wiring (config + cost source + risk engine) so a live
    loop or a backtester can construct it once and call `run` per bar."""

    def __init__(self, cost_provider: CostProvider, config: Optional[Phase3Config] = None):
        self.config = config or DEFAULT_CONFIG
        self.cost_provider = cost_provider
        self.risk_engine = PortfolioRiskEngine(self.config.risk)

    # ------------------------------------------------------------------ #
    # Public entry point
    # ------------------------------------------------------------------ #
    def run(self, request: Phase3Request, correlation_provider=None) -> Phase3Result:
        cfg = self.config
        run_id = str(uuid.uuid4())
        warnings: List[str] = []

        decision = construct_portfolio(
            portfolio_state=request.portfolio_state,
            candidates=request.candidates,
            market_context=request.market_context,
            config=cfg.portfolio_manager,
            correlation_provider=correlation_provider,
        )

        versions = {
            "phase3": cfg.version,
            "portfolio_manager": pm_version,
            "position_sizing": "0.1.0",
            "capital_feasibility": cfg.feasibility.version,
            "portfolio_risk": "1.0.0",
        }

        if decision.decision_type == DecisionType.DECISION_UNAVAILABLE:
            return Phase3Result(
                run_id=run_id,
                timestamp=request.market_context.as_of,
                portfolio_id=decision.portfolio_id,
                status=Phase3Status.DECISION_UNAVAILABLE,
                instructions=[],
                outcomes=[],
                portfolio_decision=decision,
                explanation=f"Portfolio Manager failed closed: {decision.decision_explanation}",
                component_versions=versions,
            )

        ledger = AllocationLedger(
            portfolio_state=request.portfolio_state,
            config=cfg,
            initial_committed_capital=request.committed_capital,
        )
        if request.committed_capital is None:
            warnings.append(
                "committed_capital not supplied — treated as 0 for arithmetic but flagged "
                "UNKNOWN on the capital state (capital_feasibility spec §38)."
            )

        base_risk_portfolio = adapters.risk_portfolio_from_pm(
            request.portfolio_state, request.open_position_inputs, cfg
        )
        risk_before = self.risk_engine.assess(base_risk_portfolio, request.equity_history)

        candidates_by_id = {c.opportunity_id: c for c in request.candidates}
        selected = self._ordered_selection(decision, candidates_by_id)

        outcomes: List[CandidateOutcome] = []
        instructions: List[TradeInstruction] = []

        if not selected:
            return self._finalize(
                run_id, request, decision, ledger, risk_before, risk_before,
                instructions, outcomes, warnings, versions,
                Phase3Status.NO_CANDIDATES,
                "Portfolio Manager selected no candidates; nothing to size or fund.",
            )

        if risk_before.get("risk_status") in cfg.halt_risk_statuses:
            for candidate in selected:
                outcomes.append(
                    CandidateOutcome(
                        opportunity_id=candidate.opportunity_id,
                        symbol=candidate.symbol,
                        status=AllocationStatus.SKIPPED_RISK_HALT,
                        terminal_stage=Stage.PRE_TRADE_RISK,
                        reason=(
                            f"Portfolio risk status {risk_before.get('risk_status')} — "
                            f"no new positions. {risk_before.get('decision_explanation', '')}".strip()
                        ),
                    )
                )
            return self._finalize(
                run_id, request, decision, ledger, risk_before, risk_before,
                instructions, outcomes, warnings, versions,
                Phase3Status.RISK_HALTED,
                f"New positions halted by Portfolio Risk ({risk_before.get('risk_status')}).",
            )

        current_risk_report = risk_before

        try:
            for priority, candidate in enumerate(selected, start=1):
                outcome, current_risk_report = self._allocate_one(
                    candidate=candidate,
                    priority=priority,
                    request=request,
                    ledger=ledger,
                    current_risk_report=current_risk_report,
                )
                outcomes.append(outcome)
                if outcome.instruction is not None:
                    instructions.append(outcome.instruction)
        except CostModelIntegrationError as exc:
            return Phase3Result(
                run_id=run_id,
                timestamp=request.market_context.as_of,
                portfolio_id=decision.portfolio_id,
                status=Phase3Status.PIPELINE_ERROR,
                instructions=[],
                outcomes=outcomes,
                portfolio_decision=decision,
                existing_position_actions=list(decision.existing_position_actions),
                risk_before=risk_before,
                warnings=warnings + [str(exc)],
                explanation=(
                    "Cost Model could not price a leg; the whole batch is abandoned rather "
                    "than emitting instructions funded by an unpriced trade."
                ),
                component_versions=versions,
            )

        risk_after = current_risk_report
        status = Phase3Status.OK
        explanation = (
            f"{len(instructions)} of {len(selected)} selected candidate(s) funded; "
            f"₹{ledger.allocated_capital} committed. Portfolio risk "
            f"{risk_before.get('portfolio_risk_score', 0):.1f} -> "
            f"{risk_after.get('portfolio_risk_score', 0):.1f}."
        )

        return self._finalize(
            run_id, request, decision, ledger, risk_before, risk_after,
            instructions, outcomes, warnings, versions, status, explanation,
        )

    # ------------------------------------------------------------------ #
    # Per-candidate chain
    # ------------------------------------------------------------------ #
    def _allocate_one(
        self, candidate, priority: int, request: Phase3Request,
        ledger: AllocationLedger, current_risk_report: Dict,
    ) -> Tuple[CandidateOutcome, Dict]:
        cfg = self.config
        exec_input = request.execution_inputs.get(candidate.opportunity_id)

        if exec_input is None:
            return (
                CandidateOutcome(
                    opportunity_id=candidate.opportunity_id,
                    symbol=candidate.symbol,
                    status=AllocationStatus.REJECTED_MISSING_EXECUTION_INPUT,
                    terminal_stage=Stage.EXECUTION_INPUT,
                    reason=(
                        "No ExecutionInput supplied (entry price, stop, lot size). Phase 3 will "
                        "not invent a price, so this candidate cannot be sized."
                    ),
                ),
                current_risk_report,
            )

        # --- working views of the portfolio, as of this candidate's turn ---
        account_state = ledger.account_state()
        working_risk_portfolio = adapters.risk_portfolio_from_pm(
            request.portfolio_state,
            request.open_position_inputs,
            cfg,
            extra_positions=list(ledger.accepted_positions),
            available_cash_override=float(
                adapters.d(request.portfolio_state.cash) - ledger.allocated_capital
            ),
        )

        risk_constraint = adapters.risk_sizing_constraint(current_risk_report, cfg)
        if risk_constraint.halt_sizing:
            return (
                CandidateOutcome(
                    opportunity_id=candidate.opportunity_id,
                    symbol=candidate.symbol,
                    status=AllocationStatus.SKIPPED_RISK_HALT,
                    terminal_stage=Stage.PRE_TRADE_RISK,
                    reason=(
                        f"Portfolio risk status {current_risk_report.get('risk_status')} after "
                        f"earlier allocations in this batch — no further new positions."
                    ),
                ),
                current_risk_report,
            )

        max_position_value = adapters.risk_position_cap(working_risk_portfolio, cfg)
        position_id = f"{candidate.opportunity_id}:{uuid.uuid4().hex[:8]}"

        sizing_request = adapters.sizing_request_from_candidate(
            candidate=candidate,
            exec_input=exec_input,
            position_id=position_id,
            capital_available=max(Decimal("0"), account_state.deployable_capital),
            market_context=request.market_context,
            config=cfg,
            max_position_value=max_position_value,
            portfolio_priority=priority,
        )
        pm_instruction = adapters.pm_instruction(priority)

        # --- sizing <-> feasibility loop -------------------------------
        capital_constraint: Optional[CapitalFeasibilityConstraint] = None
        sizing_result = None
        feasibility = None
        notes: List[str] = []

        for iteration in range(cfg.max_sizing_iterations):
            sizing_result = calculate_position_size(
                request=sizing_request,
                capital_constraint=capital_constraint,
                portfolio_risk_constraint=risk_constraint,
                portfolio_manager_instruction=pm_instruction,
                config=cfg.sizing,
            )

            if (
                sizing_result.sizing_status in (SizingStatus.NO_POSITION, SizingStatus.SIZING_INCOMPLETE)
                or sizing_result.recommended_quantity <= 0
            ):
                return (
                    CandidateOutcome(
                        opportunity_id=candidate.opportunity_id,
                        symbol=candidate.symbol,
                        status=AllocationStatus.REJECTED_BY_SIZING,
                        terminal_stage=Stage.POSITION_SIZING,
                        reason=sizing_result.rejection_reason
                        or f"Position Sizing returned {sizing_result.sizing_status.value} at quantity 0.",
                        sizing_result=sizing_result,
                        notes=notes,
                    ),
                    current_risk_report,
                )

            feasibility = self._evaluate_feasibility(
                sizing_request=sizing_request,
                quantity=sizing_result.recommended_quantity,
                candidate=candidate,
                exec_input=exec_input,
                ledger=ledger,
                account_state=account_state,
                request=request,
            )

            if feasibility.feasible_quantity == sizing_result.recommended_quantity:
                break

            if feasibility.feasible_quantity <= 0:
                return (
                    CandidateOutcome(
                        opportunity_id=candidate.opportunity_id,
                        symbol=candidate.symbol,
                        status=AllocationStatus.REJECTED_BY_CAPITAL,
                        terminal_stage=Stage.CAPITAL_FEASIBILITY,
                        reason=feasibility.explanation,
                        requested_quantity=sizing_result.recommended_quantity,
                        sizing_result=sizing_result,
                        feasibility_result=feasibility,
                        notes=notes,
                    ),
                    current_risk_report,
                )

            if iteration + 1 >= cfg.max_sizing_iterations:
                notes.append(
                    f"Sizing recommended {sizing_result.recommended_quantity}; capital allows "
                    f"{feasibility.feasible_quantity} ({feasibility.feasibility_status.value})."
                )
                break

            # Close the loop: hand Position Sizing the VERIFIED affordability
            # figure and let it re-derive a size under that cap, rather than
            # blindly truncating a risk-derived size to whatever fits.
            from capital_feasibility.adapters.position_sizing_adapter import (
                to_position_sizing_constraint,
            )

            capital_constraint = CapitalFeasibilityConstraint(**to_position_sizing_constraint(feasibility))
            notes.append(
                f"Re-sized under verified capital cap of {feasibility.max_affordable_quantity} share(s)."
            )

        quantity = min(sizing_result.recommended_quantity, feasibility.feasible_quantity)
        quantity = _floor_to_lot(quantity, exec_input.lot_size)
        if quantity <= 0:
            return (
                CandidateOutcome(
                    opportunity_id=candidate.opportunity_id,
                    symbol=candidate.symbol,
                    status=AllocationStatus.REJECTED_BY_CAPITAL,
                    terminal_stage=Stage.CAPITAL_FEASIBILITY,
                    reason=f"Affordable quantity rounds to 0 at lot size {exec_input.lot_size}.",
                    requested_quantity=sizing_result.recommended_quantity,
                    sizing_result=sizing_result,
                    feasibility_result=feasibility,
                    notes=notes,
                ),
                current_risk_report,
            )

        reduced_by = (
            Stage.CAPITAL_FEASIBILITY if quantity < sizing_result.recommended_quantity else None
        )

        # --- post-trade risk check (with downsize retries) -------------
        impact = None
        accepted_quantity = 0
        for attempt, factor in enumerate((Decimal("1"),) + tuple(cfg.risk_downsize_steps)):
            trial_quantity = _floor_to_lot(int(Decimal(quantity) * factor), exec_input.lot_size)
            if trial_quantity <= 0:
                continue

            proposed = adapters.proposed_risk_position(candidate, exec_input, trial_quantity)
            trial_impact = self.risk_engine.evaluate_new_trade(working_risk_portfolio, [proposed])

            blocked = bool(trial_impact.new_breaches_caused_by_trade)
            if not blocked and cfg.reject_on_score_jump:
                blocked = trial_impact.score_change > cfg.max_acceptable_score_increase

            if not blocked:
                impact = trial_impact
                accepted_quantity = trial_quantity
                if attempt > 0:
                    reduced_by = Stage.POST_TRADE_RISK
                    notes.append(
                        f"Post-trade risk required a reduction to {trial_quantity} share(s) "
                        f"({trial_impact.final_recommendation})."
                    )
                break
            impact = trial_impact

        if accepted_quantity <= 0:
            return (
                CandidateOutcome(
                    opportunity_id=candidate.opportunity_id,
                    symbol=candidate.symbol,
                    status=AllocationStatus.REJECTED_BY_PORTFOLIO_RISK,
                    terminal_stage=Stage.POST_TRADE_RISK,
                    reason=impact.final_recommendation if impact else "Portfolio risk rejected the trade.",
                    requested_quantity=sizing_result.recommended_quantity,
                    sizing_result=sizing_result,
                    feasibility_result=feasibility,
                    impact=impact,
                    notes=notes,
                ),
                current_risk_report,
            )

        # A risk-driven downsize changes the money, so the final quantity is
        # re-priced and re-gated rather than assumed affordable.
        if accepted_quantity != quantity:
            feasibility = self._evaluate_feasibility(
                sizing_request=sizing_request,
                quantity=accepted_quantity,
                candidate=candidate,
                exec_input=exec_input,
                ledger=ledger,
                account_state=account_state,
                request=request,
            )
            if feasibility.feasible_quantity < accepted_quantity:
                accepted_quantity = _floor_to_lot(feasibility.feasible_quantity, exec_input.lot_size)
            if accepted_quantity <= 0:
                return (
                    CandidateOutcome(
                        opportunity_id=candidate.opportunity_id,
                        symbol=candidate.symbol,
                        status=AllocationStatus.REJECTED_BY_CAPITAL,
                        terminal_stage=Stage.CAPITAL_FEASIBILITY,
                        reason="Risk-reduced quantity is no longer capital-feasible.",
                        sizing_result=sizing_result,
                        feasibility_result=feasibility,
                        impact=impact,
                        notes=notes,
                    ),
                    current_risk_report,
                )

        requirement = self.cost_provider.price_buy_leg(
            symbol=candidate.symbol,
            exchange=candidate.exchange,
            trade_type=candidate.trade_type.value,
            entry_price=exec_input.entry_price,
            quantity=accepted_quantity,
            liquidity=adapters.trade_capital_request(
                sizing_request, accepted_quantity, candidate.sector, exec_input
            ).liquidity,
            same_day_cnc_square_off=exec_input.same_day_cnc_square_off,
            available_capital=account_state.deployable_capital,
        )

        if cfg.min_instruction_value > 0 and requirement.position_value < cfg.min_instruction_value:
            return (
                CandidateOutcome(
                    opportunity_id=candidate.opportunity_id,
                    symbol=candidate.symbol,
                    status=AllocationStatus.REJECTED_BY_SIZING,
                    terminal_stage=Stage.POSITION_SIZING,
                    reason=(
                        f"Position value {requirement.position_value} below Phase 3 floor "
                        f"{cfg.min_instruction_value}."
                    ),
                    sizing_result=sizing_result,
                    feasibility_result=feasibility,
                    impact=impact,
                    notes=notes,
                ),
                current_risk_report,
            )

        status = (
            AllocationStatus.ALLOCATED
            if accepted_quantity == sizing_result.recommended_quantity
            else AllocationStatus.ALLOCATED_REDUCED
        )

        instruction = TradeInstruction(
            opportunity_id=candidate.opportunity_id,
            position_id=sizing_request.position_id,
            symbol=candidate.symbol,
            exchange=candidate.exchange,
            sector=candidate.sector,
            trade_type=candidate.trade_type.value,
            side="BUY",
            quantity=accepted_quantity,
            entry_price=exec_input.entry_price,
            position_value=requirement.position_value,
            estimated_entry_cost=requirement.execution_cost,
            required_capital=requirement.required_capital,
            stop_loss=exec_input.stop_loss,
            target=exec_input.target,
            estimated_trade_risk=(
                (exec_input.entry_price - exec_input.stop_loss) * accepted_quantity
                if exec_input.stop_loss is not None
                else Decimal("0")
            ),
            sizing_status=sizing_result.sizing_status.value,
            sizing_method=sizing_result.sizing_method.value,
            sizing_confidence=sizing_result.sizing_confidence.value,
            binding_constraints=[
                c.value if hasattr(c, "value") else str(c) for c in sizing_result.binding_constraints
            ],
            feasibility_status=feasibility.feasibility_status.value,
            max_affordable_quantity=feasibility.max_affordable_quantity,
            risk_score_before=impact.before_score if impact else None,
            risk_score_after=impact.after_score if impact else None,
            risk_status_after=(impact.after_full_report.get("risk_status") if impact else None),
            risk_contribution_pct=impact.risk_contribution_of_new_position_pct if impact else None,
            allocation_status=status,
            reduced_by_stage=reduced_by,
            explanation=self._instruction_explanation(
                candidate, sizing_result, feasibility, impact, accepted_quantity, notes
            ),
            model_versions=dict(candidate.model_versions),
            audit_ids={
                "sizing_audit_id": sizing_result.audit_id,
                "feasibility_audit_id": feasibility.audit_id,
            },
        )

        risk_position = adapters.proposed_risk_position(
            candidate, exec_input, accepted_quantity,
            expected_net_pnl=sizing_result.expected_net_profit,
            transaction_cost=requirement.execution_cost,
        )
        risk_position.is_proposed = False  # it is now part of the working portfolio

        ledger.commit(
            symbol=candidate.symbol,
            sector=candidate.sector,
            position_value=requirement.position_value,
            required_capital=requirement.required_capital,
            risk_position=risk_position,
        )

        # The post-trade report for the accepted size becomes the risk view
        # the NEXT candidate is judged against.
        next_report = impact.after_full_report if impact else current_risk_report

        return (
            CandidateOutcome(
                opportunity_id=candidate.opportunity_id,
                symbol=candidate.symbol,
                status=status,
                terminal_stage=Stage.ALLOCATED,
                reason=instruction.explanation,
                requested_quantity=sizing_result.recommended_quantity,
                final_quantity=accepted_quantity,
                sizing_result=sizing_result,
                feasibility_result=feasibility,
                impact=impact,
                instruction=instruction,
                notes=notes,
            ),
            next_report,
        )

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _evaluate_feasibility(
        self, sizing_request, quantity: int, candidate, exec_input: ExecutionInput,
        ledger: AllocationLedger, account_state, request: Phase3Request,
    ):
        trade_request = adapters.trade_capital_request(
            sizing_request, quantity, candidate.sector, exec_input
        )
        exposure = adapters.asset_exposure(
            request.portfolio_state,
            candidate.symbol,
            candidate.sector,
            extra_symbol_value=ledger.extra_symbol_value(candidate.symbol),
            extra_sector_value=ledger.extra_sector_value(candidate.sector),
        )

        def price_fn(qty: int):
            return self.cost_provider.price_buy_leg(
                symbol=trade_request.symbol,
                exchange=trade_request.exchange,
                trade_type=trade_request.trade_type,
                entry_price=trade_request.entry_price,
                quantity=qty,
                liquidity=trade_request.liquidity,
                same_day_cnc_square_off=trade_request.same_day_cnc_square_off,
                available_capital=account_state.deployable_capital,
            )

        return evaluate_trade(
            request=trade_request,
            account_state=account_state,
            exposure=exposure,
            price_fn=price_fn,
            config=self.config.feasibility,
        )

    @staticmethod
    def _ordered_selection(decision, candidates_by_id) -> List:
        """PM priority order: contribution score descending, falling back
        to the order PM listed them in."""
        scores = {
            o.opportunity_id: (o.contribution_score if o.contribution_score is not None else float("-inf"))
            for o in decision.candidate_outcomes
        }
        selected = [candidates_by_id[oid] for oid in decision.selected_candidates if oid in candidates_by_id]
        return sorted(
            selected,
            key=lambda c: (-scores.get(c.opportunity_id, float("-inf")),
                           decision.selected_candidates.index(c.opportunity_id)),
        )

    @staticmethod
    def _instruction_explanation(candidate, sizing_result, feasibility, impact, quantity, notes) -> str:
        parts = [
            f"{quantity} x {candidate.symbol}.",
            f"Sizing: {sizing_result.sizing_status.value} via {sizing_result.sizing_method.value} "
            f"({sizing_result.recommended_quantity} recommended).",
            f"Capital: {feasibility.feasibility_status.value}, max affordable "
            f"{feasibility.max_affordable_quantity}.",
        ]
        if impact is not None:
            parts.append(
                f"Portfolio risk {impact.before_score:.1f} -> {impact.after_score:.1f} "
                f"({impact.after_risk_class})."
            )
        parts.extend(notes)
        return " ".join(p for p in parts if p)

    def _finalize(
        self, run_id, request, decision, ledger, risk_before, risk_after,
        instructions, outcomes, warnings, versions, status, explanation,
    ) -> Phase3Result:
        account = ledger.account_state()
        start_state = AllocationLedger(
            portfolio_state=request.portfolio_state,
            config=self.config,
            initial_committed_capital=request.committed_capital,
        ).account_state()

        return Phase3Result(
            run_id=run_id,
            timestamp=request.market_context.as_of,
            portfolio_id=decision.portfolio_id,
            status=status,
            instructions=instructions,
            outcomes=outcomes,
            portfolio_decision=decision,
            existing_position_actions=list(decision.existing_position_actions),
            risk_before=risk_before,
            risk_after=risk_after,
            capital=CapitalSummary(
                total_capital=account.total_capital,
                cash=account.cash,
                reserve_required=account.reserved_capital,
                deployable_at_start=start_state.deployable_capital,
                capital_allocated=ledger.allocated_capital,
                deployable_remaining=account.deployable_capital,
                committed_capital_is_known=account.committed_capital_is_known,
            ),
            warnings=warnings,
            explanation=explanation,
            component_versions=versions,
        )


def run_phase3(
    request: Phase3Request,
    cost_provider: CostProvider,
    config: Optional[Phase3Config] = None,
    correlation_provider=None,
) -> Phase3Result:
    """One-shot functional entry point. Equivalent to constructing a
    Phase3Engine and calling `run` once."""
    return Phase3Engine(cost_provider, config).run(request, correlation_provider=correlation_provider)
