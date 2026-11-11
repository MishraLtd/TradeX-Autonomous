"""Net exits, repeated entries, full costs and an audited in-place upgrade."""
from dataclasses import asdict, replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from execution.paper_trial.config import CURRENT_FEES, D, TrialConfig, stamp
from execution.paper_trial.costs import breakdown, fee, fee_delta
from execution.paper_trial.ledger import Ledger, read_state
from execution.paper_trial.report import report, write_private
from execution.paper_trial.upgrade import upgrade
from test_paper_trial import config, DATES, market, quotes


class GreedyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'trial.sqlite'
        self.cfg = config(per_trade_limit='150', exit_policy='NET_CASH', recycle_entries=True,
                          fee_model=CURRENT_FEES)
        self.book = Ledger(self.path, self.cfg)
        self.day = DATES[0]

    def tearDown(self):
        self.book.close()
        self.temp.cleanup()

    def reset(self, **changes):
        self.book.close()
        self.path = self.path.with_name(self.path.stem + '-new.sqlite')
        self.cfg = replace(self.cfg, **changes)
        self.book = Ledger(self.path, self.cfg)

    def step(self, at, price=100, quantity=1000, live=True, status=None):
        self.book.step(at, status or market(at), quotes(at, {'SBIN': price}, live, quantity))

    def enter(self, day=None):
        self.day = day or self.day
        at = stamp(self.day, '09:30')
        self.step(at)
        self.step(at + .3)
        return self.book.s['trades'][self.day + ':0'], at + .3

    def test_profit_above_one_closes_then_reenters_on_later_quote(self):
        trade, at = self.enter()
        self.assertIsNone(trade['target'])
        self.assertIsNone(trade['stop'])
        self.step(at + 1, 102)
        self.assertEqual(trade['exit_intent']['reason'], 'NET_PROFIT_TAKE')
        self.step(at + 1.3, 102)
        self.assertEqual(trade['state'], 'CLOSED')
        self.assertEqual(len(self.book.s['trades']), 1)
        self.assertEqual(len(self.book.pending()), 1)
        self.step(at + 1.3, 102)  # duplicate close quote cannot re-enter
        self.assertEqual(len(self.book.s['trades']), 1)
        self.step(at + 1.6, 102)
        child = self.book.active()[0]
        self.assertEqual(child['direction'], 'LONG')
        self.assertEqual(child['symbol'], 'SBIN')
        self.assertEqual(child['parent_trade'], trade['id'])
        self.assertEqual(child['id'], self.day + ':0:r1')
        self.assertGreater(D(trade['exit_intent']['net_estimate_at_request']), 1)

    def test_loss_closes_and_rebuys_same_stock_at_lower_price(self):
        trade, at = self.enter()
        self.step(at + 1, 97)
        self.step(at + 1.3, 96)
        self.assertEqual(trade['reason'], 'NET_LOSS_LIMIT')
        self.assertLess(self.book.net_liquidation(trade), -2)  # trigger is not a guaranteed cap
        self.step(at + 1.6, 96)
        child = self.book.active()[0]
        self.assertEqual(child['direction'], trade['direction'])
        self.assertLess(D(child['entry_price']), D(trade['entry_price']))

    def test_short_profit_and_loss_use_buyback_costs_and_same_direction(self):
        for price, reason in ((98, 'NET_PROFIT_TAKE'), (103, 'NET_LOSS_LIMIT')):
            with self.subTest(price=price):
                self.reset()
                trade, at = self.enter(DATES[1])
                self.step(at + 1, price)
                self.step(at + 1.3, price)
                self.assertEqual(trade['reason'], reason)
                self.step(at + 1.6, price)
                self.assertEqual(self.book.active()[0]['direction'], 'SHORT')

    def test_strict_net_boundaries_include_both_legs(self):
        instruments = {k: {**v, 'tick': '0.01'} for k, v in self.cfg.instruments.items()}
        for boundary, beyond, expected, net in ((101.32, 101.33, 'NET_PROFIT_TAKE', '1'),
                                                (98.31, 98.30, 'NET_LOSS_LIMIT', '-2')):
            with self.subTest(expected=expected):
                self.reset(instruments=instruments, slippage_bps='0')
                trade, at = self.enter()
                self.step(at + 1, boundary)
                self.assertEqual(self.book.net_liquidation(trade), D(net))
                self.assertIsNone(trade['exit_intent'])
                self.step(at + 1.3, beyond)
                self.assertEqual(trade['exit_intent']['reason'], expected)

    def test_costs_alone_can_trigger_loss_even_without_price_move(self):
        self.reset(per_trade_limit='2000')
        trade, at = self.enter()
        self.step(at + 1)
        self.assertEqual(trade['exit_intent']['reason'], 'NET_LOSS_LIMIT')
        self.assertLess(self.book.net_liquidation(trade), -2)

    def test_profit_intent_does_not_invent_guaranteed_profitable_fill(self):
        trade, at = self.enter()
        self.step(at + 1, 102)
        self.assertGreater(D(trade['exit_intent']['net_estimate_at_request']), 1)
        self.step(at + 1.3, 99)
        self.assertEqual(trade['reason'], 'NET_PROFIT_TAKE')
        self.assertLess(self.book.net_liquidation(trade), 0)

    def test_short_exact_net_boundaries(self):
        instruments = {k: {**v, 'tick': '0.01'} for k, v in self.cfg.instruments.items()}
        for boundary, beyond, reason, net in ((98.68, 98.67, 'NET_PROFIT_TAKE', 1),
                                             (101.67, 101.68, 'NET_LOSS_LIMIT', -2)):
            with self.subTest(reason=reason):
                self.reset(instruments=instruments, slippage_bps='0')
                trade, at = self.enter(DATES[1])
                self.step(at + 1, boundary)
                self.assertEqual(self.book.net_liquidation(trade), net)
                self.assertIsNone(trade['exit_intent'])
                self.step(at + 1.3, beyond)
                self.assertEqual(trade['exit_intent']['reason'], reason)

    def test_partial_exit_never_recycles_until_flat_and_fee_cap_is_shared(self):
        self.reset(per_trade_limit='500')
        trade, at = self.enter()
        self.assertEqual(trade['quantity'], 4)
        self.step(at + 1, 102, quantity=10)
        self.step(at + 1.3, 102, quantity=10)
        self.assertEqual(trade['remaining'], 3)
        self.assertFalse(self.book.pending())
        self.step(at + 1.6, 102, quantity=10)
        self.step(at + 1.9, 102, quantity=20)
        self.assertEqual(trade['remaining'], 0)
        self.assertEqual(len(self.book.pending()), 1)
        self.assertEqual(D(trade['exit_fees']), fee('SELL', trade['exit_turnover'], CURRENT_FEES))
        fills = [f for f in report(self.path, at + 1.9)['fills'] if f['leg'] == 'EXIT']
        self.assertEqual(sum((D(f['fees']) for f in fills), D(0)), D(trade['exit_fees']))
        for fill in fills:
            self.assertEqual(sum((D(v) for v in fill['fee_components'].values()), D(0)), D(fill['fees']))

    def test_restart_preserves_repeat_intent_and_does_not_duplicate(self):
        trade, at = self.enter()
        self.step(at + 1, 102)
        self.step(at + 1.3, 102)
        self.book.close()
        self.book = Ledger(self.path, self.cfg)
        self.step(at + 1.6, 102, live=False)
        self.assertFalse(self.book.active())
        self.step(at + 2, 102)
        self.step(at + 2, 102)
        self.assertEqual(len(self.book.active()), 1)
        self.assertEqual(len(self.book.s['trades']), 2)

    def test_more_than_three_round_trips_and_no_cash_or_position_overuse(self):
        trade, at = self.enter()
        for n in range(6):
            price = 102 + n * 2
            at += 1
            self.step(at, price)
            self.step(at + .3, price)
            self.step(at + .6, price)
            self.assertLessEqual(len(self.book.active()) + len(self.book.pending()), 3)
            self.assertGreaterEqual(self.book.available(), 0)
        self.assertEqual(len(self.book.s['trades']), 7)
        self.assertEqual(len([t for t in self.book.s['trades'].values() if t['state'] == 'CLOSED']), 6)
        self.assertTrue(all(D(t['budget']) <= 2000 for t in self.book.s['trades'].values()))

    def test_cutoff_cancels_pending_reentry_and_never_recycles_squareoff(self):
        trade, at = self.enter()
        cutoff = stamp(self.day, '15:00')
        self.step(cutoff - .4, 102)
        self.step(cutoff - .1, 102)
        self.assertTrue(self.book.pending())
        self.step(cutoff + .3, 102)
        self.assertFalse(self.book.pending())
        self.assertEqual(len(self.book.s['trades']), 1)

    def test_daily_loss_guard_blocks_recycling(self):
        self.reset(daily_loss_limit='3')
        trade, at = self.enter()
        self.step(at + 1, 98)
        self.step(at + 1.3, 97)
        self.assertTrue(self.book.s['day_loss'][self.day])
        self.assertFalse(self.book.pending())
        self.step(at + 2, 97)
        self.assertEqual(len(self.book.s['trades']), 1)

    def test_three_scheduled_stocks_retain_directions_and_position_cap_on_repeat(self):
        for hhmm in self.cfg.entry_times:
            at = stamp(self.day, hhmm)
            self.step(at)
            self.step(at + .3)
        self.assertEqual([t['direction'] for t in self.book.active()], ['LONG', 'SHORT', 'LONG'])
        at += 1
        self.step(at, 102)
        self.step(at + .3, 102)
        self.assertEqual(len(self.book.active()) + len(self.book.pending()), 3)
        self.step(at + .6, 102)
        self.assertEqual(len(self.book.active()), 3)
        self.assertEqual(len(self.book.s['trades']), 4)

    def test_expired_repeat_can_retry_without_replaying_the_initial_slot(self):
        trade, at = self.enter()
        self.step(at + 1, 102)
        self.step(at + 1.3, 102)
        self.book.step(at + 63, market(at + 63), {})
        self.assertFalse(self.book.pending())
        self.step(at + 64, 102)
        self.step(at + 64.3, 102)
        self.assertEqual(len(self.book.s['trades']), 2)
        self.assertEqual(self.book.active()[0]['id'], self.day + ':0:r2')

    def test_stale_disconnected_frames_cannot_fill_repeat_and_missed_initial_stays_missed(self):
        trade, at = self.enter()
        self.step(at + 1, 102)
        self.step(at + 1.3, 102)
        bad = market(at + 2); bad['connected'] = False
        self.step(at + 2, 102, status=bad)
        self.book.step(at + 25, market(at + 25), quotes(at + 2))
        self.assertEqual(len(self.book.s['trades']), 1)
        self.step(stamp(self.day, '11:32'), 102)
        self.assertNotIn(self.day + ':1', self.book.s['trades'])
        self.assertFalse(any(t['symbol'] == 'ITC' for t in self.book.s['trades'].values()))

    def test_report_components_and_net_match_cash(self):
        trade, at = self.enter()
        self.step(at + 1, 102)
        self.step(at + 1.3, 102)
        r = report(self.path, at + 1.3)
        self.assertEqual(sum((D(v) for v in r['paid_fee_components'].values()), D(0)), D(r['estimated_fees']))
        self.assertGreater(D(r['paid_fee_components']['stamp_duty']), 0)
        self.assertGreater(D(r['paid_fee_components']['stt']), 0)
        self.assertEqual(D(r['equity_estimate']) - 4000, D(r['realized_net_closed']))
        self.assertEqual(r['policy_results'][0]['exit_policy'], 'NET_CASH')


