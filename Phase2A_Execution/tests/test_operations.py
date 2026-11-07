from copy import deepcopy
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
from execution import Config, Engine, Quote
from execution.brokers import PaperBroker
from execution.models import dec
from execution.operations import backup, restore, dispatch_guard, run_fenced
from execution.risk import assess
from execution.sessions import SessionClient
from execution.store import Store
from finance_fixtures import instant, trade_and_evidence


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / 'engine.sqlite')
        self.store.set('account', 'kite:TEST')
        self.trade, _ = trade_and_evidence(self.root)
        self.store.save(self.trade, 'TEST')
    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_wal_backup_restores_all_committed_journal_rows_paused(self):
        source = self.root / 'snapshot.sqlite'
        meta = backup(self.store.path, source)
        self.assertEqual(meta['account'], 'kite:TEST')
        self.assertEqual(source.stat().st_mode & 0o777, 0o600)
        target = self.root / 'restored.sqlite'
        restore(source, target, 'kite:TEST')
        restored = Store(target)
        try:
            self.assertEqual(restored.all(), self.store.all())
            self.assertEqual(restored.db.execute('SELECT count(*) FROM events').fetchone()[0], 1)
            self.assertTrue(restored.get('paused'))
            self.assertTrue(restored.get('polling')['recovery_required'])
            self.assertEqual(restored.get('financial_reconciliation')['status'], 'RESTORE_REQUIRES_RECONCILIATION')
        finally:
            restored.close()
        with self.assertRaises(ValueError):
            restore(source, target, 'kite:TEST')

    def test_tampering_and_wrong_account_cannot_restore(self):
        source = self.root / 'snapshot.sqlite'
        backup(self.store.path, source)
        with self.assertRaises(ValueError):
            restore(source, self.root / 'target.sqlite', 'kite:OTHER')
        source.write_bytes(source.read_bytes() + b'altered')
        with self.assertRaises(ValueError):
            restore(source, self.root / 'target.sqlite', 'kite:TEST')

    def test_second_continuous_worker_is_rejected_but_controls_can_lock(self):
        second = Store(self.store.path)
        try:
            self.store.acquire_worker()
            with self.assertRaises(RuntimeError):
                second.acquire_worker()
            with second.locked():
                second.set('paused', True)
            self.assertTrue(self.store.get('paused'))
        finally:
            second.close()

    def test_dispatch_requires_fresh_matching_lease_with_request_headroom(self):
        path = self.root / 'lease.json'
        for account, issued, expiry in [('TEST', 100, 135), ('OTHER', 100, 135), ('TEST', 140, 160), ('TEST', 50, 135), ('TEST', 100, 107)]:
            path.write_text(json.dumps(dict(account=account, issued_at=issued, expires_at=expiry)))
            if (account, issued, expiry) == ('TEST', 100, 135):
                dispatch_guard(path, 'TEST', now=100)
            else:
                with self.assertRaises(RuntimeError):
                    dispatch_guard(path, 'TEST', now=100)

    def test_cloud_guard_runs_after_auth_and_before_dispatch_callback_or_post(self):
        client = object.__new__(SessionClient)
        sdk = Mock()
        client.ensure = Mock(return_value=sdk)
        client.dispatch_guard = Mock(side_effect=RuntimeError('expired'))
        callback = Mock()
        with self.assertRaises(RuntimeError):
            client.request('place_order', before_send=callback)
        client.ensure.assert_called_once()
        callback.assert_not_called()
        sdk.place_order.assert_not_called()
        client.request('orders')
        sdk.orders.assert_called_once()

    def test_lease_loss_stops_child_and_removes_local_dispatch_permission(self):
        blob, lease, child = Mock(), Mock(), Mock()
        blob.acquire_lease.return_value = lease
        lease.renew.side_effect = [None, RuntimeError('connection lost')]
        child.wait.side_effect = [subprocess.TimeoutExpired('worker', 15), 0]
        child.poll.return_value = None
        child.pid = 99999
        path = self.root / 'lease.json'
        with patch('execution.operations.blob_client', return_value=blob), patch('execution.operations.subprocess.Popen', return_value=child), patch('execution.operations.os.killpg') as kill:
            with self.assertRaises(RuntimeError):
                run_fenced('https://example.blob.core.windows.net/operations/account', path, 'TEST', ['worker'])
            kill.assert_called_with(99999, signal.SIGTERM)
        self.assertFalse(path.exists())
        lease.release.assert_called_once()

    def test_lease_expiring_during_dispatch_journaling_still_blocks_post(self):
        client = object.__new__(SessionClient)
        sdk = Mock()
        client.ensure = Mock(return_value=sdk)
        client.dispatch_guard = Mock(side_effect=[None, RuntimeError('expired during journal write')])
        callback = Mock()
        with self.assertRaises(RuntimeError):
            client.request('place_order', before_send=callback)
        callback.assert_called_once()
        sdk.place_order.assert_not_called()

    def test_failed_lease_acquisition_never_starts_engine(self):
        blob = Mock()
        blob.acquire_lease.side_effect = RuntimeError('already owned')
        with patch('execution.operations.blob_client', return_value=blob), patch('execution.operations.subprocess.Popen') as start:
            with self.assertRaises(RuntimeError):
                run_fenced('https://example', self.root / 'lease', 'TEST', ['worker'])
            start.assert_not_called()

    def test_external_monitor_rejects_stale_future_and_unhealthy_records(self):
        path = Path(__file__).resolve().parents[2] / 'deploy/check_remote_health.py'
        spec = importlib.util.spec_from_file_location('monitor', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.check(dict(checked_at=100, status='HEALTHY'), 110)
        for payload in (dict(checked_at=100, status='HEALTHY'), dict(checked_at=400, status='HEALTHY'), dict(checked_at=300, status='CRITICAL', codes=['WORKER_STALE'])):
            with self.assertRaises(ValueError):
                module.check(payload, 300)


class DailyLossTests(unittest.TestCase):
    def test_account_losses_do_not_get_replenished_by_winning_trades(self):
        cfg = Config({'NSE:DEMO': '.05'}, '2000', '1000', '2', '1', daily_loss_limit='50')
        now = instant('2026-11-06 14:00')
        trades = [dict(id='loss', state='CLOSED', closed_at=now, estimated_net_pnl='-51'),
                  dict(id='win', state='CLOSED', closed_at=now, estimated_net_pnl='100')]
        result = assess(cfg, trades, Mock(), now)
        self.assertTrue(result['breached'])
        self.assertEqual(dec(result['loss_used']), 51)
        result = assess(cfg, trades, Mock(), now + 86400)
        self.assertFalse(result['breached'])

    def test_missing_closure_date_requires_reconciliation(self):
        cfg = Config({'NSE:DEMO': '.05'}, '2000', '1000', '2', '1', daily_loss_limit='50')
        result = assess(cfg, [dict(id='legacy', state='CLOSED')], Mock(), instant('2026-11-06 14:00'))
        self.assertTrue(result['errors'])

    def test_drawdown_latches_across_restart_and_flattens_through_gtt_ownership(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            now = instant('2026-11-06 10:00')
            cfg = Config({'NSE:DEMO': '.05'}, '2000', '1000', '2', '5', daily_loss_limit='10')
            store = Store(root / 'engine.sqlite')
            broker = PaperBroker(root / 'paper.sqlite', '2000', cfg.fees, lambda: now)
            try:
                engine = Engine(cfg, store, broker, lambda: now)
                broker.set_quote('NSE:DEMO', '100')
                engine.request_entry('one', 'NSE:DEMO', '1000')
                broker.set_quote('NSE:DEMO', '98')  # above the 95 stop, loss budget crosses first
                engine.tick()
                self.assertTrue(store.get('daily_risk')['breached'])
                self.assertEqual(store.all()[0]['reason'], 'DAILY_LOSS_LIMIT')
                engine = Engine(cfg, store, broker, lambda: now)
                with self.assertRaises(ValueError):
                    engine.pause(False)
                for _ in range(4):
                    engine.tick()
                self.assertEqual(store.all()[0]['state'], 'CLOSED')
                self.assertTrue(store.all()[0]['protection']['released'])
                self.assertTrue(store.get('daily_risk')['breached'])
            finally:
                broker.close()
                store.close()

    def test_stale_open_valuation_blocks_admission_without_inventing_loss(self):
        cfg = Config({'NSE:DEMO': '.05'}, '2000', '1000', '2', '1', daily_loss_limit='50')
        trade = dict(id='open', stock='NSE:DEMO', state='OPEN', fees=dict(buy_bps='0',sell_bps='0',buy_fixed='0',sell_fixed='0'),
                     orders=[dict(side='BUY', filled=1, average='100')])
        result = assess(cfg, [trade], lambda stock: Quote(dec('100'), 1), 1000)
        self.assertTrue(result['errors'])
