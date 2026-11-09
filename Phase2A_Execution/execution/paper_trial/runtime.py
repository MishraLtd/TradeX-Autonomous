"""Bounded data receiver plus independent scheduler/health loop."""
from datetime import datetime
import json
import os
from pathlib import Path
from queue import Queue, Empty, Full
import signal
import threading
import time

from .config import IST, stamp
from .ledger import Ledger
from .report import report, daily_report, write_private
from .upstox import DataError, Decoder, regular_session, resolve_instruments, load_instruments


class Receiver(threading.Thread):
    def __init__(self, client, config, stop):
        super().__init__(daemon=True, name='upstox-data')
        self.client, self.cfg, self.stop = client, config, stop
        self.messages = Queue(maxsize=128)
        self.guard = threading.Lock()
        self.generation = 0
        self.state = dict(day=None, connected=False, normal=False, calendar_valid=False,
                          reconnects=0, rejected_quotes=0, error=None)

    def update(self, **values):
        with self.guard:
            self.state.update(values)

    def status(self):
        with self.guard:
            return dict(self.state, generation=self.generation)

    def run(self):
        import websocket
        delay = 1
        prepared_day = None
        while not self.stop.is_set():
            now = time.time()
            day = datetime.fromtimestamp(now, IST).date().isoformat()
            self.update(day=day)
            if day not in self.cfg.sessions or not stamp(day, '09:10') <= now < stamp(day, self.cfg.hard_close_time):
                self.update(connected=False, normal=False)
                self.stop.wait(1)
                continue
            ws = None
            try:
                if prepared_day != day:
                    self.update(calendar_valid=False)
                    if not regular_session(self.client, day):
                        raise DataError('PLANNED_SESSION_CHANGED')
                    symbols = sorted(self.cfg.instruments, key=lambda s: self.cfg.instruments[s]['slot'])
                    instruments = resolve_instruments(symbols, load_instruments('NSE.json.gz'), load_instruments('NSE_MIS.json.gz'))
                    if instruments != self.cfg.instruments:
                        raise DataError('INSTRUMENT_METADATA_CHANGED')
                    prepared_day = day
                self.update(calendar_valid=True)
                # No credential header is forwarded to this signed one-use URL.
                ws = websocket.create_connection(self.client.websocket_url(), timeout=5,
                                                  enable_multithread=True, redirect_limit=0)
                ws.settimeout(1)
                decoder = Decoder(self.cfg.instruments)
                with self.guard:
                    self.generation += 1
                    generation = self.generation
                    self.state.update(connected=True, normal=False, error=None)
                ws.send(json.dumps(dict(guid='tradex-paper', method='sub', data=dict(
                    mode='full', instrumentKeys=list(decoder.symbols)))).encode(), opcode=websocket.ABNF.OPCODE_BINARY)
                last_frame = last_ping = connected_at = time.monotonic()
                while not self.stop.is_set() and time.time() < stamp(day, self.cfg.hard_close_time):
                    try:
                        opcode, data = ws.recv_data(control_frame=True)
                    except websocket.WebSocketTimeoutException:
                        if time.monotonic() - last_ping > 15:
                            ws.ping()
                            last_ping = time.monotonic()
                        if time.monotonic() - last_frame > 45:
                            raise DataError('STREAM_TIMEOUT')
                        continue
                    last_frame = time.monotonic()
                    if opcode == websocket.ABNF.OPCODE_CLOSE:
                        raise DataError('STREAM_CLOSED')
                    if opcode != websocket.ABNF.OPCODE_BINARY:
                        continue
                    received = time.time()
                    quotes = decoder.decode(data, received)
                    self.update(normal=decoder.normal)
                    try:
                        self.messages.put_nowait((generation, quotes))
                    except Full:
                        raise DataError('FEED_BACKLOG') from None
                    if time.monotonic() - connected_at > 60:
                        delay = 1
            except Exception as error:
                # DataError strings are controlled constants. Other exceptions
                # may contain signed URLs/tokens and must never be serialized.
                code = str(error) if isinstance(error, DataError) else 'STREAM_CONNECTION_FAILED'
                with self.guard:
                    self.state.update(connected=False, normal=False, error=code)
                    self.state['reconnects'] += 1
                    if code in ('STALE_OR_REVERSED_MARKET_FRAME', 'INVALID_MARKET_FRAME'):
                        self.state['rejected_quotes'] += 1
            finally:
                self.update(connected=False, normal=False)
                if ws is not None:
                    try:
                        ws.close(timeout=1)
                    except Exception:
                        pass
                # Discard queued old-connection frames. Reconnect snapshots
                # rebuild freshness but never replay old liquidity.
                while True:
                    try:
                        self.messages.get_nowait()
                    except Empty:
                        break
            self.stop.wait(delay)
            delay = min(30, delay * 2)


def run(config, db_path, client, report_dir):
    stop = threading.Event()
    old = {s: signal.signal(s, lambda *_: stop.set()) for s in (signal.SIGINT, signal.SIGTERM)}
    ledger = Ledger(db_path, config)
    receiver = Receiver(client, config, stop)
    receiver.start()
    last_report = 0
    base_reconnects = ledger.s['reconnects']
    seen_reconnects = 0
    try:
        while not stop.is_set():
            generation, quotes = None, {}
            try:
                generation, quotes = receiver.messages.get(timeout=1)
            except Empty:
                pass
            now, market = time.time(), receiver.status()
            if generation != market['generation'] or not market['connected']:
                quotes = {}
            receipt = os.environ.get('TRADEX_TRIAL_LEASE_RECEIPT')
            if receipt:
                from ..operations import dispatch_guard
                dispatch_guard(receipt, os.environ['TRADEX_ACCOUNT_ID'], now=now)
            ledger.step(now, market, quotes)
            if market['reconnects'] != seen_reconnects:
                with ledger.atomic():
                    ledger.s['reconnects'] = base_reconnects + market['reconnects']
                    ledger.s['last_error'] = market['error']
                    ledger.event(now, 'DATA_CONNECTION_ERROR', code=market['error'])
                seen_reconnects = market['reconnects']
            if now - last_report >= 60:
                result = report(db_path, now)
                write_private(Path(report_dir) / 'latest.json', result)
                for day in config.sessions:
                    daily_path = Path(report_dir) / (day + '.json')
                    if now >= stamp(day, config.hard_close_time) and not daily_path.exists():
                        # The DB always remains the authoritative audit trail.
                        write_private(daily_path, daily_report(result, day), exclusive=True)
                if ledger.s['completed']:
                    write_private(Path(report_dir) / 'final.json', result)
                last_report = now
    finally:
        stop.set()
        receiver.join(timeout=6)
        ledger.close()
        for s, handler in old.items():
            signal.signal(s, handler)
