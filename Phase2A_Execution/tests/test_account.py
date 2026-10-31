"""Synthetic contract/failure tests. These do NOT validate any real Kite account."""
from contextlib import redirect_stdout
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from execution import Config, Engine
from execution.__main__ import main
from execution.account import IST, authorization, cash_available, check_profile, inventory
from execution.acceptance import Reader, analyze, collect, fingerprint, order_evidence, readonly_client, save_private
from execution.brokers import PaperBroker
from execution.kite import KiteBroker
from execution.store import Store


def holding(**changes):
    row = dict(exchange="NSE", tradingsymbol="DEMO", product="CNC", quantity=60, t1_quantity=0,
               used_quantity=40, opening_quantity=100, authorised_quantity=100,
               authorised_date="2026-10-30 08:00:00", collateral_quantity=0, short_quantity=0,
               discrepancy=False, mtf={"quantity": 0, "used_quantity": 0})
    row.update(changes)
    return row


def position(quantity=-40, **changes):
    row = dict(exchange="NSE", tradingsymbol="DEMO", product="CNC", quantity=quantity, overnight_quantity=0)
    row.update(changes)
    return row


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config({"NSE:DEMO": ".05"}, "10000", "1000", "2", "1", live_account_id="AB1234")
        self.now = datetime(2026, 10, 30, 11, tzinfo=IST).timestamp()
        self.profile = dict(user_id="AB1234", exchanges=["NSE"], products=["CNC"], order_types=["LIMIT", "MARKET"],
                            meta={"demat_consent": "physical"}, email="PRIVATE_EMAIL_SENTINEL", user_name="PRIVATE_NAME_SENTINEL",
                            api_key="PRIVATE_KEY_SENTINEL", access_token="PRIVATE_TOKEN_SENTINEL")
        self.client = Mock()
        self.client.profile.return_value = deepcopy(self.profile)
        self.client.margins.return_value = dict(enabled=True, net=5000, available=dict(cash=8000, live_balance=5000, collateral=100000))
        self.client.holdings.return_value = [holding()]
        self.client.positions.return_value = {"net": [position()]}
        self.client.instruments.return_value = [dict(tradingsymbol="DEMO", instrument_type="EQ", lot_size=1, tick_size=.05)]
        self.histories, self.trades = {}, {}
        for oid, side, qty, kind, validity, protection in (("BUY1", "BUY", 9, "LIMIT", "IOC", 0),
                ("SELL1", "SELL", 40, "MARKET", "IOC", -1), ("CHILD1", "SELL", 9, "LIMIT", "DAY", 0)):
            row = dict(order_id=oid, exchange="NSE", tradingsymbol="DEMO", product="CNC", transaction_type=side,
                       quantity=qty, filled_quantity=qty, average_price=100, status="COMPLETE", order_type=kind,
                       validity=validity, price=100, market_protection=protection, order_timestamp="2026-10-30 10:00:00")
            self.histories[oid] = [dict(row, filled_quantity=0, average_price=0, status="OPEN"), row]
            quantities = [qty]
            if oid == "BUY1":
                self.histories[oid].insert(1, dict(row, filled_quantity=3, status="OPEN"))
                quantities = [3, 6]
            self.trades[oid] = [dict(trade_id=oid+str(i), order_id=oid, exchange="NSE", tradingsymbol="DEMO",
                product="CNC", transaction_type=side, quantity=q, price=100) for i, q in enumerate(quantities)]
        self.client.orders.return_value = [dict(h[-1], tag="tx"+oid) for oid, h in self.histories.items()]
        self.client.order_history.side_effect = lambda oid: deepcopy(self.histories[oid])
        self.client.order_trades.side_effect = lambda oid: deepcopy(self.trades[oid])
        self.gtt = dict(id=123, type="two-leg", status="triggered",
            condition=dict(exchange="NSE", tradingsymbol="DEMO", trigger_values=[99, 103]),
            orders=[dict(exchange="NSE", tradingsymbol="DEMO", product="CNC", transaction_type="SELL", quantity=9,
                         order_type="LIMIT", price=p, result=None if i == 0 else {"order_result": {"status": "success", "order_id": "CHILD1"}})
                    for i, p in enumerate((98.5, 103))])
        self.client.get_gtts.return_value = [self.gtt]
        self.client.get_gtt.return_value = self.gtt
        for method in ("place_order", "cancel_order", "place_gtt", "delete_gtt", "generate_session", "invalidate_access_token"):
            getattr(self.client, method).side_effect = AssertionError("Acceptance must never mutate broker state")

    def capture(self):
        return collect(self.client, self.cfg, "AB1234", clock=lambda: self.now, interval=0, source="SYNTHETIC_TEST")

    def baseline(self, snapshot):
        old = deepcopy(snapshot)
        old["source"] = "KITE_READ_ONLY"  # simulated provenance for baseline comparison only
        old["captured_at"] -= 24 * 3600
        old["holdings"] = [holding(quantity=0, t1_quantity=100, used_quantity=0)]
        old["positions"] = {"net": []}
        return old

    def report(self, snapshot=None, previous=None, replay=False):
        return analyze(snapshot or self.capture(), self.cfg, "AB1234", previous=previous, replay=replay)

    def test_delivery_sale_is_not_subtracted_twice(self):
        # Broker worked example: 100 held, 40 sold -> holdings 60, used 40, CNC -40.
        self.assertEqual(inventory("NSE:DEMO", [holding()], {"net": [position()]})["owned"], 60)

    def test_mixed_settled_and_t1_inventory_after_sale(self):
        row = holding(quantity=10, t1_quantity=40, used_quantity=50)
        self.assertEqual(inventory("NSE:DEMO", [row], {"net": [position(-50)]})["owned"], 50)

    def test_same_day_to_t1_to_settled_inventory_keeps_quantity(self):
        cases = [([], {"net": [position(9)]}), ([holding(quantity=0, t1_quantity=9, used_quantity=0, opening_quantity=9)], {"net": []}),
                 ([holding(quantity=9, used_quantity=0, opening_quantity=9)], {"net": []})]
        self.assertEqual([inventory("NSE:DEMO", h, p)["owned"] for h, p in cases], [9, 9, 9])

    def test_missing_or_inconsistent_inventory_fields_block(self):
        for field in ("used_quantity", "opening_quantity", "quantity", "t1_quantity"):
            row = holding()
            del row[field]
            with self.subTest(field=field), self.assertRaises(KeyError):
                inventory("NSE:DEMO", [row], {"net": [position()]})
        with self.assertRaises(ValueError):
            inventory("NSE:DEMO", [holding(opening_quantity=120)], {"net": [position()]})

    def test_restricted_and_cross_exchange_inventory_is_not_silently_managed(self):
        changes = [dict(collateral_quantity=10), dict(short_quantity=1), dict(discrepancy=True),
                   dict(mtf={"quantity": 5}), dict(exchange="BSE"), dict(quantity=60.5)]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                inventory("NSE:DEMO", [holding(**change)], {"net": [position()]})
        with self.assertRaises(ValueError):
            inventory("NSE:DEMO", [holding()], {"net": [position(1, product="MIS")]})
        with self.assertRaises(ValueError):
            inventory("NSE:DEMO", [holding()], {"net": [position(1, overnight_quantity=1)]})

    def test_cash_uses_current_available_bounds_without_collateral(self):
        self.assertEqual(cash_available(self.client.margins.return_value), 5000)
        row = deepcopy(self.client.margins.return_value)
        row["net"] = 1000
        self.assertEqual(cash_available(row), 1000)
        row["available"]["live_balance"] = -100
        self.assertEqual(cash_available(row), 0)

    def test_missing_current_funds_and_disabled_equity_fail(self):
        row = deepcopy(self.client.margins.return_value)
        del row["available"]["live_balance"]
        with self.assertRaises(KeyError):
            cash_available(row)
        with self.assertRaises(ValueError):
            cash_available(dict(row, enabled=False))
        with self.assertRaises(ValueError):
            cash_available(dict(self.client.margins.return_value, net="NaN"))

    def test_profile_requires_expected_identity_permissions_and_known_authorization(self):
        check_profile(self.profile, self.cfg.stocks, "AB1234")
        for change in (dict(user_id="OTHER"), dict(products=["MIS"]), dict(exchanges=["BSE"]),
                       dict(order_types=["LIMIT"]), dict(meta={})):
            with self.subTest(change=change), self.assertRaises(ValueError):
                check_profile(dict(self.profile, **change), self.cfg.stocks, "AB1234")

    def test_electronic_authorization_is_quantity_and_session_specific(self):
        profile = dict(self.profile, meta={"demat_consent": "consent"})
        self.assertEqual(authorization(profile, [holding()], "NSE:DEMO", self.now)["status"], "PASS")
        for change in (dict(authorised_quantity=60), dict(authorised_date="2026-10-29 08:00:00"), dict(authorised_date=None)):
            self.assertEqual(authorization(profile, [holding(**change)], "NSE:DEMO", self.now)["status"], "FAIL")
        late = datetime(2026, 10, 30, 17, 30, tzinfo=IST).timestamp()
        self.assertEqual(authorization(profile, [holding()], "NSE:DEMO", late)["status"], "FAIL")
        self.assertEqual(authorization(profile, [], "NSE:DEMO", self.now)["status"], "NOT_OBSERVED")

    def test_collector_never_calls_broker_mutations_or_persists_profile_secrets(self):
        captured = self.capture()
        encoded = json.dumps(captured)
        for secret in ("PRIVATE_EMAIL_SENTINEL", "PRIVATE_NAME_SENTINEL", "PRIVATE_KEY_SENTINEL", "PRIVATE_TOKEN_SENTINEL", "AB1234"):
            self.assertNotIn(secret, encoded)
        self.assertEqual(captured["account_fingerprint"], fingerprint("AB1234"))
        self.assertTrue(all(call[0] in ("profile", "margins", "holdings", "positions", "instruments", "orders", "get_gtts", "get_gtt", "order_history", "order_trades")
                            for call in self.client.method_calls))
        with self.assertRaises(ValueError):
            Reader(self.client, 0).get("place_order", "anything")

    def test_wrong_account_never_reads_its_portfolio(self):
        self.client.profile.return_value["user_id"] = "OTHER"
        report = self.report()
        self.assertEqual(report["status"], "BLOCKED")
        self.client.holdings.assert_not_called()
        self.client.margins.assert_not_called()

    def test_authentication_failure_is_reported_without_exception_secret(self):
        self.client.profile.side_effect = RuntimeError("SECRET_HTTP_AUTHORIZATION_HEADER")
        report = self.report()
        self.assertEqual(report["status"], "BLOCKED")
        self.assertNotIn("SECRET_HTTP", json.dumps(report))
        self.assertEqual(report["snapshot"]["read_errors"], {"profile": "RuntimeError"})

    def test_missing_endpoint_cannot_pass_as_an_empty_portfolio(self):
        self.client.holdings.side_effect = TimeoutError("secret request URL")
        self.assertEqual(self.report()["status"], "BLOCKED")

    def test_daily_order_fills_must_agree_with_net_cnc_positions(self):
        self.client.positions.return_value = {"net": [position(0)]}
        report = self.report()
        check = next(c for c in report["checks"] if c["code"].startswith("DAY_ORDER_RECONCILIATION"))
        self.assertEqual(check["status"], "FAIL")
        self.assertEqual(report["status"], "BLOCKED")

    def test_empty_evidence_does_not_claim_execution_acceptance(self):
        self.client.orders.return_value = []
        self.client.get_gtts.return_value = []
        self.client.holdings.return_value = []
        self.client.positions.return_value = {"net": []}
        report = self.report()
        self.assertEqual(report["status"], "INCOMPLETE")
        self.assertFalse(report["live_account_validated"])
        for case in ("PARTIAL_FILL", "LIMIT_IOC_ENTRY", "PROTECTED_MARKET_EXIT", "DELIVERY_HOLDINGS", "OVERNIGHT_SETTLEMENT", "GTT_TRIGGER_FILL"):
            self.assertEqual(report["coverage"][case]["status"], "NOT_OBSERVED")

    def test_synthetic_and_replayed_data_cannot_validate_a_real_account(self):
        snapshot = self.capture()
        previous = self.baseline(snapshot)
        synthetic = self.report(snapshot, previous)
        self.assertTrue(all(r["status"] == "PASS" for r in synthetic["coverage"].values()))
        self.assertEqual(synthetic["status"], "INCOMPLETE")
        self.assertFalse(synthetic["live_account_validated"])
        snapshot["source"] = "KITE_READ_ONLY"  # simulate a locally captured file
        replayed = self.report(snapshot, previous, replay=True)
        self.assertEqual(replayed["source"], "OFFLINE_REPLAY")
        self.assertFalse(replayed["live_account_validated"])

    def test_live_source_requires_every_observation_before_complete_status(self):
        # Pure branch test with synthetic inputs; this is never saved as account evidence.
        snapshot = self.capture()
        snapshot["source"] = "KITE_READ_ONLY"
        self.assertEqual(self.report(snapshot)["status"], "INCOMPLETE")
        self.assertEqual(self.report(snapshot, self.baseline(snapshot))["status"], "EVIDENCE_COMPLETE")

    def test_order_and_trade_quantity_disagreement_blocks_acceptance(self):
        self.trades["BUY1"][0]["quantity"] = 2
        report = self.report()
        self.assertEqual(report["status"], "BLOCKED")
        self.assertEqual(report["coverage"]["LIMIT_IOC_ENTRY"]["status"], "NOT_OBSERVED")

    def test_duplicate_fills_backward_progress_and_partial_complete_are_rejected(self):
        for mutation in ("duplicate", "backward", "incomplete"):
            evidence = dict(history=deepcopy(self.histories["BUY1"]), trades=deepcopy(self.trades["BUY1"]))
            if mutation == "duplicate":
                evidence["trades"].append(evidence["trades"][0])
            elif mutation == "backward":
                evidence["history"].insert(2, dict(evidence["history"][1], filled_quantity=1))
            else:
                evidence["history"][-1]["filled_quantity"] = 8
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                order_evidence("BUY1", evidence, self.cfg.stocks)

    def test_unfinished_partial_and_unprotected_market_orders_do_not_satisfy_coverage(self):
        self.histories["BUY1"][-1]["status"] = "OPEN"
        self.histories["SELL1"][-1]["market_protection"] = 0
        report = self.report()
        self.assertEqual(report["coverage"]["PARTIAL_FILL"]["status"], "NOT_OBSERVED")
        self.assertEqual(report["coverage"]["PROTECTED_MARKET_EXIT"]["status"], "NOT_OBSERVED")

    def test_gtt_child_identity_is_checked_against_trade_evidence(self):
        self.gtt["orders"][0]["quantity"] = 10
        self.gtt["orders"][1]["quantity"] = 10
        report = self.report()
        self.assertEqual(report["status"], "BLOCKED")
        self.assertEqual(report["coverage"]["GTT_TRIGGER_FILL"]["status"], "NOT_OBSERVED")

    def test_trigger_outside_the_approved_universe_is_rejected(self):
        snapshot = self.capture()
        snapshot["gtts"][0]["condition"]["tradingsymbol"] = "OTHER"
        for order in snapshot["gtts"][0]["orders"]:
            order["tradingsymbol"] = "OTHER"
        self.assertEqual(self.report(snapshot)["status"], "BLOCKED")

    def test_missing_trade_endpoint_and_wrong_instrument_tick_block(self):
        self.client.order_trades.side_effect = TimeoutError()
        self.client.instruments.return_value[0]["tick_size"] = .1
        self.assertEqual(self.report()["status"], "BLOCKED")

    def test_same_date_wrong_account_and_wrong_universe_are_invalid_settlement_baselines(self):
        snapshot = self.capture()
        for change in (dict(captured_at=self.now - 60), dict(account_fingerprint="wrong"), dict(stocks={"BSE:OTHER": ".05"})):
            previous = dict(self.baseline(snapshot), **change)
            with self.subTest(change=change):
                self.assertEqual(self.report(snapshot, previous)["status"], "BLOCKED")

    def test_t1_rollover_alone_is_not_settled_delivery_evidence(self):
        snapshot = self.capture()
        previous = self.baseline(snapshot)
        snapshot["holdings"] = [holding(quantity=0, t1_quantity=100, used_quantity=0)]
        snapshot["positions"] = {"net": []}
        snapshot["day_orders"] = []
        report = self.report(snapshot, previous)
        self.assertEqual(report["coverage"]["OVERNIGHT_SETTLEMENT"]["status"], "NOT_OBSERVED")

    def test_settlement_quantity_mismatch_is_not_certified(self):
        snapshot = self.capture()
        previous = self.baseline(snapshot)
        previous["holdings"][0].update(t1_quantity=101, opening_quantity=101)
        self.assertEqual(self.report(snapshot, previous)["coverage"]["OVERNIGHT_SETTLEMENT"]["status"], "NOT_OBSERVED")

    def test_private_evidence_permissions_and_round_trip(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "evidence" / "sample.kite-evidence.json"
            report = self.report()
            save_private(path, report)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(json.loads(path.read_text())["status"], "INCOMPLETE")

    def test_readonly_sdk_transport_rejects_post_put_delete_when_installed(self):
        try:
            from kiteconnect import KiteConnect
        except ImportError:
            self.skipTest("Optional SDK not installed")
        client = readonly_client("synthetic-key", "synthetic-token")
        with patch.object(KiteConnect, "_request", return_value=self.profile) as request:
            self.assertEqual(client.profile(), self.profile)
            for method in ("POST", "PUT", "DELETE"):
                with self.assertRaises(RuntimeError):
                    client._request("test.route", method)
            self.assertEqual(request.call_count, 1)

    def test_missing_credentials_cli_is_blocked_without_engine_or_client_initialization(self):
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / "config.json"
            config.write_text(json.dumps(asdict(self.cfg)))
            out = io.StringIO()
            with patch.dict("os.environ", {}, clear=True), redirect_stdout(out), \
                    patch("execution.acceptance.readonly_client", side_effect=AssertionError("No credentials")), \
                    patch("execution.__main__.Engine", side_effect=AssertionError("Read-only path")), \
                    patch("execution.__main__.Store", side_effect=AssertionError("Read-only path")):
                result = main(["--config", str(config), "--live", "validate-account"])
            self.assertEqual(result, 2)
            self.assertEqual(json.loads(out.getvalue())["source"], "NOT_RUN")

    def test_offline_cli_never_connects_or_claims_live_acceptance(self):
        with tempfile.TemporaryDirectory() as root:
            config, source, output = [Path(root) / p for p in ("config.json", "snapshot.json", "report.kite-evidence.json")]
            config.write_text(json.dumps(asdict(self.cfg)))
            source.write_text(json.dumps(self.capture()))
            with redirect_stdout(io.StringIO()), patch("execution.acceptance.readonly_client", side_effect=AssertionError("Offline")):
                result = main(["--config", str(config), "validate-account", "--snapshot", str(source), "--output", str(output)])
            self.assertEqual(result, 2)
            self.assertFalse(json.loads(output.read_text())["live_account_validated"])

    def test_live_account_guard_blocks_new_entries_before_creating_an_intent(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(Path(root) / "engine.sqlite")
            broker = PaperBroker(Path(root) / "paper.sqlite", clock=lambda: self.now)
            try:
                broker.set_quote("NSE:DEMO", "100")
                engine = Engine(self.cfg, store, broker, lambda: self.now)
                with patch.object(broker, "assert_entry_ready", create=True, side_effect=ValueError("Wrong account")):
                    with self.assertRaises(ValueError):
                        engine.request_entry("entry", "NSE:DEMO", "1000")
                self.assertEqual(store.all(), [])
                self.assertEqual(broker._orders(), [])
            finally:
                store.close()
                broker.close()

    def test_adapter_rechecks_account_permissions_before_new_entries(self):
        broker = KiteBroker(self.client, self.cfg.stocks, live=True, min_interval=0, expected_account="AB1234")
        broker.assert_entry_ready()
        self.client.profile.return_value["products"] = ["MIS"]
        with self.assertRaises(ValueError):
            broker.assert_entry_ready()
        with self.assertRaises(ValueError):
            KiteBroker(self.client, self.cfg.stocks, live=True, min_interval=0, expected_account="OTHER")

    def test_adapter_without_explicit_account_can_reconcile_but_cannot_start_buys(self):
        broker = KiteBroker(self.client, self.cfg.stocks, live=True, min_interval=0)
        broker.assert_inventory("NSE:DEMO", 60, [])
        with self.assertRaises(ValueError):
            broker.assert_entry_ready()


if __name__ == "__main__":
    unittest.main()
