"""Kite account contracts shared by the live adapter and read-only acceptance.

Reference: https://kite.trade/docs/connect/v3/portfolio/
Delivery sales reduce holdings.quantity AND appear in net CNC positions. Add
used_quantity back to reconstruct opening stock before applying today's net.
https://kite.trade/forum/discussion/comment/52974/
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from .models import dec

IST = ZoneInfo("Asia/Kolkata")


def integer(value, name, *, signed=False):
    if type(value) is not int or (not signed and value < 0):
        raise ValueError("Invalid integer field: " + name)
    return value


def check_profile(profile, stocks, expected_account):
    if not expected_account or profile.get("user_id") != expected_account:
        raise ValueError("Kite account does not match the configured account ID")
    for field in ("exchanges", "products", "order_types"):
        if not isinstance(profile.get(field), list) or not all(isinstance(v, str) for v in profile[field]):
            raise ValueError("Malformed account permissions: " + field)
    required = {s.split(":")[0] for s in stocks}
    if not required.issubset(set(profile["exchanges"])):
        raise ValueError("Account is missing a required exchange permission")
    if "CNC" not in profile["products"] or not {"LIMIT", "MARKET"}.issubset(set(profile["order_types"])):
        raise ValueError("Account must support CNC, LIMIT and MARKET orders")
    if profile.get("meta", {}).get("demat_consent") not in ("physical", "consent"):
        raise ValueError("Unknown depository sell-authorization mode; verify with the broker")


def cash_available(margins):
    if margins.get("enabled") is not True:
        raise ValueError("Equity segment is not enabled")
    available = margins["available"]
    # Never fall back to raw cash when current available funds are missing.
    # Collateral/adhoc margin is never added to the fully funded cash budget.
    return max(dec(0), min(dec(available["cash"]), dec(available["live_balance"]), dec(margins["net"])))


def inventory(stock, holdings, positions):
    exchange, symbol = stock.split(":")
    relevant = [r for r in holdings if r["tradingsymbol"] == symbol]
    if any(r["exchange"] != exchange for r in relevant):
        raise ValueError("Cross-exchange holdings require explicit reconciliation for " + stock)
    if len(relevant) > 1:
        raise ValueError("Duplicate holdings rows for " + stock)
    opening = settled = t1 = used = 0
    for row in relevant:
        if row["product"] != "CNC":
            raise ValueError("Non-CNC holding on managed stock")
        if (row.get("discrepancy", False) or row.get("collateral_quantity", 0) or
                row.get("short_quantity", 0) or row.get("mtf", {}).get("quantity", 0) or
                row.get("mtf", {}).get("used_quantity", 0)):
            raise ValueError("Restricted/pledged/MTF/discrepant holding requires reconciliation")
        settled = integer(row["quantity"], "quantity")
        t1 = integer(row["t1_quantity"], "t1_quantity")
        used = integer(row["used_quantity"], "used_quantity")
        opening = settled + t1 + used
        if integer(row["opening_quantity"], "opening_quantity") != opening:
            raise ValueError("Holdings opening quantity disagrees with settled/T1/used quantities")
    delta = 0
    for row in positions["net"]:
        if row["tradingsymbol"] != symbol:
            continue
        quantity = integer(row["quantity"], "position.quantity", signed=True)
        if row["exchange"] != exchange or row["product"] != "CNC":
            if quantity:
                raise ValueError("External/non-CNC/cross-exchange position on managed stock")
            continue
        if row.get("overnight_quantity", 0):
            raise ValueError("Overnight CNC position must be reconciled with holdings before trading")
        delta += quantity
    owned = opening + delta
    if owned < 0:
        raise ValueError("Negative reconciled inventory; missing holding or inconsistent broker snapshots")
    return dict(stock=stock, opening=opening, settled=settled, t1=t1, used=used,
                day_delta=delta, owned=owned)


def authorization(profile, holdings, stock, now):
    """Evidence only: current authorization metadata cannot guarantee a future fill."""
    mode = profile.get("meta", {}).get("demat_consent")
    if mode == "physical":
        return dict(status="PASS", mode=mode, detail="Broker reports standing depository authorization")
    if mode != "consent":
        return dict(status="FAIL", mode="unknown", detail="Broker authorization mode is unknown")
    rows = [r for r in holdings if r["exchange"] + ":" + r["tradingsymbol"] == stock]
    required = sum(integer(r["quantity"], "quantity") + integer(r["t1_quantity"], "t1_quantity") for r in rows)
    if not required:
        return dict(status="NOT_OBSERVED", mode=mode, detail="No delivery holdings to check current electronic authorization")
    current = datetime.fromtimestamp(now, IST)
    for row in rows:
        raw = row.get("authorised_date")
        try:
            date = raw if isinstance(raw, datetime) else datetime.fromisoformat(raw)
            date = date.replace(tzinfo=IST) if date.tzinfo is None else date.astimezone(IST)
        except (TypeError, ValueError):
            return dict(status="FAIL", mode=mode, detail="Missing or invalid authorization date")
        if date.date() != current.date() or (current.hour, current.minute) >= (17, 30):
            return dict(status="FAIL", mode=mode, detail="Electronic authorization is not verified for the current session")
        remaining = integer(row["quantity"], "quantity") + integer(row["t1_quantity"], "t1_quantity")
        # Conservative: don't count units already used against today's consent.
        authorized = integer(row["authorised_quantity"], "authorised_quantity") - integer(row["used_quantity"], "used_quantity")
        if authorized < remaining:
            return dict(status="FAIL", mode=mode, detail="Insufficient unused electronic sell authorization")
    return dict(status="PASS", mode=mode, detail="Current-session electronic authorization covers observed holdings")
