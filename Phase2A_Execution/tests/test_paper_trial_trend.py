"""Live-book replay and recovery timing contracts; no broker orders."""
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from execution.paper_trial.config import CURRENT_FEES, D, stamp, TrialConfig
from execution.paper_trial.ledger import Ledger, read_state
from execution.paper_trial.report import write_private
from execution.paper_trial.trend import SETTINGS, signal
from execution.paper_trial.upgrade import upgrade
from test_paper_trial import config, quotes, market, DATES


class TrendTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'trial.sqlite'
        self.cfg = config(entry_policy='TREND_ROTATION', entry_times=(), exit_policy='NET_RECOVERY',
                          trend_settings=dict(SETTINGS), net_profit_inr='5', fee_model=CURRENT_FEES,
                          recycle_entries=True)
        self.book = Ledger(self.path, self.cfg)
        self.at = stamp(DATES[0], '09:30')

    def tearDown(self):
        self.book.close()
        self.tmp.cleanup()

    def step(self, at, price=103, live=True, status=None):
        self.book.step(at, status or market(at), quotes(at, {'SBIN': price}, live=live))

    def enter(self, descending=False):
        for i in range(61):
            p = D(103) - D(i) / 20 if descending else D(100) + D(i) / 20
            self.step(self.at - 300 + i * 5, p)
        self.assertEqual(len(self.book.pending()), 1)
        self.step(self.at + .3, 100 if descending else 103)
        return self.book.active()[0]

    def recovery(self, trade, at, net, fresh=True):
        with self.book.atomic(), patch.object(self.book, 'fresh', return_value=fresh), \
                patch.object(self.book, 'net_liquidation', return_value=D(net)):
            self.book._recovery_exit(trade, at)

    def test_first_entry_delayed_and_direction_tracks_up_or_down(self):
        trade = self.enter()
        self.assertGreaterEqual(trade['entry_at'], self.at)
        self.assertEqual(trade['direction'], 'LONG')
        self.assertEqual(trade['entry_trend']['direction'], 'LONG')
        self.assertIsNone(trade['stop'])
        self.assertIsNone(trade['target'])

    def test_falling_stock_enters_short_even_when_old_formula_would_buy(self):
        self.assertEqual(self.enter(descending=True)['direction'], 'SHORT')

    def test_cooldown_then_reversal_with_new_trend(self):
        trade = self.enter()
        self.step(self.at + 1, 104)
        self.step(self.at + 1.3, 104)
        self.assertEqual(trade['reason'], 'NET_PROFIT_TAKE')
        close = trade['closed_at']
        self.assertFalse(self.book.pending())
        for i in range(1, 49):
            self.step(close + i * 5, D(104) - D(i) / 10)
            if i < 48:
                self.assertFalse(self.book.pending())
        self.assertEqual(self.book.pending()[0]['direction'], 'SHORT')
        self.assertEqual(self.book.pending()[0]['symbol'], 'SBIN')

    def test_short_can_reverse_long_after_cooldown(self):
        trade = self.enter(descending=True)
        self.step(self.at + 1, 99)
        self.step(self.at + 1.3, 99)
        self.assertEqual(trade['reason'], 'NET_PROFIT_TAKE')
        close = trade['closed_at']
        self.book.close()
        self.book = Ledger(self.path, self.cfg)
        for i in range(1, 49):
            self.step(close + i * 5, D(99) + D(i) / 10)
            if i < 48:
                self.assertFalse(self.book.pending())
        self.assertEqual(self.book.pending()[0]['direction'], 'LONG')

    def test_actual_cost_inclusive_loss_starts_wait_without_price_stop(self):
        trade = self.enter()
        self.step(self.at + 1, 102.8)
        self.assertLess(self.book.net_liquidation(trade), -5)
        self.assertIsNotNone(trade.get('recovery_at'))
        self.assertIsNone(trade['exit_intent'])
        self.assertGreater(trade['remaining'], 0)

    def test_first_draw_ranks_fastest_confirmed_move(self):
        for i in range(61):
            at = self.at - 300 + i * 5
            self.book.step(at, market(at), quotes(at, {'SBIN': D(100) + D(i) / 20,
                                                     'ITC': D(100) - D(i) / 10}))
        self.assertEqual(self.book.pending()[0]['symbol'], 'ITC')
        self.assertEqual(self.book.pending()[0]['direction'], 'SHORT')

    def test_signal_rejects_discontinuous_history(self):
        for i in range(61):
            if 20 <= i <= 30:
                continue
            self.step(self.at - 300 + i * 5, D(100) + D(i) / 20)
        self.assertFalse(self.book.pending())
        self.assertIsNone(signal(self.book.s, 'SBIN', self.at, SETTINGS))

    def test_profit_override_before_fifteen_minutes_and_exact_boundary(self):
        trade = self.enter()
        self.recovery(trade, self.at + 10, '5')
        self.assertIsNone(trade['exit_intent'])
        self.recovery(trade, self.at + 11, '5.01')
        self.assertEqual(trade['exit_intent']['reason'], 'NET_PROFIT_TAKE')

    def test_loss_trigger_waits_one_hour_then_near_zero_exit(self):
        trade = self.enter()
        self.recovery(trade, self.at + 10, '-5')
        self.assertIsNone(trade.get('recovery_at'))
        self.recovery(trade, self.at + 11, '-5.01')
        start = trade['recovery_at']
        self.assertIsNone(trade['exit_intent'])
        self.recovery(trade, start + 3599, '0')
        self.assertIsNone(trade['exit_intent'])
        self.recovery(trade, start + 3600, '-0.51')
        self.assertIsNone(trade['exit_intent'])
        self.recovery(trade, start + 3601, '-0.50')
        self.assertEqual(trade['exit_intent']['reason'], 'RECOVERY_NEAR_ZERO')

    def test_loss_wait_does_not_reset_and_timeout_requests_exit_when_feed_stale(self):
        trade = self.enter()
        self.recovery(trade, self.at + 10, '-6')
        start = trade['recovery_at']
        self.recovery(trade, start + 100, '-1')
        self.recovery(trade, start + 200, '-10')
        self.assertEqual(trade['recovery_at'], start)
        self.recovery(trade, start + 7199, '-10')
        self.assertIsNone(trade['exit_intent'])
        self.recovery(trade, start + 7200, '-10', fresh=False)
        self.assertEqual(trade['exit_intent']['reason'], 'RECOVERY_TIMEOUT')
        self.assertEqual(trade['remaining'], trade['quantity'])  # no fabricated stale fill

    def test_profit_can_exit_during_first_recovery_hour(self):
        trade = self.enter()
        self.recovery(trade, self.at + 10, '-6')
        self.recovery(trade, self.at + 20, '5.01')
        self.assertEqual(trade['exit_intent']['reason'], 'NET_PROFIT_TAKE')

    def test_restart_preserves_recovery_start_and_history(self):
        trade = self.enter()
        self.recovery(trade, self.at + 10, '-6')
        before = read_state(self.path)
        self.book.close()
        self.book = Ledger(self.path, self.cfg)
        self.assertEqual(self.book.s['trend_history'], before['trend_history'])
        trade = self.book.active()[0]
        self.recovery(trade, self.at + 7210, '-6')
        self.assertEqual(trade['exit_intent']['reason'], 'RECOVERY_TIMEOUT')

    def test_minimum_hold_no_exit_without_profit_or_recovery(self):
        trade = self.enter()
        for seconds in (10, 899, 900, 1800):
            self.recovery(trade, self.at + seconds, '-4')
            self.assertIsNone(trade['exit_intent'])

    def test_daily_risk_and_squareoff_override_wait(self):
        trade = self.enter()
        with self.book.atomic(), patch.object(self.book, 'net_liquidation', return_value=D(-101)):
            self.book._risk_and_exits(self.at + 1, DATES[0])
        self.assertEqual(trade['exit_intent']['reason'], 'RISK_HALT')
        with self.book.atomic():
            trade['exit_intent'] = None
            self.book._risk_and_exits(stamp(DATES[0], '15:00'), DATES[0])
        self.assertEqual(trade['exit_intent']['reason'], 'INTRADAY_SQUARE_OFF')

    def test_stale_snapshots_gaps_and_flat_books_cannot_supply_signal(self):
        for i in range(61):
            self.step(self.at - 300 + i * 5, 103, live=False)
        self.assertFalse(self.book.pending())
        self.assertFalse(self.book.s.get('trend_history'))
        self.step(self.at + 1)
        self.assertIsNone(signal(self.book.s, 'SBIN', self.at + 1, SETTINGS))
        for i in range(1, 62):
            self.step(self.at + 1 + i * 5, 103)
        self.assertFalse(self.book.pending())
        self.assertIsNone(signal(self.book.s, 'SBIN', self.at + 500, SETTINGS))

    def test_pending_entry_cancelled_if_trend_disappears(self):
        for i in range(61):
            self.step(self.at - 300 + i * 5, D(100) + D(i) / 20)
        slot = self.book.pending()[0]
        with patch('execution.paper_trial.trend.signal', return_value=None):
            self.step(self.at + .3)
        self.assertEqual(slot['reason'], 'TREND_NOT_CONFIRMED')
        self.assertFalse(self.book.active())

    def test_no_new_trade_without_fifteen_minutes_before_squareoff(self):
        self.at = stamp(DATES[0], '14:45')
        for i in range(61):
            self.step(self.at - 300 + i * 5, D(100) + D(i) / 20)
        self.step(self.at + 1)
        self.assertFalse(self.book.pending())
        self.assertIsNotNone(signal(self.book.s, 'SBIN', self.at + 1, SETTINGS))

    def test_pending_entry_cannot_fill_past_latest_entry_time(self):
        self.at = stamp(DATES[0], '14:45') - 1
        for i in range(61):
            self.step(self.at - 300 + i * 5, D(100) + D(i) / 20)
        self.assertEqual(len(self.book.pending()), 1)
        self.step(self.at + 1)
        self.assertFalse(self.book.active())
        self.assertFalse(self.book.pending())

    def test_migration_preserves_cash_history_and_daily_pause(self):
        self.book.close()
        old = config(entry_policy='RANDOM_ROTATION', entry_times=(), exit_policy='NET_PROFIT_PRICE_STOP',
                     fee_model=CURRENT_FEES, recycle_entries=True)
        path = Path(self.tmp.name) / 'old.sqlite'
        book = Ledger(path, old)
        with book.atomic():
            book.s['day_loss'][DATES[0]] = True
            book.s['fees'] = '69.71'
            book.s['realized_gross'] = '-29.40'
            book.s['implementation_sha256'] = 'a' * 64
            book.s['rotation'] = dict(day=DATES[0], symbol='SBIN')
        before = read_state(path)
        book.close()
        cfgpath = Path(self.tmp.name) / 'config.json'
        write_private(cfgpath, asdict(old))
        from execution.paper_trial.upstox import APPROVED_SYMBOLS
        resolved = dict(old.instruments)
        keys = ['INE338F01015','INE423A01024','INE296A01032','INE982J01020','INE095A01012']
        for i, (s, k) in enumerate(zip(APPROVED_SYMBOLS[3:], keys), 3):
            resolved[s] = dict(key='NSE_EQ|' + k, tick='0.05', lot_size=1, mis_eligible=True, slot=i)
        with patch('execution.paper_trial.upstox.expanded_instruments', return_value=resolved):
            result = upgrade(cfgpath, path, Path(self.tmp.name) / 'backup', 'a' * 64, 'b' * 40,
                             now=stamp(DATES[0], '16:00'), trend_recovery=True)
        self.assertEqual(result['cash_preserved'], '3900.89')
        after = read_state(path)
        for key in ('day_loss', 'fees', 'realized_gross', 'trades', 'slots', 'sessions'):
            self.assertEqual(after[key], before[key])
        self.assertEqual(after['rotation'], {})
        self.assertEqual(TrialConfig.load(cfgpath).net_profit_inr, '5')
        with patch('execution.paper_trial.upstox.expanded_instruments', return_value=resolved):
            result = upgrade(cfgpath, path, Path(self.tmp.name) / 'portfolio-backup',
                             after['implementation_sha256'], 'c' * 40,
                             now=stamp(DATES[0], '16:01'), profitup=True)
        self.assertEqual(result['capital_adjustment'], '6000')
        self.assertEqual(result['cash_after'], '9900.89')
        portfolio = read_state(path)
        self.assertEqual(portfolio['day_loss'], before['day_loss'])
        self.assertEqual(portfolio['fees'], before['fees'])
        self.assertEqual(portfolio['config']['entry_policy'], 'TREND_PORTFOLIO')
        self.book = Ledger(self.path, self.cfg)
