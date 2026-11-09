"""Data-receiver, cloud maintenance and failure-monitor contracts."""
from dataclasses import asdict
import io
import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from execution.paper_trial.config import stamp, dumps
from execution.paper_trial.ledger import Ledger, read_state
from execution.paper_trial.report import write_private
from execution.paper_trial.runtime import Receiver
from execution.paper_trial.upstox import DataClient
from test_paper_trial import config, DATES, KEYS, quotes, market

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'deploy'))
import paper_maintenance


class MaintenanceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'trial.sqlite'
        self.cfg = config()
        self.config_path = Path(self.temp.name) / 'config.json'
        write_private(self.config_path, asdict(self.cfg))
        self.book = Ledger(self.path, self.cfg)
        self.now = stamp(DATES[0], '09:30')
        self.book.step(self.now, market(self.now), quotes(self.now))
        self.env = patch.dict(os.environ, dict(TRADEX_MODE='upstox-paper', TRADEX_TRIAL_DB=str(self.path),
            TRADEX_TRIAL_CONFIG=str(self.config_path), TRADEX_OPERATIONS_URL='https://example.blob.core.windows.net/operations',
            TRADEX_ACCOUNT_ID='PAPER-TEST'))
        self.env.start()
        self.blobs = {}
        test = self
        class Blob:
            def __init__(self, url): self.url = url
            def upload_blob(self, data, overwrite):
                test.blobs[self.url] = data.read() if hasattr(data, 'read') else data
        self.mock = patch.object(paper_maintenance, 'blob_client', side_effect=Blob)
        self.mock.start()

    def tearDown(self):
        self.mock.stop(); self.env.stop()
        self.book.close(); self.temp.cleanup()

    def health(self, now):
        with patch.object(paper_maintenance.time, 'time', return_value=now):
            return paper_maintenance.maintain('health')

    def test_complete_backup_uploaded_manifest_last_and_health_is_sanitized(self):
        with patch.object(paper_maintenance.time, 'time', return_value=self.now):
            paper_maintenance.maintain('backup')
        self.assertTrue(list(self.blobs)[-1].endswith('.sqlite.json'))
        result = self.health(self.now)
        self.assertEqual(result['status'], 'HEALTHY')
        health = json.loads(self.blobs['https://example.blob.core.windows.net/operations/health.json'])
        for field in ('trades', 'initial_cash', 'equity_estimate', 'instruments'):
            self.assertNotIn(field, health)
        self.assertTrue(any(k.endswith('latest-report.json') for k in self.blobs))

    def test_missing_or_stale_backup_and_dead_worker_reach_external_health(self):
        result = self.health(self.now)
        self.assertIn('TRIAL_BACKUP_MISSING_OR_STALE', result['codes'])
        with patch.object(paper_maintenance.time, 'time', return_value=self.now):
            paper_maintenance.maintain('backup')
        result = self.health(self.now + 31)
        self.assertEqual(result['status'], 'DEGRADED')
        self.assertIn('WORKER_STALE', result['codes'])
        result = self.health(self.now + 28 * 3600)
        self.assertIn('TRIAL_BACKUP_MISSING_OR_STALE', result['codes'])

    def test_missing_db_publishes_failure_instead_of_fabricating_empty_state(self):
        with patch.dict(os.environ, TRADEX_TRIAL_DB=str(Path(self.temp.name) / 'missing.sqlite')):
            result = self.health(self.now)
        self.assertEqual(result['status'], 'DEGRADED')
        self.assertEqual(result['codes'], ['TRIAL_STATE_UNAVAILABLE'])
        self.assertFalse((Path(self.temp.name) / 'missing.sqlite').exists())

    def test_failed_upload_does_not_mark_backup_successful(self):
        with patch.object(paper_maintenance, 'blob_client', side_effect=RuntimeError('storage unavailable')):
            with self.assertRaises(RuntimeError): paper_maintenance.maintain('backup')
        self.assertFalse((Path(self.temp.name) / 'backup-receipt.json').exists())


class ReceiverTest(unittest.TestCase):
    def test_market_data_subscription_only_and_disconnect_is_visible(self):
        try:
            import websocket
            from execution.paper_trial.MarketDataFeed_pb2 import FeedResponse
            from google.protobuf.json_format import ParseDict
        except ImportError:
            self.skipTest('Install requirements-upstox-paper.txt for data transport contract')
        at = stamp(DATES[0], '09:30')
        cfg = config()
        stop = threading.Event()
        client = DataClient('fake-private-analytics-token')
        receiver = Receiver(client, cfg, stop)
        info = ParseDict(dict(type='market_info', currentTs=str(int(at * 1000)),
                         marketInfo={'segmentStatus': {'NSE_EQ': 'NORMAL_OPEN'}}), FeedResponse()).SerializeToString()
        test = self
        sent = []
        class Socket:
            def settimeout(self, t): pass
            def send(self, data, opcode): sent.append((data, opcode))
            def recv_data(self, control_frame):
                if not getattr(self, 'received', False):
                    self.received = True
                    return websocket.ABNF.OPCODE_BINARY, info
                stop.set()
                raise RuntimeError('signed-url-must-not-appear-in-health')
            def close(self, timeout): pass
        with patch.object(client, 'websocket_url', return_value='wss://ws.upstox.com/data?code=private'), \
             patch('execution.paper_trial.runtime.regular_session', return_value=True), \
             patch('execution.paper_trial.runtime.load_instruments', return_value=[]), \
             patch('execution.paper_trial.runtime.resolve_instruments', return_value=cfg.instruments), \
             patch('execution.paper_trial.runtime.time.time', return_value=at), \
             patch.object(websocket, 'create_connection', return_value=Socket()) as connect:
            receiver.run()
        self.assertEqual(len(sent), 1)
        data, opcode = sent[0]
        self.assertIsInstance(data, bytes)
        self.assertEqual(opcode, websocket.ABNF.OPCODE_BINARY)
        self.assertEqual(json.loads(data)['method'], 'sub')
        self.assertEqual(json.loads(data)['data']['mode'], 'full')
        self.assertNotIn('header', connect.call_args.kwargs)
        state = receiver.status()
        self.assertFalse(state['connected'])
        self.assertEqual(state['error'], 'STREAM_CONNECTION_FAILED')
        self.assertEqual(state['reconnects'], 1)
        self.assertTrue(receiver.messages.empty())

    def test_cli_rejects_live_environment_without_fetching_any_secret(self):
        from execution.paper_trial.__main__ import main
        with patch.dict(os.environ, TRADEX_MODE='live'), \
                patch('execution.paper_trial.__main__.analytics_token') as token, \
                patch('sys.stderr', new_callable=io.StringIO):
            with self.assertRaises(SystemExit): main(['prepare'])
            token.assert_not_called()


if __name__ == '__main__':
    unittest.main()
