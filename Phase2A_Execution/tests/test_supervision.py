"""Fault-injection acceptance tests for stalled execution and monitoring."""
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict, replace
from decimal import Decimal
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from execution import Config, Engine, Quote
from execution.__main__ import emit_alerts, main
from execution.brokers import PaperBroker
from execution.store import Store


class SimulatedCrash(BaseException):
    pass


class SupervisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.now = 1000.0
        self.clock = lambda: self.now
        self.cfg = Config({"NSE:DEMO": ".05", "NSE:DEMO2": ".05"}, "2000", "1000", "2", "1")
        self.store = Store(self.root / "engine.sqlite")
        self.broker = PaperBroker(self.root / "broker.sqlite", "2000", self.cfg.fees, self.clock)
        self.engine = Engine(self.cfg, self.store, self.broker, self.clock)
        self.broker.set_quote("NSE:DEMO", "100")
        self.broker.set_quote("NSE:DEMO2", "100")

    def tearDown(self):
        self.store.close()
        self.broker.close()
        self.tmp.cleanup()

    def enter(self):
        return self.engine.request_entry("one", "NSE:DEMO", "1000")

    def sells(self):
        return [o for o in self.broker._orders() if o.side == "SELL"]

    def codes(self):
        return {a["code"]: a["severity"] for a in self.engine.health()["alerts"]}

    def gap(self):
        self.enter()
        self.broker.set_quote("NSE:DEMO", "90")
        return self.engine.tick()[0]

    def advance(self, seconds, price=None):
        self.now += seconds
        if price is not None:
            self.broker.set_quote("NSE:DEMO", price)

    def restart(self, cfg=None):
        self.store.close()
        self.store = Store(self.root / "engine.sqlite")
        self.engine = Engine(cfg or self.cfg, self.store, self.broker, self.clock)

    def test_gap_exit_is_cancelled_then_replaced_after_confirmation_and_cooldown(self):
        self.gap()
        self.advance(29, "90")
        self.engine.tick()
        self.assertEqual(self.sells()[0].status, "OPEN")
        self.advance(1, "90")
        self.engine.tick()
        self.assertEqual(self.sells()[0].status, "CANCELLED")
        self.assertEqual(len(self.sells()), 1)
        self.engine.tick()  # confirmation starts the cooldown
        self.advance(4, "90")
        self.engine.tick()
        self.assertEqual(len(self.sells()), 1)
        self.advance(1, "90")
        self.engine.tick()
        closed = self.engine.tick()[0]
        self.assertEqual(closed["state"], "CLOSED")
        self.assertEqual([o.quantity for o in self.sells()], [9, 9])
        self.assertEqual(sum(o.filled for o in self.sells()), 9)
        self.assertTrue(closed["exit_escalated"])

    def test_partial_fill_timeout_sells_only_the_confirmed_residual(self):
        t = self.enter()
        self.broker.max_fill = 3
        self.broker.set_quote("NSE:DEMO", t["target"])
        self.engine.tick()
        self.advance(30)
        self.engine.tick()  # cancel partial child, without requiring a quote
        self.engine.tick()  # confirm cancellation and remaining quantity
        self.broker.max_fill = None
        self.advance(5, "90")
        self.engine.tick()
        self.assertEqual(self.engine.tick()[0]["state"], "CLOSED")
        self.assertEqual([o.quantity for o in self.sells()], [9, 6])
        self.assertEqual(sum(o.filled for o in self.sells()), 9)

    def test_offline_child_uses_broker_placement_time_for_deadline(self):
        self.enter()
        self.broker.set_quote("NSE:DEMO", "90")
        self.advance(60)
        self.restart()
        t = self.engine.tick()[0]  # first discovery is already overdue
        self.assertEqual(t["orders"][-1]["deadline"], 1030)
        self.assertEqual(self.sells()[0].status, "CANCELLED")

    def test_partial_progress_does_not_extend_exit_deadline(self):
        t = self.enter()
        self.broker.max_fill = 3
        self.broker.set_quote("NSE:DEMO", t["target"])
        deadline = self.engine.tick()[0]["orders"][-1]["deadline"]
        self.advance(15, t["target"])
        t = self.engine.tick()[0]
        self.assertEqual(t["orders"][-1]["filled"], 6)
        self.assertEqual(t["orders"][-1]["deadline"], deadline)
        self.advance(15)
        self.engine.tick()
        self.assertEqual(self.sells()[0].status, "CANCELLED")

    def test_fill_racing_timeout_cancellation_never_creates_an_extra_sell(self):
        t = self.gap()
        cancel = self.broker.cancel
        def race(order_id):
            self.broker.set_quote("NSE:DEMO", t["target"])
            return cancel(order_id)
        self.advance(30)
        with patch.object(self.broker, "cancel", race):
            self.engine.tick()
        self.assertEqual(self.engine.tick()[0]["state"], "CLOSED")
        self.assertEqual(len(self.sells()), 1)

    def test_partial_fill_racing_cancellation_reduces_replacement_quantity(self):
        t = self.enter()
        self.broker.max_fill = 3
        self.broker.set_quote("NSE:DEMO", t["target"])
        self.engine.tick()
        cancel = self.broker.cancel
        def race(order_id):
            self.broker.set_quote("NSE:DEMO", t["target"])  # three more fill before cancel
            return cancel(order_id)
        self.advance(30)
        with patch.object(self.broker, "cancel", race):
            self.engine.tick()
        self.engine.tick()
        self.broker.max_fill = None
        self.advance(5, "90")
        self.engine.tick()
        self.assertEqual(self.engine.tick()[0]["state"], "CLOSED")
        self.assertEqual([o.quantity for o in self.sells()], [9, 3])
        self.assertEqual(sum(o.filled for o in self.sells()), 9)

    def test_lost_order_cancel_response_reconciles_before_replacement(self):
        self.gap()
        cancel = self.broker.cancel
        def lost(order_id):
            cancel(order_id)
            raise TimeoutError("cancellation acknowledgement lost")
        self.advance(30)
        with patch.object(self.broker, "cancel", lost) as mocked:
            self.engine.tick()
            self.engine.tick()
        self.assertEqual(len(self.sells()), 1)
        self.advance(5, "90")
        self.engine.tick()
        self.assertEqual(self.engine.tick()[0]["state"], "CLOSED")
        self.assertEqual(sum(o.filled for o in self.sells()), 9)

    def test_pending_regular_sell_is_subject_to_the_same_timeout(self):
        self.cfg = replace(self.cfg, exit_mode="SOFTWARE")
        self.engine = Engine(self.cfg, self.store, self.broker, self.clock)
        self.enter()
        self.engine.tick()
        self.broker.max_fill = 0
        submit = self.broker.submit
        def pending(*args, **kwargs):
            oid = submit(*args, **kwargs)
            with self.broker.db:
                self.broker._save_order(replace(self.broker.order(oid), status="OPEN"))
            return oid
        self.broker.set_quote("NSE:DEMO", "90")
        with patch.object(self.broker, "submit", pending):
            self.engine.tick()
        self.advance(30, "90")
        self.engine.tick()
        self.assertEqual(self.sells()[0].status, "CANCELLED")
        self.assertEqual(len(self.sells()), 1)
        self.engine.tick()
        self.broker.max_fill = None
        self.advance(5, "90")
        self.engine.tick()
        self.assertEqual(self.engine.tick()[0]["state"], "CLOSED")
        self.assertEqual(sum(o.filled for o in self.sells()), 9)

    def test_order_cancel_requests_are_spaced_bounded_and_escalated(self):
        self.gap()
        self.advance(30)
        with patch.object(self.broker, "cancel") as cancel:
            for _ in range(10):
                self.engine.tick()
            self.assertEqual(cancel.call_count, 1)
            for _ in range(5):
                self.advance(5)
                self.engine.tick()
            self.assertEqual(cancel.call_count, 3)
        self.assertEqual(self.codes()["CANCEL_STALLED"], "CRITICAL")
        self.assertEqual(len(self.sells()), 1)
        with self.assertRaises(ValueError):
            self.engine.pause(False)

    def test_late_cancellation_confirmation_recovers_after_escalation(self):
        self.gap()
        self.advance(30)
        with patch.object(self.broker, "cancel"):
            self.engine.tick()
            self.advance(20)
            self.engine.tick()
        self.assertIn("CANCEL_STALLED", self.codes())
        self.broker.cancel(self.sells()[0].id)  # broker eventually completes it
        self.engine.tick()
        self.advance(5, "90")
        self.engine.tick()
        self.assertEqual(self.engine.tick()[0]["state"], "CLOSED")
        self.assertEqual(self.engine.health()["status"], "HEALTHY")

    def test_gtt_cancellation_also_has_a_bounded_deadline(self):
        self.enter()
        self.engine.flatten()
        with patch.object(self.broker, "cancel_protection") as cancel:
            self.engine.tick()
            for _ in range(4):
                self.advance(5)
                self.engine.tick()
        self.assertEqual(cancel.call_count, 3)
        self.assertEqual(self.codes()["CANCEL_STALLED"], "CRITICAL")
        self.assertEqual(self.sells(), [])

    def test_cancel_deadline_and_budget_survive_restart_and_config_changes(self):
        self.gap()
        self.advance(30)
        with patch.object(self.broker, "cancel") as cancel:
            before = self.engine.tick()[0]["orders"][-1]["cancel"]
            self.restart(replace(self.cfg, exit_timeout=900, cancel_timeout=900, max_cancel_attempts=20))
            self.advance(15)
            after = self.engine.tick()[0]["orders"][-1]["cancel"]
            self.assertEqual(cancel.call_count, 1)
        self.assertEqual(before, after)
        self.assertEqual(self.codes()["CANCEL_STALLED"], "CRITICAL")

    def test_crash_after_cancel_intent_consumes_attempt_without_immediate_resend(self):
        self.gap()
        self.advance(30)
        save = self.store.save
        def crash(trade, kind):
            save(trade, kind)
            if kind == "ORDER_CANCEL_INTENT":
                raise SimulatedCrash()
        with patch.object(self.store, "save", crash):
            with self.assertRaises(SimulatedCrash):
                self.engine.tick()
        self.restart()
        with patch.object(self.broker, "cancel", wraps=self.broker.cancel) as cancel:
            self.engine.tick()
            self.assertEqual(cancel.call_count, 0)
            self.advance(5)
            self.engine.tick()
            self.assertEqual(cancel.call_count, 1)
        self.assertEqual(len(self.sells()), 1)

    def test_partial_entry_cancellation_timeout_never_arms_or_sells_early(self):
        self.broker.max_fill = 3
        order = self.broker.order
        with patch.object(self.broker, "order", lambda oid: replace(order(oid), status="OPEN")), \
                patch.object(self.broker, "cancel") as cancel:
            self.enter()
            self.advance(15)
            t = self.engine.tick()[0]
        self.assertEqual(cancel.call_count, 1)
        self.assertEqual(self.codes()["CANCEL_STALLED"], "CRITICAL")
        self.assertNotIn("protection", t)
        self.assertEqual(self.sells(), [])

    def test_rejected_exits_have_cooldown_reasons_and_an_attempt_limit(self):
        self.enter()
        self.engine.flatten()
        self.engine.tick()  # delete GTT
        self.broker.max_fill = 0
        submit = self.broker.submit
        def rejected(*args, **kwargs):
            oid = submit(*args, **kwargs)
            with self.broker.db:
                self.broker._save_order(replace(self.broker.order(oid), status="REJECTED", message="Sell authorization missing"))
            return oid
        with patch.object(self.broker, "submit", rejected):
            self.engine.tick()
            for _ in range(10):
                self.engine.tick()
            self.assertEqual(len(self.sells()), 1)
            self.assertEqual(self.store.all()[0]["orders"][-1]["message"], "Sell authorization missing")
            self.assertIn("EXIT_REJECTED", self.codes())
            for _ in range(2):
                self.advance(5, "100")
                self.engine.tick()
                self.engine.tick()
        self.assertEqual(len(self.sells()), 3)
        self.assertEqual(self.codes()["EXIT_ATTEMPTS_EXHAUSTED"], "CRITICAL")
        self.broker.max_fill = None
        self.engine.retry_exit("one")
        self.advance(5, "100")
        self.engine.tick()
        self.assertEqual(self.engine.tick()[0]["state"], "CLOSED")
        self.assertEqual(len(self.sells()), 4)

    def test_unknown_buy_submission_escalates_without_reposting(self):
        with patch.object(self.broker, "submit", side_effect=TimeoutError()) as submit:
            self.enter()
            self.advance(30)
            for _ in range(4):
                self.engine.tick()
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(self.codes()["SUBMISSION_UNRESOLVED"], "CRITICAL")
        self.assertEqual(self.store.all()[0]["reserved"], "1000")
        self.assertTrue(self.engine.health()["worker"]["fresh"])

    def test_unknown_sell_that_filled_is_recovered_without_duplicate(self):
        self.enter()
        self.engine.flatten()
        self.engine.tick()
        submit = self.broker.submit
        def lost(*args, **kwargs):
            submit(*args, **kwargs)
            raise TimeoutError("lost acknowledgement")
        with patch.object(self.broker, "submit", lost):
            self.engine.tick()
        with patch.object(self.broker, "find_by_tag", side_effect=TimeoutError("order book unavailable")):
            self.advance(30)
            self.engine.tick()
        self.assertEqual(self.codes()["SUBMISSION_UNRESOLVED"], "CRITICAL")
        self.assertEqual(len(self.sells()), 1)
        self.assertEqual(self.engine.tick()[0]["state"], "CLOSED")

    def test_unknown_gtt_registration_escalates_without_repeating_create(self):
        with patch.object(self.broker, "create_protection", side_effect=TimeoutError()) as create:
            self.enter()
            self.advance(30)
            self.engine.tick()
            self.engine.tick()
        self.assertEqual(create.call_count, 1)
        self.assertEqual(self.codes()["GTT_REGISTRATION_UNRESOLVED"], "CRITICAL")
        self.assertEqual(self.sells(), [])

    def test_unresolved_gtt_child_escalates_without_a_competing_sell(self):
        t = self.enter()
        p = self.broker.protection(t["protection"]["id"])
        with self.broker.db:
            self.broker._save_protection(replace(p, status="triggered"))
        self.engine.tick()
        self.advance(30)
        self.engine.tick()
        self.assertEqual(self.codes()["RECONCILIATION_FAILED"], "CRITICAL")
        self.assertEqual(self.sells(), [])

    def test_emergency_exit_uses_broker_pricing_when_local_feed_is_unavailable(self):
        self.enter()
        self.engine.flatten()
        self.engine.tick()
        quote, submit = self.broker.quote, self.broker.submit
        def broker_priced(*args, **kwargs):
            self.assertEqual(args[1], "SELL")
            self.assertIsNone(args[4])  # market, never a stale-price limit
            with patch.object(self.broker, "quote", quote):
                return submit(*args, **kwargs)
        with patch.object(self.broker, "supports_emergency_exit", True), \
                patch.object(self.broker, "quote", side_effect=TimeoutError("quote endpoint down")), \
                patch.object(self.broker, "submit", broker_priced):
            t = self.engine.tick()[0]
        self.assertTrue(t["exit_feed"]["emergency_allowed"])
        self.assertIn("EXIT_FEED_UNAVAILABLE", self.codes())
        self.assertEqual(self.engine.tick()[0]["state"], "CLOSED")
        self.assertEqual(len(self.sells()), 1)

    def test_emergency_policy_can_require_a_fresh_quote(self):
        self.cfg = replace(self.cfg, emergency_exit_policy="WAIT_FOR_FRESH_QUOTE")
        self.engine = Engine(self.cfg, self.store, self.broker, self.clock)
        self.enter()
        self.engine.flatten()
        self.engine.tick()
        with patch.object(self.broker, "supports_emergency_exit", True), \
                patch.object(self.broker, "quote", return_value=Quote(Decimal(100), self.now - 60)):
            t = self.engine.tick()[0]
        self.assertIn("Stale", t["error"])
        self.assertFalse(t["exit_feed"]["emergency_allowed"])
        self.assertEqual(self.sells(), [])

    def test_paper_broker_never_fabricates_a_fill_from_stale_prices(self):
        self.enter()
        self.engine.flatten()
        self.engine.tick()
        self.advance(60)
        t = self.engine.tick()[0]
        self.assertIn("Stale", t["error"])
        self.assertEqual(self.sells(), [])

    def test_emergency_policy_cannot_bypass_unconfirmed_gtt_cancellation(self):
        self.enter()
        self.engine.flatten()
        with patch.object(self.broker, "supports_emergency_exit", True), \
                patch.object(self.broker, "cancel_protection"), \
                patch.object(self.broker, "quote", side_effect=TimeoutError()) as quote:
            self.engine.tick()
            self.advance(20)
            self.engine.tick()
        quote.assert_not_called()
        self.assertEqual(self.sells(), [])
        self.assertIn("CANCEL_STALLED", self.codes())

    def test_emergency_policy_cannot_bypass_inventory_mismatch(self):
        self.enter()
        self.broker.submit("NSE:DEMO", "BUY", 1, "external", Decimal(101))
        self.engine.flatten()
        self.engine.tick()
        with patch.object(self.broker, "supports_emergency_exit", True), \
                patch.object(self.broker, "quote", side_effect=TimeoutError()) as quote:
            t = self.engine.tick()[0]
        self.assertIn("Inventory mismatch", t["error"])
        quote.assert_not_called()
        self.assertEqual(self.sells(), [])

    def test_healthy_heartbeat_cannot_hide_failed_reconciliation(self):
        self.enter()
        with patch.object(self.broker, "protection", side_effect=TimeoutError("broker down")):
            self.engine.tick()
            self.advance(30)
            self.engine.tick()
        health = self.engine.health()
        self.assertTrue(health["worker"]["fresh"])
        self.assertEqual(health["status"], "CRITICAL")
        self.assertEqual(self.codes()["RECONCILIATION_FAILED"], "CRITICAL")

    def test_exception_without_message_still_records_reconciliation_failure(self):
        self.enter()
        with patch.object(self.broker, "protection", side_effect=TimeoutError()):
            t = self.engine.tick()[0]
            self.advance(30)
            self.engine.tick()
        self.assertIn("TimeoutError", t["error"])
        self.assertEqual(self.codes()["RECONCILIATION_FAILED"], "CRITICAL")

    def test_readonly_health_detects_worker_and_protection_staleness_without_api(self):
        self.enter()
        self.engine.tick()
        self.advance(31)
        with patch.object(self.broker, "protection", side_effect=AssertionError("Health must not call broker")):
            codes = self.codes()
        self.assertIn("WORKER_STALE", codes)
        self.assertIn("PROTECTION_CHECK_STALE", codes)
        self.assertIn("RECONCILIATION_STALE", codes)

    def test_health_cli_needs_no_live_credentials_and_returns_failure_status(self):
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps(asdict(self.cfg)))
        args = ["--config", str(config_path), "--db", str(self.root / "engine.sqlite"), "--live", "health"]
        self.enter()
        self.engine.tick()
        for elapsed, expected in ((0, 0), (31, 2)):
            self.advance(elapsed)
            out = io.StringIO()
            with patch.dict("os.environ", {}, clear=True), patch("execution.__main__.time.time", self.clock), redirect_stdout(out):
                result = main(args)
            self.assertEqual(result, expected)
            self.assertEqual(json.loads(out.getvalue())["worker"]["fresh"], expected == 0)

    def test_alert_logging_emits_new_alerts_and_severity_changes_once(self):
        with patch.object(self.broker, "submit", side_effect=TimeoutError("network")):
            self.enter()
        self.engine.tick()
        out = io.StringIO()
        with redirect_stderr(out):
            previous = emit_alerts(self.engine.health(), set())
            first_count = len(out.getvalue().splitlines())
            emit_alerts(self.engine.health(), previous)
            self.assertEqual(len(out.getvalue().splitlines()), first_count)
            self.advance(30)
            self.engine.tick()
            emit_alerts(self.engine.health(), previous)
        events = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertTrue(all(e["event"] == "execution_alert" for e in events))
        self.assertTrue(any(e["code"] == "SUBMISSION_UNRESOLVED" and e["severity"] == "CRITICAL" for e in events))

    def test_stalled_stock_does_not_block_other_broker_exit_reconciliation(self):
        self.enter()
        t = self.engine.request_entry("two", "NSE:DEMO2", "1000")
        self.broker.set_quote("NSE:DEMO", "90")
        self.engine.tick()
        self.advance(30)
        self.broker.set_quote("NSE:DEMO2", t["target"])
        with patch.object(self.broker, "cancel"):
            trades = self.engine.tick()
        self.assertEqual(trades[1]["state"], "CLOSED")
        self.assertEqual(trades[0]["state"], "EXITING")

    def test_timeouts_and_attempt_counts_reject_invalid_configuration(self):
        for field in ("exit_timeout", "cancel_timeout", "submission_timeout", "retry_interval", "health_max_age"):
            for value in (0, -1, float("inf"), float("nan")):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    replace(self.cfg, **{field: value})
        for value in (0, -1, 2.5, True):
            with self.assertRaises(ValueError):
                replace(self.cfg, max_cancel_attempts=value)
        with self.assertRaises(ValueError):
            replace(self.cfg, emergency_exit_policy="UNPROTECTED_MARKET")


if __name__ == "__main__":
    unittest.main()
