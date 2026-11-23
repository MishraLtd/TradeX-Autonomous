"""Cash-backed, two-venue paper matches on one ISIN. No broker order transport."""
from .config import D, price_tick, stamp
from .costs import fee

MAX_BOOK_AGE = 1.0
MAX_BOOK_SKEW = 0.5
PAIR_TIMEOUT = 2.0


def current_books(book, symbol, now, stream, diagnostics=None):
    quotes = {v: book.s['quotes'].get(k) for v, k in (('NSE', symbol), ('BSE', 'BSE:' + symbol))}
    for venue, q in quotes.items():
        reason = ('MISSING_BOOK' if not q else
                  'STREAM_MISMATCH' if not stream or q.get('stream_id') != stream else
                  'SNAPSHOT_NOT_LIVE' if not q.get('live') else
                  'BOOK_STALE' if not all(0 <= now - q[k] <= MAX_BOOK_AGE for k in ('at', 'received')) else
                  'LAST_TRADE_STALE' if not 0 <= now - q['traded'] <= book.cfg.max_quote_age else None)
        if reason:
            if diagnostics is not None:
                diagnostics.update(detail=reason, venue=venue)
            return None
    if abs(quotes['NSE']['at'] - quotes['BSE']['at']) > MAX_BOOK_SKEW:
        if diagnostics is not None:
            diagnostics.update(detail='BOOK_TIMESTAMP_SKEW')
        return None
    return quotes


def quote_pair(cfg, symbol, books, buy_venue, cash, quantity_limit=None, diagnostics=None):
    def reject(reason):
        if diagnostics is not None:
            diagnostics['detail'] = reason
        return None
    sell_venue = 'BSE' if buy_venue == 'NSE' else 'NSE'
    buy, sell = books[buy_venue], books[sell_venue]
    ask, bid = D(buy['ask']), D(sell['bid'])
    if diagnostics is not None:
        diagnostics.update(buy_venue=buy_venue, sell_venue=sell_venue,
                           ask=str(ask), bid=str(bid), cash=str(cash), quantity_limit=quantity_limit)
    if bid <= ask:
        return reject('NO_RAW_SPREAD')
    ticks = {'NSE': cfg.instruments[symbol]['tick'], 'BSE': cfg.bse_instruments[symbol]['tick']}
    slip = D(cfg.slippage_bps) / 10000
    buy_price = price_tick(ask * (1 + slip), ticks[buy_venue], True)
    sell_price = price_tick(bid * (1 - slip), ticks[sell_venue], False)
    if sell_price <= buy_price:
        return reject('SPREAD_LOST_TO_SLIPPAGE_OR_TICKS')
    max_qty = min(int(D(min(buy['ask_qty'], sell['bid_qty'])) * D(cfg.depth_participation)),
                  int(cash / sell_price))
    if quantity_limit is not None:
        max_qty = min(max_qty, quantity_limit)
    # Reserve the larger leg at full notional plus both charges, including for a short.
    # Costs are monotonic in quantity and each side has its own brokerage cap.
    lo, hi = 0, max_qty
    while lo < hi:
        mid = (lo + hi + 1) // 2
        charges = fee('BUY', mid * buy_price, cfg.fee_model, buy_venue) + fee('SELL', mid * sell_price, cfg.fee_model, sell_venue)
        if mid * sell_price + charges <= cash:
            lo = mid
        else:
            hi = mid - 1
    if not lo:
        return reject('INSUFFICIENT_DEPTH' if int(D(min(buy['ask_qty'], sell['bid_qty'])) * D(cfg.depth_participation)) < 1 else 'INSUFFICIENT_CASH')
    buy_fee = fee('BUY', lo * buy_price, cfg.fee_model, buy_venue)
    sell_fee = fee('SELL', lo * sell_price, cfg.fee_model, sell_venue)
    gross = (sell_price - buy_price) * lo
    net = gross - buy_fee - sell_fee
    if diagnostics is not None:
        diagnostics.update(quantity=lo, gross=str(gross), fees=str(buy_fee + sell_fee), net=str(net))
    if net <= 0:
        return reject('NON_POSITIVE_NET_AFTER_FEES')
    return dict(buy_venue=buy_venue, sell_venue=sell_venue, ask=ask, bid=bid, raw_gap=bid - ask,
                buy_price=buy_price, sell_price=sell_price, quantity=lo, buy_fee=buy_fee, sell_fee=sell_fee,
                gross=gross, net=net, budget=sell_price * lo + buy_fee + sell_fee,
                book_at={v: q['at'] for v, q in books.items()})


