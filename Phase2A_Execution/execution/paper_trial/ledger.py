"""Single-writer, transactional intraday simulation. Never imports a broker SDK.

Account, schedule, positions, fills and feed watermarks share one SQLite DB.
A snapshot therefore cannot restore an engine without its simulated account.
"""
from contextlib import contextmanager, closing
from dataclasses import asdict
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import time

from .config import D, IST, dumps, stamp, price_tick, implementation_fingerprint
from .costs import fee, target


def read_state(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        return json.loads(db.execute('SELECT value FROM trial WHERE id=1').fetchone()[0])


class Ledger:
    def __init__(self, path, config):
        self.path, self.cfg = Path(path), config
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = open(str(self.path) + '.lock', 'a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise RuntimeError('Another paper trial writer owns this ledger') from None
        try:
            self.db = sqlite3.connect(self.path, timeout=10)
            os.chmod(self.path, 0o600)
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS trial(id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, at REAL, kind TEXT, data TEXT);
                CREATE TABLE IF NOT EXISTS equity(at REAL PRIMARY KEY, value TEXT, stale INTEGER);
            ''')
            row = self.db.execute('SELECT value FROM trial WHERE id=1').fetchone()
            self.s = json.loads(row[0]) if row else dict(
                schema=1, config_hash=config.fingerprint, config=asdict(config),
                implementation_sha256=implementation_fingerprint(), release=os.environ.get('TRADEX_TRIAL_RELEASE', 'local'),
                trades={}, slots={}, sessions={}, quotes={}, watermarks={},
                heartbeat=0, previous_step=0, last_sample=0, realized_gross='0', fees='0',
                halt=None, day_loss={}, feed_fresh_seconds=0, rejected_quotes=0,
                reconnects=0, live_quote_updates=0, last_error=None, market={}, completed=False)
            if self.s['config_hash'] != config.fingerprint:
                raise ValueError('Trial inputs changed; restore the frozen config or use a new trial DB')
            if self.s['implementation_sha256'] != implementation_fingerprint():
                raise ValueError('Trial implementation changed; preserve the reviewed release for this experiment')
            self.save()
            self.db.commit()
        except Exception:
            self.close()
            raise

    def close(self):
        if hasattr(self, 'db'):
            self.db.close()
        if not self.lock.closed:
            self.lock.close()

    def save(self):
        self.db.execute('INSERT OR REPLACE INTO trial VALUES (1,?)', (dumps(self.s),))

    @contextmanager
    def atomic(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.save()
            self.db.commit()
        except Exception:
            self.db.rollback()
            self.s = read_state(self.path)
            raise

    def event(self, now, kind, **data):
        self.db.execute('INSERT INTO events(at,kind,data) VALUES (?,?,?)', (now, kind, dumps(data)))

    def fresh(self, symbol, now):
        q = self.s['quotes'].get(symbol)
        return bool(q and all(-2 <= now - q[k] <= self.cfg.max_quote_age for k in ('at', 'received', 'traded')))

    def active(self):
        return [t for t in self.s['trades'].values() if t['remaining'] > 0]

    def pending(self):
        return [t for t in self.s['slots'].values() if t['state'] == 'PENDING']

    def exit_price(self, trade, quote):
        buy = trade['direction'] == 'SHORT'
        raw = D(quote['ask' if buy else 'bid'])
        return price_tick(raw * (1 + D(self.cfg.slippage_bps) / 10000 * (1 if buy else -1)),
                          self.cfg.instruments[trade['symbol']]['tick'], buy)

    def unrealized(self, trade):
        quote = self.s['quotes'].get(trade['symbol'])
        if not quote:
            return D(0)
        value = self.exit_price(trade, quote)
        gross = (value - D(trade['entry_price'])) * trade['remaining'] * (1 if trade['direction'] == 'LONG' else -1)
        closing = 'SELL' if trade['direction'] == 'LONG' else 'BUY'
        extra_fee = fee(closing, D(trade['exit_turnover']) + value * trade['remaining']) - D(trade['exit_fees'])
        return gross - extra_fee

    def available(self, exclude_slot=None):
        settled = D(self.cfg.initial_cash) + D(self.s['realized_gross']) - D(self.s['fees'])
        reserved = sum((D(t['entry_price']) * t['remaining'] for t in self.active()), D(0))
        pending = sum((D(t['budget']) for t in self.pending() if t['id'] != exclude_slot), D(0))
        losses = sum((min(D(0), self.unrealized(t)) for t in self.active()), D(0))
        # Short-sale proceeds never become spendable funds. Both sides reserve
        # 100% of notional; unrealized profits never fund another entry.
        return max(D(0), settled - reserved - pending + losses)

    def request_exit(self, trade, now, reason):
        if trade.get('exit_intent'):
            return
        quote = self.s['quotes'].get(trade['symbol'], {})
        trade['exit_intent'] = dict(at=now, after=now + self.cfg.latency_seconds,
                                    quote_at=quote.get('at', 0), reason=reason)
        self.event(now, 'EXIT_REQUEST', trade=trade['id'], reason=reason)

    def _accept_quotes(self, quotes, now):
        accepted = {}
        for symbol, q in quotes.items():
            try:
                if symbol not in self.cfg.instruments or not all(
                        -2 <= now - q[k] <= self.cfg.max_quote_age for k in ('at', 'received', 'traded')):
                    raise ValueError('Quote age')
                if not 0 < D(q['bid']) <= D(q['ask']) or D(q['last']) <= 0:
                    raise ValueError('Invalid book')
                tick = D(self.cfg.instruments[symbol]['tick'])
                if any(D(q[k]) % tick for k in ('bid', 'ask', 'last')):
                    raise ValueError('Quote/tick mismatch')
                if any(type(q[k]) is not int or q[k] < 0 for k in ('bid_qty', 'ask_qty')):
                    raise ValueError('Invalid depth')
                if q['at'] <= self.s['watermarks'].get(symbol, 0):
                    continue
                self.s['quotes'][symbol] = q
                self.s['watermarks'][symbol] = q['at']
                if q.get('live') is True:
                    accepted[symbol] = q
                    self.s['live_quote_updates'] += 1
            except (KeyError, ValueError, TypeError):
                self.s['rejected_quotes'] += 1
        return accepted

    def step(self, now, market, quotes=None):
        """A timer tick and/or new market frame; only new LIVE books can fill.

        market is produced by the data-only runtime, not by an HTTP user input.
        A successful same-day calendar check AND NORMAL_OPEN are required.
        """
        with self.atomic():
            previous = self.s['previous_step']
            if previous and now < previous - 2:
                self.s['halt'] = 'CLOCK_MOVED_BACKWARDS'
            self.s['heartbeat'] = self.s['previous_step'] = now
            self.s['market'] = market
            day = datetime.fromtimestamp(now, IST).date().isoformat()
            fresh = self._accept_quotes(quotes or {}, now)
            for planned in self.cfg.sessions:
                if now >= stamp(planned, self.cfg.hard_close_time):
                    for slot_id, slot in self.s['slots'].items():
                        if slot['day'] == planned and slot['state'] == 'PENDING':
                            slot['state'], slot['reason'] = 'SKIPPED', 'ENTRY_EXPIRED'
                    unresolved = [t['id'] for t in self.active() if t['day'] <= planned]
                    if unresolved:
                        if not self.s['halt']:
                            self.event(now, 'INCIDENT', reason='INTRADAY_EXIT_UNRESOLVED', trades=unresolved)
                        self.s['halt'] = 'INTRADAY_EXIT_UNRESOLVED'
                        self.s['sessions'][planned] = 'FAILED_UNRESOLVED'
                    elif planned not in self.s['sessions']:
                        self.s['sessions'][planned] = 'MISSED'
                    elif self.s['sessions'][planned] == 'RUNNING':
                        self.s['sessions'][planned] = 'FINISHED'
            planned_today = day in self.cfg.sessions
            allowed = bool(planned_today and market.get('day') == day and market.get('calendar_valid') is True
                           and market.get('normal') is True and market.get('connected') is True
                           and stamp(day, '09:15') <= now < stamp(day, self.cfg.hard_close_time))
            if self.s['halt'] == 'RESTORED_TRIAL_REQUIRES_REVIEW':
                allowed = False
            if planned_today and now < stamp(day, self.cfg.hard_close_time):
                if now >= stamp(day, '09:15'):
                    self.s['sessions'].setdefault(day, 'RUNNING')
                if allowed and all(self.fresh(s, now) and self.s['quotes'][s].get('live') for s in self.cfg.instruments):
                    self.s['feed_fresh_seconds'] += max(0, min(2, now - previous)) if previous else 0
                self._risk_and_exits(now, day)
            # Liquidity is consumed once per stock/side/frame, shared by exits
            # and entries. A duplicate or reconnect snapshot can never refill it.
            liquidity = {(s, side): int(D(q[field]) * D(self.cfg.depth_participation))
                         for s, q in fresh.items() for side, field in (('BUY', 'ask_qty'), ('SELL', 'bid_qty'))}
            if allowed:
                for trade in self.active():
                    if trade['day'] == day:
                        self._fill_exit(trade, fresh, liquidity, now)
            if planned_today:
                self._schedule(now, day, allowed)
                for slot in list(self.pending()):
                    if slot['day'] != day or now > slot['expires'] or self.s['halt'] or self.s['day_loss'].get(day):
                        slot['state'], slot['reason'] = 'SKIPPED', 'ENTRY_EXPIRED_OR_PAUSED'
                    elif allowed:
                        self._fill_entry(slot, fresh, liquidity, now)
            self.s['completed'] = now >= stamp(self.cfg.sessions[-1], self.cfg.hard_close_time)
            if now - self.s['last_sample'] >= 60:
                summary = self.summary(now)
                self.db.execute('INSERT OR REPLACE INTO equity VALUES (?,?,?)',
                                (now, summary['equity_estimate'], bool(summary['stale_marks'])))
                self.s['last_sample'] = now

    def _risk_and_exits(self, now, day):
        losses = D(0)
        for trade in self.s['trades'].values():
            if trade['day'] != day:
                continue
            pnl = D(trade['gross_pnl']) - D(trade['entry_fees']) - D(trade['exit_fees']) + self.unrealized(trade)
            losses += max(D(0), -pnl)
        if losses >= D(self.cfg.daily_loss_limit) and not self.s['day_loss'].get(day):
            self.s['day_loss'][day] = True
            self.event(now, 'DAILY_LOSS_PAUSE', loss_estimate=losses)
        for trade in self.active():
            if trade['day'] != day:
                continue
            if now >= stamp(day, self.cfg.flatten_time):
                self.request_exit(trade, now, 'INTRADAY_SQUARE_OFF')
            elif self.s['day_loss'].get(day) or self.s['halt']:
                self.request_exit(trade, now, 'RISK_HALT')
            elif self.fresh(trade['symbol'], now):
                quote = self.s['quotes'][trade['symbol']]
                p = self.exit_price(trade, quote)
                stop_hit = p <= D(trade['stop']) if trade['direction'] == 'LONG' else p >= D(trade['stop'])
                target_hit = p >= D(trade['target']) if trade['direction'] == 'LONG' else p <= D(trade['target'])
                if stop_hit or target_hit:
                    self.request_exit(trade, now, 'STOP_LOSS' if stop_hit else 'PROFIT_TARGET')

    def _schedule(self, now, day, allowed):
        symbols = sorted(self.cfg.instruments, key=lambda s: self.cfg.instruments[s]['slot'])
        day_index = self.cfg.sessions.index(day)
        for index, hhmm in enumerate(self.cfg.entry_times):
            due = stamp(day, hhmm)
            slot_id = day + ':' + str(index)
            if slot_id in self.s['slots'] or now < due:
                continue
            slot = dict(id=slot_id, day=day, symbol=symbols[index], state='SKIPPED', reason=None,
                        direction='LONG' if (index + day_index) % 2 == 0 else 'SHORT')
            if now > due + self.cfg.entry_window:
                slot['reason'] = 'MISSED_ENTRY_WINDOW'
            elif self.s['halt'] or self.s['day_loss'].get(day):
                slot['reason'] = 'RISK_PAUSED'
            elif not allowed or not self.fresh(slot['symbol'], now):
                continue  # Wait only within this slot's short window.
            elif any(not self.fresh(t['symbol'], now) for t in self.active()):
                continue
            elif len(self.active()) + len(self.pending()) >= self.cfg.max_positions:
                slot['reason'] = 'POSITION_LIMIT'
            else:
                free_slots = self.cfg.max_positions - len(self.active()) - len(self.pending())
                budget = min(D(self.cfg.per_trade_limit), self.available() / free_slots).quantize(D('0.01'))
                if budget <= 0:
                    slot['reason'] = 'CASH_LIMIT'
                else:
                    q = self.s['quotes'][slot['symbol']]
                    if (D(q['ask']) - D(q['bid'])) / D(q['bid']) > D('0.01'):
                        continue
                    slot.update(state='PENDING', budget=str(budget), at=now,
                                after=now + self.cfg.latency_seconds, quote_at=q['at'],
                                expires=due + self.cfg.entry_window)
            self.s['slots'][slot_id] = slot
            self.event(now, 'SCHEDULE_SLOT', **slot)

    def _fill_entry(self, slot, quotes, liquidity, now):
        q = quotes.get(slot['symbol'])
        if not q or now < slot['after'] or q['at'] <= slot['quote_at']:
            return
        if any(not self.fresh(t['symbol'], now) for t in self.active()):
            return
        side = 'BUY' if slot['direction'] == 'LONG' else 'SELL'
        tick = self.cfg.instruments[slot['symbol']]['tick']
        raw = D(q['ask' if side == 'BUY' else 'bid'])
        p = price_tick(raw * (1 + D(self.cfg.slippage_bps) / 10000 * (1 if side == 'BUY' else -1)), tick, side == 'BUY')
        if p <= 0:
            return
        budget = min(D(slot['budget']), self.available(exclude_slot=slot['id']))
        wanted = int(budget / p)
        while wanted and wanted * p + fee(side, wanted * p) > budget:
            wanted -= 1
        qty = min(wanted, liquidity[(slot['symbol'], side)])
        if qty <= 0:
            if wanted == 0:
                slot['state'], slot['reason'] = 'SKIPPED', 'INSUFFICIENT_CASH_FOR_ONE_SHARE'
                self.event(now, 'ENTRY_SKIPPED', slot=slot['id'], reason=slot['reason'])
            return
        opening_fee = fee(side, qty * p)
        sign = 1 if slot['direction'] == 'LONG' else -1
        stop = price_tick(p * (1 - sign * D(self.cfg.stop_pct) / 100), tick, sign == -1)
        trade = dict(id=slot['id'], day=slot['day'], symbol=slot['symbol'], direction=slot['direction'],
                     state='OPEN', budget=slot['budget'], requested_quantity=wanted, quantity=qty, remaining=qty,
                     entry_at=now, entry_price=str(p), entry_fees=str(opening_fee),
                     target=str(target(p, qty, slot['direction'], tick, self.cfg.profit_pct)), stop=str(stop),
                     exit_turnover='0', exit_fees='0', gross_pnl='0', exit_intent=None)
        self.s['trades'][trade['id']] = trade
        self.s['fees'] = str(D(self.s['fees']) + opening_fee)
        slot['state'] = 'FILLED' if qty == wanted else 'PARTIAL_IOC'
        liquidity[(slot['symbol'], side)] -= qty
        self.event(now, 'PAPER_FILL', trade=trade['id'], leg='ENTRY', side=side, quantity=qty,
                   price=p, fees=opening_fee, quote_at=q['at'], requested_at=slot['at'],
                   cancelled_quantity=wanted - qty, spread=str(D(q['ask']) - D(q['bid'])),
                   book_price=raw, slippage_cost=abs(p - raw) * qty)

    def _fill_exit(self, trade, quotes, liquidity, now):
        intent, q = trade.get('exit_intent'), quotes.get(trade['symbol'])
        if not intent or not q or now < intent['after'] or q['at'] <= intent['quote_at']:
            return
        side = 'SELL' if trade['direction'] == 'LONG' else 'BUY'
        qty = min(trade['remaining'], liquidity[(trade['symbol'], side)])
        if not qty:
            return
        p = self.exit_price(trade, q)
        if p <= 0:
            return
        cumulative = D(trade['exit_turnover']) + p * qty
        charge = fee(side, cumulative) - D(trade['exit_fees'])
        gross = (p - D(trade['entry_price'])) * qty * (1 if trade['direction'] == 'LONG' else -1)
        trade['exit_turnover'], trade['exit_fees'] = str(cumulative), str(fee(side, cumulative))
        trade['gross_pnl'] = str(D(trade['gross_pnl']) + gross)
        trade['remaining'] -= qty
        self.s['realized_gross'] = str(D(self.s['realized_gross']) + gross)
        self.s['fees'] = str(D(self.s['fees']) + charge)
        liquidity[(trade['symbol'], side)] -= qty
        if trade['remaining'] == 0:
            trade.update(state='CLOSED', closed_at=now, reason=intent['reason'])
        self.event(now, 'PAPER_FILL', trade=trade['id'], leg='EXIT', side=side, quantity=qty,
                   price=p, fees=charge, quote_at=q['at'], requested_at=intent['at'], reason=intent['reason'],
                   book_price=q['ask' if side == 'BUY' else 'bid'],
                   slippage_cost=abs(p - D(q['ask' if side == 'BUY' else 'bid'])) * qty)

    def summary(self, now):
        return summarize(self.s, now)


def summarize(state, now=None):
    now = time.time() if now is None else now
    # Pure read-only view using the exact valuation functions of this ledger.
    from .config import TrialConfig
    obj = object.__new__(Ledger)
    obj.s, obj.cfg = state, TrialConfig(**state['config'])
    cfg = obj.cfg
    open_trades = obj.active()
    unrealized = sum((obj.unrealized(t) for t in open_trades), D(0))
    settled = D(cfg.initial_cash) + D(state['realized_gross']) - D(state['fees'])
    stale = [t['id'] for t in open_trades if not obj.fresh(t['symbol'], now)]
    day = datetime.fromtimestamp(now, IST).date().isoformat()
    expected = day in cfg.sessions and stamp(day, '09:15') <= now < stamp(day, cfg.hard_close_time)
    codes = []
    if not 0 <= now - state['heartbeat'] <= 30:
        codes.append('WORKER_STALE')
    if state['halt']:
        codes.append(state['halt'])
    if any(v == 'MISSED' for v in state['sessions'].values()):
        codes.append('PLANNED_SESSION_MISSED')
    if state['completed'] and not state['trades']:
        codes.append('TRIAL_HAS_NO_FILLS')
    if any(s.get('reason') == 'MISSED_ENTRY_WINDOW' for s in state['slots'].values()):
        codes.append('PLANNED_ENTRY_MISSED')
    if expected:
        m = state['market']
        if not (m.get('day') == day and m.get('calendar_valid')):
            codes.append('CALENDAR_UNVERIFIED')
        if not m.get('connected'):
            codes.append('FEED_DISCONNECTED')
        if not m.get('normal'):
            codes.append('MARKET_NOT_NORMAL')
        if any(not obj.fresh(symbol, now) for symbol in cfg.instruments):
            codes.append('QUOTE_STALE')
    if stale:
        codes.append('POSITION_VALUATION_STALE')
    if any(t.get('exit_intent') and now - t['exit_intent']['at'] > cfg.exit_response_timeout for t in open_trades):
        codes.append('PAPER_EXIT_STALLED')
    return dict(mode=cfg.mode, checked_at=now, status='DEGRADED' if codes else 'HEALTHY', codes=codes,
                implementation_sha256=state['implementation_sha256'], release=state['release'],
                config_hash=state['config_hash'], initial_cash=cfg.initial_cash,
                available_cash=str(obj.available()), equity_estimate=str(settled + unrealized),
                reserved_entry_notional=str(sum((D(t['entry_price']) * t['remaining'] for t in open_trades), D(0))),
                gross_realized=state['realized_gross'], estimated_fees=state['fees'],
                unrealized_net_estimate=str(unrealized), open_positions=len(open_trades), stale_marks=stale,
                sessions=state['sessions'], completed=state['completed'], halt=state['halt'],
                trades=list(state['trades'].values()), slots=list(state['slots'].values()),
                feed_fresh_seconds=state['feed_fresh_seconds'], reconnects=state['reconnects'],
                live_quote_updates=state['live_quote_updates'], market=state['market'],
                quote_ages={s: now - q['at'] for s, q in state['quotes'].items()},
                rejected_quotes=state['rejected_quotes'])
