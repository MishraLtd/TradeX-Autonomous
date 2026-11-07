from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from execution import Config, Engine
from execution.brokers import PaperBroker
from execution.costs import CostPlan, affordable_quantity, load_plan, trade_fees
from execution.models import dec, exit_levels
from execution.store import Store
from finance_fixtures import instant, profile


class CostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path, self.data = profile(self.root)
        self.now = instant("2026-11-06 10:00")
        self.plan = load_plan(self.path, "TEST", "NSE:DEMO", self.now, 3)

    def test_component_units_and_taxable_base(self):
        parts = self.plan.breakdown("DELIVERY", "BUY", "10000")
        self.assertEqual(parts["stt"], Decimal("10"))
        self.assertEqual(parts["stamp"], Decimal("1.50"))
        self.assertEqual(parts["exchange"], Decimal("0.31"))
        self.assertEqual(parts["sebi"], Decimal("0.01"))
        self.assertEqual(parts["ipft"], Decimal("0.01"))  # rounded up from 0.00001, not 0.01%!
        self.assertEqual(parts["gst"], Decimal("0.07"))
        self.assertEqual(parts["dp"], 0)
        self.assertEqual(self.plan.breakdown("DELIVERY", "SELL", "10000")["dp"], Decimal("15.34"))

    def test_capped_commission_is_per_order_not_exchange_fill(self):
        self.assertEqual(self.plan.breakdown("INTRADAY", "SELL", "1000000")["brokerage"], 20)
        self.assertEqual(self.plan.breakdown("INTRADAY", "SELL", "1000000", 3)["brokerage"], 60)

    def test_target_covers_partial_exits_and_both_holding_scenarios(self):
        for quantity in (3, 11, 3000):
            target, stop = exit_levels("100.03", quantity, ".05", "2", "1", self.plan)
            self.assertEqual(target % Decimal(".05"), 0)
            self.assertLess(stop, Decimal("100.03"))
            shares = [1, 1, quantity - 2]
            for scenario in ("INTRADAY", "DELIVERY"):
                charges = sum(self.plan.breakdown(scenario, "BUY", Decimal("100.03") * quantity).values())
                charges += sum(sum(self.plan.breakdown(scenario, "SELL", target * q).values()) for q in shares)
                self.assertGreaterEqual((target - Decimal("100.03")) * quantity - charges,
                                        Decimal("100.03") * quantity * Decimal(".02"))

    def test_whole_share_sizing_includes_fees_and_fixed_costs(self):
        budget, limit = Decimal("1000"), Decimal("100.20")
        qty = affordable_quantity(budget, limit, self.plan)
        self.assertLessEqual(qty * limit + self.plan.cost("BUY", qty * limit), budget)
        self.assertGreater((qty + 1) * limit + self.plan.cost("BUY", (qty + 1) * limit), budget)
        self.assertEqual(affordable_quantity(Decimal("100"), limit, self.plan), 0)

    def test_mixed_partial_day_trade_keeps_delivery_tax_and_full_order_brokerage(self):
        # Only a small part is sold intraday: full buy-order brokerage can coexist
        # with almost all the delivery STT. Choosing the larger scenario fails.
        delivery = self.plan.breakdown("DELIVERY", "BUY", "100000")
        day = self.plan.breakdown("INTRADAY", "BUY", "100000")
        mixed = sum(delivery.values()) + day["brokerage"] + day["gst"] - delivery["gst"]
        self.assertGreater(mixed, max(sum(delivery.values()), sum(day.values())))
        self.assertGreaterEqual(self.plan.cost("BUY", "100000"), mixed)

    def test_account_date_stock_and_example_are_checked(self):
        for account, stock, at in (("OTHER", "NSE:DEMO", self.now), ("TEST", "BSE:DEMO", self.now),
                                    ("TEST", "NSE:DEMO", instant("2027-01-01 00:00")),
                                    ("TEST", "NSE:DEMO", instant("2026-10-31 23:59"))):
            with self.subTest(account=account, stock=stock, at=at), self.assertRaises(ValueError):
                load_plan(self.path, account, stock, at, 3)
        self.data["example"] = True
        Path(self.path).write_text(json.dumps(self.data))
        with self.assertRaises(ValueError):
            load_plan(self.path, "TEST", "NSE:DEMO", self.now, 3)

    def test_missing_nonfinite_negative_and_excessive_rates_rejected(self):
        for field, value in (("gst_pct", "NaN"), ("dp_sell", "-1"), ("stt_sell_bps", "10000"),
                             ("brokerage_min", True), ("ipft_bps", "Infinity")):
            bad = deepcopy(self.plan.data)
            bad["scenarios"]["DELIVERY"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                CostPlan(bad)
        bad = deepcopy(self.plan.data)
        del bad["scenarios"]["INTRADAY"]
        with self.assertRaises(ValueError):
            CostPlan(bad)

    def test_malformed_profile_is_a_health_alert_without_hiding_exits(self):
        from execution.finance import financial_alerts
        cfg = Config({"NSE:DEMO": ".05"}, "2000", "1000", "2", "1", live_account_id="TEST", cost_profile=self.path)
        store = Store(self.root / "engine.sqlite")
        try:
            store.set("account", "kite:TEST")
            for data in ([], None, {"schema": True}, dict(self.data, stocks=[])):
                Path(self.path).write_text(json.dumps(data))
                self.assertIn("COST_PROFILE_INVALID", {a["code"] for a in financial_alerts(store, cfg, self.now)})
        finally:
            store.close()

    def test_snapshot_survives_changed_and_missing_profile_during_exit(self):
        cfg = Config({"NSE:DEMO": ".05"}, "2000", "1000", "2", "1", live_account_id="TEST", cost_profile=self.path)
        with tempfile.TemporaryDirectory() as state:
            store = Store(Path(state) / "engine.sqlite")
            broker = PaperBroker(Path(state) / "paper.sqlite", "2000", cfg.fees, lambda: self.now)
            try:
                engine = Engine(cfg, store, broker, lambda: self.now)
                broker.set_quote("NSE:DEMO", "100")
                trade = engine.request_entry("one", "NSE:DEMO", "1000")
                snapshot, target = deepcopy(trade["cost_plan"]), trade["target"]
                Path(self.path).unlink()
                engine = Engine(replace(cfg, profit_pct="20"), store, broker, lambda: self.now)
                broker.set_quote("NSE:DEMO", target)
                for _ in range(3):
                    trade = engine.tick()[0]
                self.assertEqual(trade["state"], "CLOSED")
                self.assertEqual(trade["cost_plan"], snapshot)
                self.assertEqual(trade["target"], target)
                expected = sum(trade_fees(trade).cost(o["side"], dec(o["average"]) * o["filled"])
                               for o in trade["orders"] if o["filled"])
                self.assertEqual(dec(trade["estimated_fees"]), expected)
                self.assertNotIn("financials", trade)
            finally:
                broker.close()
                store.close()
