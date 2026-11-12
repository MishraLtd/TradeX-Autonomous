"""Bounded live-book observations; deterministic signals, never predictions."""
from .config import D, stamp

SETTINGS = dict(first_entry='09:30', cooldown_seconds=240, minimum_hold_seconds=900,
                recovery_min_seconds=3600, recovery_max_seconds=7200,
                loss_trigger_inr='5', near_zero_inr='-0.50', lookback_seconds=300,
                minimum_move_pct='0.10', sample_seconds=5, maximum_gap_seconds=20)


def observe(state, quotes, now, day):
    history = state.setdefault('trend_history', {})
    for symbol, q in quotes.items():
        at = q['at']
        if at < stamp(day, '09:15') or at > now:
            continue
        rows = [r for r in history.get(symbol, []) if r[0] >= now - 360]
        point = [at, str((D(q['bid']) + D(q['ask'])) / 2)]
        if rows and int(rows[-1][0] // 5) == int(at // 5):
            rows[-1] = point
        else:
            rows.append(point)
        history[symbol] = rows[-74:]


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
    return dict(direction='LONG' if sign > 0 else 'SHORT', move_pct=str(move),
                strength=str(abs(move)), from_at=anchors[0][0], to_at=anchors[-1][0])
