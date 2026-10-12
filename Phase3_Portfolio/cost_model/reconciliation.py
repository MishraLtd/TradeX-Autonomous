"""
PART 20 — VALIDATION AGAINST REAL ZERODHA CONTRACT NOTES

    Predicted Cost -> Actual Zerodha Cost -> Difference -> Root Cause
    -> Model Adjustment

`reconcile()` produces a component-level diff report. It never mutates
the fee schedule automatically — model adjustment is a human-reviewed
step (a rate is either a statutory/broker fact that changed, in which
case fee_schedule.py gets a NEW versioned entry, or it's a systematic
slippage/impact bias, in which case the calibrated_bps history used by
slippage.py's LEVEL_4 tier gets updated). Automating that write would
risk quietly corrupting the sourced rate registry.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List

from .models import CostBreakdown


@dataclass
class ReconciliationReport:
    predicted_total_cost: Decimal
    actual_total_cost: Decimal
    difference: Decimal
    difference_pct: Decimal
    component_level_difference: Dict[str, Decimal]
    notes: List[str] = field(default_factory=list)


def reconcile(predicted: CostBreakdown, actual_components: Dict[str, Decimal]) -> ReconciliationReport:
    """
    `actual_components` — charges read off the real Zerodha contract
    note / funds statement, keyed the same as CostBreakdown's fields
    (e.g. {"brokerage": ..., "stt": ..., "gst": ..., "dp_charges": ...,
    "slippage_cost": ...}). Any key present in actual_components but
    unknown to the predicted breakdown is still reported (an
    "unknown_charge" bucket) — PART 18's "never silently drop an unknown
    cost" applies to reconciliation too, not just forward pricing.
    """
    predicted_map = {
        "brokerage": predicted.brokerage,
        "stt": predicted.stt,
        "exchange_charges": predicted.exchange_charges,
        "ipft_charges": predicted.ipft_charges,
        "sebi_charges": predicted.sebi_charges,
        "gst": predicted.gst,
        "stamp_duty": predicted.stamp_duty,
        "dp_charges": predicted.dp_charges,
        "slippage_cost": predicted.slippage_cost,
        "spread_cost": predicted.spread_cost,
        "market_impact_cost": predicted.market_impact_cost,
    }

    component_diff: Dict[str, Decimal] = {}
    notes: List[str] = []
    all_keys = set(predicted_map) | set(actual_components)
    actual_total = Decimal("0.00")

    for key in sorted(all_keys):
        pred_v = predicted_map.get(key)
        act_v = actual_components.get(key)
        if pred_v is None:
            notes.append(f"'{key}' present in actual data but not modelled by the predicted "
                         f"breakdown — investigate as a possible UNKNOWN_CHARGE.")
            pred_v = Decimal("0.00")
        if act_v is None:
            notes.append(f"'{key}' predicted but no actual figure supplied for reconciliation.")
            act_v = Decimal("0.00")
        component_diff[key] = (act_v - pred_v).quantize(Decimal("0.01"))
        actual_total += act_v

    predicted_total = predicted.total_cost
    difference = (actual_total - predicted_total).quantize(Decimal("0.01"))
    difference_pct = (
        (difference / predicted_total * 100).quantize(Decimal("0.01"))
        if predicted_total else Decimal("0")
    )

    if abs(difference_pct) > 10:
        notes.append(
            f"Difference of {difference_pct}% exceeds a 10% tolerance band — "
            f"treat this as a signal to review the fee schedule / slippage "
            f"calibration, not just noise."
        )

    return ReconciliationReport(
        predicted_total_cost=predicted_total,
        actual_total_cost=actual_total.quantize(Decimal("0.01")),
        difference=difference,
        difference_pct=difference_pct,
        component_level_difference=component_diff,
        notes=notes,
    )
