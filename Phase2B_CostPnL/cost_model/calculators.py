"""
PART 4 — EXACT COST ENGINE (calculator layer)
PART 8 — ROUNDING AND MINIMUM CHARGES

Each function here computes exactly ONE charge for exactly ONE executed
order leg (a buy OR a sell), because several real-world rules (STT on
intraday, stamp duty, DP charges) are side-specific and per-leg, not a
single blended "round-trip rate". A round-trip trade = one BUY leg calc
+ one SELL leg calc, each independently rounded — this mirrors how
Zerodha actually issues a contract note per executed order.

Rounding rule used throughout: each individual statutory/broker charge
is rounded to 2 decimal places (nearest paisa) using ROUND_HALF_UP,
which is the convention brokers use on contract notes. GST is computed
on the *rounded* upstream components (brokerage + SEBI + exchange txn),
matching how it appears on an actual Zerodha contract note.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from .models import Side, TradeType
from .fee_schedule import FeeScheduleVersion

TWO_PLACES = Decimal("0.01")


def _round(x: Decimal) -> Decimal:
    return x.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def brokerage_for_leg(schedule: FeeScheduleVersion, turnover: Decimal) -> Decimal:
    """Brokerage for one executed order (one leg).
    Delivery: always ₹0 (Zerodha zero-brokerage delivery).
    Intraday: min(0.03% of turnover, ₹20), i.e. percentage rate capped
    by the flat fee — 'whichever is lower', per Zerodha's own wording.
    """
    if schedule.brokerage_pct == 0:
        return Decimal("0.00")
    pct_amount = turnover * schedule.brokerage_pct / Decimal("100")
    amount = min(pct_amount, schedule.brokerage_flat_cap)
    return _round(amount)


def stt_for_leg(schedule: FeeScheduleVersion, turnover: Decimal, side: Side) -> Decimal:
    """STT for one leg. Delivery: applies to BOTH sides. Intraday:
    SELL side only (buy-side intraday STT is genuinely zero, not a
    rounding artefact)."""
    fact = schedule.stt_pct
    if fact.side is not None and fact.side != side:
        return Decimal("0.00")
    amount = turnover * fact.value / Decimal("100")
    return _round(amount)


def exchange_txn_charge_for_leg(schedule: FeeScheduleVersion, turnover: Decimal) -> Decimal:
    """Exchange transaction charge, both sides."""
    amount = turnover * schedule.exchange_txn_pct.value / Decimal("100")
    return _round(amount)


def ipft_charge_for_leg(schedule: FeeScheduleVersion, turnover: Decimal) -> Decimal:
    """NSE Investor Protection Fund Trust charge, both sides (₹0 on BSE
    in this schedule — Zerodha does not itemise it separately for BSE)."""
    amount = turnover * schedule.ipft_pct.value / Decimal("100")
    return _round(amount)


def sebi_charge_for_leg(schedule: FeeScheduleVersion, turnover: Decimal) -> Decimal:
    """SEBI turnover fee, both sides. ₹10/crore == 0.0001%."""
    amount = turnover * schedule.sebi_pct.value / Decimal("100")
    return _round(amount)


def gst_for_leg(schedule: FeeScheduleVersion, brokerage: Decimal,
                 sebi_charge: Decimal, exchange_txn: Decimal,
                 ipft_charge: Decimal) -> Decimal:
    """18% GST on (brokerage + SEBI charges + exchange transaction
    charges [+ IPFT, which Zerodha's own worked examples fold into the
    same GST base as the other exchange-levied charges])."""
    base = brokerage + sebi_charge + exchange_txn + ipft_charge
    amount = base * schedule.gst_pct / Decimal("100")
    return _round(amount)


def stamp_duty_for_leg(schedule: FeeScheduleVersion, turnover: Decimal, side: Side) -> Decimal:
    """Stamp duty — BUY side only, both delivery and intraday. Rate is
    quoted as 'X% or ₹Y per crore' — these are equivalent at the stated
    rate (0.015% == ₹1500/crore), so the percentage formula alone is
    sufficient; the per-crore figure is kept as a cross-check constant."""
    fact = schedule.stamp_duty_pct
    if fact.side is not None and fact.side != side:
        return Decimal("0.00")
    amount = turnover * fact.value / Decimal("100")
    return _round(amount)


def dp_charge_for_leg(schedule: FeeScheduleVersion, side: Side,
                       trade_type: TradeType,
                       shares_from_demat_holdings: bool,
                       distinct_scrips_sold_today: int = 1) -> Decimal:
    """DP (Depository Participant) charge: flat ₹15.34 per scrip per
    day, charged ONLY when shares are debited from the demat account —
    i.e. a DELIVERY SELL of shares that actually sat in the demat
    account. It does NOT depend on quantity, and it is NOT charged on
    intraday (MIS) trades, which never settle into demat.

    `distinct_scrips_sold_today` lets a caller amortise/report the
    charge correctly when multiple *different* lots of the SAME scrip
    are sold same-day (Zerodha charges this once per scrip per day, not
    once per sell order) — for a single-order cost estimate this is 1.
    """
    if trade_type != TradeType.DELIVERY:
        return Decimal("0.00")
    if side != Side.SELL:
        return Decimal("0.00")
    if not shares_from_demat_holdings:
        # e.g. same-day CNC buy+sell that never actually settled into
        # demat holdings — Zerodha does not charge DP fees on this.
        return Decimal("0.00")
    return _round(schedule.dp_charge_flat * distinct_scrips_sold_today)
