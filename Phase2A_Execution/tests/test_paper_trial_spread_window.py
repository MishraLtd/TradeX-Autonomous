"""Bounded spread resumption, preserved accounting and rejection evidence."""
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from execution.paper_trial.config import TrialConfig, implementation_fingerprint, stamp
from execution.paper_trial.ledger import Ledger, read_state
from execution.paper_trial.report import report, write_private
from execution.paper_trial.spread import quote_pair
from execution.paper_trial.upgrade import upgrade
from test_paper_trial import market
from test_paper_trial_spread import portfolio, pair, quote


class WindowTest(unittest.TestCase):
    def test_window_waits_skips_closed_day_fills_and_stops_without_trend_fallback(self):
        cfg = replace(portfolio(), spread_window=('2026-11-24', '2026-11-27'))
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'trial.sqlite'
            book = Ledger(path, cfg)
            try:
                for day, verified in (('2026-11-23', True), ('2026-11-24', False)):
                    at = stamp(day, '10:15')
                    book.step(at, dict(market(at), stream_id='a', calendar_valid=verified), pair(at))
                    self.assertFalse(book.pending() or book.active() or book.s['trades'])
                for day in ('2026-11-25', '2026-11-26', '2026-11-27'):
                    at = stamp(day, '10:15')
                    for offset in (0, .3):
                        book.step(at + offset, dict(market(at + offset), stream_id='a'), pair(at + offset))
                    self.assertEqual(book.s['spread_session']['matched'], 1)
                    self.assertEqual(book.summary(at + .3)['entry_policy'], 'CROSS_EXCHANGE')
                end = stamp('2026-11-27', cfg.hard_close_time)
                book.step(end, market(end), {})
                self.assertTrue(book.s['completed'])
                book.close()
                book = Ledger(path, cfg)
                for day in ('2026-11-28', '2026-11-30'):
                    at = stamp(day, '10:15')
                    with patch('execution.paper_trial.trend.signal', return_value={'direction': 'LONG', 'strength': '1'}):
                        book.step(at, dict(market(at), stream_id='a'), pair(at))
                    self.assertFalse(book.pending() or book.active())
                    self.assertEqual(len(book.s['trades']), 3)
                    self.assertEqual(book.summary(at)['entry_policy'], 'PAUSED_OUTSIDE_SPREAD_WINDOW')
            finally:
                book.close()

    def test_invalid_windows_and_impossible_latency_fail_closed(self):
        for window in (('2026-11-27', '2026-11-24'), ('2026-11-24',), ('bad', '2026-11-27')):
            with self.assertRaises(ValueError):
                replace(portfolio(), spread_window=window)
        with self.assertRaisesRegex(ValueError, 'latency'):
            replace(portfolio(), spread_window=('2026-11-24', '2026-11-27'), latency_seconds=2)

    def test_upgrade_keeps_trades_fees_cash_and_backup(self):
        old = portfolio()
        with TemporaryDirectory() as directory:
            base = Path(directory)
            path, config = base / 'trial.sqlite', base / 'config.json'
            with_book = Ledger(path, old)
            at = stamp('2026-11-20', '10:15')
            for offset in (0, .3):
                with_book.step(at + offset, dict(market(at + offset), stream_id='a'), pair(at + offset))
            with_book.close()
            before = read_state(path)
            old_shape = asdict(old)
            old_shape.pop('spread_window')
            write_private(config, old_shape)
            args = config, path, base / 'backup', implementation_fingerprint(), 'b' * 40
            with patch('execution.paper_trial.upstox.load_instruments', return_value=[]), \
                 patch('execution.paper_trial.upstox.resolve_instruments', return_value=old.instruments), \
                 patch('execution.paper_trial.upstox.bse_listings', return_value=old.bse_instruments):
                receipt = upgrade(*args, now=stamp('2026-11-23', '16:00'), cross_exchange_week=True,
                                  spread_start='2026-11-24', spread_end='2026-11-27', check=True)
                self.assertFalse((base / 'backup').exists())
                self.assertEqual(read_state(path), before)
                upgrade(*args, now=stamp('2026-11-23', '16:00'), cross_exchange_week=True,
                        spread_start='2026-11-24', spread_end='2026-11-27')
            after = read_state(path)
            for field in ('trades', 'fees', 'realized_gross', 'slots', 'sessions'):
                self.assertEqual(after[field], before[field])
            self.assertEqual(receipt['capital_adjustment'], '0')
            self.assertEqual(read_state(base / 'backup/trial.sqlite'), before)
            loaded = TrialConfig.load(config)
            self.assertEqual(after['config_hash'], loaded.fingerprint)
            self.assertEqual(loaded.spread_dates, ['2026-11-24', '2026-11-27'])

    def test_rejection_and_expiry_include_actual_diagnostics(self):
        cfg = portfolio()
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'trial.sqlite'
            book = Ledger(path, cfg)
            at = stamp('2026-11-20', '10:15')
            try:
                book.step(at, dict(market(at), stream_id='a'), pair(at))
                book.step(at + .3, dict(market(at + .3), stream_id='a'), pair(at + .3, 102, 102))
                slot = next(iter(book.s['slots'].values()))
                self.assertEqual(slot['diagnostics']['detail'], 'NO_RAW_SPREAD')
                self.assertFalse(book.s['trades'])
                at += 3
                book.step(at, dict(market(at), stream_id='a'), pair(at))
                book.step(at + 1.5, dict(market(at + 1.5), stream_id='a'), {})
                book.step(at + 2.01, dict(market(at + 2.01), stream_id='a'), {})
                slot = list(book.s['slots'].values())[-1]
                self.assertEqual(slot['reason'], 'PAIR_EXPIRED')
                self.assertEqual(slot['diagnostics']['detail'], 'BOOK_STALE')
                events = [e for e in report(path, at + 2.01)['events'] if e['kind'] == 'ENTRY_SKIPPED']
                self.assertEqual(events[-1]['diagnostics'], slot['diagnostics'])
            finally:
                book.close()

    def test_current_health_excludes_old_bse_and_names_stale_nse(self):
        with TemporaryDirectory() as directory:
            book = Ledger(Path(directory) / 'trial.sqlite', portfolio())
            at = stamp('2026-11-23', '10:15')
            try:
                book.step(at, market(at), {'SBIN': quote(at, 100)})
                result = book.summary(at)
                self.assertIn('QUOTE_STALE', result['codes'])
                self.assertNotIn('SBIN', result['stale_quotes'])
                self.assertFalse(any(s.startswith('BSE:') for s in result['stale_quotes']))
                self.assertEqual(result['stale_quotes']['ITC']['reason'], 'MISSING_QUOTE')
            finally:
                book.close()
