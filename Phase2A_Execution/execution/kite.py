"""Optional Kite SDK adapter. Instantiation requires explicit live enablement.

    https://kite.trade/docs/connect/v3/orders/
    https://kite.trade/docs/connect/v3/market-quotes/
    https://kite.trade/docs/connect/v3/portfolio/
    https://kite.trade/docs/connect/v3/gtt/

Tags are recovery hints, NOT broker-side idempotency guarantees. No POST retries.
"""
from datetime import datetime
from zoneinfo import ZoneInfo
import time
from .models import Quote, Order, dec
from .protection import Protection, ProtectionSpec
from .account import cash_available, check_profile, inventory


class KiteBroker:
    protection_required = True
    supports_emergency_exit = True  # SELL submission uses broker market_protection=-1

    def __init__(self, client, stocks, *, live=False, min_interval=0.35, expected_account=None):
        if not live:
            raise ValueError("Live broker requires explicit live=True")
        self.client = client
        self.stocks, self.expected_account = stocks, expected_account
        self.min_interval, self._last_call, self._last_quote = min_interval, 0.0, 0.0
        self.identity = "kite:" + self._call(client.profile)["user_id"]
        if expected_account is not None and self.identity != "kite:" + expected_account:
            raise ValueError("Kite account does not match the configured account ID")
        for exchange in {s.split(":")[0] for s in stocks}:
            instruments = {r["tradingsymbol"]: r for r in self._call(client.instruments, exchange)}
            for stock, tick in stocks.items():
                ex, symbol = stock.split(":")
                if ex != exchange:
                    continue
                row = instruments.get(symbol)
                if not row or row["instrument_type"] != "EQ" or row["lot_size"] != 1 or dec(row["tick_size"]) != dec(tick):
                    raise ValueError("Whitelist/tick does not match Kite equity instrument: " + stock)

    def _call(self, method, *args, **kwargs):
        time.sleep(max(0, self.min_interval - (time.monotonic() - self._last_call)))
        self._last_call = time.monotonic()
        return method(*args, **kwargs)

    def available_cash(self):
        return cash_available(self._call(self.client.margins, "equity"))

    def assert_entry_ready(self):
        if not self.expected_account:
            raise ValueError("Configure live_account_id before requesting a new live entry")
        profile = self._call(self.client.profile)
        check_profile(profile, self.stocks, self.expected_account)
        if self.identity != "kite:" + profile["user_id"]:
            raise ValueError("Broker account changed during this session")

    def quote(self, stock):
        time.sleep(max(0, (1.05 if self.min_interval else 0) - (time.monotonic() - self._last_quote)))
        row = self._call(self.client.quote, stock).get(stock)
        self._last_quote = time.monotonic()
        if not row or not row.get("timestamp") or not row.get("last_trade_time"):
            raise ValueError("Incomplete Kite quote")
        def epoch(value):
            if isinstance(value, str):
                value = datetime.fromisoformat(value)
            if value.tzinfo is None:
                value = value.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
            return value.timestamp()
        return Quote(dec(row["last_price"]), min(epoch(row["timestamp"]), epoch(row["last_trade_time"])))

    @staticmethod
    def _order(row):
        placed = row.get("order_timestamp")
        if placed is not None:
            if isinstance(placed, str):
                placed = datetime.fromisoformat(placed)
            if placed.tzinfo is None:
                placed = placed.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
            placed = placed.timestamp()
        return Order(str(row["order_id"]), row.get("tag") or "",
                     row["exchange"] + ":" + row["tradingsymbol"], row["transaction_type"],
                     int(row["quantity"]), int(row["filled_quantity"]), dec(row["average_price"]), row["status"],
                     placed, row.get("status_message_raw") or row.get("status_message"))

    def order(self, order_id):
        history = self._call(self.client.order_history, order_id)
        if not history:
            raise ValueError("No broker history; reconcile using trade journal")
        return self._order(history[-1])

    def find_by_tag(self, tag):
        matches = [self._order(r) for r in self._call(self.client.orders) if r.get("tag") == tag]
        if len(matches) > 1:
            raise ValueError("Multiple broker orders share one intent tag")
        return matches[0] if matches else None

    def assert_inventory(self, stock, expected, own_order_ids):
        def match(row):
            return row["exchange"] + ":" + row["tradingsymbol"] == stock
        snapshot = inventory(stock, self._call(self.client.holdings), self._call(self.client.positions))
        owned = snapshot["owned"]
        if owned != expected:
            raise ValueError(f"Inventory mismatch for {stock}: broker={owned}, engine={expected}")
        for row in self._call(self.client.orders):
            if match(row) and str(row["order_id"]) not in own_order_ids and not self._order(row).terminal:
                raise ValueError("Unmanaged pending order on managed stock")

    def submit(self, stock, side, quantity, tag, limit=None):
        exchange, symbol = stock.split(":")
        args = dict(variety="regular", exchange=exchange, tradingsymbol=symbol,
                    transaction_type=side, quantity=quantity, product="CNC", validity="IOC", tag=tag)
        if side == "BUY":
            if limit is None:
                raise ValueError("Entry requires a bounded limit price")
            args.update(order_type="LIMIT", price=float(limit))
        else:
            args.update(order_type="MARKET", market_protection=-1)
        return str(self._call(self.client.place_order, **args))

    def cancel(self, order_id):
        return self._call(self.client.cancel_order, variety="regular", order_id=order_id)

    def protection_ids(self, stock):
        return [str(row["id"]) for row in self._call(self.client.get_gtts)
                if self._gtt_stock(row) == stock]

    @staticmethod
    def _gtt_stock(row):
        condition = row["condition"]
        return condition["exchange"] + ":" + condition["tradingsymbol"]

    def assert_no_protection(self, stock, own_ids):
        for row in self._call(self.client.get_gtts):
            if (self._gtt_stock(row) == stock and str(row["id"]) not in own_ids and
                    row["status"] not in ("deleted", "cancelled", "expired", "rejected")):
                raise ValueError("Unmanaged GTT on managed stock; reconcile it before trading")

    def create_protection(self, spec, last_price):
        if not dec(spec.stop) < dec(last_price) < dec(spec.target):
            raise ValueError("Market price already outside protection levels")
        exchange, symbol = spec.stock.split(":")
        # Kite's SDK deliberately strips unknown order fields. GTT does not
        # accept our regular-order tags; never pretend this POST is idempotent.
        orders = [dict(transaction_type="SELL", quantity=spec.quantity, order_type="LIMIT",
                       product="CNC", price=float(price)) for price in (spec.stop_limit, spec.target_limit)]
        result = self._call(self.client.place_gtt, trigger_type="two-leg", tradingsymbol=symbol,
                            exchange=exchange, trigger_values=[float(spec.stop), float(spec.target)],
                            last_price=float(last_price), orders=orders)
        return str(result["trigger_id"])

    def protection(self, trigger_id):
        row = self._call(self.client.get_gtt, trigger_id)
        return self.decode_protection(row, trigger_id)

    @staticmethod
    def decode_protection(row, trigger_id):
        if str(row["id"]) != str(trigger_id):
            raise ValueError("Unexpected GTT ID")
        orders = row["orders"]
        values = row["condition"]["trigger_values"]
        stock = KiteBroker._gtt_stock(row)
        if row["type"] != "two-leg" or len(orders) != 2 or len(values) != 2:
            raise ValueError("Expected two-leg GTT")
        for o in orders:
            if (o["exchange"] + ":" + o["tradingsymbol"] != stock or o["product"] != "CNC" or
                    o["transaction_type"] != "SELL" or o["order_type"] != "LIMIT" or
                    o["quantity"] != orders[0]["quantity"]):
                raise ValueError("Invalid GTT order contract")
        spec = ProtectionSpec(stock, orders[0]["quantity"], str(values[0]), str(values[1]),
                              str(orders[0]["price"]), str(orders[1]["price"]))
        ids, leg, failed, pending = [], None, False, False
        results = [(i, o["result"]) for i, o in enumerate(orders) if o.get("result") is not None]
        if len(results) > 1 or (results and row["status"] in ("active", "disabled")):
            raise ValueError("Inconsistent OCO results")
        for index, result in results:
            leg = "STOP_LOSS" if index == 0 else "PROFIT_TARGET"
            outcome = result.get("order_result", {})
            if outcome.get("order_id"):
                ids.append(str(outcome["order_id"]))
            else:
                failed = outcome.get("status") == "failed"
                pending = not failed
        return Protection(str(row["id"]), row["status"], spec, tuple(ids), leg, failed, pending)

    def cancel_protection(self, trigger_id):
        return self._call(self.client.delete_gtt, trigger_id)
