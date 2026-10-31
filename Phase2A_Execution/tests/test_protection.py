"""Outages, lost acknowledgements and races at the broker OCO boundary."""
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from execution import Config, Engine
from execution.brokers import PaperBroker
from execution.protection import Protection, ProtectionSpec
from execution.store import Store


class SimulatedCrash(BaseException):
    """Bypass normal exception recovery to exercise persisted crash windows."""


class ProtectionTests(unittest.TestCase):
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

    def restart(self):
        self.store.close()
        self.store = Store(self.root / "engine.sqlite")
        self.engine = Engine(self.cfg, self.store, self.broker, self.clock)

    def change_remote(self, **changes):
        trigger_id = self.store.all()[0]["protection"]["id"]
        with self.broker.db:
            self.broker._save_protection(replace(self.broker.protection(trigger_id), **changes))

    def test_confirmed_fills_get_verified_protection_immediately(self):
        t = self.enter()
        self.assertEqual(t["protection"]["health"], "ACTIVE")
        p = self.broker.protection(t["protection"]["id"])
        self.assertEqual(p.spec.quantity, 9)
        self.assertEqual(Decimal(p.spec.stop), 99)
        self.assertLess(Decimal(p.spec.stop_limit), Decimal(p.spec.stop))
        self.assertFalse(t["protection"]["released"])

    def test_target_executes_while_engine_is_offline_then_restart_reconciles(self):
        t = self.enter()
        self.store.close()
        # There is no engine loop running at this point.
        self.broker.set_quote("NSE:DEMO", t["target"])
        self.assertEqual(self.sells()[0].filled, 9)
        self.store = Store(self.root / "engine.sqlite")
        self.engine = Engine(self.cfg, self.store, self.broker, self.clock)
        closed = self.engine.tick()[0]
        self.assertEqual(closed["state"], "CLOSED")
        self.assertEqual(closed["reason"], "PROFIT_TARGET")
        self.assertEqual(len(self.sells()), 1)
        self.assertEqual(closed["reserved"], "0")

    def test_stop_executes_without_engine_or_fresh_engine_feed(self):
        t = self.enter()
        self.broker.set_quote("NSE:DEMO", t["stop"])
        self.now += 3600
        # Closure uses confirmed broker fills, not a fresh quote.
        self.assertEqual(self.engine.tick()[0]["state"], "CLOSED")
        self.assertEqual(len(self.sells()), 1)

    def test_gap_below_stop_limit_leaves_one_resting_order(self):
        self.enter()
        self.broker.set_quote("NSE:DEMO", "90")
        for _ in range(3):
            t = self.engine.tick()[0]
        self.assertEqual(t["state"], "EXITING")
        self.assertEqual(self.sells()[0].filled, 0)
        self.assertEqual(len(self.sells()), 1)
        self.broker.set_quote("NSE:DEMO", "99")
        self.assertEqual(self.engine.tick()[0]["state"], "CLOSED")

    def test_oco_never_sends_second_leg_after_stop_then_target(self):
        t = self.enter()
        self.broker.set_quote("NSE:DEMO", t["stop"])
        self.broker.set_quote("NSE:DEMO", t["target"])
        self.assertEqual(len(self.sells()), 1)
        self.assertEqual(self.engine.tick()[0]["reason"], "STOP_LOSS")

    def test_active_protection_does_not_require_polling_quote(self):
        self.enter()
        with patch.object(self.broker, "quote", side_effect=RuntimeError("feed down")):
            t = self.engine.tick()[0]
        self.assertIsNone(t["error"])
        self.assertEqual(t["protection"]["health"], "ACTIVE")

    def test_pause_preserves_broker_protection(self):
        t = self.enter()
        self.engine.pause()
        self.broker.set_quote("NSE:DEMO", t["target"])
        self.assertEqual(self.engine.tick()[0]["state"], "CLOSED")

    def test_restart_config_cannot_downgrade_existing_gtt(self):
        t = self.enter()
        self.engine = Engine(replace(self.cfg, exit_mode="SOFTWARE", profit_pct="10"), self.store, self.broker, self.clock)
        self.engine.tick()
        self.broker.set_quote("NSE:DEMO", t["target"])
        self.assertEqual(self.engine.tick()[0]["state"], "CLOSED")
        self.assertEqual(len(self.sells()), 1)

    def test_lost_create_response_never_creates_duplicate_or_software_sell(self):
        create = self.broker.create_protection
        def lost(*args):
            create(*args)
            raise TimeoutError("response lost")
        with patch.object(self.broker, "create_protection", lost):
            t = self.enter()
        for _ in range(3):
            self.engine.tick()
        self.assertEqual(len(self.broker.protection_ids("NSE:DEMO")), 1)
        self.assertEqual(self.sells(), [])
        self.assertEqual(t["protection"]["id"], "")
        self.assertTrue(self.store.get("paused"))
        with self.assertRaises(ValueError):
            self.engine.request_entry("two", "NSE:DEMO2", "1000")

    def test_lost_create_response_can_be_adopted_after_offline_fill(self):
        create = self.broker.create_protection
        def lost(*args):
            create(*args)
            raise TimeoutError()
        with patch.object(self.broker, "create_protection", lost):
            t = self.enter()
        self.broker.set_quote("NSE:DEMO", t["target"])
        trigger_id = self.broker.protection_ids("NSE:DEMO")[0]
        self.engine.adopt_protection("one", trigger_id)
        self.assertEqual(self.engine.tick()[0]["state"], "CLOSED")
        self.assertEqual(len(self.sells()), 1)

    def test_readback_failure_after_create_keeps_id_for_recovery(self):
        with patch.object(self.broker, "protection", side_effect=TimeoutError()):
            t = self.enter()
        self.assertTrue(t["protection"]["id"])
        self.assertEqual(self.engine.tick()[0]["protection"]["health"], "ACTIVE")
        self.assertEqual(len(self.broker.protection_ids("NSE:DEMO")), 1)

    def test_crash_after_broker_create_before_id_commit_never_reposts(self):
        save = self.store.save
        def crash(trade, kind):
            if kind == "GTT_CREATED":
                raise SimulatedCrash()
            save(trade, kind)
        with patch.object(self.store, "save", crash):
            with self.assertRaises(SimulatedCrash):
                self.enter()
        self.restart()
        t = self.engine.tick()[0]
        self.assertEqual(t["protection"]["id"], "")
        trigger_ids = self.broker.protection_ids("NSE:DEMO")
        self.assertEqual(len(trigger_ids), 1)
        self.assertEqual(self.sells(), [])
        self.engine.adopt_protection("one", trigger_ids[0])
        self.assertEqual(self.engine.tick()[0]["protection"]["health"], "ACTIVE")

    def test_missing_create_requires_explicit_absence_verification_before_exit(self):
        with patch.object(self.broker, "create_protection", side_effect=TimeoutError()):
            self.enter()
        with self.assertRaises(ValueError):
            self.engine.resolve_protection_absent("one")
        self.engine.resolve_protection_absent("one", confirmed_not_created=True)
        self.engine.tick()
        self.assertEqual(self.engine.tick()[0]["state"], "CLOSED")
        self.assertEqual(len(self.sells()), 1)

    def test_existing_unknown_gtt_cannot_be_declared_absent(self):
        create = self.broker.create_protection
        def lost(*args):
            create(*args)
            raise TimeoutError()
        with patch.object(self.broker, "create_protection", lost):
            self.enter()
        with self.assertRaises(ValueError):
            self.engine.resolve_protection_absent("one", confirmed_not_created=True)

    def test_adopt_rejects_wrong_levels(self):
        with patch.object(self.broker, "create_protection", side_effect=TimeoutError()):
            t = self.enter()
        wrong = ProtectionSpec(**dict(t["protection"]["spec"], target="120", target_limit="120"))
        trigger_id = self.broker.create_protection(wrong, Decimal(100))
        with self.assertRaises(ValueError):
            self.engine.adopt_protection("one", trigger_id)

    def test_adopt_rejects_ambiguous_duplicate_protections(self):
        with patch.object(self.broker, "create_protection", side_effect=TimeoutError()):
            t = self.enter()
        spec = ProtectionSpec(**t["protection"]["spec"])
        first = self.broker.create_protection(spec, Decimal(100))
        self.broker.create_protection(spec, Decimal(100))
        with self.assertRaises(ValueError):
            self.engine.adopt_protection("one", first)

    def test_cancel_acknowledgement_is_not_permission_to_sell(self):
        self.enter()
        self.engine.flatten()
        with patch.object(self.broker, "cancel_protection"):
            for _ in range(3):
                t = self.engine.tick()[0]
        self.assertEqual(self.sells(), [])
        self.assertEqual(t["protection"]["health"], "CANCELLING")

    def test_flatten_cancels_gtt_then_sells_after_verified_deletion(self):
        self.enter()
        self.engine.flatten()
        self.engine.tick()
        self.assertEqual(self.sells(), [])
        self.engine.tick()
        self.assertEqual(len(self.sells()), 1)
        self.assertEqual(self.engine.tick()[0]["state"], "CLOSED")

    def test_trigger_firing_during_cancel_is_adopted_not_double_sold(self):
        t = self.enter()
        cancel = self.broker.cancel_protection
        def race(trigger_id):
            self.broker.set_quote("NSE:DEMO", t["target"])
            cancel(trigger_id)
        self.engine.flatten()
        with patch.object(self.broker, "cancel_protection", race):
            self.engine.tick()
        self.assertEqual(self.engine.tick()[0]["state"], "CLOSED")
        self.assertEqual(len(self.sells()), 1)

    def test_lost_delete_response_reconciles_before_direct_sell(self):
        self.enter()
        cancel = self.broker.cancel_protection
        def lost(trigger_id):
            cancel(trigger_id)
            raise TimeoutError()
        self.engine.flatten()
        with patch.object(self.broker, "cancel_protection", lost):
            self.engine.tick()
        self.assertEqual(self.sells(), [])
        self.engine.tick()
        self.assertEqual(self.engine.tick()[0]["state"], "CLOSED")
        self.assertEqual(len(self.sells()), 1)

    def test_crash_after_delete_before_journal_commit_recovers_safely(self):
        self.enter()
        self.engine.flatten()
        save = self.store.save
        def crash(trade, kind):
            if kind == "GTT_CANCEL_SENT":
                raise SimulatedCrash()
            save(trade, kind)
        with patch.object(self.store, "save", crash):
            with self.assertRaises(SimulatedCrash):
                self.engine.tick()
        self.restart()
        self.assertEqual(self.sells(), [])
        self.engine.tick()
        self.assertEqual(self.engine.tick()[0]["state"], "CLOSED")
        self.assertEqual(len(self.sells()), 1)

    def test_partial_fill_during_gtt_cancel_never_resells_filled_shares(self):
        t = self.enter()
        self.broker.max_fill = 3
        cancel = self.broker.cancel_protection
        def race(trigger_id):
            self.broker.set_quote("NSE:DEMO", t["target"])
            cancel(trigger_id)
        self.engine.flatten()
        with patch.object(self.broker, "cancel_protection", race):
            self.engine.tick()
        self.engine.tick()  # adopt the partial child and request its cancellation
        self.assertEqual(len(self.sells()), 1)
        self.assertEqual(self.sells()[0].filled, 3)
        self.broker.max_fill = None
        self.engine.tick()  # verify cancellation before selling the remaining six
        self.assertEqual(self.engine.tick()[0]["state"], "CLOSED")
        self.assertEqual([o.quantity for o in self.sells()], [9, 6])
        self.assertEqual(sum(o.filled for o in self.sells()), 9)

    def test_deleted_gtt_with_pending_child_blocks_fallback(self):
        self.enter()
        self.change_remote(status="deleted", submission_pending=True)
        self.engine.flatten()
        t = self.engine.tick()[0]
        self.assertIn("unresolved", t["error"])
        self.assertFalse(t["protection"]["released"])
        self.assertEqual(self.sells(), [])

    def test_triggered_without_child_result_blocks_competing_exit(self):
        self.enter()
        self.change_remote(status="triggered")
        self.engine.flatten()
        t = self.engine.tick()[0]
        self.assertIn("unresolved", t["error"])
        self.assertEqual(t["protection"]["health"], "UNCONFIRMED")
        self.assertEqual(self.sells(), [])

    def test_partial_gtt_fill_keeps_only_original_child_until_complete(self):
        t = self.enter()
        self.broker.max_fill = 3
        for _ in range(3):
            self.broker.set_quote("NSE:DEMO", t["target"])
            result = self.engine.tick()[0]
            self.assertEqual(len(self.sells()), 1)
        self.assertEqual(result["state"], "CLOSED")

    def test_cancelled_partial_child_only_sells_remaining_shares(self):
        t = self.enter()
        self.broker.max_fill = 3
        self.broker.set_quote("NSE:DEMO", t["target"])
        self.engine.tick()
        self.broker.cancel(self.sells()[0].id)
        self.broker.max_fill = None
        self.engine.tick()
        result = self.engine.tick()[0]
        self.assertEqual(result["state"], "CLOSED")
        self.assertEqual([o.quantity for o in self.sells()], [9, 6])
        self.assertEqual(sum(o.filled for o in self.sells()), 9)

    def test_flatten_cancels_partial_gtt_child_before_replacement(self):
        t = self.enter()
        self.broker.max_fill = 3
        self.broker.set_quote("NSE:DEMO", t["target"])
        self.engine.flatten()
        self.engine.tick()
        self.assertEqual(len(self.sells()), 1)
        self.assertEqual(self.sells()[0].status, "CANCELLED")
        self.broker.max_fill = None
        self.engine.tick()
        self.assertEqual(self.engine.tick()[0]["state"], "CLOSED")
        self.assertEqual([o.quantity for o in self.sells()], [9, 6])

    def test_lost_protection_pauses_and_closes_confirmed_exposure(self):
        for status in ("expired", "cancelled", "deleted", "rejected"):
            with self.subTest(status=status):
                # Independent fixture for each broker terminal status.
                self.tearDown()
                self.setUp()
                self.enter()
                self.change_remote(status=status)
                self.engine.tick()
                t = self.engine.tick()[0]
                self.assertTrue(self.store.get("paused"))
                self.assertEqual(t["state"], "CLOSED")
                self.assertEqual(t["reason"], "PROTECTION_LOST")

    def test_disabled_gtt_is_deleted_before_fallback(self):
        self.enter()
        self.change_remote(status="disabled")
        self.engine.tick()
        self.assertEqual(self.sells(), [])
        self.engine.tick()
        self.assertEqual(self.engine.tick()[0]["state"], "CLOSED")

    def test_explicit_gtt_submission_failure_allows_bounded_exit(self):
        self.enter()
        self.change_remote(status="triggered", triggered_leg="STOP_LOSS", submission_failed=True)
        self.engine.tick()
        t = self.engine.tick()[0]
        self.assertEqual(t["state"], "CLOSED")
        self.assertEqual(len(self.sells()), 1)

    def test_missing_or_modified_broker_record_blocks_fallback(self):
        t = self.enter()
        old = self.broker.protection(t["protection"]["id"])
        self.change_remote(spec=replace(old.spec, quantity=8))
        self.engine.flatten()
        t = self.engine.tick()[0]
        self.assertIn("mismatch", t["error"])
        self.assertEqual(self.sells(), [])

    def test_child_with_wrong_stock_is_never_adopted(self):
        t = self.enter()
        self.broker.set_quote("NSE:DEMO", t["target"])
        order = self.broker.order
        with patch.object(self.broker, "order", lambda oid: replace(order(oid), stock="NSE:OTHER")):
            result = self.engine.tick()[0]
        self.assertIn("child identity mismatch", result["error"])
        self.assertEqual(len(result["orders"]), 1)
        self.assertEqual(self.engine.tick()[0]["state"], "CLOSED")
        self.assertEqual(len(self.sells()), 1)

    def test_known_child_association_cannot_disappear(self):
        t = self.enter()
        self.broker.max_fill = 3
        self.broker.set_quote("NSE:DEMO", t["target"])
        self.engine.tick()
        self.change_remote(child_ids=())
        self.engine.flatten()
        t = self.engine.tick()[0]
        self.assertIn("association disappeared", t["error"])
        self.assertEqual(len(self.sells()), 1)

    def test_foreign_active_gtt_prevents_buy(self):
        self.broker.create_protection(ProtectionSpec("NSE:DEMO", 1, "99", "102", "98", "102"), Decimal(100))
        with self.assertRaises(ValueError):
            self.enter()
        self.assertEqual(self.broker._orders(), [])

    def test_paper_broker_restart_keeps_triggers(self):
        t = self.enter()
        self.broker.close()
        self.broker = PaperBroker(self.root / "broker.sqlite", fees=self.cfg.fees, clock=self.clock)
        self.broker.set_quote("NSE:DEMO", t["target"])
        self.engine = Engine(self.cfg, self.store, self.broker, self.clock)
        self.assertEqual(self.engine.tick()[0]["state"], "CLOSED")

    def test_live_broker_cannot_use_software_only_configuration(self):
        with patch.object(self.broker, "protection_required", True):
            with self.assertRaises(ValueError):
                Engine(replace(self.cfg, exit_mode="SOFTWARE"), self.store, self.broker, self.clock)

    def test_existing_unprotected_trade_is_upgraded_on_restart(self):
        old = Engine(replace(self.cfg, exit_mode="SOFTWARE"), self.store, self.broker, self.clock)
        old.request_entry("one", "NSE:DEMO", "1000")
        t = old.tick()[0]
        del t["exit_mode"]  # pre-upgrade journal format
        self.store.save(t, "LEGACY_FIXTURE")
        result = self.engine.tick()[0]
        self.assertEqual(result["protection"]["health"], "ACTIVE")

    def test_partial_entry_only_protects_confirmed_filled_quantity(self):
        self.broker.max_fill = 3
        t = self.enter()
        self.assertEqual(t["protection"]["spec"]["quantity"], 3)

    def test_pending_entry_waits_for_cancel_confirmation_before_arming(self):
        self.broker.max_fill = 3
        get = self.broker.order
        with patch.object(self.broker, "order", lambda oid: replace(get(oid), status="OPEN")):
            t = self.enter()
            self.assertNotIn("protection", t)
        self.assertEqual(self.engine.tick()[0]["protection"]["spec"]["quantity"], 3)


if __name__ == "__main__":
    unittest.main()
