"""
Cost Model integration — Sections 18, 19, 39, 42.

This module deliberately does NOT reimplement Phase 1's cost logic
(brokerage/STT/GST/slippage/impact). It defines the INTERFACE the
Return Prediction Engine expects from your existing Cost Model, plus
a `NullCostModel` stand-in for local testing when the real one isn't
wired up yet.

Wire your actual Phase 1 Cost Model in by implementing `CostModelProtocol`
against its real API and passing that implementation into
`apply_cost_model()`. Nothing else in this package should ever compute
brokerage/STT/slippage itself — see Section 18/39 ("keep the two
responsibilities conceptually separate").
"""

from __future__ import annotations
from typing import Protocol
from ..schemas import RawPrediction, NetPrediction, PredictionStatus


class CostModelProtocol(Protocol):
    """Whatever your Phase 1 Cost Model object is, it must expose this
    method. Adapt with a thin wrapper class if the real signature differs."""

    version: str

    def estimate_total_friction_pct(
        self,
        symbol: str,
        expected_gross_return_pct: float,
        horizon: str,
        capital_inr: float,
    ) -> float:
        """Return total expected friction (brokerage+STT+exchange+SEBI+
        GST+stamp duty+spread+slippage+impact) as a signed percentage
        of position value."""
        ...


class NullCostModel:
    """Stand-in ONLY for local dev/testing before the real Cost Model is
    wired in. Uses a crude flat friction estimate — NEVER use this for
    anything resembling a real economic-viability decision."""

    version = "null-cost-model-DO-NOT-USE-IN-PRODUCTION"

    def estimate_total_friction_pct(self, symbol, expected_gross_return_pct, horizon, capital_inr):
        return 0.35  # flat placeholder, percent


def apply_cost_model(
    raw: RawPrediction,
    cost_model: CostModelProtocol,
    capital_inr: float,
    viability_margin_pct: float = 0.0,
) -> NetPrediction:
    """Section 18/19 pipeline: gross -> cost model -> net.

    `viability_margin_pct`: require net return to clear this margin
    (beyond just >0) before calling it economically_viable — useful for
    demanding a safety buffer at ₹1,000 capital where a single bad
    friction estimate can wipe out a thin edge (Section 20/43).
    """
    if raw.status != PredictionStatus.VALID:
        return NetPrediction(
            raw=raw, expected_total_friction_pct=float("nan"),
            expected_net_return_pct=float("nan"),
            cost_model_version=cost_model.version,
            economically_viable=False, status=raw.status,
        )

    friction = cost_model.estimate_total_friction_pct(
        symbol=raw.symbol,
        expected_gross_return_pct=raw.expected_gross_return_pct,
        horizon=raw.horizon,
        capital_inr=capital_inr,
    )
    net_return = raw.expected_gross_return_pct - friction
    viable = net_return > viability_margin_pct

    return NetPrediction(
        raw=raw,
        expected_total_friction_pct=friction,
        expected_net_return_pct=net_return,
        cost_model_version=cost_model.version,
        economically_viable=viable,
        status=raw.status,
    )
