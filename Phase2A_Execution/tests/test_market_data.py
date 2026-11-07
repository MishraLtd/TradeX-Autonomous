"""Deterministic data faults, reconnection and measured exit-response acceptance."""
from contextlib import redirect_stdout
from dataclasses import asdict, replace
from datetime import datetime
from decimal import Decimal
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from execution import Config, Engine, Quote
from execution.__main__ import main
from execution.brokers import PaperBroker
from execution.kite import KiteBroker
from execution.live import LiveRuntime
from execution.market import IST
from execution.marketdata import QuoteBook
from execution.polling import Cadence, PollMonitor, operational_alerts
from execution.sessions import SessionClient, save_session
from execution.store import Store
from execution.supervision import health_snapshot
from finance_fixtures import profile as cost_profile


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 2, 10, tzinfo=IST).timestamp()
        self.elapsed = 0.0
    def wall(self):
        return self.now
    def mono(self):
        return self.elapsed
    def advance(self, seconds):
        self.now += seconds
        self.elapsed += seconds
    def date(self, offset=0):
        return datetime.fromtimestamp(self.now + offset, IST)


def row(clock, price=100, exchange=0, traded=0):
    return dict(last_price=price, timestamp=clock.date(exchange), last_trade_time=clock.date(traded))


def sdk(clock):
    client = Mock()
    client.profile.return_value = dict(user_id="TEST", exchanges=["NSE"], products=["CNC"],
                                     order_types=["LIMIT", "MARKET"], meta={"demat_consent": "physical"})
    client.instruments.return_value = [dict(tradingsymbol=s, instrument_type="EQ", lot_size=1, tick_size=.05)
                                       for s in ("DEMO", "OTHER")]
    client.margins.return_value = dict(enabled=True, net=2000, available=dict(cash=2000, live_balance=2000))
    client.holdings.return_value = []
    client.positions.return_value = {"net": []}
    client.orders.return_value = []
    client.get_gtts.return_value = []
    client.quote.side_effect = lambda stocks: {s: row(clock) for s in stocks}
    client.place_order.return_value = "new-id"
    return client


def order_row(clock, oid="one", filled=0, status="OPEN", update=0):
    return dict(order_id=oid, tag="tag-" + oid, exchange="NSE", tradingsymbol="DEMO", transaction_type="BUY",
                quantity=9, filled_quantity=filled, average_price=100 if filled else 0, status=status,
                order_timestamp=clock.date(-10), exchange_update_timestamp=clock.date(update))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.clock = Clock()
        self.store = Store(self.root / "engine.sqlite")
        self.cfg = Config({"NSE:DEMO": ".05", "NSE:OTHER": ".05"}, "2000", "1000", "2", "1", live_account_id="TEST")
    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()


