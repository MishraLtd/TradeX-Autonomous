from datetime import datetime, timezone
from decimal import Decimal
import inspect
import unittest
from unittest.mock import Mock
from execution.kite import KiteBroker


class KiteTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.client.profile.return_value = {"user_id": "TEST"}
        self.client.instruments.return_value = [dict(tradingsymbol="DEMO", instrument_type="EQ", lot_size=1, tick_size=.05)]
        self.client.holdings.return_value = []
        self.client.positions.return_value = {"net": []}
        self.client.orders.return_value = []
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
        self.client.margins.return_value = {"available": {"cash": 100, "live_balance": 80, "collateral": 100000}}
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
        self.client.holdings.return_value = [dict(exchange="NSE", tradingsymbol="DEMO", quantity=10, t1_quantity=2)]
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


if __name__ == "__main__":
    unittest.main()
