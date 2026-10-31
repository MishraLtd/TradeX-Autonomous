"""Read-only Kite account evidence. No trading, authorization or token mutations.

An evidence report is an observation, not a certificate or a live-trading switch.
Missing scenarios stay NOT_OBSERVED; mock/replay data cannot validate an account.
"""
from datetime import datetime
import hashlib
import math
import os
from pathlib import Path
import tempfile
import time

from .account import IST, authorization, cash_available, check_profile, integer, inventory
from .models import dec, dumps, positive

READ_METHODS = {"profile", "margins", "holdings", "positions", "orders", "order_history",
                "order_trades", "get_gtts", "get_gtt", "instruments"}
HOLDING_FIELDS = ("exchange", "tradingsymbol", "product", "quantity", "t1_quantity", "used_quantity",
                  "opening_quantity", "authorised_quantity", "authorised_date", "collateral_quantity",
                  "short_quantity", "discrepancy")
POSITION_FIELDS = ("exchange", "tradingsymbol", "product", "quantity", "overnight_quantity")
ORDER_FIELDS = ("order_id", "exchange", "tradingsymbol", "product", "transaction_type", "quantity",
                "filled_quantity", "average_price", "status", "order_type", "validity", "price",
                "market_protection", "order_timestamp")
TRADE_FIELDS = ("trade_id", "order_id", "exchange", "tradingsymbol", "product", "transaction_type", "quantity", "price")