class QuoteTests(Base):
    def setUp(self):
        super().setUp()
        self.book = QuoteBook(self.clock.wall, 15, self.store)
    def test_missing_or_bad_quote_never_reuses_last_good_value(self):
        self.book.accept("NSE:DEMO", row(self.clock))
        for value in (None, {}, dict(last_price=0), row(self.clock, price="NaN"), row(self.clock, price=-1)):
            with self.assertRaises(ValueError):
                self.book.accept("NSE:DEMO", value)
            with self.assertRaises(ValueError):
                self.book.get("NSE:DEMO")
        self.assertEqual(self.store.get("market_data")["NSE:DEMO"]["state"], "UNAVAILABLE")
    def test_both_exchange_and_trade_timestamps_reject_future_and_stale(self):
        for exchange, traded in ((3, 0), (0, 3), (-16, 0), (0, -16)):
            with self.subTest(exchange=exchange, traded=traded), self.assertRaises(ValueError):
                self.book.accept("NSE:DEMO", row(self.clock, exchange=exchange, traded=traded))
    def test_duplicate_timestamps_do_not_make_a_frozen_feed_fresh(self):
        frozen = row(self.clock)
        self.book.accept("NSE:DEMO", frozen)
        self.clock.advance(10)
        self.book.accept("NSE:DEMO", frozen)
        self.clock.advance(6)
        with self.assertRaises(ValueError):
            self.book.accept("NSE:DEMO", frozen)
    def test_regression_watermarks_survive_restart_and_reconnect(self):
        self.book.accept("NSE:DEMO", row(self.clock))
        self.book.clear()
        restarted = QuoteBook(self.clock.wall, 15, self.store)
        for exchange, traded in ((-1, 0), (0, -1)):
            with self.assertRaises(ValueError):
                restarted.accept("NSE:DEMO", row(self.clock, exchange=exchange, traded=traded))
        self.clock.advance(1)
        self.assertEqual(restarted.accept("NSE:DEMO", row(self.clock)).price, 100)
    def test_snapshot_expires_during_a_slow_cycle(self):
        self.book.accept("NSE:DEMO", row(self.clock))
        self.clock.advance(16)
        with self.assertRaises(ValueError):
            self.book.get("NSE:DEMO")
    def test_backward_local_clock_invalidates_cached_receipt(self):
        self.book.accept("NSE:DEMO", row(self.clock))
        self.clock.now -= 1
        with self.assertRaises(ValueError):
            self.book.get("NSE:DEMO")
    def test_bad_symbol_does_not_poison_healthy_symbol(self):
        self.book.accept("NSE:OTHER", row(self.clock))
        with self.assertRaises(ValueError):
            self.book.accept("NSE:DEMO", None)
        self.assertEqual(self.book.get("NSE:OTHER").price, 100)


