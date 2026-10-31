from datetime import datetime, timezone
from decimal import Decimal
import inspect
import unittest
from unittest.mock import Mock
from execution.kite import KiteBroker
from execution.protection import ProtectionSpec


class KiteTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.client.profile.return_value = {"user_id": "TEST"}
        self.client.instruments.return_value = [dict(tradingsymbol="DEMO", instrument_type="EQ", lot_size=1, tick_size=.05)]
        self.client.holdings.return_value = []
        self.client.positions.return_value = {"net": []}
        self.client.orders.return_value = []
        self.client.get_gtts.return_value = []
        self.client.place_order.return_value = "123"
        self.broker = KiteBroker(self.client, {"NSE:DEMO": ".05"}, live=True, min_interval=0)

    def test_live_requires_explicit_enable(self):
        with self.assertRaises(ValueError):
            KiteBroker(self.client, {"NSE:DEMO": ".05"})

    def test_limit_entry_and_protected_market_exit(self):
        self.broker.submit("NSE:DEMO", "BUY", 3, "tag", Decimal("100.05"))
        buy = self.client.place_order.call_args.kwargs
        self.assertEqual((buy["order_type"], buy["validity"], buy["product"]), ("LIMIT", "IOC", "CNC"))
        self.assertEqual(buy["price"], 100.05)
        self.broker.submit("NSE:DEMO", "SELL", 3, "tag2")
        sell = self.client.place_order.call_args.kwargs
        self.assertEqual((sell["order_type"], sell["market_protection"]), ("MARKET", -1))

    def test_tick_mismatch_fails_startup(self):
        with self.assertRaises(ValueError):
            KiteBroker(self.client, {"NSE:DEMO": ".01"}, live=True, min_interval=0)

    def test_no_leverage_in_cash(self):
        self.client.margins.return_value = {"enabled": True, "net": 80, "available": {"cash": 100, "live_balance": 80, "collateral": 100000}}
        self.assertEqual(self.broker.available_cash(), 80)

    def test_quote_uses_actual_trade_time_not_receive_time(self):
        self.client.quote.return_value = {"NSE:DEMO": {"last_price": 100,
            "timestamp": "2026-10-30 15:20:00", "last_trade_time": "2026-10-30 15:19:00"}}
        quote = self.broker.quote("NSE:DEMO")
        self.assertEqual(quote.timestamp, datetime(2026, 10, 30, 9, 49, tzinfo=timezone.utc).timestamp())
        self.client.quote.return_value = {}
        with self.assertRaises(ValueError):
            self.broker.quote("NSE:DEMO")

    def test_holdings_and_todays_positions_reconciled(self):
        self.client.holdings.return_value = [dict(exchange="NSE", tradingsymbol="DEMO", product="CNC",
            quantity=7, t1_quantity=2, used_quantity=3, opening_quantity=12)]
        self.client.positions.return_value = {"net": [dict(exchange="NSE", tradingsymbol="DEMO", product="CNC", quantity=-3)]}
        self.broker.assert_inventory("NSE:DEMO", 9, [])
        with self.assertRaises(ValueError):
            self.broker.assert_inventory("NSE:DEMO", 12, [])

    def test_duplicate_tags_never_silently_adopted(self):
        row = dict(order_id="1", tag="tag", exchange="NSE", tradingsymbol="DEMO", transaction_type="BUY",
                   quantity=1, filled_quantity=1, average_price=100, status="COMPLETE")
        self.client.orders.return_value = [row, dict(row, order_id="2")]
        with self.assertRaises(ValueError):
            self.broker.find_by_tag("tag")

    def test_external_pending_order_blocks_managed_symbol(self):
        row = dict(order_id="external", tag="", exchange="NSE", tradingsymbol="DEMO", transaction_type="BUY",
                   quantity=1, filled_quantity=0, average_price=0, status="OPEN")
        self.client.orders.return_value = [row]
        with self.assertRaises(ValueError):
            self.broker.assert_inventory("NSE:DEMO", 0, [])

    def test_sdk_supports_market_protection_when_installed(self):
        try:
            from kiteconnect import KiteConnect
        except ImportError:
            self.skipTest("Optional live SDK not installed")
        self.assertIn("market_protection", inspect.signature(KiteConnect.place_order).parameters)

    def gtt_row(self, status="active", result=None):
        spec = ProtectionSpec("NSE:DEMO", 3, "99", "103", "98.50", "103")
        return dict(id=123, type="two-leg", status=status,
                    condition=dict(exchange="NSE", tradingsymbol="DEMO", trigger_values=[99, 103]),
                    orders=[dict(exchange="NSE", tradingsymbol="DEMO", transaction_type="SELL",
                                 quantity=3, product="CNC", order_type="LIMIT", price=float(price),
                                 result=result if index == 0 else None)
                            for index, price in enumerate((spec.stop_limit, spec.target_limit))])

    def test_gtt_payload_is_two_cnc_sell_limits_in_stop_target_order(self):
        spec = ProtectionSpec("NSE:DEMO", 3, "99", "103", "98.50", "103")
        self.client.place_gtt.return_value = {"trigger_id": 123}
        self.assertEqual(self.broker.create_protection(spec, Decimal(100)), "123")
        args = self.client.place_gtt.call_args.kwargs
        self.assertEqual(args["trigger_type"], "two-leg")
        self.assertEqual(args["trigger_values"], [99, 103])
        self.assertEqual([o["price"] for o in args["orders"]], [98.5, 103])
        self.assertTrue(all(o["product"] == "CNC" and o["order_type"] == "LIMIT" and
                            o["transaction_type"] == "SELL" and o["quantity"] == 3 for o in args["orders"]))
        self.assertFalse(any("tag" in o for o in args["orders"]))

    def test_gtt_payload_binds_to_installed_sdk(self):
        try:
            from kiteconnect import KiteConnect
        except ImportError:
            self.skipTest("Optional live SDK not installed")
        client = KiteConnect(api_key="offline-test-only")
        client._post = Mock(return_value={"trigger_id": 123})
        spec = ProtectionSpec("NSE:DEMO", 3, "99", "103", "98.50", "103")
        self.broker.client = client
        self.assertEqual(self.broker.create_protection(spec, Decimal(100)), "123")
        self.assertEqual(client._post.call_args.args[0], "gtt.place")

    def test_decode_active_gtt_validates_all_order_fields(self):
        self.client.get_gtt.return_value = self.gtt_row()
        p = self.broker.protection("123")
        self.assertEqual(p.status, "active")
        self.assertFalse(p.released)
        for field, bad in (("quantity", 4), ("product", "MIS"), ("transaction_type", "BUY"), ("order_type", "MARKET")):
            with self.subTest(field=field):
                row = self.gtt_row()
                row["orders"][1][field] = bad
                self.client.get_gtt.return_value = row
                with self.assertRaises(ValueError):
                    self.broker.protection("123")

    def test_decode_triggered_child_uses_nested_order_result(self):
        self.client.get_gtt.return_value = self.gtt_row("triggered", {"order_result": {"status": "success", "order_id": "child-1"}})
        p = self.broker.protection("123")
        self.assertEqual(p.child_ids, ("child-1",))
        self.assertEqual(p.triggered_leg, "STOP_LOSS")
        self.assertTrue(p.released)

    def test_decode_explicit_failure_vs_pending_result(self):
        self.client.get_gtt.return_value = self.gtt_row("triggered", {"order_result": {"status": "failed", "order_id": ""}})
        self.assertTrue(self.broker.protection("123").submission_failed)
        self.assertTrue(self.broker.protection("123").released)
        self.client.get_gtt.return_value = self.gtt_row("triggered", {"order_result": {"status": "pending"}})
        self.assertTrue(self.broker.protection("123").submission_pending)
        self.assertFalse(self.broker.protection("123").released)

    def test_deleted_with_unresolved_child_result_is_not_released(self):
        self.client.get_gtt.return_value = self.gtt_row("deleted", {"order_result": {"status": "pending"}})
        self.assertFalse(self.broker.protection("123").released)

    def test_active_gtt_with_child_result_is_inconsistent(self):
        self.client.get_gtt.return_value = self.gtt_row("active", {"order_result": {"order_id": "child"}})
        with self.assertRaises(ValueError):
            self.broker.protection("123")

    def test_both_oco_legs_with_results_are_rejected(self):
        row = self.gtt_row("triggered", {"order_result": {"order_id": "first"}})
        row["orders"][1]["result"] = {"order_result": {"order_id": "second"}}
        self.client.get_gtt.return_value = row
        with self.assertRaises(ValueError):
            self.broker.protection("123")

    def test_foreign_single_leg_gtt_is_also_detected(self):
        row = self.gtt_row()
        row["type"] = "single"
        row["orders"] = row["orders"][:1]
        self.client.get_gtts.return_value = [row]
        self.assertEqual(self.broker.protection_ids("NSE:DEMO"), ["123"])
        with self.assertRaises(ValueError):
            self.broker.assert_no_protection("NSE:DEMO", [])
        self.broker.assert_no_protection("NSE:DEMO", ["123"])

    def test_gtt_delete_targets_only_owned_identifier(self):
        self.broker.cancel_protection("123")
        self.client.delete_gtt.assert_called_once_with("123")

    def test_order_preserves_broker_placement_time_and_rejection_reason(self):
        row = dict(order_id="child", tag=None, exchange="NSE", tradingsymbol="DEMO", transaction_type="SELL",
                   quantity=3, filled_quantity=0, average_price=0, status="REJECTED",
                   order_timestamp="2026-10-30 15:20:00", status_message="Rejected", status_message_raw="Sell authorization missing")
        self.client.order_history.return_value = [row]
        order = self.broker.order("child")
        order.check()
        self.assertEqual(order.placed_at, datetime(2026, 10, 30, 9, 50, tzinfo=timezone.utc).timestamp())
        self.assertEqual(order.message, "Sell authorization missing")
        row["order_timestamp"] = datetime(2026, 10, 30, 9, 50, tzinfo=timezone.utc)
        self.assertEqual(self.broker.order("child").placed_at, order.placed_at)

    def test_emergency_sell_uses_broker_protection_without_local_quote(self):
        self.client.quote.side_effect = AssertionError("Emergency adapter submission must not need a local quote")
        self.assertTrue(self.broker.supports_emergency_exit)
        self.broker.submit("NSE:DEMO", "SELL", 3, "tag")
        args = self.client.place_order.call_args.kwargs
        self.assertEqual((args["order_type"], args["validity"], args["market_protection"]), ("MARKET", "IOC", -1))
        self.assertNotIn("price", args)


if __name__ == "__main__":
    unittest.main()
