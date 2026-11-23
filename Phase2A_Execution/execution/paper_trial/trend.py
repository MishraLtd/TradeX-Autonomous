"""Bounded live-book observations; deterministic signals, never predictions."""
from .config import D, stamp

LEGACY_SETTINGS = dict(first_entry='09:30', cooldown_seconds=240, minimum_hold_seconds=900,
                       recovery_min_seconds=3600, recovery_max_seconds=7200,
                       loss_trigger_inr='5', near_zero_inr='-0.50', lookback_seconds=300,
                       minimum_move_pct='0.10', sample_seconds=5, maximum_gap_seconds=20)

SETTINGS = dict(first_entry='09:30', cooldown_seconds=240, minimum_hold_seconds=900,
                recovery_min_seconds=3600, recovery_max_seconds=7200,
                loss_trigger_inr='5', near_zero_inr='-0.50', lookback_seconds=300,
                volatility_lookback_seconds=900, minimum_move_pct='0.10',
                sample_seconds=5, maximum_gap_seconds=20)


def observe(state, quotes, now, day):
    history = state.setdefault('trend_history', {})
    for symbol, q in quotes.items():
        at = q['at']
        if at < stamp(day, '09:15') or at > now:
            continue
        # Preserve the opening 15-minute cooling period plus a small margin.
        rows = [r for r in history.get(symbol, []) if r[0] >= now - 960]
        point = [at, str((D(q['bid']) + D(q['ask'])) / 2)]
        if rows and int(rows[-1][0] // 5) == int(at // 5):
            rows[-1] = point
        else:
            rows.append(point)
        history[symbol] = rows[-194:]


def signal(state, symbol, now, settings):
    rows = state.get('trend_history', {}).get(symbol, [])
    if not rows or now - rows[-1][0] > 15:
        return None
    anchors = []
    for seconds in (settings['lookback_seconds'], 120, 60, 0):
        prior = [r for r in rows if r[0] <= now - seconds]
        if not prior or now - seconds - prior[-1][0] > 15:
            return None
        anchors.append(prior[-1])
    window = [r for r in rows if r[0] >= anchors[0][0]]
    if any(b[0] - a[0] > settings['maximum_gap_seconds'] for a, b in zip(window, window[1:])):
        return None
    move = (D(anchors[-1][1]) / D(anchors[0][1]) - 1) * 100
    if abs(move) < D(settings['minimum_move_pct']):
        return None
    sign = 1 if move > 0 else -1
    if any((D(b[1]) - D(a[1])) * sign <= 0 for a, b in zip(anchors[1:], anchors[2:])):
        return None
    # Rank only confirmed trends by observed 15-minute volatility.  The score
    # is 60% high-low range plus 40% realised one-minute movement, both in %.
    # It does not predict price: direction remains the independently confirmed
    # five-minute move above.
    volatility_start = now - settings.get('volatility_lookback_seconds', 900)
    volatility_rows = [r for r in rows if r[0] >= volatility_start]
    # A reconnect or a migrated historical test may not contain the full
    # opening window.  Keep the direction usable, but make its score the
    # confirmed directional strength; a normal 09:30 start always reaches the
    # volatility branch below.
    if not volatility_rows or volatility_rows[0][0] > volatility_start + 15:
        return dict(direction='LONG' if sign > 0 else 'SHORT', move_pct=str(move),
                    strength=str(abs(move)), range_pct=None, realized_volatility_pct=None,
                    volatility_score=str(abs(move)), formula='fallback_confirmed_5m_move_pct',
                    from_at=anchors[0][0], to_at=anchors[-1][0])
    minute_points = []
    for point in volatility_rows:
        if not minute_points or point[0] - minute_points[-1][0] >= 55:
            minute_points.append(point)
    if len(minute_points) < 5:
        return dict(direction='LONG' if sign > 0 else 'SHORT', move_pct=str(move),
                    strength=str(abs(move)), range_pct=None, realized_volatility_pct=None,
                    volatility_score=str(abs(move)), formula='fallback_confirmed_5m_move_pct',
                    from_at=anchors[0][0], to_at=anchors[-1][0])
    prices = [D(r[1]) for r in minute_points]
    low, high = min(prices), max(prices)
    range_pct = (high / low - 1) * 100
    realized_pct = sum((abs(b / a - 1) for a, b in zip(prices, prices[1:])), D(0)) * 100
    volatility_score = range_pct * D('0.60') + realized_pct * D('0.40')
    return dict(direction='LONG' if sign > 0 else 'SHORT', move_pct=str(move),
                strength=str(abs(move)), range_pct=str(range_pct),
                realized_volatility_pct=str(realized_pct), volatility_score=str(volatility_score),
                formula='0.60*15m_range_pct + 0.40*15m_realized_1m_move_pct',
                from_at=anchors[0][0], to_at=anchors[-1][0])
