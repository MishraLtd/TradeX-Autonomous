"""
PART 1 / PART 17 — RATE REGISTRY

Every rate the Cost Model uses is stored here with:
    effective_from, effective_to (None = still current),
    source, source_url, calculation rule, segment/side applicability.

Rates below were verified against zerodha.com/charges (fetched live,
September 2026) plus the Indian Stamp Act (Finance Act 2019 amendment,
effective 1 July 2020) for the state-stamp-duty unification, and SEBI's
published turnover-fee circular for the ₹10/crore SEBI charge. This file
is the ONLY place numeric rates may appear; calculators must never
hard-code a rate.

The engine (engine.py) refuses to price a trade using a schedule whose
effective_to has passed relative to the trade timestamp — see
StaleFeeScheduleError — rather than silently reusing an old rate.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional, Dict, List

from .models import Exchange, TradeType, Side
from .exceptions import CostModelDataIncomplete, StaleFeeScheduleError

ZERODHA_SOURCE = "Zerodha — Charges page"
ZERODHA_URL = "https://zerodha.com/charges"
STAMP_ACT_SOURCE = "Indian Stamp Act, 1899 (Finance Act 2019 amendment, eff. 1 Jul 2020)"
SEBI_SOURCE = "SEBI turnover-fee circular (via Zerodha charges page pass-through)"


@dataclass(frozen=True)
class RateFact:
    """One sourced numeric fact."""
    name: str
    value: Decimal
    unit: str                # "pct" | "rupee_flat" | "rupee_per_crore" | "rupee_per_scrip"
    side: Optional[Side]     # None = both sides
    source: str
    source_url: str
    calculation_rule: str


@dataclass(frozen=True)
class FeeScheduleVersion:
    version_id: str
    effective_from: date
    effective_to: Optional[date]   # None = open-ended / current
    exchange: Exchange
    trade_type: TradeType

    brokerage_pct: Decimal
    brokerage_flat_cap: Decimal          # per-order cap, e.g. ₹20

    stt_pct: RateFact
    exchange_txn_pct: RateFact
    ipft_pct: RateFact                   # NSE only; 0 on BSE in this build
    sebi_pct: RateFact
    gst_pct: Decimal
    stamp_duty_pct: RateFact
    stamp_duty_per_crore_min: Decimal    # e.g. 1500/crore for delivery — used as sanity cross-check
    dp_charge_flat: Decimal              # per scrip per day, sell side, delivery only


def _rf(name, value, unit, side, rule) -> RateFact:
    return RateFact(
        name=name, value=Decimal(value), unit=unit, side=side,
        source=ZERODHA_SOURCE, source_url=ZERODHA_URL,
        calculation_rule=rule,
    )


# --------------------------------------------------------------------------
# Registry: list of all known schedule versions. New rate changes are
# added as NEW entries — never mutate an existing entry — so that
# backtests over historical dates remain reproducible (PART 13).
# --------------------------------------------------------------------------

_SCHEDULES: List[FeeScheduleVersion] = [
    # ---- Equity Delivery, NSE ----
    FeeScheduleVersion(
        version_id="NSE-EQ-DELIVERY-2026-09",
        effective_from=date(2026, 1, 1),
        effective_to=None,
        exchange=Exchange.NSE,
        trade_type=TradeType.DELIVERY,
        brokerage_pct=Decimal("0"),
        brokerage_flat_cap=Decimal("0"),
        stt_pct=_rf("STT", "0.1", "pct", None,
                    "0.1% of turnover, charged on BOTH buy and sell legs"),
        exchange_txn_pct=_rf("NSE transaction charge", "0.00307", "pct", None,
                              "0.00307% of turnover, both sides"),
        ipft_pct=_rf("NSE IPFT", "0.0001", "pct", None,
                     "₹0.01/crore + GST of turnover, both sides"),
        sebi_pct=_rf("SEBI turnover fee", "0.0001", "pct", None,
                     "₹10/crore of turnover, both sides"),
        gst_pct=Decimal("18"),
        stamp_duty_pct=_rf("Stamp duty", "0.015", "pct", Side.BUY,
                            "0.015% of turnover OR ₹1500/crore, buy side ONLY"),
        stamp_duty_per_crore_min=Decimal("1500"),
        dp_charge_flat=Decimal("15.34"),
    ),
    # ---- Equity Delivery, BSE ----
    FeeScheduleVersion(
        version_id="BSE-EQ-DELIVERY-2026-09",
        effective_from=date(2026, 1, 1),
        effective_to=None,
        exchange=Exchange.BSE,
        trade_type=TradeType.DELIVERY,
        brokerage_pct=Decimal("0"),
        brokerage_flat_cap=Decimal("0"),
        stt_pct=_rf("STT", "0.1", "pct", None,
                    "0.1% of turnover, charged on BOTH buy and sell legs"),
        exchange_txn_pct=_rf("BSE transaction charge", "0.00375", "pct", None,
                              "0.00375% of turnover, both sides (group-dependent; "
                              "0.00375% used as the standard equity group rate)"),
        ipft_pct=_rf("BSE IPFT", "0", "pct", None, "Not separately itemised by Zerodha for BSE"),
        sebi_pct=_rf("SEBI turnover fee", "0.0001", "pct", None,
                     "₹10/crore of turnover, both sides"),
        gst_pct=Decimal("18"),
        stamp_duty_pct=_rf("Stamp duty", "0.015", "pct", Side.BUY,
                            "0.015% of turnover OR ₹1500/crore, buy side ONLY"),
        stamp_duty_per_crore_min=Decimal("1500"),
        dp_charge_flat=Decimal("15.34"),
    ),
    # ---- Equity Intraday (MIS), NSE ----
    FeeScheduleVersion(
        version_id="NSE-EQ-INTRADAY-2026-09",
        effective_from=date(2026, 1, 1),
        effective_to=None,
        exchange=Exchange.NSE,
        trade_type=TradeType.INTRADAY,
        brokerage_pct=Decimal("0.03"),
        brokerage_flat_cap=Decimal("20"),
        stt_pct=_rf("STT", "0.025", "pct", Side.SELL,
                    "0.025% of turnover, SELL side only"),
        exchange_txn_pct=_rf("NSE transaction charge", "0.00307", "pct", None,
                              "0.00307% of turnover, both sides"),
        ipft_pct=_rf("NSE IPFT", "0.0001", "pct", None,
                     "₹0.01/crore + GST of turnover, both sides"),
        sebi_pct=_rf("SEBI turnover fee", "0.0001", "pct", None,
                     "₹10/crore of turnover, both sides"),
        gst_pct=Decimal("18"),
        stamp_duty_pct=_rf("Stamp duty", "0.003", "pct", Side.BUY,
                            "0.003% of turnover OR ₹300/crore, buy side ONLY"),
        stamp_duty_per_crore_min=Decimal("300"),
        dp_charge_flat=Decimal("0"),   # no DP charge on intraday — never settles into demat
    ),
    # ---- Equity Intraday (MIS), BSE ----
    FeeScheduleVersion(
        version_id="BSE-EQ-INTRADAY-2026-09",
        effective_from=date(2026, 1, 1),
        effective_to=None,
        exchange=Exchange.BSE,
        trade_type=TradeType.INTRADAY,
        brokerage_pct=Decimal("0.03"),
        brokerage_flat_cap=Decimal("20"),
        stt_pct=_rf("STT", "0.025", "pct", Side.SELL,
                    "0.025% of turnover, SELL side only"),
        exchange_txn_pct=_rf("BSE transaction charge", "0.00375", "pct", None,
                              "0.00375% of turnover, both sides"),
        ipft_pct=_rf("BSE IPFT", "0", "pct", None, "Not separately itemised by Zerodha for BSE"),
        sebi_pct=_rf("SEBI turnover fee", "0.0001", "pct", None,
                     "₹10/crore of turnover, both sides"),
        gst_pct=Decimal("18"),
        stamp_duty_pct=_rf("Stamp duty", "0.003", "pct", Side.BUY,
                            "0.003% of turnover OR ₹300/crore, buy side ONLY"),
        stamp_duty_per_crore_min=Decimal("300"),
        dp_charge_flat=Decimal("0"),
    ),
]

_SCHEDULE_INDEX: Dict[tuple, List[FeeScheduleVersion]] = {}
for _s in _SCHEDULES:
    _SCHEDULE_INDEX.setdefault((_s.exchange, _s.trade_type), []).append(_s)


def get_schedule(exchange: Exchange, trade_type: TradeType,
                  as_of: date) -> FeeScheduleVersion:
    """Return the fee schedule version applicable to `as_of`. Raises
    CostModelDataIncomplete / StaleFeeScheduleError rather than ever
    returning a best-guess schedule."""
    candidates = _SCHEDULE_INDEX.get((exchange, trade_type))
    if not candidates:
        raise CostModelDataIncomplete(
            code="UNKNOWN_SEGMENT",
            message=f"No fee schedule registered for exchange={exchange} "
                    f"trade_type={trade_type}.",
        )
    applicable = [
        s for s in candidates
        if s.effective_from <= as_of and (s.effective_to is None or as_of <= s.effective_to)
    ]
    if not applicable:
        latest = max(candidates, key=lambda s: s.effective_from)
        if as_of > latest.effective_from and latest.effective_to is not None:
            raise StaleFeeScheduleError(
                segment=f"{exchange}/{trade_type}", as_of=as_of,
                effective_to=latest.effective_to,
            )
        raise CostModelDataIncomplete(
            code="NO_SCHEDULE_FOR_DATE",
            message=f"No fee schedule for exchange={exchange} "
                    f"trade_type={trade_type} covers date {as_of}.",
        )
    # most recently effective version wins
    return max(applicable, key=lambda s: s.effective_from)
