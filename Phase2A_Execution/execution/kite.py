"""Optional Kite SDK adapter. Instantiation requires explicit live enablement.

    https://kite.trade/docs/connect/v3/orders/
    https://kite.trade/docs/connect/v3/market-quotes/
    https://kite.trade/docs/connect/v3/portfolio/

Tags are recovery hints, NOT broker-side idempotency guarantees. No POST retries.
"""
from datetime import datetime
from zoneinfo import ZoneInfo
import time
from .models import Quote, Order, dec


class KiteBroker:
    def __init__(self, client, stocks, *, live=False, min_interval=0.35):
        if not live:
            raise ValueError("Live broker requires explicit live=True")
        self.client = client
        self.min_interval, self._last_call, self._last_quote = min_interval, 0.0, 0.0
        self.identity = "kite:" + self._call(client.profile)["user_id"]
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
        available = self._call(self.client.margins, "equity")["available"]
        # Use cash, never collateral or leverage.
        return max(dec(0), min(dec(available["cash"]), dec(available.get("live_balance", available["cash"]))))

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
        return Order(str(row["order_id"]), row.get("tag") or "",
                     row["exchange"] + ":" + row["tradingsymbol"], row["transaction_type"],
                     int(row["quantity"]), int(row["filled_quantity"]), dec(row["average_price"]), row["status"])

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
        # Holdings are settled + T1 opening stock; net CNC positions include today's buys/sells.
        holdings = sum(int(r["quantity"]) + int(r.get("t1_quantity", 0))
                       for r in self._call(self.client.holdings) if match(r))
        positions = self._call(self.client.positions)["net"]
        if any(match(r) and r["product"] != "CNC" and r["quantity"] for r in positions):
            raise ValueError("External/non-CNC position on managed stock")
        owned = holdings + sum(int(r["quantity"]) for r in positions if match(r) and r["product"] == "CNC")
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
