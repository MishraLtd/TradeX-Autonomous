"""
Portfolio snapshot for reproducibility/auditability (spec §29-30).

A snapshot is a plain, JSON-serializable dict capturing everything the
decision was based on, so the decision can be replayed later.
"""

import hashlib
import json
from dataclasses import asdict
from datetime import datetime
from typing import Any, Dict, List

from .candidate import PortfolioCandidate
from .portfolio_state import MarketContext, PortfolioState


def _default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "value"):  # Enum
        return obj.value
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    return str(obj)


def build_snapshot(
    portfolio_state: PortfolioState,
    candidates: List[PortfolioCandidate],
    context: MarketContext,
) -> Dict[str, Any]:
    payload = {
        "portfolio_state": asdict(portfolio_state),
        "candidates": [asdict(c) for c in candidates],
        "market_context": asdict(context),
    }
    serialized = json.dumps(payload, default=_default, sort_keys=True)
    snapshot_id = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]
    return {"snapshot_id": snapshot_id, "payload": payload}
