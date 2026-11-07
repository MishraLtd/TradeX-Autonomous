"""Conservative account loss budget: losses consume it, wins never replenish it."""
from datetime import datetime
from decimal import Decimal
from .market import IST
from .models import dec
from .costs import trade_fees


def assess(config, trades, quote, now):
    day = datetime.fromtimestamp(now, IST).date().isoformat()
    losses = Decimal(0)
    errors = []
    for trade in trades:
        if trade['state'] == 'NO_FILL':
            continue
        if trade['state'] == 'CLOSED':
            at = trade.get('closed_at') or trade.get('financials', {}).get('last_fill_at')
            if at is None:
                errors.append(trade['id'] + ': closure date needs financial reconciliation')
                continue
            if datetime.fromtimestamp(at, IST).date().isoformat() == day:
                value = trade.get('financials', {}).get('actual_net_pnl')
                value = trade.get('estimated_net_pnl') if value is None else value
                losses += max(Decimal(0), -dec(value))
            continue
        buys = [o for o in trade['orders'] if o['side'] == 'BUY' and o['filled']]
        sells = [o for o in trade['orders'] if o['side'] == 'SELL' and o['filled']]
        bought = sum(o['filled'] for o in buys)
        remaining = bought - sum(o['filled'] for o in sells)
        if remaining < 0:
            errors.append(trade['id'] + ': oversell')
            continue
        try:
            fees = trade_fees(trade)
            invested = sum((dec(o['average']) * o['filled'] for o in buys), Decimal(0))
            proceeds = sum((dec(o['average']) * o['filled'] for o in sells), Decimal(0))
            charges = sum((fees.cost(o['side'], dec(o['average']) * o['filled']) for o in buys + sells), Decimal(0))
            if remaining:
                price = quote(trade['stock'])
                price.check(now, config.max_quote_age)
                value = dec(price.price) * remaining
                proceeds += value
                charges += fees.cost('SELL', value)
            losses += max(Decimal(0), invested + charges - proceeds)
        except Exception:
            errors.append(trade['id'] + ': fresh valuation unavailable')
    return dict(day=day, checked_at=now, loss_used=str(losses), limit=config.daily_loss_limit,
                breached=losses >= dec(config.daily_loss_limit), errors=errors)


def alerts(store, config, now):
    if config.daily_loss_limit is None:
        return []
    state = store.get('daily_risk', {})
    day = datetime.fromtimestamp(now, IST).date().isoformat()
    stale = state.get('day') != day or not 0 <= now - state.get('checked_at', 0) <= config.health_max_age
    if not (stale or state.get('breached') or state.get('errors')):
        return []
    return [dict(code='DAILY_LOSS_LIMIT' if state.get('breached') and state.get('day') == day else 'RISK_VALUATION_UNAVAILABLE',
                 severity='CRITICAL', trade_id=None, ref='account', deadline=None,
                 message='Account loss budget breached or fresh risk valuation unavailable',
                 action='Inspect daily_risk; losses latch for the IST day. Existing exit ownership checks remain required.')]
