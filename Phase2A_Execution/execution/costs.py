"""Account-bound forward cost allowances. Posted broker costs live in finance.py.

These are conservative estimates, never a claim about the eventual contract note.
Use both delivery and same-day scenarios even though the order product is CNC.
"""
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
from .market import IST
from .models import Fees, dec, dumps, positive


CENT = Decimal("0.01")
FIELDS = {"brokerage_bps", "brokerage_cap", "brokerage_min", "exchange_bps", "sebi_bps",
          "ipft_bps", "gst_pct", "stt_buy_bps", "stt_sell_bps", "stamp_buy_bps",
          "stamp_sell_bps", "buy_fixed", "sell_fixed", "dp_sell", "rounding_buffer"}


def digest(data):
    return hashlib.sha256(dumps(data).encode()).hexdigest()


def ceil_money(value):
    return dec(value).quantize(CENT, rounding=ROUND_CEILING)


class CostPlan:
    """Immutable-by-copy snapshot selected for one stock and one entry.

    Variable charges apply to turnover; order caps/minima/fixed charges and DP
    are reserved for every possible filled exit order. This deliberately
    over-reserves a DP charge that may only be posted once per scrip/day.
    """
    def __init__(self, data):
        self.data = deepcopy(data)
        scenarios = self.data["scenarios"]
        if not isinstance(scenarios, dict) or set(scenarios) != {"DELIVERY", "INTRADAY"}:
            raise ValueError("Cost plan needs DELIVERY and INTRADAY scenarios")
        count = self.data["exit_orders"]
        if type(count) is not int or not 1 <= count <= 100:
            raise ValueError("Invalid cost-plan exit order allowance")
        for rates in scenarios.values():
            if not isinstance(rates, dict) or set(rates) != FIELDS:
                raise ValueError("Cost schedule has missing or unknown fields")
            for key, value in rates.items():
                if key == "brokerage_cap" and value is None:
                    continue
                if isinstance(value, bool) or dec(value) < 0:
                    raise ValueError("Cost rates must be finite and nonnegative")
            if rates["brokerage_cap"] is not None and dec(rates["brokerage_min"]) > dec(rates["brokerage_cap"]):
                raise ValueError("Brokerage minimum exceeds cap")
            taxable = sum(dec(rates[k]) for k in ("brokerage_bps", "exchange_bps", "sebi_bps", "ipft_bps"))
            for side in ("buy", "sell"):
                variable = taxable * (1 + dec(rates["gst_pct"]) / 100)
                variable += dec(rates[f"stt_{side}_bps"]) + dec(rates[f"stamp_{side}_bps"])
                if variable >= 10000:
                    raise ValueError("Variable costs must be below turnover")
        rates = list(scenarios.values())
        taxable_bound = sum(dec(r["brokerage_bps"]) for r in rates)
        taxable_bound += sum(max(dec(r[k]) for r in rates) for k in ("exchange_bps", "sebi_bps", "ipft_bps"))
        taxable_bound *= 1 + max(dec(r["gst_pct"]) for r in rates) / 100
        for side in ("buy", "sell"):
            bound = taxable_bound + sum(max(dec(r[f"{component}_{side}_bps"]) for r in rates)
                                        for component in ("stt", "stamp"))
            if bound >= 10000:
                raise ValueError("Combined cost allowance must remain below turnover")

    def breakdown(self, scenario, side, value, orders=1):
        if side not in ("BUY", "SELL") or type(orders) is not int or orders < 1:
            raise ValueError("Invalid cost side or order count")
        value = positive(value)
        r = self.data["scenarios"][scenario]
        # Sum of per-order minima is bounded above by variable + N * minimum.
        # A cap is per ORDER, never per partial exchange fill.
        brokerage = value * dec(r["brokerage_bps"]) / 10000 + orders * dec(r["brokerage_min"])
        if r["brokerage_cap"] is not None:
            brokerage = min(brokerage, orders * dec(r["brokerage_cap"]))
        result = {"brokerage": ceil_money(brokerage)}
        for component in ("exchange", "sebi", "ipft"):
            result[component] = ceil_money(value * dec(r[component + "_bps"]) / 10000)
        result["gst"] = ceil_money(sum(result.values()) * dec(r["gst_pct"]) / 100)
        result["stt"] = ceil_money(value * dec(r[f"stt_{side.lower()}_bps"]) / 10000)
        result["stamp"] = ceil_money(value * dec(r[f"stamp_{side.lower()}_bps"]) / 10000)
        result["dp"] = ceil_money(orders * dec(r["dp_sell"])) if side == "SELL" else Decimal(0)
        result["allowance"] = ceil_money(orders * (dec(r[side.lower() + "_fixed"]) + dec(r["rounding_buffer"])))
        # Additional per-order rounding can exceed rounding on combined turnover.
        result["allowance"] += ceil_money((orders - 1) * CENT * (9 + 4 * dec(r["gst_pct"]) / 100))
        return result

    def cost(self, side, value, orders=1):
        parts = [self.breakdown(s, side, value, orders) for s in self.data["scenarios"]]
        bound = {key: max(p[key] for p in parts) for key in parts[0]}
        # A partly intraday buy can incur full-order intraday brokerage AND
        # delivery taxes on the retained shares. max(total_delivery, total_day)
        # is not an upper bound for that mixture. Combine component allowances.
        bound["brokerage"] = sum(p["brokerage"] for p in parts)
        gst = max(dec(s["gst_pct"]) for s in self.data["scenarios"].values()) / 100
        bound["gst"] = ceil_money(sum(bound[k] for k in ("brokerage", "exchange", "sebi", "ipft")) * gst)
        bound["allowance"] += ceil_money((orders - 1) * CENT * (1 + gst))
        return sum(bound.values())

    def target(self, average, quantity, tick, profit_pct):
        invested = positive(average) * quantity
        tick = positive(tick)
        goal = invested * positive(profit_pct) / 100
        buy = self.cost("BUY", invested)
        def sufficient(ticks):
            proceeds = ticks * tick * quantity
            return proceeds - invested - buy - self.cost("SELL", proceeds, self.data["exit_orders"]) >= goal
        low = int(average / tick)
        high = max(low + 1, 2 * low)
        for _ in range(128):
            if sufficient(high):
                break
            high *= 2
        else:
            raise ValueError("Cannot bound a fee-aware target")
        while high - low > 1:
            middle = (low + high) // 2
            if sufficient(middle):
                high = middle
            else:
                low = middle
        return high * tick


