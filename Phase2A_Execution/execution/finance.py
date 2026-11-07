"""Cumulative broker-statement reconciliation, separate from execution recovery.

No broker mutations, inferred fills, margin-as-cash comparisons, or automatic
rate calibration. Imports are operator-normalized evidence, not authenticated
broker statements. Source hashes make revisions traceable, not trustworthy.
"""
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, ROUND_FLOOR
import hashlib
import json
from pathlib import Path
import re
from .costs import CENT, digest, load_plan, trade_fees
from .market import IST
from .models import dec, dumps, positive


COMPONENTS = {"brokerage", "stt", "exchange", "sebi", "ipft", "gst", "stamp", "dp", "other"}
TERMINAL = {"COMPLETE", "CANCELLED", "REJECTED"}
SOURCE_KINDS = {"trades", "charges", "holdings", "funds", "pnl"}
PRICE_TOLERANCE = Decimal("0.005")  # broker order averages may be rounded to 2 places


def read_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON field: " + key)
            result[key] = value
        return result
    def invalid(value):
        raise ValueError("Non-finite JSON number")
    with open(path) as handle:
        return json.load(handle, object_pairs_hook=unique, parse_constant=invalid)


def identifier(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError("Invalid evidence identifier")
    return value


def quantity(value):
    if type(value) is not int or value < 0:
        raise ValueError("Evidence quantities must be nonnegative integers")
    return value


def money(value, *, signed=False):
    if isinstance(value, bool):
        raise ValueError("Boolean is not money")
    value = dec(value)
    if (value < 0 and not signed) or value != value.quantize(CENT):
        raise ValueError("Posted money must be in whole paise with the correct sign")
    return value


def timestamp(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Evidence times must be UTC epoch seconds")
    return float(positive(value))


def stock_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:NSE|BSE):[A-Z0-9&_.-]+", value):
        raise ValueError("Invalid evidence stock")
    return value


def order_signature(order):
    return digest({k: order[k] for k in ("id", "stock", "side", "filled", "average")})


def engine_signature(trades):
    return digest([dict(id=t["id"], stock=t["stock"], created=t["created"], state=t["state"],
                        orders=[{k: o.get(k) for k in ("id", "stock", "side", "quantity", "filled",
                                                      "average", "status", "submitted", "placed_at")}
                                for o in t["orders"]],
                        fees=t.get("fees"), cost_plan=t.get("cost_plan")) for t in trades])


def verify_sources(evidence, directory):
    root = Path(directory).resolve()
    sources = {}
    for row in evidence["sources"]:
        key = identifier(row["id"])
        if key in sources or row["kind"] not in SOURCE_KINDS:
            raise ValueError("Duplicate source ID or unsupported source kind")
        path = (root / row["path"]).resolve()
        if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("Evidence source must be a local file within the bundle (maximum 32 MiB)")
        hasher = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                hasher.update(chunk)
        if hasher.hexdigest() != row["sha256"]:
            raise ValueError("Evidence source hash mismatch: " + key)
        sources[key] = dict(kind=row["kind"], sha256=row["sha256"])
    if {v["kind"] for v in sources.values()} != SOURCE_KINDS:
        raise ValueError("Tradebook, charges, holdings, funds and P&L evidence are all required")
    return sources


def allocate(total, weights):
    """Deterministic turnover allocation; largest remainders preserve every paise.

    Allocation is NOT a broker-provided per-order charge. Totals are the posted
    costs. Use narrow groups (for example one ISIN/day) where evidence permits.
    """
    total = money(total)
    denominator = sum(weights.values())
    shares = {key: total * value / denominator for key, value in weights.items()}
    result = {key: value.quantize(CENT, rounding=ROUND_FLOOR) for key, value in shares.items()}
    remaining = int((total - sum(result.values())) / CENT)
    ranked = sorted(shares, key=lambda key: (-(shares[key] - result[key]), key))
    for key in ranked[:remaining]:
        result[key] += CENT
    return result


def reconcile(evidence, trades, account, cfg, directory, now):
    """Never changes execution state. Malformed/incomplete evidence fails closed."""
    report = dict(schema=1, status="INVALID", basis="OPERATOR_IMPORTED_RECORDS",
                  broker_authenticated=False, checked_at=now, account=account,
                  evidence_hash=digest(evidence), engine_hash=engine_signature(trades),
                  as_of=evidence.get("as_of"), issues=[], warnings=[], financial_match=False,
                  covered_orders={}, trades={}, sources={})
    def issue(code, ref, detail):
        report["issues"].append(dict(code=code, ref=ref, detail=detail))
    try:
        _reconcile(evidence, trades, account, cfg, directory, now, report, issue)
    except (ValueError, TypeError, KeyError, OSError, ArithmeticError) as exc:
        issue("INVALID_EVIDENCE", "bundle", type(exc).__name__ + ": " + str(exc))
        report["status"] = "INVALID"
        report["financial_match"] = False
    report["report_id"] = digest({k: v for k, v in report.items() if k != "checked_at"})
    return report


def _reconcile(e, trades, account, cfg, directory, now, report, issue):
    if type(e["schema"]) is not int or e["schema"] != 1:
        raise ValueError("Unsupported financial evidence schema")
    if not account or not account.startswith("kite:") or e["account_id"] != account[5:]:
        raise ValueError("Evidence must match the live account bound to this database")
    if cfg.live_account_id and cfg.live_account_id != e["account_id"]:
        raise ValueError("Configured account differs from evidence")
    start, end = timestamp(e["period_start"]), timestamp(e["as_of"])
    if not start <= end <= now:
        raise ValueError("Invalid/future evidence period")
    if trades and start > min(t["created"] for t in trades):
        raise ValueError("Evidence must cover the entire engine history from its opening baseline")
    report["baseline"] = dict(period_start=start, holdings={stock_name(k): quantity(v) for k, v in e["holdings"]["opening"].items()},
                              cash=str(money(e["cash"]["opening"], signed=True).quantize(CENT)),
                              unsettled=str(money(e["cash"]["unsettled_opening"], signed=True).quantize(CENT)))
    sources = report["sources"] = verify_sources(e, directory)
    def source(key, kind):
        if key not in sources or sources[key]["kind"] != kind:
            raise ValueError("Missing or wrong source kind for " + str(key))
    def compare(code, ref, actual, expected, tolerance=CENT):
        if abs(actual - expected) > tolerance:
            issue(code, ref, dict(reported=str(actual), calculated=str(expected), difference=str(actual - expected)))
    expected, owner = {}, {}
    for t in trades:
        if t["created"] > end:
            raise ValueError("Evidence predates current engine activity; recollect before applying")
        for o in t["orders"]:
            if o["status"] not in TERMINAL:
                issue("ORDER_UNRESOLVED", t["id"], "Reconcile terminal order status before financial import")
            if not o["id"]:
                if o["filled"] or o["status"] not in TERMINAL:
                    issue("ORDER_ID_MISSING", t["id"], "An order lacks authoritative identity")
                continue
            if o["id"] in expected:
                raise ValueError("Engine order ID is not unique")
            expected[o["id"]], owner[o["id"]] = o, t
            if o.get("submitted", t["created"]) > end:
                raise ValueError("Evidence predates an engine order")
    fills = defaultdict(list)
    seen = set()
    for fill in e["fills"]:
        key, oid = identifier(fill["id"]), identifier(fill["order_id"])
        source(fill["source"], "trades")
        stock = stock_name(fill["stock"])
        at = timestamp(fill["at"])
        # Exchange trade IDs can repeat across dates/exchanges, but not within them.
        unique = (stock.split(":")[0], datetime.fromtimestamp(at, IST).date().isoformat(), key)
        if unique in seen:
            raise ValueError("Duplicate exchange fill ID/date")
        seen.add(unique)
        if not start <= at <= end or not quantity(fill["quantity"]) or isinstance(fill["price"], bool):
            raise ValueError("Fill outside evidence period or invalid quantity/price")
        price = positive(fill["price"])
        if fill["side"] not in ("BUY", "SELL"):
            raise ValueError("Invalid fill side")
        if oid not in expected:
            issue("UNOWNED_FILL", oid, "External orders are not silently attributed to this engine")
            continue
        order = expected[oid]
        if (stock, fill["side"]) != (order["stock"], order["side"]):
            issue("FILL_IDENTITY_MISMATCH", oid, "Exchange/symbol/side differs from engine order")
        if at < (order.get("placed_at") or order["submitted"]) - 2:
            issue("FILL_TIME_MISMATCH", oid, "Fill predates order placement")
        fills[oid].append(dict(quantity=fill["quantity"], value=price * fill["quantity"], at=at))
    values, quantities = {}, {}
    for oid, o in expected.items():
        rows = fills[oid]
        q = quantities[oid] = sum(row["quantity"] for row in rows)
        value = values[oid] = sum((row["value"] for row in rows), Decimal(0))
        compare("FILL_QUANTITY_MISMATCH", oid, q, o["filled"], Decimal(0))
        if q:
            compare("FILL_VALUE_MISMATCH", oid, value, dec(o["average"]) * q, PRICE_TOLERANCE * q)
        if q == o["filled"] and o["status"] in TERMINAL:
            report["covered_orders"][oid] = order_signature(o)
    charged, group_ids = set(), set()
    allocation = defaultdict(lambda: Decimal(0))
    component_totals = {key: Decimal(0) for key in COMPONENTS}
    for group in e["charge_groups"]:
        gid = identifier(group["id"])
        if gid in group_ids:
            raise ValueError("Duplicate charge group")
        group_ids.add(gid)
        if not group["sources"] or len(set(group["sources"])) != len(group["sources"]):
            raise ValueError("Charge group needs unique source references")
        for key in group["sources"]:
            source(key, "charges")
        ids = group["order_ids"]
        if not ids or len(set(ids)) != len(ids) or charged.intersection(ids):
            raise ValueError("An order must belong to exactly one complete charge group")
        if set(group["components"]) != COMPONENTS:
            raise ValueError("Every charge component must be explicit, including zero DP/other charges")
        if any(oid not in expected or values[oid] <= 0 for oid in ids):
            raise ValueError("Charges reference an unknown or unfilled order")
        components = {key: money(value) for key, value in group["components"].items()}
        if components["other"]:
            identifier(group.get("other_description"))
        for key, value in components.items():
            component_totals[key] += value
        for oid, value in allocate(sum(components.values()), {oid: values[oid] for oid in ids}).items():
            allocation[oid] += value
        charged.update(ids)
    for oid, value in values.items():
        if value and oid not in charged:
            issue("CHARGES_MISSING", oid, "A final contract note and DP/ledger charges, including explicit zeros, are required")
    actual_costs = sum(component_totals.values())
    holdings = e["holdings"]
    source(holdings["source"], "holdings")
    opening = {stock_name(k): quantity(v) for k, v in holdings["opening"].items()}
    closing = {stock_name(k): quantity(v) for k, v in holdings["closing"].items()}
    inventory = defaultdict(int, opening)
    cashflow, gross, estimated = Decimal(0), Decimal(0), Decimal(0)
    current_estimated, plans, current_order_costs = Decimal(0), {}, {}
    if cfg.cost_profile:
        plans = {stock: load_plan(cfg.cost_profile, account[5:], stock, now, cfg.max_exit_attempts)
                 for stock in cfg.stocks}
    for t in trades:
        buys = [o for o in t["orders"] if o["side"] == "BUY" and o["id"] in values]
        sells = [o for o in t["orders"] if o["side"] == "SELL" and o["id"] in values]
        buy_qty = sum(quantities[o["id"]] for o in buys)
        sell_qty = sum(quantities[o["id"]] for o in sells)
        buy_value = sum((values[o["id"]] for o in buys), Decimal(0))
        sell_value = sum((values[o["id"]] for o in sells), Decimal(0))
        if sell_qty > buy_qty:
            raise ValueError("Imported sells exceed owned entry fills")
        trade_gross = sell_value - (buy_value * sell_qty / buy_qty if buy_qty else Decimal(0))
        costs = sum((allocation[o["id"]] for o in buys + sells), Decimal(0))
        gross += trade_gross
        cashflow += sell_value - buy_value
        inventory[t["stock"]] += buy_qty - sell_qty
        report["trades"][t["id"]] = dict(bought=buy_qty, sold=sell_qty, remaining=buy_qty - sell_qty,
            last_fill_at=max((row['at'] for o in buys + sells for row in fills[o['id']]), default=None),
            realized_gross=str(trade_gross), allocated_actual_costs=str(costs),
            actual_net_pnl=str(trade_gross - costs) if buy_qty == sell_qty else None,
            allocation_basis="GROUP_TURNOVER_LARGEST_REMAINDER")
        fees = trade_fees(t)
        for o in buys + sells:
            value = values[o["id"]]
            if not value:
                continue
            estimated += fees.cost(o["side"], value)
            plan = plans.get(t["stock"])
            if plan and all(plan.data["valid_from"] <= datetime.fromtimestamp(row["at"], IST).date().isoformat()
                            <= plan.data["valid_until"] for row in fills[o["id"]]):
                current_order_costs[o["id"]] = plan.cost(o["side"], value)
                current_estimated += current_order_costs[o["id"]]
    for stock in set(inventory) | closing.keys():
        compare("HOLDINGS_MISMATCH", stock, closing.get(stock, 0), inventory[stock], Decimal(0))
    cash = e["cash"]
    source(cash["source"], "funds")
    opening_cash = money(cash["opening"], signed=True) + money(cash["unsettled_opening"], signed=True)
    closing_cash = money(cash["closing"], signed=True) + money(cash["unsettled_closing"], signed=True)
    movements, movement_ids = Decimal(0), set()
    for row in cash["movements"]:
        key = identifier(row["id"])
        if key in movement_ids:
            raise ValueError("Duplicate/unexplained non-trading cash movement")
        identifier(row["description"])
        movement_ids.add(key)
        source(row["source"], "funds")
        movements += money(row["amount"], signed=True)
    expected_cash = opening_cash + cashflow - actual_costs + movements
    compare("CASH_MISMATCH", "funds", closing_cash, expected_cash)
    pnl = e["pnl"]
    source(pnl["source"], "pnl")
    compare("PNL_GROSS_MISMATCH", "pnl", money(pnl["realized_gross"], signed=True), gross)
    compare("PNL_NET_MISMATCH", "pnl", money(pnl["net_after_costs"], signed=True), gross - actual_costs)
    report["totals"] = dict(components={k: str(v) for k, v in sorted(component_totals.items())},
        actual_costs=str(actual_costs), original_estimated_costs=str(estimated), cost_variance=str(actual_costs - estimated),
        realized_gross=str(gross), net_after_costs=str(gross - actual_costs),
        calculated_cash=str(expected_cash), reported_cash=str(closing_cash),
        non_trading_cash_movements=str(movements), inventory=dict(inventory))
    report["financial_match"] = not report["issues"]
    if cfg.cost_profile:
        report["current_profile_hashes"] = sorted({p.data["profile_hash"] for p in plans.values()})
        report["totals"]["current_profile_allowance"] = str(current_estimated)
        report["cost_comparison"] = dict(compared_groups=[], outside_profile_scope=[])
        for group in e["charge_groups"]:
            if not all(oid in current_order_costs for oid in group["order_ids"]):
                report["cost_comparison"]["outside_profile_scope"].append(group["id"])
                continue
            report["cost_comparison"]["compared_groups"].append(group["id"])
            if sum(money(v) for v in group["components"].values()) > sum(current_order_costs[oid] for oid in group["order_ids"]) + CENT:
                issue("COST_ALLOWANCE_SHORTFALL", group["id"], "Posted group costs exceed the current account cost allowance; review the profile and rerun")
        if report["cost_comparison"]["outside_profile_scope"]:
            report["warnings"].append("Historical groups outside current tariff dates/stocks were accounted for but not used to validate current cost allowances")
    else:
        report["warnings"].append("No account cost profile: only legacy estimated costs compared; new live entries remain blocked")
    if actual_costs > estimated + CENT:
        report["warnings"].append("Actual costs exceeded the original trade estimate; historical estimates remain unchanged")
    report["status"] = "MATCHED" if not report["issues"] else "DISCREPANCY"


def record_report(store, report):
    """Caller holds store.locked(); receipt, per-trade results and audit are atomic."""
    trades = store.all()
    if report["account"] != store.get("account") or report["engine_hash"] != engine_signature(trades):
        raise ValueError("Account/engine changed during reconciliation; recompute")
    previous_end = store.get("financial_latest_as_of")
    if report["status"] != "INVALID" and previous_end:
        if timestamp(report["as_of"]) < timestamp(previous_end):
            raise ValueError("An older financial report cannot replace the current receipt")
    baseline = store.get("financial_baseline")
    if report["status"] != "INVALID" and baseline and report["baseline"] != baseline:
        raise ValueError("The cumulative opening baseline cannot change; investigate the source statements")
    with store.db:
        exists = store.db.execute("SELECT 1 FROM financial_reports WHERE id=?", (report["report_id"],)).fetchone()
        if not exists:
            store.db.execute("INSERT INTO financial_reports VALUES (?, ?, ?)",
                             (report["report_id"], report["checked_at"], dumps(report)))
        receipt = {k: report.get(k) for k in ("report_id", "status", "as_of", "checked_at", "covered_orders", "issues", "current_profile_hashes")}
        if report["status"] == "INVALID":
            receipt["as_of"] = None
        else:
            store.db.execute("INSERT OR REPLACE INTO settings VALUES ('financial_latest_as_of',?)", (dumps(report["as_of"]),))
        store.db.execute("INSERT OR REPLACE INTO settings VALUES ('financial_reconciliation',?)", (dumps(receipt),))
        if report["financial_match"]:
            store.db.execute("INSERT OR REPLACE INTO settings VALUES ('financial_baseline',?)", (dumps(report["baseline"]),))
            for trade in trades:
                if trade["id"] not in report["trades"]:
                    continue
                financials = dict(report["trades"][trade["id"]], report_id=report["report_id"], as_of=report["as_of"],
                                  status="MATCHED_IMPORTED_RECORDS")
                if trade.get("financials") == financials:
                    continue
                trade["financials"] = financials
                store.db.execute("UPDATE trades SET data=? WHERE id=?", (dumps(trade), trade["id"]))
                store.db.execute("INSERT INTO events(at,trade_id,kind,data) VALUES (?,?,?,?)",
                                 (report["checked_at"], trade["id"], "FINANCIAL_RECONCILIATION", dumps(trade)))


def financial_alerts(store, config, now):
    account = store.get("account", "")
    if not account.startswith("kite:"):
        return []
    alerts = []
    def add(code, message):
        alerts.append(dict(code=code, severity="CRITICAL", trade_id=None, ref="finances", deadline=None,
                           message=message, action="Review the account cost profile and reconcile-finances evidence; existing exits continue."))
    hashes = set()
    if not config.cost_profile:
        add("COST_PROFILE_REQUIRED", "New live entries require a reviewed account-specific cost profile")
    else:
        try:
            for stock in config.stocks:
                plan = load_plan(config.cost_profile, account[5:], stock, now, config.max_exit_attempts)
                hashes.add(plan.data["profile_hash"])
        except (ValueError, KeyError, TypeError, OSError, ArithmeticError):
            add("COST_PROFILE_INVALID", "Account cost profile is missing, invalid, expired or does not cover the configured stocks")
    receipt = store.get("financial_reconciliation", {})
    if receipt and receipt.get("status") != "MATCHED":
        add("FINANCIAL_DISCREPANCY", "The last financial import has unresolved discrepancies")
    if receipt.get("status") == "MATCHED" and receipt.get("covered_orders") and hashes and hashes != set(receipt.get("current_profile_hashes") or []):
        add("COST_PROFILE_RECONCILIATION_REQUIRED", "Account cost profile changed since the financial receipt; rerun reconciliation")
    covered = receipt.get("covered_orders", {}) if receipt.get("status") == "MATCHED" else {}
    day = datetime.fromtimestamp(now, IST).date()
    overdue = [o for t in store.all() for o in t["orders"] if o["filled"] and
               datetime.fromtimestamp(o.get("placed_at") or o["submitted"], IST).date() < day and
               covered.get(o["id"]) != order_signature(o)]
    if overdue:
        add("FINANCIAL_RECONCILIATION_REQUIRED", "Prior-day fills need matching cumulative broker statements before new entries")
    return alerts


def template(store, cfg, now):
    account = store.get("account", "kite:" + (cfg.live_account_id or "ACCOUNT_ID"))
    trades = store.all()
    return dict(schema=1, account_id=account.removeprefix("kite:"),
        period_start=min((t["created"] for t in trades), default=now), as_of=now,
        sources=[dict(id=kind, kind=kind, path=kind + ".csv", sha256="REPLACE_WITH_SOURCE_SHA256")
                 for kind in sorted(SOURCE_KINDS)], fills=[], charge_groups=[],
        holdings=dict(source="holdings", opening={}, closing={}),
        cash=dict(source="funds", opening=None, closing=None, unsettled_opening=None, unsettled_closing=None, movements=[]),
        pnl=dict(source="pnl", realized_gross=None, net_after_costs=None),
        engine_reference=dict(note="REFERENCE ONLY. Supply independent broker values above; never copy estimates as evidence.",
                              orders=[{k: o.get(k) for k in ("id", "stock", "side", "filled", "average", "status")}
                                      for t in trades for o in t["orders"]]))