class AdapterTests(Base):
    def setUp(self):
        super().setUp()
        self.client = sdk(self.clock)
        self.broker = KiteBroker(self.client, self.cfg.stocks, live=True, min_interval=0,
                                 clock=self.clock.wall, store=self.store)
        self.trades = [dict(stock=s, state="OPEN") for s in self.cfg.stocks]
    def test_cycle_batches_quotes_and_reuses_only_current_cycle(self):
        self.broker.begin_poll(self.trades)
        self.broker.quote("NSE:DEMO")
        self.broker.quote("NSE:OTHER")
        self.assertEqual(self.client.quote.call_count, 1)
        self.assertEqual(set(self.client.quote.call_args.args[0]), set(self.cfg.stocks))
        self.broker.end_poll()
        self.clock.advance(5)
        self.broker.quote("NSE:DEMO")
        self.assertEqual(self.client.quote.call_count, 2)
    def test_partial_or_failed_batch_never_replays_previous_prices(self):
        self.broker.begin_poll(self.trades)
        self.broker.end_poll()
        self.client.quote.side_effect = None
        self.client.quote.return_value = {"NSE:OTHER": row(self.clock)}
        self.broker.begin_poll(self.trades)
        with self.assertRaises(ValueError):
            self.broker.quote("NSE:DEMO")
        self.assertEqual(self.broker.quote("NSE:OTHER").price, 100)
        self.assertTrue(self.broker.poll_failed)
        self.broker.end_poll()
        self.client.quote.side_effect = TimeoutError("do not log the response")
        self.broker.begin_poll(self.trades)
        self.assertIsNone(self.broker.cached_quote("NSE:OTHER"))

    def test_quote_expiring_during_protection_checks_requires_poll_recovery(self):
        self.broker.begin_poll(self.trades)
        self.assertFalse(self.broker.poll_failed)
        self.clock.advance(16)
        self.assertIsNone(self.broker.cached_quote("NSE:DEMO"))
        self.assertTrue(self.broker.poll_failed)
    def test_quote_requests_are_chunked_at_documented_limit(self):
        stocks = ["NSE:S" + str(i) for i in range(501)]
        self.broker.fetch_quotes(stocks)
        self.assertEqual([len(c.args[0]) for c in self.client.quote.call_args_list], [500, 1])
    def test_quote_spacing_applies_after_errors_too(self):
        self.broker.min_interval = .35
        self.broker._last_call = self.broker._last_quote = 0
        calls = []
        def quotes(stocks):
            calls.append(self.clock.mono())
            if len(calls) == 2:
                raise TimeoutError()
            return {s: row(self.clock) for s in stocks}
        self.client.quote.side_effect = quotes
        with patch("execution.kite.time.monotonic", self.clock.mono), patch("execution.kite.time.sleep", self.clock.advance):
            for _ in range(3):
                self.broker.fetch_quotes(["NSE:DEMO"])
        self.assertTrue(all(b - a >= 1.05 - 1e-9 for a, b in zip(calls, calls[1:])))
    def test_order_updates_share_one_snapshot_but_mutations_invalidate_it(self):
        self.client.orders.return_value = [order_row(self.clock, "one"), order_row(self.clock, "two")]
        self.broker.begin_poll(self.trades)
        self.broker.order("one")
        self.broker.order("two")
        self.assertEqual(self.client.orders.call_count, 1)
        self.broker.cancel("one")
        self.client.orders.return_value = [order_row(self.clock, "one", status="CANCELLED")]
        self.assertEqual(self.broker.order("one").status, "CANCELLED")
        self.assertEqual(self.client.orders.call_count, 2)
    def test_order_snapshot_expires_and_missing_orders_require_history(self):
        self.client.orders.return_value = [order_row(self.clock)]
        self.broker.begin_poll(self.trades)
        self.broker.order("one")
        self.clock.advance(16)
        self.client.orders.return_value = []
        self.client.order_history.return_value = []
        with self.assertRaisesRegex(ValueError, "No broker history"):
            self.broker.order("one")
        self.assertEqual(self.client.orders.call_count, 2)
        self.client.order_history.assert_called_once_with("one")
    def test_duplicate_ids_in_an_order_snapshot_fail_closed(self):
        self.client.orders.return_value = [order_row(self.clock), order_row(self.clock)]
        self.broker.begin_poll(self.trades)
        with self.assertRaisesRegex(ValueError, "Duplicate IDs"):
            self.broker.order("one")
    def test_lost_post_ack_invalidates_book_without_retrying(self):
        self.client.orders.return_value = [order_row(self.clock)]
        self.broker.begin_poll(self.trades)
        self.broker.order("one")
        self.client.place_order.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            self.broker.submit("NSE:DEMO", "SELL", 9, "exit")
        self.broker.order("one")
        self.assertEqual(self.client.place_order.call_count, 1)
        self.assertEqual(self.client.orders.call_count, 2)
    def test_dispatch_timestamp_is_after_session_validation_before_sdk_post(self):
        path = self.root / "live.kite-session.json"
        save_session(path, dict(user_id="TEST", api_key="key", access_token="token", login_time=self.clock.date()),
                     "key", "TEST", self.clock.wall())
        guarded = SessionClient(Mock(return_value=self.client), "key", "TEST", path, self.store, self.clock.wall, {})
        broker = KiteBroker(guarded, self.cfg.stocks, live=True, min_interval=0, clock=self.clock.wall)
        guarded.last_verified = 0
        profile = self.client.profile.return_value
        self.client.profile.side_effect = lambda: (self.clock.advance(2), profile)[1]
        calls = []
        self.client.place_order.side_effect = lambda **kwargs: (calls.append(("post", self.clock.wall())), "id")[1]
        before = self.clock.wall()
        broker.submit("NSE:DEMO", "SELL", 9, "exit", on_send=lambda: calls.append(("dispatch", self.clock.wall())))
        self.assertEqual(calls, [("dispatch", before + 2), ("post", before + 2)])

    def test_installed_sdk_accepts_batch_quotes_and_parses_order_update_times(self):
        try:
            from kiteconnect import KiteConnect
        except ImportError:
            self.skipTest("Optional live SDK not installed")
        client = KiteConnect(api_key="synthetic")
        stamp = self.clock.date().strftime("%Y-%m-%d %H:%M:%S")
        with patch.object(client, "_get", return_value={"NSE:DEMO": dict(last_price=100, timestamp=stamp, last_trade_time=stamp)}) as get:
            quotes = client.quote(["NSE:DEMO"])
        get.assert_called_once_with("market.quote", params={"i": ["NSE:DEMO"]})
        self.assertIsInstance(quotes["NSE:DEMO"]["timestamp"], datetime)
        data = order_row(self.clock)
        data["order_timestamp"] = data["exchange_update_timestamp"] = stamp
        with patch.object(client, "_get", return_value=[data]):
            decoded = KiteBroker._order(client.orders()[0])
        self.assertEqual(decoded.updated_at, self.clock.wall())

    def test_installed_sdk_dispatch_callback_is_before_the_only_post(self):
        try:
            from kiteconnect import KiteConnect
        except ImportError:
            self.skipTest("Optional live SDK not installed")
        client = KiteConnect(api_key="synthetic")
        with patch.object(client, "profile", return_value={"user_id": "TEST"}), \
                patch.object(client, "instruments", return_value=self.client.instruments.return_value):
            broker = KiteBroker(client, self.cfg.stocks, live=True, min_interval=0, clock=self.clock.wall)
        events = []
        def posted(*args, **kwargs):
            events.append("post")
            raise TimeoutError()
        with patch.object(client, "_post", side_effect=posted) as post, self.assertRaises(TimeoutError):
            broker.submit("NSE:DEMO", "SELL", 9, "exit", on_send=lambda: events.append("send"))
        self.assertEqual(events, ["send", "post"])
        self.assertEqual(post.call_count, 1)


