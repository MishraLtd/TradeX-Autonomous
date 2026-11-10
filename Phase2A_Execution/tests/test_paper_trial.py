"""Deterministic market-replay and recovery tests. No broker credentials/orders."""
from dataclasses import replace, asdict
from datetime import datetime
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from execution.paper_trial.config import TrialConfig, D, IST, stamp
from execution.paper_trial.costs import fee, target
from execution.paper_trial.ledger import Ledger, read_state, summarize
from execution.paper_trial.report import report, daily_report, snapshot, restore, write_private
from execution.paper_trial.upstox import DataClient, DataError, Decoder, resolve_instruments, regular_session

DATES = ['2026-11-11', '2026-11-12', '2026-11-13', '2026-11-16', '2026-11-17']
KEYS = {'SBIN': 'NSE_EQ|INE062A01020', 'ITC': 'NSE_EQ|INE154A01025', 'HDFCBANK': 'NSE_EQ|INE040A01034'}


def config(**kwargs):
    return TrialConfig(instruments={s: dict(key=k, tick='0.05', lot_size=1, mis_eligible=True, slot=i)
                                   for i, (s, k) in enumerate(KEYS.items())}, sessions=DATES, **kwargs)


def quotes(at, prices=None, live=True, quantity=1000):
    return {s: dict(at=at, received=at, traded=at, bid=str((prices or {}).get(s, 100)),
                   ask=str(D((prices or {}).get(s, 100)) + D('.05')), last=str((prices or {}).get(s, 100)),
                   bid_qty=quantity, ask_qty=quantity, live=live) for s in KEYS}


