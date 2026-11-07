"""Session, calendar and cross-day acceptance tests. No real credentials/orders."""
from contextlib import redirect_stdout
from dataclasses import asdict, replace
from datetime import datetime
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from execution import Config, Engine
from execution.__main__ import main
from execution.brokers import PaperBroker
from execution.live import LiveRuntime
from execution.market import IST, MarketCalendar, MarketClosed
from execution.sessions import SessionClient, SessionUnavailable, expires_at, read_session, save_session
from execution.store import Store
from execution.supervision import health_snapshot


def instant(text):
    return datetime.fromisoformat(text).replace(tzinfo=IST).timestamp()


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.calendar = MarketCalendar.load()

    def test_regular_session_and_premarket_boundaries_use_ist(self):
        for time, expected in (("08:29:59", "CLOSED"), ("08:30:00", "PREMARKET"),
                               ("09:14:59", "PREMARKET"), ("09:15:00", "OPEN"),
                               ("15:14:59", "OPEN"), ("15:15:00", "CLOSED")):
            for exchange in ("NSE", "BSE"):
                with self.subTest(time=time, exchange=exchange):
                    self.assertEqual(self.calendar.status(exchange, instant("2026-11-02 " + time))["state"], expected)

    def test_weekends_holidays_and_next_open(self):
        for day, opening in (("2026-10-31", "2026-11-02"), ("2026-11-10", "2026-11-11"),
                              ("2026-11-24", "2026-11-25"), ("2026-12-25", "2026-12-28")):
            state = self.calendar.status("NSE", instant(day + " 12:00"))
            self.assertEqual(state["state"], "CLOSED")
            self.assertEqual(state["next_open"], instant(opening + " 09:15"))

    def test_muhurat_is_a_verified_sunday_session_not_a_normal_day(self):
        for exchange in ("NSE", "BSE"):
            self.assertEqual(self.calendar.status(exchange, instant("2026-11-08 09:30"))["state"], "CLOSED")
            self.assertEqual(self.calendar.status(exchange, instant("2026-11-08 14:10"))["state"], "PREMARKET")
            self.assertEqual(self.calendar.status(exchange, instant("2026-11-08 14:20"))["state"], "OPEN")
            self.assertEqual(self.calendar.status(exchange, instant("2026-11-08 15:15"))["state"], "CLOSED")

    def test_unknown_exchange_and_uncovered_year_fail_closed(self):
        for exchange, when in (("NSE", "2027-01-01 10:00"), ("NSE", "2026-10-30 10:00"), ("MCX", "2026-11-02 10:00")):
            self.assertEqual(self.calendar.status(exchange, instant(when))["state"], "UNKNOWN")
            with self.assertRaises(MarketClosed):
                self.calendar.require_open(exchange + ":DEMO", instant(when))
        self.assertIsNone(self.calendar.status("NSE", instant("2026-12-31 18:00"))["next_open"])

    def test_unknown_special_date_does_not_guess_next_open(self):
        data = json.loads(json.dumps(self.calendar.data))
        data["exchanges"]["NSE"]["unverified"] = ["2026-11-08"]
        calendar = MarketCalendar(data)
        self.assertIsNone(calendar.status("NSE", instant("2026-11-07 12:00"))["next_open"])
        self.assertEqual(calendar.status("NSE", instant("2026-11-08 12:00"))["state"], "UNKNOWN")

    def test_close_buffer_blocks_late_entries_and_exit_actions(self):
        for when, buffer in (("15:14:00", 60), ("15:14:50", 10)):
            with self.assertRaises(MarketClosed):
                self.calendar.require_open("NSE:DEMO", instant("2026-11-02 " + when), buffer)

    def test_invalid_calendar_cannot_enable_trading(self):
        for values in ({"valid_through": "2028-12-31"}, {"timezone": "UTC"}, {"schema": 2}):
            with self.assertRaises(ValueError):
                MarketCalendar(dict(self.calendar.data, **values))
        data = json.loads(json.dumps(self.calendar.data))
        data["exchanges"]["NSE"]["regular"] = ["15:30", "09:15"]
        with self.assertRaises(ValueError):
            MarketCalendar(data)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "live.kite-session.json"
        self.store = Store(self.root / "engine.sqlite")
        self.now = instant("2026-11-02 10:00")
        self.clock = lambda: self.now
        self.cfg = Config({"NSE:DEMO": ".05"}, "2000", "1000", "2", "1", live_account_id="TEST")
        self.sdk = Mock()
        self.sdk.profile.return_value = dict(user_id="TEST", exchanges=["NSE"], products=["CNC"],
                                            order_types=["LIMIT", "MARKET"], meta={"demat_consent": "physical"})
        self.sdk.instruments.return_value = [dict(tradingsymbol="DEMO", instrument_type="EQ", lot_size=1, tick_size=.05)]
        self.sdk.margins.return_value = dict(enabled=True, net=2000, available=dict(cash=2000, live_balance=2000))
        self.sdk.holdings.return_value = []
        self.sdk.positions.return_value = {"net": []}
        self.sdk.orders.return_value = []
        self.sdk.get_gtts.return_value = []
        self.factory = Mock(return_value=self.sdk)
        self.client = SessionClient(self.factory, "synthetic-key", "TEST", self.path, self.store, self.clock, {})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def save(self, token="synthetic-token", account="TEST", issued=None):
        return save_session(self.path, dict(user_id=account, api_key="synthetic-key", access_token=token,
                                           login_time=datetime.fromtimestamp(issued or self.now, IST)),
                            "synthetic-key", "TEST", self.now)

    def codes(self):
        return {a["code"] for a in health_snapshot(self.store, self.cfg, self.now)["alerts"]}

    def test_private_atomic_receipt_and_metadata_do_not_leak_tokens(self):
        metadata = self.save()
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("access_token", metadata)
        self.client.ensure()
        serialized = json.dumps(self.store.get("session"))
        self.assertNotIn("synthetic-token", serialized)
        self.assertNotIn("synthetic-key", serialized)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)

    def test_expiry_is_next_day_six_am_ist_and_exact_boundary_blocks_api(self):
        self.save()
        self.assertEqual(expires_at(self.now), instant("2026-11-03 06:00"))
        self.now = instant("2026-11-03 06:00")
        with self.assertRaises(SessionUnavailable):
            self.client.orders()
        self.factory.assert_not_called()
        self.assertIn("SESSION_EXPIRED", self.codes())

    def test_expiring_alert_and_readonly_expiry_work_without_worker_tick(self):
        self.save()
        self.client.ensure()
        self.now = instant("2026-11-03 05:01")
        self.assertIn("SESSION_EXPIRING", self.codes())
        self.now = instant("2026-11-03 06:00")
        self.assertIn("SESSION_EXPIRED", self.codes())

    def test_missing_session_blocks_all_api_calls(self):
        with self.assertRaises(SessionUnavailable):
            self.client.place_order(quantity=1)
        self.factory.assert_not_called()
        self.assertIn("SESSION_MISSING", self.codes())

    def test_session_rotation_rechecks_identity_without_restarting_worker(self):
        self.save()
        self.client.orders()
        first = self.client.revision
        self.save("second-synthetic-token")
        self.client.orders()
        self.assertNotEqual(first, self.client.revision)
        self.assertEqual(self.factory.call_count, 2)
        self.assertEqual(self.sdk.profile.call_count, 2)
        self.assertEqual(self.sdk.orders.call_count, 2)

    def test_revocation_survives_process_restart_and_never_retries_the_post(self):
        class TokenException(Exception):
            pass
        self.save()
        self.sdk.place_order.side_effect = TokenException("secret must not be echoed")
        with self.assertRaises(SessionUnavailable):
            self.client.place_order(quantity=1)
        self.assertEqual(self.sdk.place_order.call_count, 1)
        restarted = SessionClient(self.factory, "synthetic-key", "TEST", self.path, self.store, self.clock, {})
        with self.assertRaises(SessionUnavailable):
            restarted.place_order(quantity=1)
        self.assertEqual(self.sdk.place_order.call_count, 1)
        self.save("replacement-token")
        self.sdk.place_order.side_effect = None
        restarted.orders()
        self.assertEqual(self.store.get("session")["state"], "VALID")

    def test_transport_error_has_no_post_retry_and_no_credentials_in_error(self):
        self.save()
        self.sdk.place_order.side_effect = TimeoutError("https://broker/?access_token=synthetic-token")
        with self.assertRaisesRegex(RuntimeError, "Broker request failed") as error:
            self.client.place_order(quantity=1)
        self.assertNotIn("synthetic-token", str(error.exception))
        self.assertEqual(self.sdk.place_order.call_count, 1)

    def test_bad_profile_blocks_wrong_account_before_any_order(self):
        self.save()
        self.sdk.profile.return_value = {"user_id": "OTHER"}
        with self.assertRaises(SessionUnavailable):
            self.client.place_order(quantity=1)
        self.sdk.place_order.assert_not_called()

    def test_private_file_permissions_and_symlinks_are_enforced(self):
        self.save()
        self.path.chmod(0o644)
        with self.assertRaises(SessionUnavailable):
            self.client.ensure()
        self.path.chmod(0o600)
        link = self.root / "link.kite-session.json"
        link.symlink_to(self.path)
        with self.assertRaises(SessionUnavailable):
            read_session(link, "synthetic-key", "TEST", self.now, {})

    def test_tampered_expiry_identity_and_future_issue_time_are_rejected(self):
        for field, value in (("expires_at", self.now + 999999), ("account_id", "OTHER"),
                              ("api_key_hash", "wrong"), ("issued_at", self.now + 60)):
            self.save()
            data = json.loads(self.path.read_text())
            data[field] = value
            self.path.write_text(json.dumps(data))
            with self.subTest(field=field), self.assertRaises(SessionUnavailable):
                self.client.ensure()
        self.path.write_text("not json")
        with self.assertRaises(SessionUnavailable):
            self.client.ensure()

    def test_bad_login_does_not_overwrite_working_session(self):
        self.save()
        before = self.path.read_text()
        with self.assertRaises(ValueError):
            self.save(account="OTHER")
        self.assertEqual(self.path.read_text(), before)

    def test_environment_migration_requires_explicit_issue_time(self):
        with self.assertRaises(SessionUnavailable):
            read_session(self.path, "synthetic-key", "TEST", self.now, {"KITE_ACCESS_TOKEN": "synthetic-token"})
        data = read_session(self.path, "synthetic-key", "TEST", self.now, {
            "KITE_ACCESS_TOKEN": "synthetic-token", "KITE_ACCESS_TOKEN_ISSUED_AT": "2026-11-02T09:00:00+05:30"})
        self.assertEqual(data["expires_at"], instant("2026-11-03 06:00"))

    def test_invalid_file_never_falls_back_to_environment(self):
        self.save()
        self.path.write_text("invalid")
        with self.assertRaises(SessionUnavailable):
            read_session(self.path, "synthetic-key", "TEST", self.now, {
                "KITE_ACCESS_TOKEN": "other-token", "KITE_ACCESS_TOKEN_ISSUED_AT": "2026-11-02T09:00:00+05:30"})

    def test_renewal_is_detected_even_when_a_session_expires_between_api_calls(self):
        self.save()
        self.client.orders()
        self.now = instant("2026-11-03 06:00")
        with self.assertRaises(SessionUnavailable):
            self.client.place_order(quantity=1)
        self.sdk.place_order.assert_not_called()
        self.save("next-day-token")
        self.client.orders()
        self.assertEqual(self.sdk.orders.call_count, 2)

    def test_runtime_survives_missing_then_renewed_then_expired_session(self):
        runtime = LiveRuntime(self.cfg, self.store, "synthetic-key", self.path, self.factory, self.clock, {})
        with patch("execution.kite.time.sleep"):
            _, health = runtime.tick()
            self.assertTrue(health["worker"]["fresh"])
            self.assertIsNone(runtime.engine)
            self.assertIn("SESSION_MISSING", {a["code"] for a in health["alerts"]})
            self.save()
            _, health = runtime.tick()
            self.assertEqual(health["status"], "HEALTHY")
            self.assertEqual(health["preflight"]["status"], "READY")
            self.now = instant("2026-11-03 06:00")
            runtime.tick()
            self.assertIn("SESSION_EXPIRED", self.codes())
            self.now = instant("2026-11-03 08:30")
            self.save("next-day-token")
            _, health = runtime.tick()
            self.assertEqual(health["status"], "HEALTHY")
            self.assertEqual(health["preflight"]["identity"]["trading_date"], "2026-11-03")
        self.sdk.place_order.assert_not_called()
        self.sdk.place_gtt.assert_not_called()

    def test_flat_account_still_checks_session_revocation_every_minute(self):
        class TokenException(Exception):
            pass
        self.save()
        self.client.ensure()
        self.now += 60
        self.sdk.profile.side_effect = TokenException()
        with self.assertRaises(SessionUnavailable):
            self.client.ensure()
        self.assertIn("SESSION_REVOKED", self.codes())

    def test_runtime_does_not_write_to_another_accounts_database(self):
        self.store.set("account", "kite:OTHER")
        self.store.set("heartbeat", 123)
        runtime = LiveRuntime(self.cfg, self.store, "synthetic-key", self.path, self.factory, self.clock, {})
        with self.assertRaisesRegex(ValueError, "different broker account"):
            runtime.tick()
        self.assertEqual(self.store.get("heartbeat"), 123)
        self.assertIsNone(self.store.get("session"))
        self.assertIsNone(self.store.get("live_runtime"))
        self.factory.assert_not_called()

    def test_daily_preflight_uses_one_shared_account_snapshot(self):
        self.save()
        runtime = LiveRuntime(self.cfg, self.store, "synthetic-key", self.path, self.factory, self.clock, {})
        with patch("execution.kite.time.sleep"):
            _, health = runtime.tick()
        self.assertEqual(health["preflight"]["status"], "READY")
        for call in (self.sdk.holdings, self.sdk.positions, self.sdk.orders, self.sdk.get_gtts):
            self.assertEqual(call.call_count, 1)

    def test_next_day_instrument_drift_blocks_entries_before_submission(self):
        self.save()
        runtime = LiveRuntime(self.cfg, self.store, "synthetic-key", self.path, self.factory, self.clock, {})
        with patch("execution.kite.time.sleep"):
            runtime.tick()
            self.now = instant("2026-11-03 09:16")
            self.save("next-day-token")
            self.sdk.instruments.return_value[0]["tick_size"] = .10
            _, health = runtime.tick()
            self.assertEqual(health["preflight"]["status"], "FAILED")
            self.assertTrue(self.store.get("paused"))
            with self.assertRaisesRegex(ValueError, "paused"):
                runtime.engine.request_entry("one", "NSE:DEMO", "1000")
        self.sdk.place_order.assert_not_called()

    def test_preflight_rejects_an_unmanaged_active_trigger(self):
        self.save()
        self.sdk.get_gtts.return_value = [dict(id=987, status="active", condition={"exchange": "NSE", "tradingsymbol": "DEMO"})]
        runtime = LiveRuntime(self.cfg, self.store, "synthetic-key", self.path, self.factory, self.clock, {})
        with patch("execution.kite.time.sleep"):
            _, health = runtime.tick()
        self.assertEqual(health["preflight"]["status"], "FAILED")
        self.assertIn("Unmanaged GTT", health["preflight"]["error"])
        self.sdk.place_order.assert_not_called()

    def test_sdk_login_contract_is_supported_and_secrets_are_not_printed(self):
        try:
            from kiteconnect import KiteConnect
        except ImportError:
            self.skipTest("Optional live SDK not installed")
        config = self.root / "config.json"
        config.write_text(json.dumps(asdict(self.cfg)))
        output = io.StringIO()
        response = dict(user_id="TEST", api_key="synthetic-key", access_token="synthetic-token", login_time="2026-11-02 10:00:00")
        args = ["--config", str(config), "--session-file", str(self.path), "login"]
        with patch.dict(os.environ, {"KITE_API_KEY": "synthetic-key", "KITE_API_SECRET": "synthetic-secret"}, clear=True), \
                patch("execution.__main__.sys.stdin.isatty", return_value=True), \
                patch("execution.__main__.getpass.getpass", return_value="synthetic-request"), \
                patch("execution.__main__.time.time", self.clock), \
                patch.object(KiteConnect, "_post", return_value=response) as post, \
                patch.object(KiteConnect, "profile", return_value={"user_id": "TEST"}), redirect_stdout(output):
            self.assertEqual(main(args), 0)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.args, ("api.token",))
        self.assertNotIn("api_secret", post.call_args.kwargs["params"])
        self.assertNotIn("synthetic-token", output.getvalue())
        self.assertNotIn("synthetic-secret", output.getvalue())
        self.assertEqual(read_session(self.path, "synthetic-key", "TEST", self.now, {})["access_token"], "synthetic-token")


class OvernightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.now = instant("2026-11-02 10:00")
        self.clock = lambda: self.now
        self.cfg = Config({"NSE:DEMO": ".05", "NSE:OTHER": ".05"}, "2000", "1000", "2", "1")
        self.calendar = MarketCalendar.load()
        self.store = Store(self.root / "engine.sqlite")
        self.broker = PaperBroker(self.root / "paper.sqlite", "2000", self.cfg.fees, self.clock)
        self.engine = Engine(self.cfg, self.store, self.broker, self.clock, self.calendar)
        self.broker.set_quote("NSE:DEMO", "100")

    def tearDown(self):
        self.broker.close()
        self.store.close()
        self.tmp.cleanup()

    def enter(self):
        return self.engine.request_entry("one", "NSE:DEMO", "1000")

    def codes(self):
        return {a["code"] for a in self.engine.health()["alerts"]}

    def test_closed_market_never_requests_quote_or_cancels_protection(self):
        before = self.enter()
        self.now = instant("2026-11-02 18:00")
        with patch.object(self.broker, "quote", side_effect=AssertionError("No off-hours quotes")), \
                patch.object(self.broker, "cancel_protection") as cancel, patch.object(self.broker, "submit") as submit:
            after = self.engine.tick()[0]
            self.assertEqual(self.engine.health()["status"], "HEALTHY")
            cancel.assert_not_called()
            submit.assert_not_called()
        self.assertEqual(before["protection"]["id"], after["protection"]["id"])
        self.assertEqual(after["exit_feed"]["state"], "MARKET_CLOSED")

    def test_closed_entry_is_blocked_before_a_trade_intent(self):
        for when in ("2026-11-02 09:00", "2026-11-02 15:14", "2026-11-10 10:00", "2027-01-01 10:00"):
            self.now = instant(when)
            with self.assertRaises(MarketClosed):
                self.enter()
        self.assertEqual(self.store.all(), [])
        self.assertEqual(self.broker._orders(), [])

    def test_flatten_outside_hours_keeps_gtt_then_exits_after_confirmed_release(self):
        before = self.enter()
        self.now = instant("2026-11-02 18:00")
        self.engine.flatten()
        after = self.engine.tick()[0]
        self.assertEqual(after["reason"], "OPERATOR_EXIT")
        self.assertEqual(self.broker.protection(before["protection"]["id"]).status, "active")
        self.assertEqual(len(self.broker._orders()), 1)
        self.now = instant("2026-11-03 09:16")
        self.broker.set_quote("NSE:DEMO", "100")
        self.engine.tick()  # request GTT cancellation
        self.assertEqual(len(self.broker._orders()), 1)
        self.engine.tick()  # authoritative release then residual sell
        self.assertEqual(self.engine.tick()[0]["state"], "CLOSED")
        self.assertEqual(len(self.broker._orders()), 2)

    def test_pending_child_does_not_consume_cancel_budget_while_closed(self):
        self.enter()
        self.now = instant("2026-11-02 15:14:45")
        self.broker.set_quote("NSE:DEMO", "90")
        self.engine.tick()
        self.now = instant("2026-11-02 16:00")
        with patch.object(self.broker, "cancel") as cancel:
            for _ in range(3):
                trade = self.engine.tick()[0]
            cancel.assert_not_called()
        self.assertNotIn("cancel", trade["orders"][-1])
        self.assertIn("EXIT_TIMEOUT", self.codes())  # real outstanding risk remains visible
        self.assertNotIn("EXIT_FEED_UNAVAILABLE", self.codes())

    def test_overdue_entry_stays_visible_when_closure_defers_cancellation(self):
        self.broker.max_fill = 0
        order = self.broker.order
        with patch.object(self.broker, "order", lambda oid: replace(order(oid), status="OPEN")), \
                patch.object(self.broker, "cancel") as cancel:
            self.enter()
            self.now = instant("2026-11-02 18:00")
            self.engine.tick()
            cancel.assert_not_called()
        self.assertIn("ENTRY_TIMEOUT", self.codes())
        self.assertNotIn("EXIT_FEED_UNAVAILABLE", self.codes())

    def test_next_day_missing_child_history_never_creates_a_replacement(self):
        self.enter()
        self.broker.set_quote("NSE:DEMO", "90")
        self.engine.tick()
        self.now = instant("2026-11-03 09:16")
        with patch.object(self.broker, "order", side_effect=ValueError("Daily history no longer available")), \
                patch.object(self.broker, "submit") as submit:
            self.engine.tick()
            self.now += 31
            self.engine.tick()
            submit.assert_not_called()
        self.assertIn("RECONCILIATION_FAILED", self.codes())
        self.assertIn("PREFLIGHT_FAILED", self.codes())

    def test_cross_day_unknown_submission_is_not_assumed_absent(self):
        with patch.object(self.broker, "submit", side_effect=TimeoutError()) as submit:
            self.enter()
            self.now = instant("2026-11-03 09:16")
            self.engine.tick()
            self.assertEqual(submit.call_count, 1)
        self.assertEqual(self.store.all()[0]["orders"][0]["status"], "UNKNOWN")
        self.assertIn("SUBMISSION_UNRESOLVED", self.codes())

    def test_restart_next_day_reuses_position_and_gtt_without_old_terminal_history(self):
        before = self.enter()
        self.store.close()
        self.store = Store(self.root / "engine.sqlite")
        self.engine = Engine(self.cfg, self.store, self.broker, self.clock, self.calendar)
        self.now = instant("2026-11-03 08:30")
        with patch.object(self.broker, "order", side_effect=AssertionError("Terminal orders are persisted")):
            after = self.engine.tick()[0]
        self.assertEqual(after["target"], before["target"])
        self.assertEqual(after["protection"]["id"], before["protection"]["id"])
        self.assertEqual(self.store.get("preflight")["status"], "READY")
        self.assertEqual(len(self.broker._orders()), 1)

    def test_premarket_validation_is_readonly_and_detects_foreign_inventory(self):
        self.now = instant("2026-11-02 08:30")
        with patch.object(self.broker, "assert_inventory", side_effect=ValueError("Foreign inventory")), \
                patch.object(self.broker, "submit") as submit, patch.object(self.broker, "quote") as quote:
            result = self.engine.preflight()
            submit.assert_not_called()
            quote.assert_not_called()
        self.assertEqual(result["status"], "FAILED")
        self.now = instant("2026-11-02 09:16")
        with patch.object(self.broker, "assert_inventory", side_effect=ValueError("Foreign inventory")), self.assertRaises(ValueError):
            self.enter()
        self.assertEqual(self.store.all(), [])

    def test_daily_and_session_rotation_rerun_instrument_and_account_checks(self):
        with patch.object(self.broker, "preflight", create=True, return_value={}) as check:
            self.engine.tick()
            self.engine.tick()
            self.assertEqual(check.call_count, 1)
            self.store.set("session", {"revision": "renewed"})
            self.engine.tick()
            self.assertEqual(check.call_count, 2)
            self.now = instant("2026-11-03 08:30")
            self.engine.tick()
            self.assertEqual(check.call_count, 3)

    def test_preflight_failure_does_not_stop_existing_confirmed_exit(self):
        self.enter()
        self.engine.flatten()
        self.store.set("preflight", {})
        with patch.object(self.broker, "preflight", create=True, side_effect=ValueError("Instrument master unavailable")):
            self.engine.tick()
            self.engine.tick()
            self.assertEqual(self.engine.tick()[0]["state"], "CLOSED")
        self.assertIn("PREFLIGHT_FAILED", self.codes())

    def test_close_during_quote_does_not_send_order_or_invent_unknown_submission(self):
        quote = self.broker.quote
        def closing(stock):
            result = quote(stock)
            self.now = instant("2026-11-02 15:15")
            return result
        with patch.object(self.broker, "quote", closing), patch.object(self.broker, "submit") as submit:
            with self.assertRaises((MarketClosed, ValueError)):
                self.enter()
            submit.assert_not_called()
        self.assertEqual(self.broker._orders(), [])

    def test_local_gtt_closure_rejection_does_not_leave_unknown_registration(self):
        with patch.object(self.broker, "create_protection", side_effect=MarketClosed()):
            trade = self.enter()
        self.assertNotIn("protection", trade)
        self.assertNotIn("GTT_REGISTRATION_UNRESOLVED", self.codes())
        self.assertIn("POSITION_UNPROTECTED", self.codes())
        self.now = instant("2026-11-03 09:16")
        self.broker.set_quote("NSE:DEMO", "100")
        recovered = self.engine.tick()[0]
        self.assertEqual(recovered["protection"]["health"], "ACTIVE")
        self.assertEqual(len(self.broker._orders()), 1)

    def test_boundary_rejection_before_post_does_not_consume_a_sell_attempt(self):
        self.enter()
        self.engine.flatten()
        self.engine.tick()
        with patch.object(self.broker, "submit", side_effect=MarketClosed()):
            self.engine.tick()
        self.assertEqual(len(self.store.all()[0]["orders"]), 1)
        self.assertNotIn("SUBMISSION_UNRESOLVED", self.codes())
        self.engine.tick()
        self.assertEqual(self.engine.tick()[0]["state"], "CLOSED")
        self.assertEqual(len(self.broker._orders()), 2)

    def test_health_calendar_coverage_alert_works_after_worker_stops(self):
        self.engine.tick()
        self.now = instant("2026-12-24 10:00")
        self.assertIn("CALENDAR_EXPIRING", self.codes())
        self.now = instant("2027-01-01 10:00")
        self.assertIn("CALENDAR_UNVERIFIED", self.codes())


if __name__ == "__main__":
    unittest.main()