def load_plan(path, account_id, stock, now, exit_orders):
    def unique(pairs):
        data = {}
        for key, value in pairs:
            if key in data:
                raise ValueError("Duplicate cost profile field")
            data[key] = value
        return data
    with open(path) as handle:
        profile = json.load(handle, object_pairs_hook=unique)
    if not isinstance(profile, dict) or type(profile.get("schema")) is not int or profile["schema"] != 1 or profile.get("example") is not False:
        raise ValueError("Install a reviewed account cost profile, not the example template")
    if not account_id or profile.get("account_id") != account_id:
        raise ValueError("Cost profile belongs to a different account")
    start, end = date.fromisoformat(profile["valid_from"]), date.fromisoformat(profile["valid_until"])
    today = datetime.fromtimestamp(now, IST).date()
    if start > end or not start <= today <= end:
        raise ValueError("Account cost profile is outside its reviewed date coverage")
    for key in ("profile_id", "reviewed_by", "source_reference"):
        if not isinstance(profile.get(key), str) or not profile[key].strip():
            raise ValueError("Cost profile needs identity, reviewer and source reference")
    schedules = profile["stocks"]
    if not isinstance(schedules, dict) or stock not in schedules:
        raise ValueError("No reviewed cost schedule for this exact stock/exchange")
    return CostPlan(dict(schema=1, profile_id=profile["profile_id"], account_id=account_id,
                         profile_hash=digest(profile), source_reference=profile["source_reference"],
                         valid_from=profile["valid_from"], valid_until=profile["valid_until"],
                         stock=stock, scenarios=schedules[stock], exit_orders=exit_orders))


def trade_fees(trade):
    return CostPlan(trade["cost_plan"]) if trade.get("cost_plan") else Fees(**trade["fees"])


def affordable_quantity(budget, limit, fees):
    """Integer search includes capped commissions and fixed/rounded allowances."""
    low, high = 0, int(positive(budget) / positive(limit)) + 1
    while high - low > 1:
        quantity = (low + high) // 2
        value = quantity * limit
        if value + fees.cost("BUY", value) <= budget:
            low = quantity
        else:
            high = quantity
    return low
