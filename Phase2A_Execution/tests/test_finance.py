from contextlib import redirect_stdout
from copy import deepcopy
from dataclasses import asdict, replace
from decimal import Decimal
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from execution import Config, Engine
from execution.__main__ import main
from execution.brokers import PaperBroker
from execution.finance import allocate, financial_alerts, read_json, reconcile, record_report, template
from execution.models import dec, dumps
from execution.store import Store
from finance_fixtures import instant, profile, trade_and_evidence


class FinanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path, self.profile = profile(self.root)
        self.cfg = Config({"NSE:DEMO": ".05", "NSE:OTHER": ".05"}, "2000", "1500", "2", "1",
                          live_account_id="TEST", cost_profile=self.path)
        self.trade, self.evidence = trade_and_evidence(self.root)
        self.now = instant("2026-11-08 10:00")
        self.store = Store(self.root / "engine.sqlite")
        self.store.set("account", "kite:TEST")
        self.store.save(self.trade, "SYNTHETIC")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def report(self, evidence=None, trades=None):
        return reconcile(evidence if evidence is not None else self.evidence,
                         trades if trades is not None else self.store.all(), "kite:TEST", self.cfg, self.root, self.now)

    def record(self, report=None):
        report = report or self.report()
        with self.store.locked():
            record_report(self.store, report)
        return report

    def codes(self, report):
        return {x["code"] for x in report["issues"]}

    def test_partial_fills_retry_orders_and_actual_net_reconcile(self):
        report = self.report()
        self.assertEqual(report["status"], "MATCHED", report["issues"])
        self.assertFalse(report["broker_authenticated"])
        self.assertEqual(dec(report["totals"]["actual_costs"]), 20)
        self.assertEqual(dec(report["totals"]["realized_gross"]), 70)
        self.assertEqual(dec(report["trades"]["one"]["actual_net_pnl"]), 50)
        self.assertEqual(set(report["covered_orders"]), {"B1", "S1", "S2"})
        self.assertTrue(report["warnings"])  # legacy estimate was lower

    def test_every_paise_allocated_deterministically(self):
        result = allocate(Decimal("0.02"), {"C": Decimal(1), "A": Decimal(1), "B": Decimal(1)})
        self.assertEqual(result, {"A": Decimal(".01"), "B": Decimal(".01"), "C": Decimal("0")})
        self.assertEqual(sum(result.values()), Decimal(".02"))

    def test_missing_fill_is_a_discrepancy_not_zero_cost(self):
        self.evidence["fills"].pop()
        report = self.report()
        self.assertNotEqual(report["status"], "MATCHED")
        self.assertIn("FILL_QUANTITY_MISMATCH", self.codes(report))
        self.assertFalse(report["financial_match"])

    def test_duplicate_exchange_fill_rejected(self):
        self.evidence["fills"].append(deepcopy(self.evidence["fills"][0]))
        self.assertEqual(self.report()["status"], "INVALID")

    def test_unowned_and_wrong_identity_fill_block(self):
        for update, code in ((dict(order_id="external"), "UNOWNED_FILL"),
                             (dict(stock="BSE:DEMO"), "FILL_IDENTITY_MISMATCH"),
                             (dict(side="SELL"), "FILL_IDENTITY_MISMATCH")):
            e = deepcopy(self.evidence)
            e["fills"][0].update(update)
            with self.subTest(update=update):
                report = self.report(e)
                self.assertIn(code, self.codes(report))
                self.assertNotEqual(report["status"], "MATCHED")

    def test_average_rounding_allowed_but_wrong_fill_value_detected(self):
        trades = [deepcopy(self.trade)]
        trades[0]["orders"][0]["average"] = "100.004"
        self.assertEqual(self.report(trades=trades)["status"], "MATCHED")
        trades[0]["orders"][0]["average"] = "100.01"
        self.assertIn("FILL_VALUE_MISMATCH", self.codes(self.report(trades=trades)))

    def test_missing_final_charges_and_nonterminal_orders_stay_unreconciled(self):
        e = deepcopy(self.evidence)
        e["charge_groups"] = []
        self.assertIn("CHARGES_MISSING", self.codes(self.report(e)))
        trades = [deepcopy(self.trade)]
        trades[0]["orders"][0]["status"] = "OPEN"
        report = self.report(trades=trades)
        self.assertIn("ORDER_UNRESOLVED", self.codes(report))
        self.assertNotIn("B1", report["covered_orders"])

    def test_unknown_missing_and_double_counted_components_fail_closed(self):
        for mode in ("missing", "unknown", "duplicate", "overlap"):
            e = deepcopy(self.evidence)
            group = e["charge_groups"][0]
            if mode == "missing":
                del group["components"]["dp"]
            elif mode == "unknown":
                group["components"]["new_tax"] = "1"
            else:
                second = deepcopy(group)
                if mode == "overlap":
                    second["id"] = "C2"
                e["charge_groups"].append(second)
            with self.subTest(mode=mode):
                self.assertEqual(self.report(e)["status"], "INVALID")

    def test_holdings_cash_and_both_pnl_figures_are_independent_checks(self):
        cases = [("holdings", "closing", {"BSE:LEGACY": 4}, "HOLDINGS_MISMATCH"),
                 ("cash", "closing", "1050.02", "CASH_MISMATCH"),
                 ("pnl", "realized_gross", "71", "PNL_GROSS_MISMATCH"),
                 ("pnl", "net_after_costs", "51", "PNL_NET_MISMATCH")]
        for section, field, value, code in cases:
            e = deepcopy(self.evidence)
            e[section][field] = value
            with self.subTest(code=code):
                self.assertIn(code, self.codes(self.report(e)))

    def test_open_position_has_realized_pnl_and_correct_inventory(self):
        trades = [deepcopy(self.trade)]
        trades[0]["state"] = "OPEN"
        trades[0]["orders"].pop()
        self.evidence["fills"].pop()
        self.evidence["charge_groups"][0]["order_ids"].remove("S2")
        self.evidence["charge_groups"][0]["components"]["stt"] = "1"
        self.evidence["holdings"]["closing"]["NSE:DEMO"] = 6
        self.evidence["cash"]["closing"] = "422"
        self.evidence["pnl"].update(realized_gross="40", net_after_costs="22")
        report = self.report(trades=trades)
        self.assertEqual(report["status"], "MATCHED", report["issues"])
        self.assertIsNone(report["trades"]["one"]["actual_net_pnl"])
        self.assertEqual(report["trades"]["one"]["remaining"], 6)

    def test_unsettled_cash_is_explicit_and_deposit_does_not_inflate_pnl(self):
        cash = self.evidence["cash"]
        cash.update(closing="550", unsettled_closing="600")
        cash["movements"] = [dict(id="deposit-1", source="funds", description="Capital deposit", amount="100")]
        report = self.report()
        self.assertEqual(report["status"], "MATCHED")
        self.assertEqual(dec(report["totals"]["net_after_costs"]), 50)
        cash["movements"].append(deepcopy(cash["movements"][0]))
        self.assertEqual(self.report()["status"], "INVALID")

    def test_nonfinite_boolean_negative_quantity_and_fractional_paise_rejected(self):
        for section, field, value in (("cash", "closing", "NaN"), ("cash", "closing", True),
                                      ("cash", "closing", "1050.001"), ("pnl", "net_after_costs", "Infinity")):
            e = deepcopy(self.evidence)
            e[section][field] = value
            with self.subTest(value=value):
                self.assertEqual(self.report(e)["status"], "INVALID")
        for value in (-1, 0, True, 1.2):
            e = deepcopy(self.evidence)
            e["fills"][0]["quantity"] = value
            self.assertEqual(self.report(e)["status"], "INVALID")

    def test_sources_must_exist_match_hash_and_have_correct_kind(self):
        for change in (dict(sha256="0" * 64), dict(path="missing.csv"), dict(path="../outside.csv"), dict(kind="pnl")):
            e = deepcopy(self.evidence)
            e["sources"][0].update(change)
            with self.subTest(change=change):
                self.assertEqual(self.report(e)["status"], "INVALID")
        self.evidence["cash"]["source"] = "trades"
        self.assertEqual(self.report()["status"], "INVALID")

    def test_account_future_and_truncated_history_are_rejected(self):
        for change in (dict(account_id="OTHER"), dict(as_of=self.now + 1),
                       dict(period_start=self.trade["created"] + 1), dict(as_of=self.trade["created"] - 1)):
            e = dict(self.evidence, **change)
            with self.subTest(change=change):
                self.assertEqual(self.report(e)["status"], "INVALID")

    def test_current_allowance_shortfall_blocks_until_reviewed_profile_changes(self):
        for rates in self.profile["stocks"]["NSE:DEMO"].values():
            for key in rates:
                rates[key] = None if key == "brokerage_cap" else "0"
        Path(self.path).write_text(json.dumps(self.profile))
        report = self.record()
        self.assertTrue(report["financial_match"])
        self.assertIn("COST_ALLOWANCE_SHORTFALL", self.codes(report))
        self.assertIn("FINANCIAL_DISCREPANCY", {a["code"] for a in financial_alerts(self.store, self.cfg, self.now)})
        profile(self.root)
        corrected = self.record()
        self.assertEqual(corrected["status"], "MATCHED")
        self.assertNotEqual(corrected["report_id"], report["report_id"])
        self.assertEqual(financial_alerts(self.store, self.cfg, self.now), [])

    def test_report_is_durable_idempotent_and_preserves_original_estimates(self):
        report = self.record()
        self.record()
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM financial_reports").fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM events WHERE kind='FINANCIAL_RECONCILIATION'").fetchone()[0], 1)
        self.store.close()
        self.store = Store(self.root / "engine.sqlite")
        trade = self.store.all()[0]
        self.assertEqual(trade["estimated_net_pnl"], "66.895")
        self.assertEqual(dec(trade["financials"]["actual_net_pnl"]), 50)
        self.assertEqual(trade["financials"]["report_id"], report["report_id"])
        self.assertEqual(financial_alerts(self.store, self.cfg, self.now), [])

    def test_changed_profile_requires_new_comparison(self):
        self.record()
        self.profile["profile_id"] = "SYNTHETIC-REVISION-2"
        Path(self.path).write_text(json.dumps(self.profile))
        self.assertIn("COST_PROFILE_RECONCILIATION_REQUIRED", {a["code"] for a in financial_alerts(self.store, self.cfg, self.now)})
        self.record()
        self.assertEqual(financial_alerts(self.store, self.cfg, self.now), [])

    def test_new_tariff_is_not_retroactively_applied_to_historical_bills(self):
        self.profile["valid_from"] = "2026-11-07"
        Path(self.path).write_text(json.dumps(self.profile))
        report = self.record()
        self.assertEqual(report["status"], "MATCHED")
        self.assertEqual(report["cost_comparison"], dict(compared_groups=[], outside_profile_scope=["C1"]))
        self.assertEqual(dec(report["totals"]["actual_costs"]), 20)
        self.assertTrue(any("Historical groups" in w for w in report["warnings"]))

    def test_removed_symbol_history_does_not_require_a_new_tradable_tariff(self):
        self.cfg = replace(self.cfg, stocks={"NSE:OTHER": ".05"})
        report = self.record()
        self.assertEqual(report["status"], "MATCHED")
        self.assertEqual(report["cost_comparison"]["outside_profile_scope"], ["C1"])

    def test_stale_engine_report_and_old_cutoff_cannot_replace_receipt(self):
        report = self.report()
        changed = deepcopy(self.trade)
        changed["orders"][0]["average"] = "99"
        self.store.save(changed, "CORRECTION")
        with self.assertRaises(ValueError):
            self.record(report)
        self.store.save(self.trade, "RESTORE")
        self.record()
        self.evidence["as_of"] -= 60
        with self.assertRaises(ValueError):
            self.record()

    def test_opening_baseline_cannot_be_reset_to_hide_a_cash_difference(self):
        self.record()
        self.evidence["cash"].update(opening="1100", closing="1150")
        with self.assertRaises(ValueError):
            self.record()

    def test_invalid_future_report_does_not_poison_recovery_watermark(self):
        e = dict(self.evidence, as_of=self.now + 999999)
        self.record(self.report(e))
        self.assertEqual(self.record()["status"], "MATCHED")

    def test_atomic_report_failure_rolls_back_receipt_and_trade_updates(self):
        self.store.db.execute("CREATE TRIGGER fail_finance BEFORE UPDATE ON trades BEGIN SELECT RAISE(ABORT, 'injected'); END")
        with self.assertRaises(Exception):
            self.record()
        self.assertIsNone(self.store.get("financial_reconciliation"))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM financial_reports").fetchone()[0], 0)
        self.assertNotIn("financials", self.store.all()[0])

    def test_prior_day_unreconciled_fill_and_missing_profile_block_new_entries(self):
        self.assertIn("FINANCIAL_RECONCILIATION_REQUIRED", {a["code"] for a in financial_alerts(self.store, self.cfg, self.now)})
        self.record()
        self.assertEqual(financial_alerts(self.store, self.cfg, self.now), [])
        self.assertIn("COST_PROFILE_REQUIRED", {a["code"] for a in financial_alerts(self.store, replace(self.cfg, cost_profile=None), self.now)})
        changed = self.store.all()[0]
        changed["orders"][0]["average"] = "100.01"
        self.store.save(changed, "CORRECTION")
        self.assertIn("FINANCIAL_RECONCILIATION_REQUIRED", {a["code"] for a in financial_alerts(self.store, self.cfg, self.now)})

    def test_failed_reconciliation_leaves_previous_actuals_traceable(self):
        matched = self.record()
        self.evidence["cash"]["closing"] = "1040"
        failed = self.record()
        self.assertFalse(failed["financial_match"])
        self.assertEqual(self.store.all()[0]["financials"]["report_id"], matched["report_id"])
        self.assertEqual(self.store.get("financial_reconciliation")["status"], "DISCREPANCY")

    def test_incomplete_template_cannot_certify_account(self):
        e = template(self.store, self.cfg, self.now)
        self.assertEqual(self.report(e)["status"], "INVALID")

    def test_duplicate_json_keys_and_nan_cannot_be_silently_loaded(self):
        path = self.root / "bad.json"
        for content in ('{"account_id":"A","account_id":"B"}', '{"cash":NaN}'):
            path.write_text(content)
            with self.assertRaises(ValueError):
                read_json(path)

    def test_cli_replay_has_no_account_mutations_and_report_is_private(self):
        config = self.root / "config.json"
        config.write_text(dumps(asdict(self.cfg)))
        evidence = self.root / "input.financial-evidence.json"
        evidence.write_text(dumps(self.evidence))
        output = self.root / "output.financial-report.json"
        args = ["--config", str(config), "--db", str(self.store.path), "reconcile-finances", "--evidence", str(evidence), "--output", str(output)]
        with patch("execution.__main__.time.time", return_value=self.now), patch("execution.__main__.Engine") as engine, redirect_stdout(io.StringIO()):
            self.assertEqual(main(args + ["--replay"]), 0)
            self.assertIsNone(self.store.get("financial_reconciliation"))
            self.assertEqual(main(args), 0)
            engine.assert_not_called()
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.get("financial_reconciliation")["status"], "MATCHED")


