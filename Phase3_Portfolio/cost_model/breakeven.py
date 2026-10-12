"""
PART 9 — BREAK-EVEN ENGINE

Given an entry price, quantity, and a TOTAL round-trip cost already
computed by the engine for the buy leg (the sell-leg cost, in general,
itself depends on the exit price — for delivery/intraday sells this
dependency is small since STT/exchange/stamp/GST all scale linearly
with turnover, so we solve it in closed form).

Let:
    P0   = entry price, Q = quantity, B = buy-leg fixed-ish costs
    Pexit = required exit price
    S(Pexit) = sell-leg costs, all of which are PROPORTIONAL to
               Pexit*Q except the flat DP charge D (delivery-sell-only,
               independent of price) and, for intraday, the brokerage
               flat cap.

For a SELL leg, every percentage charge (STT-on-sell for intraday, full
STT for delivery, exchange txn, SEBI, GST-on-those, and, for delivery,
also stamp/brokerage are BUY-side-only so they don't appear here) is a
fixed fraction `r_sell` of the sell turnover Pexit*Q, plus a flat
component D (DP charge) and, for intraday, brokerage capped at ₹20
which for realistic small-capital trade sizes (turnover << ₹66,667) is
simply 0.03% of turnover (since 0.03% * 66,667 = 20), so it is *also*
proportional to turnover below that threshold — we handle the cap
explicitly rather than assuming linearity blindly.

    Net P&L = (Pexit - P0)*Q - B - D - r_sell * Pexit * Q  [+ brokerage
              cap adjustment if turnover crosses the flat-fee threshold]

Solving Net P&L = target for Pexit:

    Pexit * Q * (1 - r_sell) = target + B + D + P0*Q
    Pexit = (target + B + D + P0*Q) / (Q * (1 - r_sell))

This is solved numerically (bisection) in practice so that the exact
same leg-cost calculators used everywhere else in the engine are reused
verbatim — guaranteeing the break-even price is never inconsistent with
the actual per-leg calculators (no duplicated/hand-derived formula that
could drift out of sync, PART 25 self-critique).
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from .models import TradeInput, Side, TradeType
from .fee_schedule import get_schedule
from . import calculators as calc


def _net_pnl_at_exit(trade: TradeInput, exit_price: Decimal, buy_leg_total: Decimal) -> Decimal:
    schedule = get_schedule(trade.exchange, trade.trade_type,
                             trade.entry_timestamp.date() if trade.entry_timestamp else __import__("datetime").date.today())
    sell_turnover = (exit_price * trade.quantity).quantize(Decimal("0.01"))

    brokerage = calc.brokerage_for_leg(schedule, sell_turnover)
    stt = calc.stt_for_leg(schedule, sell_turnover, Side.SELL)
    exch = calc.exchange_txn_charge_for_leg(schedule, sell_turnover)
    ipft = calc.ipft_charge_for_leg(schedule, sell_turnover)
    sebi = calc.sebi_charge_for_leg(schedule, sell_turnover)
    gst = calc.gst_for_leg(schedule, brokerage, sebi, exch, ipft)
    stamp = calc.stamp_duty_for_leg(schedule, sell_turnover, Side.SELL)  # 0, buy-side only
    dp = calc.dp_charge_for_leg(
        schedule, Side.SELL, trade.trade_type,
        shares_from_demat_holdings=bool(trade.shares_from_demat_holdings),
    )
    sell_leg_total = brokerage + stt + exch + ipft + sebi + gst + stamp + dp

    gross_pnl = (exit_price - trade.entry_price) * trade.quantity
    return gross_pnl - buy_leg_total - sell_leg_total


def solve_break_even_price(trade: TradeInput, buy_leg_total: Decimal,
                            target_net_pnl: Decimal = Decimal("0")) -> Decimal:
    """Bisection search for the exit price at which net P&L == target_net_pnl.
    Reuses the real per-leg calculators (see module docstring) — never a
    hand-rolled closed-form shortcut that could drift out of sync."""
    lo = trade.entry_price * Decimal("0.50")
    hi = trade.entry_price * Decimal("3.00")

    f_lo = _net_pnl_at_exit(trade, lo, buy_leg_total) - target_net_pnl
    f_hi = _net_pnl_at_exit(trade, hi, buy_leg_total) - target_net_pnl

    # widen the bracket if needed (should rarely trigger for realistic inputs)
    tries = 0
    while f_lo > 0 and tries < 5:
        lo = lo * Decimal("0.5")
        f_lo = _net_pnl_at_exit(trade, lo, buy_leg_total) - target_net_pnl
        tries += 1
    tries = 0
    while f_hi < 0 and tries < 5:
        hi = hi * Decimal("2")
        f_hi = _net_pnl_at_exit(trade, hi, buy_leg_total) - target_net_pnl
        tries += 1

    for _ in range(60):
        mid = (lo + hi) / 2
        f_mid = _net_pnl_at_exit(trade, mid, buy_leg_total) - target_net_pnl
        if f_mid == 0:
            lo = hi = mid
            break
        if (f_mid > 0) == (f_lo > 0):
            lo, f_lo = mid, f_mid
        else:
            hi, f_hi = mid, f_mid

    result = (lo + hi) / 2
    return result.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def break_even_table(trade: TradeInput, buy_leg_total: Decimal,
                      targets=(Decimal("0"), Decimal("1"), Decimal("5"),
                                Decimal("10"), Decimal("20"))) -> dict:
    """PART 9 worked table: exit price required for each target net P&L."""
    return {
        str(t): solve_break_even_price(trade, buy_leg_total, t)
        for t in targets
    }
