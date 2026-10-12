import uuid
from datetime import datetime, timezone


def new_audit_id() -> str:
    return str(uuid.uuid4())


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def build_audit_trail(
    request,
    config,
    diagnostics: dict,
    adjustments: dict,
    cap_result: dict,
    final_qty: int,
    sizing_level,
    sizing_status,
) -> dict:
    """A single dict capturing everything needed to answer 'why did TradeX
    select N shares' from stored data alone (Part 67)."""
    return {
        "position_id": request.position_id,
        "opportunity_id": request.opportunity_id,
        "symbol": request.symbol,
        "config_version": config.version,
        "inputs": {
            "entry_price": str(request.entry_price),
            "stop_loss": str(request.stop_loss) if request.stop_loss is not None else None,
            "capital_available_for_sizing": str(request.capital_available_for_sizing),
            "max_risk_allowed": str(request.max_risk_allowed) if request.max_risk_allowed else None,
            "atr_pct": str(request.atr_pct) if request.atr_pct is not None else None,
            "model_confidence": str(request.model_confidence) if request.model_confidence is not None else None,
            "market_regime": request.market_regime.value,
        },
        "risk_diagnostics": {k: str(v) for k, v in diagnostics.items()},
        "adjustments": {k: str(v) for k, v in adjustments.items()},
        "cap_candidates": [
            {"value": str(v), "constraint": c.value} for v, c in cap_result["all_candidates"]
        ],
        "binding_constraint": cap_result["binding_constraint"].value,
        "final_quantity": final_qty,
        "sizing_level": sizing_level.value,
        "sizing_status": sizing_status.value,
        "model_versions": request.model_versions,
        "timestamp": now_utc().isoformat(),
    }