def fingerprint(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def readonly_client(api_key, access_token):
    from kiteconnect import KiteConnect
    class ReadOnlyKite(KiteConnect):
        def _request(self, route, method, *args, **kwargs):
            if method != "GET":
                raise RuntimeError("Account validation transport permits GET requests only")
            return super()._request(route, method, *args, **kwargs)
    client = ReadOnlyKite(api_key=api_key, timeout=10)
    client.set_access_token(access_token)
    return client


def select(row, fields):
    # Allowlist output. Never persist user names, email, tokens or free-text API errors.
    return {k: row[k].isoformat() if isinstance(row[k], datetime) else row[k] for k in fields if k in row}


class Reader:
    def __init__(self, client, interval=0.35):
        self.client, self.interval, self.last = client, interval, 0.0
        self.errors = {}

    def get(self, method, *args):
        if method not in READ_METHODS:
            raise ValueError("Read-only collector rejected method")
        time.sleep(max(0, self.interval - (time.monotonic() - self.last)))
        self.last = time.monotonic()
        try:
            return getattr(self.client, method)(*args)
        except Exception as exc:
            # Exception strings can contain request URLs/headers. Only retain type.
            self.errors[method] = type(exc).__name__
            return None


def collect(client, config, expected_account, *, order_ids=(), trigger_ids=(), clock=time.time,
            interval=0.35, source="KITE_READ_ONLY"):
    if not expected_account:
        raise ValueError("An expected account ID is required before collecting account data")
    if len(order_ids) > 20 or len(trigger_ids) > 20:
        raise ValueError("Select at most 20 orders and 20 triggers per capture")
    reader = Reader(client, interval)
    snapshot = dict(schema=1, source=source, captured_at=clock(), stocks=config.stocks,
                    account_fingerprint=None, profile=None, margins=None, holdings=None,
                    positions=None, instruments=[], day_orders=None, order_evidence={}, gtts=[], read_errors=reader.errors)
    profile = reader.get("profile")
    if profile is None:
        return snapshot
    snapshot["account_fingerprint"] = fingerprint(profile.get("user_id"))
    snapshot["profile"] = select(profile, ("exchanges", "products", "order_types"))
    snapshot["profile"]["meta"] = {"demat_consent": profile.get("meta", {}).get("demat_consent")}
    if profile.get("user_id") != expected_account:
        return snapshot  # do not fetch another account's portfolio
    margins = reader.get("margins", "equity")
    if margins is not None:
        snapshot["margins"] = select(margins, ("enabled", "net"))
        snapshot["margins"]["available"] = select(margins.get("available", {}), ("cash", "live_balance"))
    symbols = {s.split(":")[1] for s in config.stocks}
    holdings, positions = reader.get("holdings"), reader.get("positions")
    if holdings is not None:
        snapshot["holdings"] = [dict(select(r, HOLDING_FIELDS), mtf=select(r.get("mtf", {}), ("quantity", "used_quantity")))
                                for r in holdings if r.get("tradingsymbol") in symbols]
    if positions is not None:
        snapshot["positions"] = {"net": [select(r, POSITION_FIELDS) for r in positions["net"]
                                          if r.get("tradingsymbol") in symbols]}
    for exchange in sorted({s.split(":")[0] for s in config.stocks}):
        rows = reader.get("instruments", exchange)
        if rows is not None:
            snapshot["instruments"].extend(dict(select(r, ("tradingsymbol", "instrument_type", "lot_size", "tick_size")), exchange=exchange)
                                           for r in rows if exchange + ":" + r.get("tradingsymbol", "") in config.stocks)
    orders = reader.get("orders")
    if orders is not None:
        snapshot["day_orders"] = [select(r, ORDER_FIELDS) for r in orders if
                                  r.get("exchange", "") + ":" + r.get("tradingsymbol", "") in config.stocks]
    selected = set(map(str, order_ids))
    if orders is not None and not selected:
        # Discover only engine-tagged orders, not unrelated account activity.
        selected.update(str(r["order_id"]) for r in orders if str(r.get("tag") or "").startswith("tx") and
                        r.get("exchange", "") + ":" + r.get("tradingsymbol", "") in config.stocks)
    rows = reader.get("get_gtts")
    candidates = set(map(str, trigger_ids))
    if rows is not None and not candidates:
        candidates.update(str(r["id"]) for r in rows if r.get("type") == "two-leg" and
                          r.get("condition", {}).get("exchange", "") + ":" +
                          r.get("condition", {}).get("tradingsymbol", "") in config.stocks)
    if len(candidates) > 20 or len(selected) > 50:
        raise ValueError("Too much evidence; select specific order/trigger IDs")
    for trigger_id in sorted(candidates):
        row = reader.get("get_gtt", trigger_id)
        if row is None:
            continue
        gtt = select(row, ("id", "type", "status"))
        gtt["condition"] = select(row["condition"], ("exchange", "tradingsymbol", "trigger_values"))
        gtt["orders"] = []
        for order in row["orders"]:
            leg = select(order, ("exchange", "tradingsymbol", "product", "transaction_type", "quantity", "order_type", "price"))
            result = order.get("result")
            leg["result"] = None if result is None else {"order_result": select(result.get("order_result") or {}, ("status", "order_id"))}
            if result and result.get("order_result", {}).get("order_id"):
                selected.add(str(result["order_result"]["order_id"]))
            gtt["orders"].append(leg)
        snapshot["gtts"].append(gtt)
    if len(selected) > 50:
        raise ValueError("Too many child orders; select fewer triggers")
    for order_id in sorted(selected):
        history, trades = reader.get("order_history", order_id), reader.get("order_trades", order_id)
        snapshot["order_evidence"][order_id] = dict(
            history=None if history is None else [select(r, ORDER_FIELDS) for r in history],
            trades=None if trades is None else [select(r, TRADE_FIELDS) for r in trades])
    snapshot["finished_at"] = clock()
    return snapshot


def order_evidence(order_id, evidence, stocks):
    history, trades = evidence["history"], evidence["trades"]
    if not history or trades is None:
        raise ValueError("Missing order history or trade confirmation")
    last = history[-1]
    stock = last["exchange"] + ":" + last["tradingsymbol"]
    if stock not in stocks or last["product"] != "CNC" or last["transaction_type"] not in ("BUY", "SELL"):
        raise ValueError("Order is outside the approved CNC contract")
    quantity = integer(last["quantity"], "quantity")
    if quantity < 1:
        raise ValueError("Invalid order quantity")
    filled, partial = 0, False
    for row in history:
        if (str(row["order_id"]), row["exchange"] + ":" + row["tradingsymbol"], row["product"],
                row["transaction_type"], row["quantity"]) != (order_id, stock, "CNC", last["transaction_type"], quantity):
            raise ValueError("Order history identity changed")
        current = integer(row["filled_quantity"], "filled_quantity")
        if not filled <= current <= quantity or (row["status"] == "COMPLETE" and current != quantity):
            raise ValueError("Inconsistent fill progression")
        partial = partial or 0 < current < quantity
        filled = current
    seen, total, value = set(), 0, dec(0)
    for trade in trades:
        key = str(trade["trade_id"])
        if key in seen or (str(trade["order_id"]), trade["exchange"] + ":" + trade["tradingsymbol"],
                           trade["transaction_type"], trade["product"]) != (order_id, stock, last["transaction_type"], "CNC"):
            raise ValueError("Duplicate or mismatched fill confirmation")
        seen.add(key)
        qty = integer(trade["quantity"], "trade.quantity")
        if qty < 1:
            raise ValueError("Invalid fill quantity")
        total += qty
        value += positive(trade["price"]) * qty
    if total != filled or (filled and abs(value / filled - dec(last["average_price"])) > dec("0.01")):
        raise ValueError("Trade confirmations disagree with order fills/average price")
    terminal = last["status"] in ("COMPLETE", "CANCELLED", "REJECTED")
    return dict(order_id=order_id, quantity=quantity, stock=stock, filled=filled, partial=partial, terminal=terminal, side=last["transaction_type"],
                order_type=last["order_type"], validity=last["validity"],
                protected=dec(last.get("market_protection", 0)) == -1 or 0 < dec(last.get("market_protection", 0)) <= 100)


def analyze(snapshot, config, expected_account, *, previous=None, replay=False):
    checks, coverage, inventories = [], {}, {}
    def add(code, status, detail):
        checks.append(dict(code=code, status=status, detail=detail))
    def test(code, fn):
        try:
            result = fn()
            if result is False:
                raise ValueError("Broker response does not match the configured contract")
            add(code, "PASS", "Broker response contract checked")
            return result
        except (ValueError, KeyError, TypeError, AttributeError, ArithmeticError) as exc:
            add(code, "FAIL", str(exc) if isinstance(exc, ValueError) else "Missing or malformed broker fields")
            return None
    def observed(code, condition, detail):
        coverage[code] = dict(status="PASS" if condition else "NOT_OBSERVED", detail=detail)
    captured = snapshot.get("captured_at")
    if snapshot.get("schema") != 1 or type(captured) not in (float, int) or not math.isfinite(captured) or captured <= 0:
        raise ValueError("Invalid account evidence schema/timestamp")
    bound = bool(expected_account) and snapshot.get("account_fingerprint") == fingerprint(expected_account)
    add("ACCOUNT_BINDING", "PASS" if bound else "FAIL", "Expected account fingerprint must match")
    add("UNIVERSE_BINDING", "PASS" if snapshot.get("stocks") == config.stocks else "FAIL", "Capture must use the configured stock universe")
    for method, error in snapshot.get("read_errors", {}).items():
        add("READ_" + method.upper(), "FAIL", "Broker read failed: " + error)
    profile = snapshot.get("profile")
    if profile is not None and bound:
        test("ACCOUNT_PERMISSIONS", lambda: check_profile(dict(profile, user_id=expected_account), config.stocks, expected_account))
    else:
        add("ACCOUNT_PERMISSIONS", "FAIL", "No verified profile")
    available = test("AVAILABLE_CASH", lambda: cash_available(snapshot["margins"]))
    if available is not None and available <= 0:
        add("FUNDED_ACCOUNT", "FAIL", "No available cash for a fully funded entry")
    rows = {r["exchange"] + ":" + r["tradingsymbol"]: r for r in snapshot.get("instruments", [])}
    for stock, tick in config.stocks.items():
        row = rows.get(stock, {})
        valid = row.get("instrument_type") == "EQ" and row.get("lot_size") == 1 and row.get("tick_size") is not None
        if valid:
            valid = test("TICK_" + stock, lambda: dec(row["tick_size"]) == dec(tick))
        add("INSTRUMENT_" + stock, "PASS" if valid else "FAIL", "Expected cash equity, one-share lot, configured tick")
    holdings, positions = snapshot.get("holdings"), snapshot.get("positions")
    auth = []
    if holdings is not None and positions is not None:
        for stock in config.stocks:
            value = test("INVENTORY_" + stock, lambda: inventory(stock, holdings, positions))
            if value is not None:
                inventories[stock] = value
            result = test("AUTH_CONTRACT_" + stock, lambda: authorization(profile or {}, holdings, stock, captured))
            if result is not None:
                auth.append(result)
                add("SELL_AUTHORIZATION_" + stock, result["status"], result["detail"])
    else:
        add("PORTFOLIO", "FAIL", "Holdings or positions could not be read")
    def day_orders_match(stock, current):
        if snapshot.get("day_orders") is None:
            raise ValueError("Daily order book could not be read")
        total, seen = 0, set()
        for row in snapshot["day_orders"]:
            if row["exchange"] + ":" + row["tradingsymbol"] != stock or row["product"] != "CNC":
                continue
            if str(row["order_id"]) in seen or row["transaction_type"] not in ("BUY", "SELL"):
                raise ValueError("Duplicate or invalid daily order record")
            seen.add(str(row["order_id"]))
            filled = integer(row["filled_quantity"], "filled_quantity")
            if filled > integer(row["quantity"], "quantity"):
                raise ValueError("Daily order overfilled its requested quantity")
            total += filled * (1 if row["transaction_type"] == "BUY" else -1)
        if total != current["day_delta"]:
            raise ValueError("Daily order fills disagree with net CNC positions; collect after broker snapshots converge")
    for stock, current in inventories.items():
        test("DAY_ORDER_RECONCILIATION_" + stock, lambda: day_orders_match(stock, current))
    observed("DELIVERY_HOLDINGS", any(v["settled"] > 0 for v in inventories.values()), "Requires observed settled delivery holdings")
    observed("SELL_AUTHORIZATION", bool(auth) and all(r["status"] == "PASS" for r in auth), "Current authorization metadata; not a guarantee for a future session")
    summaries = []
    for order_id, evidence in snapshot.get("order_evidence", {}).items():
        result = test("ORDER_" + order_id, lambda: order_evidence(order_id, evidence, config.stocks))
        if result is not None:
            summaries.append(result)
    filled = [r for r in summaries if r["filled"] and r["terminal"]]
    observed("LIMIT_IOC_ENTRY", any(r["side"] == "BUY" and r["order_type"] == "LIMIT" and r["validity"] == "IOC" for r in filled),
             "Requires a terminal CNC LIMIT IOC buy and matching actual trade confirmations")
    observed("PROTECTED_MARKET_EXIT", any(r["side"] == "SELL" and r["order_type"] == "MARKET" and r["validity"] == "IOC" and r["protected"] for r in filled),
             "Requires a terminal protected market sell; converted/ambiguous order types need broker review")
    observed("PARTIAL_FILL", any(r["partial"] and r["terminal"] for r in summaries), "Requires partial fill history with final trade-quantity reconciliation")
    observed("DELIVERY_SELL", any(r["side"] == "SELL" and inventories.get(r["stock"], {}).get("used", 0) > 0 for r in filled),
             "Requires an executed sell and broker evidence that delivery holdings were used")
    from .kite import KiteBroker
    gtts = []
    for row in snapshot.get("gtts", []):
        result = test("GTT_" + str(row["id"]), lambda: KiteBroker.decode_protection(row, str(row["id"])))
        if result is not None and result.spec.stock not in config.stocks:
            add("GTT_UNIVERSE_" + result.id, "FAIL", "Selected trigger is outside the approved stock universe")
        if result is not None and result.spec.stock in config.stocks:
            gtts.append(result)
            if result.submission_failed:
                add("GTT_SUBMISSION_" + result.id, "FAIL", "Broker reports failed child submission; reconcile before acceptance")
    observed("PAIRED_GTT", any(g.status in ("active", "triggered") for g in gtts), "Requires observed paired CNC sell-limit protection")
    linked = []
    by_id = {r["order_id"]: r for r in summaries}
    for gtt in gtts:
        for child_id in gtt.child_ids:
            child = by_id.get(child_id)
            matches = child is not None and (child["stock"], child["quantity"], child["side"], child["order_type"]) == (
                gtt.spec.stock, gtt.spec.quantity, "SELL", "LIMIT")
            add("GTT_CHILD_" + child_id, "PASS" if matches else "FAIL", "Trigger child must match the verified sell order")
            linked.append(bool(matches and child["filled"] and child["terminal"]))
    observed("GTT_TRIGGER_FILL", any(linked), "Requires a trigger-linked child order and matching actual fills")
    settled = False
    if previous is not None:
        old = previous.get("snapshot", previous)
        comparable = (old.get("schema") == 1 and old.get("source") == "KITE_READ_ONLY" and
                      old.get("account_fingerprint") == snapshot.get("account_fingerprint") and old.get("stocks") == config.stocks and
                      isinstance(old.get("captured_at"), (float, int)) and old["captured_at"] < captured)
        if comparable:
            comparable = datetime.fromtimestamp(old["captured_at"], IST).date() < datetime.fromtimestamp(captured, IST).date()
        add("SETTLEMENT_BASELINE", "PASS" if comparable else "FAIL", "Baseline must precede this capture on a different IST date for the same account/universe")
        if comparable:
            for stock, current in inventories.items():
                before = test("PREVIOUS_INVENTORY_" + stock, lambda: inventory(stock, old["holdings"], old["positions"]))
                if before is not None and before["owned"] > 0 and (before["day_delta"] > 0 or before["t1"] > 0):
                    matches = current["opening"] == before["owned"] and current["settled"] > before["settled"] and current["t1"] == 0
                    add("SETTLEMENT_CONTINUITY_" + stock, "PASS" if matches else "NOT_OBSERVED", "Compare carried quantity and migration into settled holdings; review intervening activity")
                    settled = settled or matches
    observed("OVERNIGHT_SETTLEMENT", settled, "Needs same-account captures on different IST dates showing quantity continuity into settled holdings")
    failed = any(c["status"] == "FAIL" for c in checks)
    complete = all(c["status"] == "PASS" for c in coverage.values())
    live = not replay and snapshot.get("source") == "KITE_READ_ONLY"
    status = "BLOCKED" if failed else "EVIDENCE_COMPLETE" if complete and live else "INCOMPLETE"
    return dict(schema=1, status=status, source="OFFLINE_REPLAY" if replay else snapshot["source"],
                live_account_validated=bool(live and complete and not failed),
                captured_at=captured, account_fingerprint=snapshot.get("account_fingerprint"),
                available_cash=None if available is None else str(available), checks=checks, coverage=coverage,
                inventory=inventories, snapshot=snapshot,
                note="Observational acceptance evidence, not a production certificate or permission to submit trades.")


def save_private(path, report):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".account-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(dumps(report) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return str(path)
