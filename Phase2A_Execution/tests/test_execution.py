from dataclasses import asdict, replace
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from execution import Config, Engine, Fees, Order, Quote
from execution.brokers import PaperBroker
from execution.models import dec, exit_levels
from execution.store import Store


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.now = 1000.0
        self.clock = lambda: self.now
        self.cfg = Config({"NSE:DEMO": "0.05", "NSE:DEMO2": "0.05"}, "2000", "1000", "2", "1", exit_mode="SOFTWARE")
        self.store = Store(self.root / "engine.sqlite")
        self.broker = PaperBroker(self.root / "broker.sqlite", "2000", self.cfg.fees, self.clock)
        self.engine = Engine(self.cfg, self.store, self.broker, self.clock)
        self.broker.set_quote("NSE:DEMO", "100")
        self.broker.set_quote("NSE:DEMO2", "100")

    def tearDown(self):
        self.broker.close()
        self.store.close()
        self.tmp.cleanup()

    def enter(self):
        self.engine.request_entry("one", "NSE:DEMO", "1000")
        return self.engine.tick()[0]

    def close_at(self, price):
        self.broker.set_quote("NSE:DEMO", price)
        self.engine.tick()
        return self.engine.tick()[0]

    def test_profit_target_closes_and_meets_estimated_net_goal(self):
        t = self.enter()
        closed = self.close_at(t["target"])
        self.assertEqual(closed["state"], "CLOSED")
        self.assertEqual(closed["reason"], "PROFIT_TARGET")
        self.assertGreaterEqual(dec(closed["estimated_net_pnl"]), Decimal(100) * t["filled_quantity"] * Decimal(".02"))
        self.assertEqual(len(self.broker._orders()), 2)

    def test_stop_can_gap_below_trigger(self):
        self.enter()
        closed = self.close_at("90")
        self.assertEqual(closed["reason"], "STOP_LOSS")
        self.assertLess(dec(closed["estimated_net_pnl"]), 0)
        self.assertEqual(dec(closed["orders"][-1]["average"]), 90)

    def test_actual_fill_not_requested_limit_sets_exits(self):
        t = self.enter()
        self.assertEqual(dec(t["entry_price"]), 100)
        self.assertEqual(dec(t["stop"]), 99)

    def test_tick_rounding_and_fixed_fees(self):
        fees = Fees(buy_fixed="3", sell_fixed="20")
        target, stop = exit_levels("100.03", 2, ".05", "2", "1", fees)
        net = (target-Decimal("100.03"))*2-fees.cost("BUY", Decimal("200.06"))-fees.cost("SELL", target*2)
        self.assertGreaterEqual(net, Decimal("200.06")*Decimal(".02"))
        self.assertEqual(target % Decimal(".05"), 0)
        self.assertLess(stop, Decimal("100.03"))

    def test_unapproved_symbol_rejected_before_submission(self):
        with self.assertRaises(ValueError):
            self.engine.request_entry("one", "NSE:OTHER", "1000")
        self.assertEqual(self.broker._orders(), [])

    def test_duplicate_request_id_is_idempotent(self):
        self.enter()
        self.engine.request_entry("one", "NSE:DEMO", "1000")
        self.assertEqual(len(self.broker._orders()), 1)
        with self.assertRaises(ValueError):
            self.engine.request_entry("one", "NSE:DEMO", "900")

    def test_duplicate_stock_is_rejected(self):
        self.enter()
        with self.assertRaises(ValueError):
            self.engine.request_entry("two", "NSE:DEMO", "1000")

    def test_capital_is_reserved_across_requests(self):
        cfg = replace(self.cfg, capital_limit="1500")
        engine = Engine(cfg, self.store, self.broker, self.clock)
        engine.request_entry("one", "NSE:DEMO", "1000")
        with self.assertRaises(ValueError):
            engine.request_entry("two", "NSE:DEMO2", "1000")

    def test_partial_buy_protects_only_filled_quantity(self):
        self.broker.max_fill = 3
        t = self.enter()
        self.assertEqual(t["filled_quantity"], 3)
        closed = self.close_at(t["target"])
        self.assertEqual(closed["state"], "CLOSED")
        self.assertEqual(closed["orders"][-1]["quantity"], 3)

    def test_partial_exit_retries_remaining_only(self):
        t = self.enter()
        self.broker.max_fill = 3
        self.broker.set_quote("NSE:DEMO", t["target"])
        for _ in range(5):
            result = self.engine.tick()[0]
        self.assertEqual(result["state"], "CLOSED")
        self.assertEqual([o.quantity for o in self.broker._orders() if o.side == "SELL"], [9, 6, 3])

    def test_stale_quotes_pause_entries_but_recover_exits(self):
        t = self.enter()
        self.now += 60
        error = self.engine.tick()[0]
        self.assertIn("Stale", error["error"])
        self.assertTrue(self.store.get("paused"))
        closed = self.close_at(t["target"])
        self.assertEqual(closed["state"], "CLOSED")

    def test_stale_entry_never_submits(self):
        self.now += 60
        with self.assertRaises(ValueError):
            self.engine.request_entry("one", "NSE:DEMO", "1000")
        self.assertEqual(self.broker._orders(), [])

    def test_restart_preserves_levels_and_position(self):
        t = self.enter()
        self.store.close()
        self.store = Store(self.root / "engine.sqlite")
        # Changing policy applies only to future entries.
        self.engine = Engine(replace(self.cfg, profit_pct="10"), self.store, self.broker, self.clock)
        closed = self.close_at(t["target"])
        self.assertEqual(closed["state"], "CLOSED")
        self.assertEqual(closed["target"], t["target"])

    def test_timeout_after_acceptance_adopts_existing_order(self):
        submit = self.broker.submit
        def lost_response(*args, **kwargs):
            submit(*args, **kwargs)
            raise TimeoutError()
        with patch.object(self.broker, "submit", lost_response):
            self.engine.request_entry("one", "NSE:DEMO", "1000")
        self.assertEqual(self.engine.tick()[0]["state"], "OPEN")
        self.assertEqual(len(self.broker._orders()), 1)

    def test_unknown_submission_is_never_blindly_retried(self):
        with patch.object(self.broker, "submit", side_effect=TimeoutError()):
            self.engine.request_entry("one", "NSE:DEMO", "1000")
        for _ in range(3):
            result = self.engine.tick()[0]
        self.assertIn("Unknown submission", result["error"])
        self.assertEqual(self.broker._orders(), [])
        self.assertEqual(result["reserved"], "1000")

    def test_exit_timeout_after_acceptance_does_not_oversell(self):
        t = self.enter()
        submit = self.broker.submit
        def lost_response(*args, **kwargs):
            submit(*args, **kwargs)
            raise TimeoutError()
        self.broker.set_quote("NSE:DEMO", t["target"])
        with patch.object(self.broker, "submit", lost_response):
            self.engine.tick()
        self.assertEqual(self.engine.tick()[0]["state"], "CLOSED")
        self.assertEqual(len(self.broker._orders()), 2)

    def test_pause_still_protects_open_positions(self):
        t = self.enter()
        self.engine.pause()
        with self.assertRaises(ValueError):
            self.engine.request_entry("two", "NSE:DEMO2", "1000")
        self.assertEqual(self.close_at(t["stop"])["state"], "CLOSED")

    def test_flatten_requests_exit(self):
        self.enter()
        self.engine.flatten()
        self.engine.tick()
        self.assertEqual(self.engine.tick()[0]["reason"], "OPERATOR_EXIT")

    def test_one_pending_exit_blocks_second_sell(self):
        t = self.enter()
        self.broker.set_quote("NSE:DEMO", t["target"])
        self.engine.tick()
        real = self.broker.order
        def pending(order_id):
            o = real(order_id)
            return replace(o, status="OPEN", filled=0, average=Decimal(0)) if o.side == "SELL" else o
        with patch.object(self.broker, "order", pending):
            for _ in range(3):
                self.engine.tick()
        self.assertEqual(len(self.broker._orders()), 2)

    def test_open_partial_entry_cancel_confirmed_before_exit(self):
        self.broker.max_fill = 3
        self.engine.request_entry("one", "NSE:DEMO", "1000")
        real = self.broker.order
        with patch.object(self.broker, "order", lambda oid: replace(real(oid), status="OPEN")), patch.object(self.broker, "cancel") as cancel:
            self.engine.tick()
            cancel.assert_called_once()
            self.assertEqual(len(self.broker._orders()), 1)
        self.assertEqual(self.engine.tick()[0]["filled_quantity"], 3)

    def test_external_inventory_change_blocks_sell(self):
        t = self.enter()
        self.broker.submit("NSE:DEMO", "SELL", 1, "external")
        self.broker.set_quote("NSE:DEMO", t["target"])
        result = self.engine.tick()[0]
        self.assertIn("Inventory mismatch", result["error"])
        self.assertEqual(len(self.broker._orders()), 2)

    def test_failed_stock_does_not_block_other_exits(self):
        self.enter()
        self.engine.request_entry("two", "NSE:DEMO2", "1000")
        second = self.engine.tick()[1]
        self.now += 60
        self.broker.set_quote("NSE:DEMO2", second["target"])
        self.engine.tick()
        result = self.engine.tick()
        self.assertIsNotNone(result[0]["error"])
        self.assertEqual(result[1]["state"], "CLOSED")

    def test_exit_attempts_bounded_and_operator_retry(self):
        t = self.enter()
        self.broker.max_fill = 0
        self.broker.set_quote("NSE:DEMO", t["target"])
        for _ in range(5):
            result = self.engine.tick()[0]
        self.assertIn("exhausted", result["error"])
        self.assertEqual(len(self.broker._orders()), 4)
        self.broker.max_fill = None
        self.engine.retry_exit("one")
        self.engine.tick()
        self.assertEqual(self.engine.tick()[0]["state"], "CLOSED")

    def test_process_lock_prevents_overlapping_operations(self):
        other = Store(self.root / "engine.sqlite")
        try:
            with self.store.locked():
                with self.assertRaises(RuntimeError):
                    with other.locked():
                        self.fail("Lock unexpectedly acquired")
        finally:
            other.close()

    def test_account_identity_cannot_change(self):
        with patch.object(self.broker, "identity", "kite:other"):
            with self.assertRaises(ValueError):
                Engine(self.cfg, self.store, self.broker)

    def test_invalid_numbers_and_configuration(self):
        for value in ("NaN", "Infinity", "-1", "0"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.engine.request_entry("one", "NSE:DEMO", value)
        with self.assertRaises(ValueError):
            replace(self.cfg, stop_pct="100")
        with self.assertRaises(ValueError):
            replace(self.cfg, max_positions=1.5)

    def test_no_fill_releases_capital(self):
        self.broker.slippage = Decimal(100)
        self.engine.request_entry("one", "NSE:DEMO", "1000")
        t = self.engine.tick()[0]
        self.assertEqual(t["state"], "NO_FILL")
        self.assertEqual(t["reserved"], "0")

    def test_paper_broker_survives_independent_restart(self):
        self.enter()
        self.broker.close()
        self.broker = PaperBroker(self.root / "broker.sqlite", fees=self.cfg.fees, clock=self.clock)
        self.engine = Engine(self.cfg, self.store, self.broker, self.clock)
        t = self.engine.tick()[0]
        self.assertEqual(t["state"], "OPEN")
        self.assertEqual(self.close_at(t["target"])["state"], "CLOSED")

    def test_unknown_submission_requires_manual_attestation_to_release(self):
        with patch.object(self.broker, "submit", side_effect=TimeoutError()):
            t = self.engine.request_entry("one", "NSE:DEMO", "1000")
        tag = t["orders"][0]["tag"]
        with self.assertRaises(ValueError):
            self.engine.resolve_absent("one", tag)
        self.engine.resolve_absent("one", tag, confirmed_not_placed=True)
        self.assertEqual(self.engine.tick()[0]["state"], "NO_FILL")

    def test_cannot_mark_existing_broker_order_absent(self):
        submit = self.broker.submit
        def lost_response(*args, **kwargs):
            submit(*args, **kwargs)
            raise TimeoutError()
        with patch.object(self.broker, "submit", lost_response):
            t = self.engine.request_entry("one", "NSE:DEMO", "1000")
        with self.assertRaises(ValueError):
            self.engine.resolve_absent("one", t["orders"][0]["tag"], confirmed_not_placed=True)


if __name__ == "__main__":
    unittest.main()