class FinancialGateExitTests(unittest.TestCase):
    def test_financial_blocks_do_not_interrupt_existing_gtt_exit(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            path, _ = profile(root)
            now = instant("2026-11-06 10:00")
            cfg = Config({"NSE:DEMO": ".05", "NSE:OTHER": ".05"}, "2000", "1000", "2", "1", live_account_id="TEST", cost_profile=path)
            store = Store(root / "engine.sqlite")
            broker = PaperBroker(root / "paper.sqlite", "2000", cfg.fees, lambda: now)
            broker.identity = "kite:TEST"  # Synthetic adapter; no real broker calls.
            try:
                engine = Engine(cfg, store, broker, lambda: now)
                broker.set_quote("NSE:DEMO", "100")
                trade = engine.request_entry("one", "NSE:DEMO", "1000")
                self.assertEqual(trade["protection"]["health"], "ACTIVE")
                store.set("financial_reconciliation", dict(status="DISCREPANCY"))
                Path(path).unlink()
                with self.assertRaisesRegex(ValueError, "financial reconciliation"):
                    engine.request_entry("two", "NSE:OTHER", "1000")
                broker.set_quote("NSE:DEMO", trade["target"])
                for _ in range(3):
                    trade = engine.tick()[0]
                self.assertEqual(trade["state"], "CLOSED")
                self.assertTrue(trade["protection"]["released"])
                self.assertIn("FINANCIAL_DISCREPANCY", {a["code"] for a in engine.health()["alerts"]})
            finally:
                broker.close()
                store.close()