def market(at, **kwargs):
    return dict(day=datetime.fromtimestamp(at, IST).date().isoformat(), calendar_valid=True,
                normal=True, connected=True, **kwargs)


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'trial.sqlite'
        self.cfg = config()
        self.book = Ledger(self.path, self.cfg)
        self.day = DATES[0]

    def tearDown(self):
        self.book.close()
        self.temp.cleanup()

    def step(self, at, prices=None, live=True, quantity=1000, status=None):
        self.book.step(at, status or market(at), quotes(at, prices, live, quantity))

    def enter(self, index=0, day=None, quantity=1000):
        at = stamp(day or self.day, self.cfg.entry_times[index])
        self.step(at)
        self.step(at + .3, quantity=quantity)
        return self.book.s['trades'][(day or self.day) + ':' + str(index)], at + .3

    def test_budget_positions_and_no_short_sale_buying_power(self):
        for index in range(3):
            self.enter(index)
        self.assertEqual(len(self.book.active()), 3)
        self.assertEqual([t['direction'] for t in self.book.active()], ['LONG', 'SHORT', 'LONG'])
        used = sum(D(t['entry_price']) * t['quantity'] + D(t['entry_fees']) for t in self.book.active())
        self.assertLessEqual(used, D(4000))
        self.assertTrue(all(D(t['budget']) <= 2000 for t in self.book.active()))
        self.assertLess(self.book.available(), 100)

    def test_long_target_round_trip_and_explicit_costs(self):
        trade, at = self.enter()
        self.step(at + 1, {'SBIN': 103})
        self.assertEqual(trade['remaining'], trade['quantity'])  # next quote + latency required
        self.step(at + 1.3, {'SBIN': 103})
        self.assertEqual(trade['state'], 'CLOSED')
        self.assertEqual(trade['reason'], 'PROFIT_TARGET')
        result = report(self.path, at + 1.3)
        self.assertEqual(result['closed_trades'], 1)
        self.assertGreater(D(result['realized_net_closed']), 0)
        self.assertEqual(len(result['fills']), 2)
        self.assertGreater(D(result['slippage_cost_estimate']), 0)

    def test_sell_first_buy_back_profit_and_loss_accounting(self):
        trade, at = self.enter(day=DATES[1])
        self.assertEqual(trade['direction'], 'SHORT')
        self.assertLess(self.book.available(), D(self.cfg.initial_cash))
        self.step(at + 1, {'SBIN': 97})
        self.step(at + 1.3, {'SBIN': 97})
        self.assertEqual(trade['state'], 'CLOSED')
        self.assertEqual(trade['reason'], 'PROFIT_TARGET')
        self.assertGreater(D(trade['gross_pnl']), 0)
        self.assertGreater(self.book.available(), 4000)

    def test_short_stop_gap_fills_current_ask_not_stop(self):
        trade, at = self.enter(day=DATES[1])
        self.step(at + 1, {'SBIN': 102})
        self.step(at + 1.3, {'SBIN': 104})
        self.assertEqual(trade['reason'], 'STOP_LOSS')
        self.assertLess(D(trade['gross_pnl']), 0)
        exit_price = D(trade['exit_turnover']) / trade['quantity']
        self.assertGreater(exit_price, D(trade['stop']))

    def test_cash_is_rechecked_after_pending_intent_and_market_loss(self):
        self.book.close()
        self.cfg = config(daily_loss_limit='1000')
        self.path = Path(self.temp.name) / 'loss.sqlite'
        self.book = Ledger(self.path, self.cfg)
        self.enter(0)
        self.enter(1)
        at = stamp(self.day, '11:30')
        self.step(at)
        self.step(at + .3, {'SBIN': 60})
        settled = D(4000) + D(self.book.s['realized_gross']) - D(self.book.s['fees'])
        used = sum(D(t['entry_price']) * t['remaining'] for t in self.book.active())
        loss = sum(min(D(0), self.book.unrealized(t)) for t in self.book.active())
        self.assertLessEqual(used, settled + loss)

    def test_partial_ioc_and_exit_depth_consumed_once(self):
        trade, at = self.enter(quantity=50)
        self.assertEqual(trade['quantity'], 5)
        self.assertEqual(self.book.s['slots'][trade['id']]['state'], 'PARTIAL_IOC')
        self.step(at + 1, {'SBIN': 103}, quantity=10)
        self.step(at + 1.3, {'SBIN': 103}, quantity=10)
        self.assertEqual(trade['remaining'], 4)
        self.step(at + 1.3, {'SBIN': 103}, quantity=10000)  # same timestamp can't replenish liquidity
        self.assertEqual(trade['remaining'], 4)
        self.step(at + 1.6, {'SBIN': 103}, quantity=40)
        self.assertEqual(trade['remaining'], 0)
        self.assertEqual(D(trade['exit_fees']), fee('SELL', trade['exit_turnover']))

    def test_stale_feed_closed_market_and_unverified_calendar_do_not_fill(self):
        at = stamp(self.day, '09:30')
        for key in ('normal', 'connected', 'calendar_valid'):
            m = market(at); m[key] = False
            self.step(at, status=m)
            self.assertEqual(len(self.book.active()), 0)
            self.assertEqual(len(self.book.pending()), 0)
        self.book.step(at + 20, market(at + 20), quotes(at))
        self.assertFalse(self.book.pending())
        self.assertGreater(self.book.s['rejected_quotes'], 0)

    def test_missing_slot_never_replayed(self):
        at = stamp(self.day, '09:31') + 1
        self.step(at)
        self.step(at + 1)
        self.assertEqual(len(self.book.active()), 0)
        self.assertEqual(self.book.s['slots'][self.day + ':0']['reason'], 'MISSED_ENTRY_WINDOW')

    def test_restart_durable_intent_and_snapshot_never_fill(self):
        at = stamp(self.day, '09:30')
        self.step(at)
        self.book.close()
        self.book = Ledger(self.path, self.cfg)
        self.step(at + 1, live=False)
        self.assertEqual(len(self.book.active()), 0)
        self.step(at + 1.3)
        self.assertEqual(len(self.book.active()), 1)
        self.book.close()
        self.book = Ledger(self.path, self.cfg)
        self.step(at + 2)
        self.assertEqual(len(self.book.active()), 1)
        self.assertEqual(len(report(self.path, at + 2)['fills']), 1)

    def test_single_writer_and_config_freeze(self):
        with self.assertRaises(RuntimeError):
            Ledger(self.path, self.cfg)
        self.book.close()
        with self.assertRaises(ValueError):
            Ledger(self.path, replace(self.cfg, initial_cash='5000'))
        self.book = Ledger(self.path, self.cfg)

    def test_square_off_uses_next_fresh_quote(self):
        trade, _ = self.enter()
        at = stamp(self.day, '15:00')
        self.book.step(at, market(at), {})
        self.assertEqual(trade['exit_intent']['reason'], 'INTRADAY_SQUARE_OFF')
        self.assertEqual(trade['state'], 'OPEN')
        self.step(at + 1, live=False)
        self.assertEqual(trade['state'], 'OPEN')
        self.step(at + 1.3)
        self.assertEqual(trade['state'], 'CLOSED')
        self.step(stamp(self.day, '15:10'))
        self.assertEqual(self.book.s['sessions'][self.day], 'FINISHED')

    def test_failed_square_off_halts_no_overnight_fabricated_fill(self):
        trade, _ = self.enter()
        at = stamp(self.day, '15:10')
        self.book.step(at, market(at), {})
        self.assertEqual(self.book.s['halt'], 'INTRADAY_EXIT_UNRESOLVED')
        self.step(stamp(DATES[1], '09:30'))
        self.step(stamp(DATES[1], '09:30') + 1)
        self.assertEqual(trade['remaining'], trade['quantity'])
        self.assertEqual(len(self.book.s['trades']), 1)
        self.assertIn('INTRADAY_EXIT_UNRESOLVED', self.book.summary(at)['codes'])

    def test_daily_loss_pauses_next_slots_and_requests_exit(self):
        trade, at = self.enter()
        self.step(at + 1, {'SBIN': 90})
        self.step(at + 1.3, {'SBIN': 90})
        self.assertEqual(trade['reason'], 'RISK_HALT')
        self.assertTrue(self.book.s['day_loss'][self.day])
        self.step(stamp(self.day, '10:30'))
        self.assertEqual(self.book.s['slots'][self.day + ':1']['reason'], 'RISK_PAUSED')

    def test_backup_restore_preserves_fills_cash_watermarks_and_halts(self):
        trade, at = self.enter()
        destination = Path(self.temp.name) / 'backup.sqlite'
        snapshot(self.path, destination)
        restored = Path(self.temp.name) / 'restored.sqlite'
        restore(destination, restored, self.cfg)
        restored_state = read_state(restored)
        self.assertEqual(restored_state['trades'], self.book.s['trades'])
        self.assertEqual(restored_state['fees'], self.book.s['fees'])
        self.assertEqual(restored_state['watermarks'], self.book.s['watermarks'])
        self.assertEqual(restored_state['halt'], 'RESTORED_TRIAL_REQUIRES_REVIEW')
        # Restoring the restored artifact must also verify its updated manifest;
        # this catches hashing a main DB before its WAL was checkpointed/closed.
        restore(restored, Path(self.temp.name) / 'second-restore.sqlite', self.cfg)
        with self.assertRaises(FileExistsError):
            restore(destination, restored, self.cfg)
        with destination.open('ab') as handle:
            handle.write(b'bad')
        with self.assertRaises(ValueError):
            restore(destination, Path(self.temp.name) / 'bad.sqlite', self.cfg)

    def test_five_session_replay_and_report(self):
        for day in DATES:
            for index in range(3):
                self.enter(index, day)
            at = stamp(day, '15:00')
            self.step(at)
            self.step(at + 1)
            self.step(stamp(day, '15:10'))
        result = report(self.path, stamp(DATES[-1], '15:10'))
        self.assertEqual(result['closed_trades'], 15)
        self.assertEqual(result['open_positions'], 0)
        self.assertTrue(result['completed'])
        self.assertEqual(len(result['fills']), 30)
        self.assertEqual(set(result['sessions'].values()), {'FINISHED'})
        daily = daily_report(result, DATES[0])
        self.assertEqual(len(daily['trades']), 3)
        self.assertEqual(len(daily['fills']), 6)

    def test_future_nan_and_nonpositive_quotes_rejected(self):
        at = stamp(self.day, '09:30')
        for field, value in [('at', float('nan')), ('at', at + 10), ('bid', '0'), ('last', 'NaN'), ('bid_qty', -1)]:
            q = quotes(at)
            for row in q.values(): row[field] = value
            self.book.step(at, market(at), q)
        self.assertFalse(self.book.pending())
        self.assertFalse(self.book.active())

    def test_unfilled_exit_is_unhealthy_before_end_of_day(self):
        trade, at = self.enter()
        self.step(at + 1, {'SBIN': 103}, quantity=0)
        self.step(at + 62, {'SBIN': 103}, quantity=0)
        self.assertEqual(trade['state'], 'OPEN')
        self.assertIn('PAPER_EXIT_STALLED', self.book.summary(at + 62)['codes'])


