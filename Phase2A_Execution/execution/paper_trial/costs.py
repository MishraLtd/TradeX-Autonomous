"""Estimated Upstox Basic NSE cash intraday fees, not contract-note reconciliation.

Source: https://upstox.com/brokerage-charges/ (reviewed 2026-11-10).
Intraday has no DP charge. Round to paise per simulated order. Partial fills
use the difference between cumulative estimates, so caps are not reset.
"""
from decimal import ROUND_HALF_UP
from .config import D, price_tick


def fee(side, turnover):
    if side not in ('BUY', 'SELL') or D(turnover) < 0:
        raise ValueError('Invalid fee input')
    value = D(turnover)
    brokerage = min(D('20'), value * D('0.001'))
    exchange = value * D('0.0000297')
    sebi = value * D('0.000001')
    ipft = value * D('0.000001')
    gst = (brokerage + exchange + ipft) * D('0.18')
    tax = value * (D('0.00003') if side == 'BUY' else D('0.00025'))
    return (brokerage + exchange + sebi + ipft + gst + tax).quantize(D('0.01'), rounding=ROUND_HALF_UP)


def target(entry, quantity, direction, tick, profit_pct):
    """First tick achieving the net target after estimated entry AND exit fees."""
    entry, tick = D(entry), D(tick)
    opening, closing = ('BUY', 'SELL') if direction == 'LONG' else ('SELL', 'BUY')
    needed = entry * quantity * D(profit_pct) / 100
    def net(p):
        gross = (p - entry) * quantity * (1 if direction == 'LONG' else -1)
        return gross - fee(opening, entry * quantity) - fee(closing, p * quantity)
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