class PriceStopTest(unittest.TestCase):
    """Corrected policy: fees are additional to the INR 2 whole-trade price loss."""
    tearDown = GreedyTest.tearDown
    reset = GreedyTest.reset
    step = GreedyTest.step
    enter = GreedyTest.enter

    def setUp(self):
        GreedyTest.setUp(self)
        self.reset(exit_policy='NET_PROFIT_PRICE_STOP')

    def test_costs_exceeding_two_do_not_trigger_price_stop(self):
        self.reset(per_trade_limit='2000', slippage_bps='0')
        trade, at = self.enter()
        self.step(at + 1)
        self.assertLess(self.book.net_liquidation(trade), -2)
        self.assertGreater(self.book.gross_liquidation(trade), -2)
        self.assertIsNone(trade['exit_intent'])

    def test_long_234_example_costs_are_added_after_two_rupee_price_loss(self):
        self.reset(per_trade_limit='300', slippage_bps='0')
        at = stamp(self.day, '09:30')
        self.step(at, 233.95)
        self.step(at + .3, 233.95)
        trade = self.book.active()[0]
        self.assertEqual(D(trade['entry_price']), 234)
        self.assertEqual(trade['quantity'], 1)
        self.step(at + 1, 232)
        self.assertEqual(self.book.gross_liquidation(trade), -2)
        self.assertLess(self.book.net_liquidation(trade), -2)
        self.assertIsNone(trade['exit_intent'])  # strictly more than INR 2
        self.step(at + 1.3, 231.95)
        self.assertEqual(trade['exit_intent']['reason'], 'PRICE_LOSS_LIMIT')
        self.step(at + 1.6, 231.95)
        cost = D(trade['entry_fees']) + D(trade['exit_fees'])
        self.assertEqual(self.book.net_liquidation(trade), -D('2.05') - cost)
        self.step(at + 1.9, 231.95)
        self.assertEqual(self.book.active()[0]['parent_trade'], trade['id'])
        self.assertEqual(self.book.active()[0]['direction'], 'LONG')

    def test_short_price_stop_excludes_buyback_taxes_and_has_strict_boundary(self):
        self.reset(slippage_bps='0')
        trade, at = self.enter(DATES[1])
        self.step(at + 1, 101.95)  # BUY ask 102, against SELL entry 100
        self.assertEqual(self.book.gross_liquidation(trade), -2)
        self.assertLess(self.book.net_liquidation(trade), -2)
        self.assertIsNone(trade['exit_intent'])
        self.step(at + 1.3, 102)
        self.assertEqual(trade['exit_intent']['reason'], 'PRICE_LOSS_LIMIT')
        self.step(at + 1.6, 102)
        self.step(at + 1.9, 102)
        self.assertEqual(self.book.active()[0]['direction'], 'SHORT')

    def test_one_rupee_profit_still_requires_all_costs_covered(self):
        instruments = {k: {**v, 'tick': '0.01'} for k, v in self.cfg.instruments.items()}
        self.reset(slippage_bps='0', instruments=instruments)
        trade, at = self.enter()
        self.step(at + 1, 101.10)
        self.assertGreater(self.book.gross_liquidation(trade), 1)
        self.assertLess(self.book.net_liquidation(trade), 1)
        self.assertIsNone(trade['exit_intent'])
        self.step(at + 1.3, 101.32)
        self.assertEqual(self.book.net_liquidation(trade), 1)
        self.assertIsNone(trade['exit_intent'])
        self.step(at + 1.6, 101.33)
        self.assertEqual(trade['exit_intent']['reason'], 'NET_PROFIT_TAKE')

    def test_two_rupee_stop_is_per_position_not_per_share(self):
        self.reset(per_trade_limit='350')
        trade, at = self.enter()
        self.assertEqual(trade['quantity'], 3)
        self.step(at + 1, 99.55)
        self.assertLess(self.book.net_liquidation(trade), -2)
        self.assertGreater(self.book.gross_liquidation(trade), -2)
        self.assertIsNone(trade['exit_intent'])
        self.step(at + 1.3, 99.40)
        self.assertEqual(trade['exit_intent']['reason'], 'PRICE_LOSS_LIMIT')

    def test_partial_exits_include_realized_gross_and_report_costs_separately(self):
        self.reset(per_trade_limit='500')
        trade, at = self.enter()
        self.step(at + 1, 99.5, quantity=10)
        self.step(at + 1.3, 99.5, quantity=10)
        self.assertEqual(trade['remaining'], 3)
        self.assertEqual(self.book.gross_liquidation(trade), D('-2.80'))
        r = report(self.path, at + 1.3)
        self.assertEqual(r['loss_basis'], 'PRICE')
        self.assertEqual(r['price_loss_inr'], '2')
        self.assertIsNone(r['net_loss_inr'])
        t = r['trades'][0]
        self.assertEqual(D(t['net_pnl_estimate']), D(t['gross_pnl_estimate']) - D(t['estimated_total_fees']))
        self.assertFalse(self.book.pending())

    def test_daily_guard_still_counts_costs(self):
        self.reset(per_trade_limit='2000', daily_loss_limit='4', slippage_bps='0')
        trade, at = self.enter()
        self.step(at + 1)
        self.assertGreater(self.book.gross_liquidation(trade), -2)
        self.assertTrue(self.book.s['day_loss'][self.day])
        self.assertEqual(trade['exit_intent']['reason'], 'RISK_HALT')