class TransportTest(unittest.TestCase):
    def test_no_order_or_arbitrary_url_can_receive_authorization(self):
        class Opener:
            def __init__(self): self.calls = []
            def open(self, request, timeout):
                self.calls.append(request)
                return io.BytesIO(b'{"status":"success","data":[]}')
        opener = Opener()
        client = DataClient('private-test-token-value', opener)
        for path in ('/v2/order/place', '/v2/portfolio/positions', '/v2/order/retrieve-all',
                     'https://other.example/', '/v2/market/timings/2026-11-11?token=x',
                     '/v3/feed/market-data-feed/authorize/../orders'):
            with self.assertRaises(DataError): client._get(path)
        self.assertEqual(opener.calls, [])
        self.assertIsNone(client.timings(DATES[0]))
        self.assertEqual(opener.calls[0].method, 'GET')
        self.assertEqual(opener.calls[0].full_url, 'https://api.upstox.com/v2/market/timings/' + DATES[0])

    def test_requests_transport_keeps_redirects_and_errors_private(self):
        try:
            import requests
        except ImportError:
            self.skipTest('Install paper transport dependencies')
        from unittest.mock import MagicMock
        client = DataClient('private-test-token-value')
        response = MagicMock()
        response.__enter__.return_value = response
        response.status_code = 200
        response.raw.read.return_value = b'{"status":"success","data":[]}'
        with patch.object(requests, 'get', return_value=response) as get:
            self.assertIsNone(client.timings(DATES[0]))
            self.assertFalse(get.call_args.kwargs['allow_redirects'])
            self.assertTrue(get.call_args.kwargs['stream'])
            response.status_code = 302
            with self.assertRaisesRegex(DataError, 'HTTP_REDIRECT_REFUSED'):
                client.timings(DATES[0])
            response.status_code = 401
            with self.assertRaisesRegex(DataError, 'UPSTOX_HTTP_401'):
                client.timings(DATES[0])

    def test_signed_websocket_destination_validated(self):
        client = DataClient('private-test-token-value')
        for url in ('ws://ws.upstox.com/feed', 'wss://upstox.com.evil.example/feed',
                    'wss://user:password@ws.upstox.com/feed', 'wss://ws.upstox.com:444/feed'):
            with patch.object(client, '_get', return_value={'authorized_redirect_uri': url}):
                with self.assertRaises(DataError): client.websocket_url()

    def test_holidays_and_short_sessions_are_not_scheduled(self):
        client = DataClient('private-test-token-value')
        with patch.object(client, 'timings', return_value=None):
            self.assertFalse(regular_session(client, DATES[0]))
        with patch.object(client, 'timings', return_value=(stamp(DATES[0], '14:20'), stamp(DATES[0], '15:30'))):
            self.assertFalse(regular_session(client, DATES[0]))

    def test_instrument_mapping_requires_equity_and_intraday_eligibility(self):
        rows = [dict(segment='NSE_EQ', instrument_type='EQ', security_type='NORMAL',
                     trading_symbol=s, instrument_key=k, tick_size=5, lot_size=1) for s, k in KEYS.items()]
        instruments = resolve_instruments(list(KEYS), rows, rows)
        self.assertEqual(instruments['SBIN']['tick'], '0.05')
        with self.assertRaises(DataError): resolve_instruments(list(KEYS), rows + [rows[0]], rows)
        with self.assertRaises(DataError): resolve_instruments(list(KEYS), rows, [])

    def test_mode_and_timetable_reject_live_and_overnight(self):
        for kwargs in ({'mode': 'live'}, {'flatten_time': '15:30'}, {'per_trade_limit': '5000'},
                       {'max_positions': 4}, {'latency_seconds': 0}, {'profit_pct': 'NaN'}):
            with self.assertRaises(ValueError): config(**kwargs)

    def test_net_target_is_tick_correct_both_directions(self):
        for direction in ('LONG', 'SHORT'):
            p = target(100, 10, direction, '.05', 2)
            sign = 1 if direction == 'LONG' else -1
            opening, closing = ('BUY', 'SELL') if sign == 1 else ('SELL', 'BUY')
            net = lambda price: (price - 100) * 10 * sign - fee(opening, 1000) - fee(closing, price * 10)
            self.assertGreaterEqual(net(p), 20)
            self.assertLess(net(p - sign * D('.05')), 20)