class RecoveryTests(Base):
    def setUp(self):
        super().setUp()
        self.cfg = replace(self.cfg, cost_profile=cost_profile(self.root)[0])
        self.client = sdk(self.clock)
        self.path = self.root / "live.kite-session.json"
        save_session(self.path, dict(user_id="TEST", api_key="key", access_token="token", login_time=self.clock.date()),
                     "key", "TEST", self.clock.wall())
        self.factory = Mock(return_value=self.client)
        self.runtime = LiveRuntime(self.cfg, self.store, "key", self.path, self.factory,
                                   self.clock.wall, {}, self.clock.mono)
    def test_disconnect_backoff_then_full_reconciliation_before_resuming(self):
        with patch("execution.kite.time.sleep"):
            _, first = self.runtime.tick()
            self.assertEqual(first["status"], "HEALTHY")
            self.client.holdings.side_effect = TimeoutError("lost")
            self.store.set("preflight", {})
            _, broken = self.runtime.tick()
            self.assertEqual(broken["polling"]["status"], "RECOVERING")
            count = self.factory.call_count
            self.runtime.tick()  # backoff: no network attempt
            self.assertEqual(self.factory.call_count, count)
            with self.assertRaises(ValueError):
                self.runtime.engine.pause(False)
            self.client.holdings.side_effect = None
            self.clock.advance(self.cfg.poll_interval)
            _, recovered = self.runtime.tick()
            self.assertEqual(recovered["status"], "HEALTHY")
            self.assertEqual(self.factory.call_count, count + 1)
            self.client.reqsession.close.assert_called_once()
            self.assertEqual(recovered["preflight"]["status"], "READY")
            self.assertTrue(recovered["paused"])
            self.runtime.engine.pause(False)
        self.client.place_order.assert_not_called()
    def test_incomplete_inventory_recovery_cannot_clear_gate(self):
        with patch("execution.kite.time.sleep"):
            self.runtime.tick()
            self.store.set("polling", dict(self.store.get("polling"), recovery_required=True))
            self.client.holdings.return_value = [dict(exchange="NSE", tradingsymbol="DEMO", product="CNC",
                quantity=1, t1_quantity=0, used_quantity=0, opening_quantity=1)]
            _, health = self.runtime.tick()
        self.assertTrue(health["polling"]["recovery_required"])
        self.assertEqual(health["preflight"]["status"], "FAILED")
        self.client.place_order.assert_not_called()
    def test_sdk_read_timeout_is_configured(self):
        with patch("execution.kite.time.sleep"):
            self.runtime.tick()
        self.assertEqual(self.factory.call_args.kwargs["timeout"], self.cfg.broker_request_timeout)


