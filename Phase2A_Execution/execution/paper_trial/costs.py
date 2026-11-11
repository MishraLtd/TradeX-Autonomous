"""Estimated Upstox Basic NSE cash intraday fees, not contract-note reconciliation.

Source: https://upstox.com/brokerage-charges/ (reviewed 2026-11-11).
Use its explicitly dated March 2026 table, not the older repeated mobile
table. The legacy schedule remains available for historical trial evidence.
Intraday has no DP charge. Round to paise per simulated order. Partial fills
use the difference between cumulative estimates, so caps are not reset.
"""
from decimal import ROUND_HALF_UP
from .config import D, price_tick, LEGACY_FEES, CURRENT_FEES


def breakdown(side, turnover, model=LEGACY_FEES):
    """Unrounded components plus an explicit adjustment to the order total.

    This is a per-order estimate. Actual contract-note aggregation/rounding,
    account-specific plans and exceptional broker charges may differ.
    """
    if side not in ('BUY', 'SELL') or D(turnover) < 0:
        raise ValueError('Invalid fee input')
    if model not in (LEGACY_FEES, CURRENT_FEES):
        raise ValueError('Unsupported fee model')
    value = D(turnover)
    brokerage = min(D('20'), value * D('0.001'))
    exchange = value * D('0.0000307' if model == CURRENT_FEES else '0.0000297')
    sebi = value * D('0.000001')
    ipft = value * D('0.000000001' if model == CURRENT_FEES else '0.000001')
    gst = (brokerage + exchange + ipft) * D('0.18')
    parts = dict(brokerage=brokerage, exchange=exchange, sebi=sebi, ipft=ipft, gst=gst,
                 stamp_duty=value * D('0.00003') if side == 'BUY' else D(0),
                 stt=value * D('0.00025') if side == 'SELL' else D(0), dp=D(0))
    raw = sum(parts.values(), D(0))
    parts['rounding_adjustment'] = raw.quantize(D('0.01'), rounding=ROUND_HALF_UP) - raw
    return parts


def fee(side, turnover, model=LEGACY_FEES):
    return sum(breakdown(side, turnover, model).values(), D(0)).quantize(D('0.01'))


def fee_delta(side, before, after, model):
    old, new = breakdown(side, before, model), breakdown(side, after, model)
    return {key: new[key] - old[key] for key in new}


def target(entry, quantity, direction, tick, profit_pct, model=LEGACY_FEES):
    """First tick achieving the net target after estimated entry AND exit fees."""
    entry, tick = D(entry), D(tick)
    opening, closing = ('BUY', 'SELL') if direction == 'LONG' else ('SELL', 'BUY')
    needed = entry * quantity * D(profit_pct) / 100
    def net(p):
        gross = (p - entry) * quantity * (1 if direction == 'LONG' else -1)
        return gross - fee(opening, entry * quantity, model) - fee(closing, p * quantity, model)
    lo, hi = 1, int(price_tick(entry * 2, tick, True) / tick)
    while lo < hi:
        mid = (lo + hi) // 2
        if (net(mid * tick) >= needed) == (direction == 'LONG'):
            hi = mid
        else:
            lo = mid + 1
    result = lo * tick if direction == 'LONG' else (lo - 1) * tick
    if result <= 0 or net(result) < needed:
        raise ValueError('No positive target meets estimated costs')
    return result