class DecoderTest(unittest.TestCase):
    def message(self, at, price=100):
        return dict(type='live_feed', currentTs=str(int(at * 1000)), feeds={
            KEYS['SBIN']: {'fullFeed': {'marketFF': {'ltpc': {'ltp': price, 'ltt': str(int(at * 1000))},
                'marketLevel': {'bidAskQuote': [{'bidP': price, 'bidQ': '1000', 'askP': price + .05, 'askQ': '1000'}]}}}}})

    def test_initial_snapshot_auction_and_backward_time(self):
        d = Decoder(config().instruments)
        at = stamp(DATES[0], '09:30')
        info = dict(currentTs=str(int(at * 1000)), marketInfo={'segmentStatus': {'NSE_EQ': 'NORMAL_OPEN'}})
        d.normalize(info, at)
        self.assertTrue(d.normal)
        self.assertFalse(d.normalize(self.message(at), at)['SBIN']['live'])
        self.assertTrue(d.normalize(self.message(at + 1), at + 1)['SBIN']['live'])
        with self.assertRaises(DataError): d.normalize(self.message(at), at + 2)
        info['currentTs'] = str(int((at + 2) * 1000))
        info['marketInfo']['casMarketStatus'] = {'NSE_EQ': {'status': 'CTS_CLOSE'}}
        d.normalize(info, at + 2)
        self.assertFalse(d.normal)

    def test_stale_ltt_or_iep_cannot_be_an_executable_quote(self):
        d = Decoder(config().instruments)
        at = stamp(DATES[0], '09:30')
        msg = self.message(at)
        full = msg['feeds'][KEYS['SBIN']]['fullFeed']['marketFF']
        full['ltpc']['ltt'] = str(int((at - 60) * 1000))
        self.assertEqual(d.normalize(msg, at), {})
        full['ltpc']['ltt'] = str(int(at * 1000))
        full['ltpc']['iep'] = 100
        self.assertEqual(d.normalize(msg, at), {})

    def test_official_protobuf_contract(self):
        try:
            from execution.paper_trial.MarketDataFeed_pb2 import FeedResponse
            from google.protobuf.json_format import ParseDict
        except ImportError:
            self.skipTest('Install requirements-upstox-paper.txt for binary contract test')
        at = stamp(DATES[0], '09:30')
        wire = ParseDict(self.message(at), FeedResponse()).SerializeToString()
        result = Decoder(config().instruments).decode(wire, at)
        self.assertEqual(D(result['SBIN']['bid']), 100)
        self.assertFalse(result['SBIN']['live'])


if __name__ == '__main__':
    unittest.main()