class MonitoringTests(Base):
    def setUp(self):
        super().setUp()
        self.monitor = PollMonitor(self.cfg, self.store, self.clock.wall, self.clock.mono)
    def codes(self):
        return {a["code"] for a in operational_alerts(self.store, self.cfg, self.clock.wall())}
    def test_cadence_subtracts_work_and_skips_missed_slots(self):
        cadence = Cadence(5, self.clock.mono)
        for duration, expected in ((0, 5), (2, 3), (5, 0), (13, 2), (21, 4)):
            cadence.begin()
            self.clock.advance(duration)
            self.assertEqual(cadence.delay(), expected)
    def test_hanging_cycle_is_visible_to_readonly_health_while_account_locked(self):
        self.monitor.begin()
        self.clock.advance(21)
        config = self.root / "config.json"
        config.write_text(json.dumps(asdict(self.cfg)))
        output = io.StringIO()
        with self.store.locked(), patch("execution.__main__.time.time", self.clock.wall), redirect_stdout(output):
            result = main(["--config", str(config), "--db", str(self.store.path), "health"])
        self.assertEqual(result, 2)
        self.assertIn("POLL_CYCLE_OVERRUN", {a["code"] for a in json.loads(output.getvalue())["alerts"]})
    def test_overrun_and_clock_jump_fail_health_and_pause_entries(self):
        self.monitor.begin()
        self.clock.advance(21)
        self.monitor.finish(True, True)
        self.assertIn("POLL_CYCLE_OVERRUN", self.codes())
        self.assertTrue(self.store.get("paused"))
        self.monitor.begin()
        self.clock.now -= 5
        self.monitor.finish(True, True)
        self.assertIn("POLL_CLOCK_CHANGED", self.codes())
    def test_failed_cycle_never_refreshes_success_timestamp(self):
        self.monitor.begin()
        self.monitor.finish(True, True)
        last = self.store.get("polling")["last_success_at"]
        self.clock.advance(30)
        self.monitor.begin()
        self.monitor.finish(False, True)
        self.assertEqual(self.store.get("polling")["last_success_at"], last)
        self.assertIn("POLL_STALE", self.codes())
        self.assertEqual(self.store.get("polling")["max_start_gap"], 30)
    def test_restart_after_interrupted_cycle_requires_recovery(self):
        self.monitor.begin()
        self.clock.advance(3)
        restarted = PollMonitor(self.cfg, self.store, self.clock.wall, self.clock.mono)
        restarted.begin()
        self.assertTrue(restarted.state["recovery_required"])
        self.assertTrue(restarted.state["interrupted"])
    def test_backoff_is_capped_and_exposure_keeps_fast_reconciliation(self):
        for _ in range(10):
            self.monitor.begin()
            result = self.monitor.finish(False, False)
        self.assertEqual(result["next_retry_at"] - self.clock.wall(), 30)
        self.monitor.begin()
        result = self.monitor.finish(False, True)
        self.assertEqual(result["next_retry_at"] - self.clock.wall(), 5)
    def test_invalid_timing_configuration_is_rejected(self):
        for name in ("poll_interval", "poll_cycle_budget", "broker_request_timeout", "reconnect_max_delay", "exit_response_timeout"):
            for value in (0, -1, float("nan"), float("inf")):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    replace(self.cfg, **{name: value})
        for kwargs in (dict(poll_interval=2), dict(broker_request_timeout=21), dict(exit_response_timeout=44), dict(reconnect_max_delay=4)):
            with self.assertRaises(ValueError):
                replace(self.cfg, **kwargs)


