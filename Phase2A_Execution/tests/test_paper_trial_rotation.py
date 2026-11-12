"""Rotation replay, restart, eligibility and cash-preserving migration checks."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from execution.paper_trial.config import CURRENT_FEES, D, stamp
from execution.paper_trial.ledger import Ledger, read_state
from execution.paper_trial.report import report
from test_paper_trial import config, quotes, market, DATES


class RotationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'trial.sqlite'
        self.cfg = config(entry_policy='RANDOM_ROTATION', entry_times=(),
                          exit_policy='NET_PROFIT_PRICE_STOP', fee_model=CURRENT_FEES,
                          recycle_entries=True)
        self.book = Ledger(self.path, self.cfg)
        self.at = stamp(DATES[0], '10:07')
        self.draw = patch('execution.paper_trial.ledger.secrets.choice',
                          side_effect=lambda ss: 'SBIN' if 'SBIN' in ss else ss[0]).start()
        self.addCleanup(patch.stopall)

    def tearDown(self):
        self.book.close()
        self.temp.cleanup()

    def step(self, seconds=0, price=100, quantity=50, live=True, status=None):
        self.at += seconds
        self.book.step(self.at, status or market(self.at),
                       quotes(self.at, {'SBIN': price}, live=live, quantity=quantity))

    def enter(self):
        if not self.book.pending():
            self.step(.3)
        self.step(.3)
        return self.book.active()[0]

    def close_trade(self, price):
        trade = self.book.active()[0]
        self.step(.3, price)
        self.step(.3, price)
        self.assertEqual(trade['state'], 'CLOSED')
        return trade

    def events(self, kind):
        return [e for e in report(self.path, self.at)['events'] if e['kind'] == kind]

    def test_arbitrary_time_uniform_choice_and_single_position(self):
        self.step()
        self.draw.assert_called_once_with(['HDFCBANK', 'ITC', 'SBIN'])
        self.assertEqual(self.events('ROTATION_SELECT')[0]['probability_each'], '1/3')
        self.assertEqual(len(self.book.pending()), 1)
        self.assertEqual(D(self.book.pending()[0]['budget']), 2000)
        self.step(.3)
        self.step(.3)
        self.assertEqual(len(self.book.active()), 1)
        self.assertFalse(self.book.pending())
        trade = self.book.active()[0]
        self.assertEqual(trade['entry_policy'], 'RANDOM_ROTATION')
        self.assertEqual(trade['direction'], 'LONG')
        self.assertEqual(self.events('SCHEDULE_SLOT'), [])
        self.assertLessEqual(D(trade['entry_price']) * trade['quantity'] + D(trade['entry_fees']), 2000)

    def test_three_small_net_wins_rotate_and_exclude_previous_stock(self):
        for i in range(3):
            self.enter()
            trade = self.close_trade(101)
            self.assertGreater(D(trade['gross_pnl']) - D(trade['entry_fees']) - D(trade['exit_fees']), 1)
            if i < 2:
                self.assertEqual(self.book.pending()[0]['symbol'], 'SBIN')
                self.assertEqual(self.draw.call_count, 1)
        leave = self.events('ROTATION_LEAVE')[0]
        self.assertEqual(leave['reason'], 'LAST_THREE_NET_AT_MOST_10')
        self.assertLessEqual(sum(map(D, leave['last_three'])), 10)
        self.assertNotEqual(self.book.pending()[0]['symbol'], 'SBIN')
        self.assertNotIn('SBIN', self.draw.call_args.args[0])
        self.assertEqual(self.events('ROTATION_SELECT')[-1]['probability_each'], '1/2')

    def test_three_losses_and_partial_exit_count_once_on_complete(self):
        self.enter()
        self.step(.3, 99.5)
        self.step(.3, 99.5, quantity=10)
        self.assertEqual(self.book.active()[0]['remaining'], 4)
        self.assertEqual(self.events('ROTATION_RESULT'), [])
        self.assertFalse(self.book.pending())
        self.step(.3, 99.5)
        self.assertEqual(len(self.events('ROTATION_RESULT')), 1)
        for _ in range(2):
            self.enter()
            self.close_trade(99.5)
        self.assertEqual(self.events('ROTATION_LEAVE')[0]['reason'], 'THREE_NET_LOSSES')
        self.assertEqual(len(self.events('ROTATION_RESULT')), 3)

    def test_rolling_window_keeps_profitable_stock_then_rotates(self):
        for _ in range(3):
            self.enter()
            self.close_trade(104)
        self.assertFalse(self.events('ROTATION_LEAVE'))
        for _ in range(2):
            self.enter()
            self.close_trade(101)
        self.assertFalse(self.events('ROTATION_LEAVE'))
        self.enter()
        self.close_trade(101)
        self.assertEqual(len(self.events('ROTATION_LEAVE')), 1)
        self.assertEqual(len(self.book.s['trades']), 6)  # no daily count cap

    def test_restart_retains_selection_window_and_pending_order(self):
        self.enter()
        self.close_trade(101)
        previous = read_state(self.path)['rotation']
        self.book.close()
        self.book = Ledger(self.path, self.cfg)
        self.assertEqual(self.book.s['rotation'], previous)
        self.step(.3, live=False)
        self.assertFalse(self.book.active())
        self.step(.3)
        self.assertEqual(len(self.book.active()), 1)
        self.assertEqual(len(self.events('ROTATION_RESULT')), 1)
        self.assertEqual(self.draw.call_count, 1)

    def test_no_entries_when_market_invalid_stale_or_unaffordable(self):
        for flag in ('normal', 'connected', 'calendar_valid'):
            m = market(self.at); m[flag] = False
            self.step(.3, status=m)
        self.step(.3, live=False)
        self.assertEqual(self.draw.call_count, 0)
        self.at += 20
        self.book.step(self.at, market(self.at), quotes(self.at - 20))
        self.assertEqual(self.draw.call_count, 0)
        self.at += 1
        expensive = quotes(self.at, {s: 3000 for s in self.cfg.instruments})
        self.book.step(self.at, market(self.at), expensive)
        self.assertEqual(self.draw.call_count, 0)

    def test_no_reentry_after_cutoff_but_final_close_is_counted(self):
        self.enter()
        self.at = stamp(DATES[0], '15:00')
        self.step()
        self.step(.3)
        self.assertFalse(self.book.active())
        self.assertFalse(self.book.pending())
        self.assertEqual(len(self.events('ROTATION_RESULT')), 1)
        self.assertEqual(self.draw.call_count, 1)
        self.assertEqual(report(self.path, self.at)['entry_policy'], 'RANDOM_ROTATION')

    def test_daily_loss_pause_blocks_reentry(self):
        self.enter()
        self.step(.3, 70)
        self.step(.3, 70)
        self.assertTrue(self.book.s['day_loss'][DATES[0]])
        self.assertFalse(self.book.pending())
        self.assertEqual(len(self.events('ROTATION_RESULT')), 1)

    def test_next_session_resets_rotation_and_preserves_direction_formula(self):
        self.enter()
        self.at = stamp(DATES[0], '15:00')
        self.step()
        self.step(.3)
        self.at = stamp(DATES[1], '09:15')
        self.step()
        self.step(.3)
        self.assertEqual(self.book.active()[0]['direction'], 'SHORT')
        self.assertEqual(self.book.s['rotation']['window'], [])
        self.assertEqual(len(self.book.s['trades']), 2)

    def test_rotation_requires_empty_times_and_reviewed_exit_policy(self):
        with self.assertRaises(ValueError):
            replace(self.cfg, entry_times=('09:30', '10:30', '11:30'))
        with self.assertRaises(ValueError):
            replace(self.cfg, exit_policy='NET_CASH')

    def test_new_trial_preparation_uses_rotation_and_skips_late_start(self):
        from execution.paper_trial.upstox import prepare_config
        with patch('execution.paper_trial.upstox.load_instruments', return_value=[]), \
                patch('execution.paper_trial.upstox.resolve_instruments', return_value=self.cfg.instruments), \
                patch('execution.paper_trial.upstox.regular_session',
                      side_effect=lambda client, day: day in DATES + ['2026-11-18']):
            cfg = prepare_config(Mock(), now=stamp(DATES[0], '09:10'))
        self.assertEqual(cfg.entry_policy, 'TREND_ROTATION')
        self.assertFalse(cfg.entry_times)
        self.assertEqual(cfg.sessions, DATES[1:] + ['2026-11-18'])

    def test_exact_ten_net_rotates_and_zero_resets_loss_streak(self):
        # Replay a persisted close with controlled accounting at the boundary.
        self.enter()
        trade = self.close_trade(101)
        with self.book.atomic():
            self.book.s['slots'] = {trade['id']: self.book.s['slots'][trade['id']]}
            prior_gross = D(trade['gross_pnl'])
            trade['gross_pnl'] = str(D(trade['entry_fees']) + D(trade['exit_fees']))
            self.book.s['realized_gross'] = str(D(self.book.s['realized_gross']) + D(trade['gross_pnl']) - prior_gross)
            self.book.s['rotation'].update(head=trade['id'], processed=None, window=['5', '5'], losses=2)
        self.step(.3)
        self.assertEqual(self.events('ROTATION_RESULT')[-1]['consecutive_losses'], 0)
        self.assertEqual(self.events('ROTATION_LEAVE')[-1]['last_three'], ['5', '5', '0.00'])
        self.assertEqual(self.events('ROTATION_LEAVE')[-1]['reason'], 'LAST_THREE_NET_AT_MOST_10')

    def test_just_over_ten_net_keeps_stock(self):
        self.enter()
        trade = self.close_trade(101)
        net = D(trade['gross_pnl']) - D(trade['entry_fees']) - D(trade['exit_fees'])
        with self.book.atomic():
            self.book.s['slots'] = {trade['id']: self.book.s['slots'][trade['id']]}
            self.book.s['rotation'].update(head=trade['id'], processed=None,
                                           window=['5', str(D('5.01') - net)], losses=0)
        self.step(.3)
        self.assertFalse(self.events('ROTATION_LEAVE'))
        self.assertEqual(self.book.pending()[0]['symbol'], 'SBIN')

    def test_no_alternative_waits_without_redrawing_previous_stock(self):
        for _ in range(2):
            self.enter()
            self.close_trade(101)
        self.enter()
        self.step(.3, 101)
        self.at += .3
        q = quotes(self.at, {'SBIN': 101, 'ITC': 3000, 'HDFCBANK': 3000}, quantity=50)
        self.book.step(self.at, market(self.at), q)
        self.assertEqual(len(self.events('ROTATION_LEAVE')), 1)
        self.assertFalse(self.book.pending())
        self.assertIsNone(self.book.s['rotation']['symbol'])
        self.step(.3)
        self.assertNotEqual(self.book.pending()[0]['symbol'], 'SBIN')


if __name__ == '__main__':
    unittest.main()