class FeeBreakdownTest(unittest.TestCase):
    def test_published_current_components_both_sides_and_rounding(self):
        buy, sell = breakdown('BUY', '100000', CURRENT_FEES), breakdown('SELL', '100000', CURRENT_FEES)
        self.assertEqual(buy['brokerage'], D(20))
        self.assertEqual(buy['exchange'], D('3.07'))
        self.assertEqual(buy['ipft'], D('0.0001'))
        self.assertEqual(buy['sebi'], D('.1'))
        self.assertEqual(buy['stamp_duty'], D(3))
        self.assertEqual(buy['stt'], 0)
        self.assertEqual(sell['stamp_duty'], 0)
        self.assertEqual(sell['stt'], D(25))
        self.assertEqual(buy['gst'], D('4.152618'))
        self.assertEqual(fee('BUY', 100000, CURRENT_FEES), D('30.32'))
        self.assertEqual(fee('SELL', 100000, CURRENT_FEES), D('52.32'))

    def test_cumulative_partial_fills_do_not_recharge_brokerage_cap(self):
        a = fee_delta('BUY', 0, 15000, CURRENT_FEES)
        b = fee_delta('BUY', 15000, 40000, CURRENT_FEES)
        self.assertEqual(a['brokerage'] + b['brokerage'], 20)
        self.assertEqual(sum(a.values()) + sum(b.values()), fee('BUY', 40000, CURRENT_FEES))


class UpgradeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.path, self.config_path = self.base / 'trial.sqlite', self.base / 'config.json'
        self.cfg = config()
        self.book = Ledger(self.path, self.cfg)
        at = stamp(DATES[0], '09:30')
        for t, price in ((at, 100), (at + .3, 100), (at + 1, 103), (at + 1.3, 103)):
            self.book.step(t, market(t), quotes(t, {'SBIN': price}))
        # Simulate the exact legacy JSON shape (without new optional fields).
        old = asdict(self.cfg)
        for key in ('exit_policy', 'net_profit_inr', 'net_loss_inr', 'recycle_entries', 'price_loss_inr'):
            old.pop(key)
        write_private(self.config_path, old)
        with self.book.atomic():
            self.book.s['config'] = old
            self.book.s['implementation_sha256'] = 'a' * 64
            for trade in self.book.s['trades'].values():
                for key in ('fee_model', 'exit_policy', 'cycle', 'root_slot', 'parent_trade'):
                    trade.pop(key, None)
        self.original = read_state(self.path)
        self.book.close()
        self.now = stamp(DATES[0], '16:00')

    def tearDown(self):
        self.temp.cleanup()

    def migrate(self, **kw):
        return upgrade(self.config_path, self.path, self.base / 'backup', 'a' * 64,
                       'b' * 40, now=kw.pop('now', self.now), **kw)

    def test_flat_upgrade_preserves_every_trade_balance_dates_and_audits_policy(self):
        checked = self.migrate(check=True)
        self.assertFalse((self.base / 'backup').exists())
        result = self.migrate()
        cfg = TrialConfig.load(self.config_path)
        book = Ledger(self.path, cfg)
        try:
            self.assertEqual(book.s['trades'], self.original['trades'])
            for field in ('fees', 'realized_gross', 'slots', 'sessions', 'watermarks'):
                self.assertEqual(book.s[field], self.original[field])
            self.assertEqual(cfg.sessions, self.cfg.sessions)
            self.assertEqual(result['cash_preserved'], checked['cash_preserved'])
            self.assertEqual(cfg.exit_policy, 'NET_PROFIT_PRICE_STOP')
            self.assertEqual(cfg.fee_model, CURRENT_FEES)
            self.assertEqual(read_state(self.base / 'backup/trial.sqlite'), self.original)
            r = report(self.path, self.now)
            self.assertEqual(len(r['policy_history']), 1)
            self.assertEqual(r['policy_results'][0]['exit_policy'], 'FIXED_PERCENT')
            self.assertEqual(D(r['equity_estimate']), D(result['cash_preserved']))
        finally:
            book.close()
        with self.assertRaises(ValueError):
            self.migrate()

    def test_active_session_and_finished_week_refused_without_mutation(self):
        for now in (stamp(DATES[1], '10:00'), stamp(DATES[-1], '16:00')):
            with self.assertRaises(ValueError):
                self.migrate(now=now)
        self.assertEqual(read_state(self.path), self.original)
        self.assertFalse((self.base / 'backup').exists())

    def test_running_worker_lock_refuses_migration(self):
        import fcntl
        with open(str(self.path) + '.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, 'Stop the paper worker'):
                self.migrate()
        self.assertEqual(read_state(self.path), self.original)

    def test_next_day_uses_original_schedule_and_reports_two_policy_phases(self):
        self.migrate()
        book = Ledger(self.path, TrialConfig.load(self.config_path))
        try:
            at = stamp(DATES[1], '09:30')
            book.step(at, market(at), quotes(at))
            book.step(at + .3, market(at + .3), quotes(at + .3))
            trade = book.s['trades'][DATES[1] + ':0']
            self.assertEqual(trade['direction'], 'SHORT')
            self.assertEqual(trade['exit_policy'], 'NET_PROFIT_PRICE_STOP')
            r = report(self.path, at + .3)
            self.assertEqual(len(r['policy_results']), 2)
            self.assertEqual(len(r['fills']), 3)
            self.assertEqual(sum((D(v) for v in r['paid_fee_components'].values()), D(0)), D(r['estimated_fees']))
        finally:
            book.close()

    def test_failed_config_write_rolls_back_database_and_keeps_backup(self):
        real_write = write_private
        failed = False
        def fail_once(path, value, **kwargs):
            nonlocal failed
            if Path(path) == self.config_path and not failed:
                failed = True
                raise OSError('simulated disk error')
            return real_write(path, value, **kwargs)
        with patch('execution.paper_trial.upgrade.write_private', side_effect=fail_once):
            with self.assertRaises(OSError):
                self.migrate()
        self.assertEqual(read_state(self.path), self.original)
        self.assertEqual(TrialConfig.load(self.config_path).fingerprint, self.cfg.fingerprint)
        self.assertEqual(read_state(self.base / 'backup/trial.sqlite'), self.original)

    def test_previous_net_cash_release_can_upgrade_without_losing_policy_history(self):
        import hashlib
        import sqlite3
        from execution.paper_trial.config import dumps
        old = asdict(replace(self.cfg, exit_policy='NET_CASH', recycle_entries=True, fee_model=CURRENT_FEES))
        old.pop('price_loss_inr')  # exact configuration shape of release 41ce136
        expected_hash = hashlib.sha256(dumps(old).encode()).hexdigest()
        self.assertEqual(TrialConfig(**old).fingerprint, expected_hash)
        state = json.loads(json.dumps(self.original))
        prior_history = [{'at': self.now - 100, 'from_release': 'original', 'to_release': 'net-cash'}]
        state.update(config=old, config_hash=expected_hash, policy_history=prior_history)
        with sqlite3.connect(self.path) as db:
            db.execute('UPDATE trial SET value=?', (json.dumps(state),))
        write_private(self.config_path, old)
        self.migrate()
        after = read_state(self.path)
        self.assertEqual(after['policy_history'][:-1], prior_history)
        self.assertEqual(after['trades'], state['trades'])
        self.assertEqual(after['fees'], state['fees'])
        self.assertEqual(after['config']['exit_policy'], 'NET_PROFIT_PRICE_STOP')

    def test_open_position_pending_entry_and_source_mismatch_refused(self):
        import sqlite3
        for change in ('open', 'pending', 'source', 'halt'):
            state = json.loads(json.dumps(self.original))
            if change == 'open':
                next(iter(state['trades'].values()))['remaining'] = 1
            elif change == 'pending':
                next(iter(state['slots'].values()))['state'] = 'PENDING'
            elif change == 'source':
                state['implementation_sha256'] = 'c' * 64
            else:
                state['halt'] = 'INTRADAY_EXIT_UNRESOLVED'
            with sqlite3.connect(self.path) as db:
                db.execute('UPDATE trial SET value=?', (json.dumps(state),))
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.migrate()
        self.assertFalse((self.base / 'backup').exists())