def depth_signature(quote, side):
    field = 'ask' if side == 'BUY' else 'bid'
    return [str(D(quote[field]).normalize()), quote[field + '_qty']]


def unused_depth(consumed, books, buy_venue):
    # A fresh feed timestamp alone does not replenish previously consumed depth.
    sell_venue = 'BSE' if buy_venue == 'NSE' else 'NSE'
    return (consumed.get(buy_venue + 'BUY') != depth_signature(books[buy_venue], 'BUY')
            and consumed.get(sell_venue + 'SELL') != depth_signature(books[sell_venue], 'SELL'))


def record_fill(book, slot, pair, books, now, consumed):
    qty, symbol = pair['quantity'], slot['symbol']
    # The legs match simultaneously. The NSE leg is the reporting reference for direction.
    long = pair['buy_venue'] == 'NSE'
    opening, closing = ('buy', 'sell') if long else ('sell', 'buy')
    book.s['trades'][slot['id']] = dict(
        id=slot['id'], day=slot['day'], symbol=symbol, isin=book.cfg.instruments[symbol]['key'].split('|')[1],
        direction=slot['direction'], state='CLOSED', budget=slot['budget'], remaining=0, quantity=qty,
        requested_quantity=slot['signal']['quantity'], entry_at=now, closed_at=now, entry_price=str(pair[opening + '_price']),
        entry_fees=str(pair[opening + '_fee']), exit_fees=str(pair[closing + '_fee']),
        entry_venue='NSE', exit_venue='BSE', exit_price=str(pair[closing + '_price']),
        exit_turnover=str(qty * pair[closing + '_price']), gross_pnl=str(pair['gross']), spread_match=pair,
        entry_policy='CROSS_EXCHANGE', exit_policy='MATCHED_PAIR', fee_model=book.cfg.fee_model,
        target=None, stop=None, exit_intent=None, reason='POSITIVE_NET_SPREAD')
    for leg, side in (('ENTRY', opening), ('EXIT', closing)):
        venue, price = pair[side + '_venue'], pair[side + '_price']
        raw = pair['ask' if side == 'buy' else 'bid']
        q = books[venue]
        book.event(now, 'PAPER_FILL', trade=slot['id'], leg=leg, side=side.upper(), venue=venue,
                   quantity=qty, price=price, fees=pair[side + '_fee'], requested_at=slot['at'],
                   quote_at=q['at'], book_price=raw, slippage_cost=qty * abs(price - raw),
                   reason='POSITIVE_NET_SPREAD', fee_model=book.cfg.fee_model)
        consumed[venue + side.upper()] = depth_signature(q, side.upper())
    book.s['realized_gross'] = str(D(book.s['realized_gross']) + pair['gross'])
    book.s['fees'] = str(D(book.s['fees']) + pair['buy_fee'] + pair['sell_fee'])
    slot['state'] = 'FILLED' if qty == slot['signal']['quantity'] else 'PARTIAL_IOC'


