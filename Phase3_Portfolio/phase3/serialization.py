"""
JSON-safe projection of a Phase3Result, for the Trade Journal / DB layer.

Deliberately lossy in one direction only: the full upstream result
objects (PositionSizeResult, CapitalFeasibilityResult, TradeImpactResult)
are summarized here, while the fields an operator or a downstream service
needs to act on or audit are kept verbatim. Anything that needs the full
object should hold the Phase3Result itself.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "value") and hasattr(value, "name"):  # Enum
        return value.value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def instruction_to_dict(instruction) -> Dict[str, Any]:
    return {
        "opportunity_id": instruction.opportunity_id,
        "position_id": instruction.position_id,
        "symbol": instruction.symbol,
        "exchange": instruction.exchange,
        "sector": instruction.sector,
        "trade_type": instruction.trade_type,
        "side": instruction.side,
        "quantity": instruction.quantity,
        "entry_price": _plain(instruction.entry_price),
        "position_value": _plain(instruction.position_value),
        "estimated_entry_cost": _plain(instruction.estimated_entry_cost),
        "required_capital": _plain(instruction.required_capital),
        "stop_loss": _plain(instruction.stop_loss),
        "target": _plain(instruction.target),
        "estimated_trade_risk": _plain(instruction.estimated_trade_risk),
        "sizing_status": instruction.sizing_status,
        "sizing_method": instruction.sizing_method,
        "sizing_confidence": instruction.sizing_confidence,
        "binding_constraints": list(instruction.binding_constraints),
        "feasibility_status": instruction.feasibility_status,
        "max_affordable_quantity": instruction.max_affordable_quantity,
        "risk_score_before": instruction.risk_score_before,
        "risk_score_after": instruction.risk_score_after,
        "risk_status_after": instruction.risk_status_after,
        "risk_contribution_pct": instruction.risk_contribution_pct,
        "allocation_status": _plain(instruction.allocation_status),
        "reduced_by_stage": _plain(instruction.reduced_by_stage),
        "explanation": instruction.explanation,
        "model_versions": dict(instruction.model_versions),
        "audit_ids": dict(instruction.audit_ids),
    }


def outcome_to_dict(outcome) -> Dict[str, Any]:
    return {
        "opportunity_id": outcome.opportunity_id,
        "symbol": outcome.symbol,
        "status": _plain(outcome.status),
        "terminal_stage": _plain(outcome.terminal_stage),
        "reason": outcome.reason,
        "requested_quantity": outcome.requested_quantity,
        "final_quantity": outcome.final_quantity,
        "notes": list(outcome.notes),
        "sizing_status": (
            outcome.sizing_result.sizing_status.value if outcome.sizing_result is not None else None
        ),
        "feasibility_status": (
            outcome.feasibility_result.feasibility_status.value
            if outcome.feasibility_result is not None
            else None
        ),
        "new_risk_breaches": (
            list(outcome.impact.new_breaches_caused_by_trade) if outcome.impact is not None else []
        ),
    }


def result_to_dict(result, include_risk_reports: bool = False) -> Dict[str, Any]:
    payload = {
        "run_id": result.run_id,
        "timestamp": _plain(result.timestamp),
        "portfolio_id": result.portfolio_id,
        "status": _plain(result.status),
        "explanation": result.explanation,
        "warnings": list(result.warnings),
        "component_versions": dict(result.component_versions),
        "instructions": [instruction_to_dict(i) for i in result.instructions],
        "outcomes": [outcome_to_dict(o) for o in result.outcomes],
        "decision_id": getattr(result.portfolio_decision, "decision_id", None),
        "existing_position_actions": [
            {
                "position_id": a.position_id,
                "symbol": a.symbol,
                "action": _plain(a.action),
                "explanation": a.explanation,
            }
            for a in result.existing_position_actions
        ],
    }

    if result.capital is not None:
        payload["capital"] = {
            "total_capital": _plain(result.capital.total_capital),
            "cash": _plain(result.capital.cash),
            "reserve_required": _plain(result.capital.reserve_required),
            "deployable_at_start": _plain(result.capital.deployable_at_start),
            "capital_allocated": _plain(result.capital.capital_allocated),
            "deployable_remaining": _plain(result.capital.deployable_remaining),
            "committed_capital_is_known": result.capital.committed_capital_is_known,
        }

    payload["risk_summary"] = {
        "score_before": result.risk_before.get("portfolio_risk_score"),
        "score_after": result.risk_after.get("portfolio_risk_score"),
        "status_before": result.risk_before.get("risk_status"),
        "status_after": result.risk_after.get("risk_status"),
        "drivers_after": result.risk_after.get("risk_drivers", []),
    }

    if include_risk_reports:
        payload["risk_before"] = _plain(result.risk_before)
        payload["risk_after"] = _plain(result.risk_after)

    return payload