class ResponseTests(Base):
    def setUp(self):
        super().setUp()
        self.broker = PaperBroker(self.root / "paper.sqlite", "10000", self.cfg.fees, self.clock.wall)
        self.engine = Engine(self.cfg, self.store, self.broker, self.clock.wall, monotonic=self.clock.mono)
        self.broker.set_quote("NSE:DEMO", 100)
    def tearDown(self):
        self.broker.close()
        super().tearDown()
    def codes(self):
        return {a["code"] for a in self.engine.health()["alerts"]}
    def enter(self):
        return self.engine.request_entry("one", "NSE:DEMO", "1000")
    def test_operator_trigger_times_dispatch_and_ack_after_gtt_release(self):
        self.enter()
        start = self.clock.wall()
        self.engine.flatten()
        self.engine.tick()
        self.clock.advance(5)
        self.engine.tick()
        response = self.store.all()[0]["exit_response"]
        self.assertEqual(response["observed_at"], start)
        self.assertEqual(response["elapsed_to_send"], 5)
        self.assertFalse(response["breached"])
        self.assertEqual(response["acknowledged_at"], response["sent_at"])
        self.assertEqual(len(self.broker._orders()), 2)
    def test_response_deadline_alert_never_bypasses_pending_cancellation(self):
        self.enter()
        self.engine.flatten()
        with patch.object(self.broker, "cancel_protection"):
            self.engine.tick()
            self.clock.advance(61)
            self.engine.tick()
        self.assertIn("EXIT_RESPONSE_LATE", self.codes())
        self.assertEqual(len(self.broker._orders()), 1)
    def test_response_budget_survives_restart_and_config_changes(self):
        self.enter()
        self.engine.flatten()
        before = self.store.all()[0]["exit_response"]
        self.engine = Engine(replace(self.cfg, exit_response_timeout=120), self.store, self.broker, self.clock.wall)
        self.clock.advance(61)
        with patch.object(self.broker, "cancel_protection"):
            self.engine.tick()
        after = self.store.all()[0]["exit_response"]
        self.assertEqual(after["deadline"], before["deadline"])
        self.assertEqual(after["budget"], 60)
    def test_lost_ack_preserves_dispatch_time_and_does_not_resubmit(self):
        self.enter()
        self.engine.flatten()
        self.engine.tick()
        send = self.broker.submit
        def lost(*args, **kwargs):
            send(*args, **kwargs)
            raise TimeoutError()
        with patch.object(self.broker, "submit", side_effect=lost) as submit:
            self.engine.tick()
            self.engine.tick()
            self.assertEqual(submit.call_count, 1)
        trade = self.store.all()[0]
        self.assertEqual(trade["state"], "CLOSED")
        self.assertIsNotNone(trade["exit_response"]["sent_at"])
        self.assertIsNone(trade["exit_response"]["acknowledged_at"])
        self.assertEqual(len(self.broker._orders()), 2)
    def test_broker_trigger_delay_is_measured_without_a_competing_sell(self):
        trade = self.enter()
        with patch.object(self.broker, "cached_quote", create=True, side_effect=lambda stock: Quote(Decimal(trade["target"]), self.clock.wall())):
            self.engine.tick()
            deadline = self.store.all()[0]["broker_trigger_watch"]["deadline"]
            self.clock.advance(61)
            self.engine.tick()
        self.assertIn("BROKER_TRIGGER_DELAY", self.codes())
        self.assertEqual(self.store.all()[0]["broker_trigger_watch"]["deadline"], deadline)
        self.assertEqual(len(self.broker._orders()), 1)
        self.broker.set_quote("NSE:DEMO", trade["target"])
        finished = self.engine.tick()[0]
        self.assertEqual(finished["state"], "CLOSED")
        self.assertEqual(finished["broker_trigger_watch"]["source_to_submission"], 61)
        self.assertEqual(len(self.broker._orders()), 2)

    def test_unobserved_price_crossing_reports_discovery_lag_not_invented_trigger_latency(self):
        trade = self.enter()
        self.clock.advance(5)
        self.broker.set_quote("NSE:DEMO", trade["target"])
        self.clock.advance(2)
        finished = self.engine.tick()[0]
        observation = next(iter(finished["broker_exit_observations"].values()))
        self.assertEqual(observation["discovery_seconds"], 2)
        self.assertIsNone(observation["actual_trigger_at"])

    def test_closed_market_does_not_turn_overnight_quotes_into_data_failure(self):
        self.enter()
        book = QuoteBook(self.clock.wall, 15, self.store)
        book.accept("NSE:DEMO", row(self.clock))
        from execution.market import MarketCalendar
        self.store.set("calendar", MarketCalendar.load().data)
        self.clock.advance(8 * 3600)
        self.assertNotIn("MARKET_DATA_UNAVAILABLE", self.codes())
        self.clock.advance(16 * 3600)
        self.assertIn("MARKET_DATA_UNAVAILABLE", self.codes())
    def test_old_or_wrong_order_updates_cannot_replace_persisted_state(self):
        self.broker.max_fill = 0
        original = self.broker.order
        with patch.object(self.broker, "order", side_effect=lambda oid: replace(original(oid), status="OPEN", updated_at=self.clock.wall())):
            self.enter()
        before = self.store.all()[0]["orders"][0]
        for updates in (dict(updated_at=self.clock.wall() - 1), dict(updated_at=self.clock.wall() + 3),
                        dict(updated_at=None), dict(id="wrong")):
            with patch.object(self.broker, "order", side_effect=lambda oid: replace(original(oid), status="OPEN", **updates)):
                self.engine.tick()
            after = self.store.all()[0]["orders"][0]
            self.assertEqual(after["id"], before["id"])
            self.assertEqual(after["updated_at"], before["updated_at"])
            self.assertTrue(self.store.all()[0]["error"])
    def test_cycle_overrun_rotates_work_instead_of_starving_later_positions(self):
        self.enter()
        self.broker.set_quote("NSE:OTHER", 100)
        self.engine.request_entry("two", "NSE:OTHER", "1000")
        processed = []
        def slow(trade):
            processed.append(trade["id"])
            self.clock.advance(21)
        with patch.object(self.engine, "_progress", side_effect=slow):
            self.engine.tick()
            self.assertEqual(self.store.get("poll_deferred"), ["two"])
            self.engine.tick()
        self.assertEqual(processed, ["one", "two"])
    def test_five_position_persistent_price_trigger_meets_measured_bound(self):
        stocks = {"NSE:S" + str(i): ".05" for i in range(5)}
        cfg = replace(self.cfg, stocks=stocks, capital_limit="5000", exit_mode="SOFTWARE")
        engine = Engine(cfg, self.store, self.broker, self.clock.wall, monotonic=self.clock.mono)
        for index, stock in enumerate(stocks):
            self.broker.set_quote(stock, 100)
            engine.request_entry(str(index), stock, "1000")
        engine.tick()
        # Worst phase: a persistent crossing just after the previous poll read.
        event = self.clock.wall()
        self.clock.advance(cfg.poll_cycle_budget + cfg.poll_interval)
        for stock in stocks:
            self.broker.set_quote(stock, 110)  # still fresh when the next cycle reads
        check = self.broker.assert_inventory
        def delayed(*args):
            self.clock.advance(2)
            return check(*args)
        with patch.object(self.broker, "assert_inventory", side_effect=delayed):
            trades = engine.tick()
        elapsed = [t["orders"][-1]["sent_at"] - event for t in trades]
        bound = 2 * cfg.poll_cycle_budget + cfg.poll_interval
        self.assertEqual(max(elapsed), 35)
        self.assertLessEqual(max(elapsed), bound)
        self.assertLessEqual(bound, cfg.exit_response_timeout)
        self.assertTrue(all(not t["exit_response"]["breached"] for t in trades))
        self.assertEqual(len([o for o in self.broker._orders() if o.side == "SELL"]), 5)


if __name__ == "__main__":
    unittest.main()
