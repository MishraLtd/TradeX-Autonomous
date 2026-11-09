import concurrent.futures
from contextlib import closing
import http.client
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from execution import Config, Engine
from execution.api import EntryService, make_server
from execution.brokers import PaperBroker
from execution.store import Store


class EntryApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.now = 1000.
        self.cfg = Config({'NSE:DEMO': '.05'}, '2000', '1000', '2', '1', exit_mode='SOFTWARE')
        self.store = Store(self.root / 'engine.sqlite')
        self.broker = PaperBroker(self.root / 'paper.sqlite', '2000', self.cfg.fees, lambda: self.now)
        self.engine = Engine(self.cfg, self.store, self.broker, lambda: self.now)
        self.broker.set_quote('NSE:DEMO', '100')
        self.store.set('heartbeat', self.now)
        self.service = EntryService(self.cfg, self.store, lambda: self.engine, lambda: self.now)
        self.token = 'x' * 48
        self.server = make_server(self.service, self.token, 0)
        self.server.timeout = 3
        self.payload = dict(request_id='manual-001', stock='NSE:DEMO', budget='1000')

    def tearDown(self):
        self.server.server_close()
        self.broker.close()
        self.store.close()
        self.tmp.cleanup()

    def request(self, payload=None, headers=None, method='POST', path='/v1/entries', raw=None):
        body = raw if raw is not None else json.dumps(self.payload if payload is None else payload)
        hdr = {'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'}
        hdr.update(headers or {})
        def client():
            with closing(http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)) as conn:
                conn.request(method, path, body, hdr)
                response = conn.getresponse()
                return response.status, json.loads(response.read())
        with concurrent.futures.ThreadPoolExecutor() as pool:
            result = pool.submit(client)
            self.server.handle_request()
            return result.result()

    def test_authenticated_entry_replay_and_profit_exit(self):
        self.assertEqual(self.request()[0], 202)
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(len(self.broker._orders()), 1)
        trade = self.engine.tick()[0]
        self.broker.set_quote('NSE:DEMO', trade['target'])
        self.engine.tick()
        self.assertEqual(self.engine.tick()[0]['state'], 'CLOSED')

    def test_unauthorized_and_browser_origin_never_trade(self):
        self.assertEqual(self.request(headers={'Authorization': 'Bearer wrong'})[0], 401)
        self.assertEqual(self.request(headers={'Origin': 'https://example.com'})[0], 403)
        self.assertEqual(self.broker._orders(), [])

    def test_unapproved_stock_and_extra_overrides_rejected(self):
        self.assertEqual(self.request(dict(self.payload, stock='NSE:OTHER'))[0], 409)
        self.assertEqual(self.request(dict(self.payload, stop_pct='0'))[0], 409)
        self.assertEqual(self.broker._orders(), [])

    def test_conflicting_request_id_rejected(self):
        self.request()
        self.assertEqual(self.request(dict(self.payload, budget='900'))[0], 409)
        self.assertEqual(len(self.broker._orders()), 1)

    def test_worker_must_be_fresh(self):
        for heartbeat in (None, self.now - 31, self.now + 1):
            self.store.set('heartbeat', heartbeat)
            self.assertEqual(self.request()[0], 409)
        self.assertEqual(self.broker._orders(), [])

    def test_pause_budget_and_stale_quotes_preserve_engine_gates(self):
        self.store.set('paused', True)
        self.assertEqual(self.request()[0], 409)
        self.store.set('paused', False)
        self.assertEqual(self.request(dict(self.payload, budget='1001'))[0], 409)
        self.now += 20
        self.assertEqual(self.request()[0], 409)
        self.assertEqual(self.broker._orders(), [])

    def test_invalid_bodies_and_money(self):
        for raw in ('{', '[]', '{"stock":"a","stock":"b"}'):
            self.assertIn(self.request(raw=raw)[0], (400, 409))
        for value in (1000, True, 'NaN', '-1', '0', '1e3', '100.001'):
            self.assertEqual(self.request(dict(self.payload, budget=value))[0], 409)
        self.assertEqual(self.request(raw=' ' * 4097)[0], 413)
        self.assertEqual(self.request(headers={'Content-Type': 'text/plain'})[0], 415)

    def test_universe_and_unknown_routes(self):
        status, data = self.request(method='GET', path='/v1/universe')
        self.assertEqual(status, 200)
        self.assertEqual(data['stocks'], ['NSE:DEMO'])
        self.assertEqual(self.request(path='/v1/flatten')[0], 404)

    def test_restart_replays_without_broker_or_fresh_heartbeat(self):
        self.request()
        self.store.close()
        self.store = Store(self.root / 'engine.sqlite')
        self.store.set('heartbeat', None)
        def unavailable():
            raise RuntimeError('Broker offline')
        service = EntryService(self.cfg, self.store, unavailable, lambda: self.now)
        self.assertTrue(service.submit(self.payload)['replayed'])
        self.assertEqual(len(self.broker._orders()), 1)

    def test_weak_token_refused(self):
        with self.assertRaises(ValueError):
            make_server(self.service, 'short')

    def test_service_failure_never_claims_no_order(self):
        with patch.object(self.service, 'prepare', side_effect=RuntimeError('private broker details')):
            status, data = self.request()
        self.assertEqual(status, 503)
        self.assertEqual(data['error'], 'UNAVAILABLE_OR_OUTCOME_UNKNOWN')
        self.assertNotIn('private broker details', json.dumps(data))


if __name__ == '__main__':
    unittest.main()