def step(book, now, day, allowed, fresh):
    stream = book.s['market'].get('stream_id')
    permitted = (allowed and stream and not book.s['halt'] and not book.s['day_loss'].get(day)
                 and stamp(day, book.cfg.trend_settings['first_entry']) <= now < stamp(day, book.cfg.flatten_time))
    stats = book.s.setdefault('spread_session', {})
    if stats.get('day') != day:
        stats.clear()
        stats.update(day=day, sequence=0, checked=0, matched=0, consumed={})
    for slot in book.pending():
        if slot.get('policy') != 'CROSS_EXCHANGE':
            raise ValueError('Cross-exchange activation requires a flat portfolio without pending entries')
        symbol = slot['symbol']
        reason = None
        diagnostics = {}
        if not permitted or slot['stream_id'] != stream or slot['day'] != day:
            reason = 'PAIR_SESSION_UNAVAILABLE'
        elif now >= slot['expires']:
            reason = 'PAIR_EXPIRED'
            diagnostics.update(detail=slot.get('last_wait', 'DEADLINE_REACHED'),
                               last_wait_at=slot.get('last_wait_at'))
        elif now >= slot['after']:
            quotes = current_books(book, symbol, now, stream, diagnostics)
            if not quotes:
                slot.update(last_wait=diagnostics['detail'], last_wait_at=now)
                continue
            if any(q['at'] < slot['after'] or q['at'] <= slot['signal']['book_at'][v] for v, q in quotes.items()):
                slot.update(last_wait='WAITING_FOR_NEW_BOOKS_AFTER_LATENCY', last_wait_at=now)
                continue
            budget = min(D(slot['budget']), book.available(exclude_slot=slot['id']))
            pair = quote_pair(book.cfg, symbol, quotes, slot['signal']['buy_venue'], budget, slot['signal']['quantity'], diagnostics)
            consumed = stats['consumed'].setdefault(symbol, {})
            if pair and unused_depth(consumed, quotes, pair['buy_venue']):
                record_fill(book, slot, pair, quotes, now, consumed)
                stats['matched'] += 1
                continue
            if pair:
                diagnostics['detail'] = 'DEPTH_ALREADY_CONSUMED'
            reason = 'PAIR_NO_LONGER_EXECUTABLE'
        if reason:
            diagnostics.update(elapsed_seconds=now - slot['at'], latency_seconds=book.cfg.latency_seconds,
                               books={v: {**{k: q.get(k) for k in ('bid', 'ask', 'bid_qty', 'ask_qty', 'live', 'stream_id')},
                                          'ages': {k: now - q[k] if k in q else None for k in ('at', 'received', 'traded')}}
                                      for v, key in (('NSE', symbol), ('BSE', 'BSE:' + symbol))
                                      for q in [book.s['quotes'].get(key, {})]})
            slot.update(state='SKIPPED', reason=reason, diagnostics=diagnostics)
            book.event(now, 'ENTRY_SKIPPED', slot=slot['id'], symbol=symbol, reason=reason, diagnostics=diagnostics)
    if not permitted or not fresh:
        return
    occupied = {s['symbol'] for s in book.pending() + book.active()}
    cash = book.available()
    ranked = []
    for symbol in sorted({alias.removeprefix('BSE:') for alias in fresh} - occupied):
        books = current_books(book, symbol, now, stream)
        if not books:
            continue
        stats['checked'] += 1
        for venue in ('NSE', 'BSE'):
            p = quote_pair(book.cfg, symbol, books, venue, cash)
            if p and unused_depth(stats['consumed'].get(symbol, {}), books, venue):
                ranked.append((p['net'], symbol, venue, books))
    for _, symbol, venue, books in sorted(ranked, key=lambda row: (-row[0], row[1])):
        if len(occupied) >= book.cfg.max_positions:
            break
        p = quote_pair(book.cfg, symbol, books, venue, cash)
        if not p:
            continue
        stats['sequence'] += 1
        key = f"{day}:spread:{stats['sequence']}"
        slot = dict(id=key, day=day, symbol=symbol, policy='CROSS_EXCHANGE', signal=p, state='PENDING',
                    direction='LONG' if venue == 'NSE' else 'SHORT', at=now, after=now + book.cfg.latency_seconds,
                    expires=min(now + PAIR_TIMEOUT, stamp(day, book.cfg.flatten_time)), stream_id=stream,
                    budget=str(p['budget']), reason=None)
        book.s['slots'][key] = slot
        book.event(now, 'SPREAD_ENTRY_REQUEST', **slot)
        cash -= p['budget']
        occupied.add(symbol)
