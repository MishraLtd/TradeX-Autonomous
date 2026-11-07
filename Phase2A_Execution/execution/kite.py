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
from .models import Order, dec
from .protection import Protection, ProtectionSpec
from .account import cash_available, check_profile, inventory
from .market import MarketCalendar, MarketClosed
from .marketdata import QuoteBook
from .sessions import SessionClient


class KiteBroker:
    protection_required = True
    supports_emergency_exit = True  # SELL submission uses broker market_protection=-1
    requires_market_hours = True
    supports_submission_timing = True

    def __init__(self, client, stocks, *, live=False, min_interval=0.35, expected_account=None,
                 calendar=None, clock=time.time, store=None, max_quote_age=15):
        if not live:
            raise ValueError("Live broker requires explicit live=True")
        self.client = client
        self.calendar, self.clock = calendar or MarketCalendar.load(), clock
        self.stocks, self.expected_account = stocks, expected_account
        self.min_interval, self._last_call, self._last_quote = min_interval, 0.0, 0.0
        self.quotes = QuoteBook(clock, max_quote_age, store)
        self._polling, self._order_snapshot, self._watched = False, None, set()
        self.poll_failed = False
        self.identity = "kite:" + self._call(client.profile)["user_id"]
        if expected_account is not None and self.identity != "kite:" + expected_account:
            raise ValueError("Kite account does not match the configured account ID")
        self.validate_instruments()

    def validate_instruments(self):
        for exchange in {s.split(":")[0] for s in self.stocks}:
            instruments = {r["tradingsymbol"]: r for r in self._call(self.client.instruments, exchange)}
            for stock, tick in self.stocks.items():
                ex, symbol = stock.split(":")
                if ex != exchange:
                    continue
                row = instruments.get(symbol)
                if not row or row["instrument_type"] != "EQ" or row["lot_size"] != 1 or dec(row["tick_size"]) != dec(tick):
                    raise ValueError("Whitelist/tick does not match Kite equity instrument: " + stock)

    def preflight(self):
        self.assert_entry_ready()
        self.validate_instruments()
        return dict(cash_available=str(self.available_cash()))

    def _call(self, method, *args, **kwargs):
        time.sleep(max(0, self.min_interval - (time.monotonic() - self._last_call)))
        self._last_call = time.monotonic()
        try:
            return method(*args, **kwargs)
        except MarketClosed:
            raise  # a local window guard is not a disconnected broker
        except Exception:
            self.poll_failed = True
            self._order_snapshot = None
            raise

    def begin_poll(self, trades):
        self._polling, self.poll_failed, self._order_snapshot = True, False, None
        self.quotes.clear()
        self._watched = {t["stock"] for t in trades if t["state"] not in ("CLOSED", "NO_FILL") and
                         self.calendar.status(t["stock"].split(":")[0], self.clock())["state"] == "OPEN"}
        self.fetch_quotes(self._watched)

    def end_poll(self):
        self._polling, self._order_snapshot, self._watched = False, None, set()
        self.quotes.clear()

    def fetch_quotes(self, stocks):
        stocks = sorted(set(stocks))
        for start in range(0, len(stocks), 500):
            batch = stocks[start:start + 500]
            time.sleep(max(0, (1.05 if self.min_interval else 0) - (time.monotonic() - self._last_quote)))
            try:
                rows = self._call(self.client.quote, batch)
                if not isinstance(rows, dict):
                    raise ValueError("Invalid quote snapshot")
                for stock in batch:
                    try:
                        self.quotes.accept(stock, rows.get(stock))
                    except ValueError:
                        self.poll_failed = True  # other stocks and order reconciliation still proceed
            except Exception as exc:
                self.poll_failed = True
                for stock in batch:
                    self.quotes.fail(stock, "Quote request failed (" + type(exc).__name__ + ")")
            finally:
                self._last_quote = time.monotonic()

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
        self.calendar.require_open(stock, self.clock())
        if not self._polling or stock not in self._watched:
            self.fetch_quotes([stock])
        try:
            return self.quotes.get(stock)
        except ValueError:
            self.poll_failed = True
            raise

    def cached_quote(self, stock):
        try:
            return self.quotes.get(stock)
        except ValueError:
            self.poll_failed = True
            return None

    @staticmethod
    def _order(row):
        placed = row.get("order_timestamp")
        if placed is not None:
            if isinstance(placed, str):
                placed = datetime.fromisoformat(placed)
            if placed.tzinfo is None:
                placed = placed.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
            placed = placed.timestamp()
        updated = row.get("exchange_update_timestamp") or row.get("order_timestamp")
        if updated is not None:
            updated = QuoteBook.epoch(updated)
        return Order(str(row["order_id"]), row.get("tag") or "",
                     row["exchange"] + ":" + row["tradingsymbol"], row["transaction_type"],
                     int(row["quantity"]), int(row["filled_quantity"]), dec(row["average_price"]), row["status"],
                     placed, row.get("status_message_raw") or row.get("status_message"), updated)

    def _orders(self):
        if self._order_snapshot is not None and not 0 <= self.clock() - self._order_snapshot_at <= self.quotes.max_age:
            self._order_snapshot = None
        if self._order_snapshot is None:
            rows = self._call(self.client.orders)
            ids = [str(row["order_id"]) for row in rows]
            if len(ids) != len(set(ids)):
                raise ValueError("Duplicate IDs in broker order snapshot")
            if self._polling:
                self._order_snapshot = rows
                self._order_snapshot_at = self.clock()
            return rows
        return self._order_snapshot

    def order(self, order_id):
        if self._polling:
            row = next((r for r in self._orders() if str(r["order_id"]) == str(order_id)), None)
            if row is not None:
                return self._order(row)
        history = self._call(self.client.order_history, order_id)
        if not history:
            raise ValueError("No broker history; reconcile using trade journal")
        return self._order(history[-1])

    def find_by_tag(self, tag):
        matches = [self._order(r) for r in self._orders() if r.get("tag") == tag]
        if len(matches) > 1:
            raise ValueError("Multiple broker orders share one intent tag")
        return matches[0] if matches else None

    def assert_inventory(self, stock, expected, own_order_ids):
        self._check_inventory(stock, expected, own_order_ids, self._call(self.client.holdings),
                              self._call(self.client.positions), self._call(self.client.orders))

    @classmethod
    def _check_inventory(cls, stock, expected, own_order_ids, holdings, positions, orders):
        def match(row):
            return row["exchange"] + ":" + row["tradingsymbol"] == stock
        snapshot = inventory(stock, holdings, positions)
        owned = snapshot["owned"]
        if owned != expected:
            raise ValueError(f"Inventory mismatch for {stock}: broker={owned}, engine={expected}")
        for row in orders:
            if match(row) and str(row["order_id"]) not in own_order_ids and not cls._order(row).terminal:
                raise ValueError("Unmanaged pending order on managed stock")

    def preflight_inventory(self, expected, order_ids, trigger_ids):
        # One account snapshot per daily check, rather than four requests for
        # every symbol in a potentially large approved universe.
        holdings = self._call(self.client.holdings)
        positions = self._call(self.client.positions)
        orders = self._call(self.client.orders)
        triggers = self._call(self.client.get_gtts)
        for stock, quantity in expected.items():
            self._check_inventory(stock, quantity, order_ids.get(stock, []), holdings, positions, orders)
        for row in triggers:
            stock = self._gtt_stock(row)
            if (stock in expected and str(row["id"]) not in trigger_ids.get(stock, []) and
                    row["status"] not in ("deleted", "cancelled", "expired", "rejected")):
                raise ValueError("Unmanaged GTT on managed stock; reconcile it before trading")

    def submit(self, stock, side, quantity, tag, limit=None, *, on_send=None):
        self.calendar.require_open(stock, self.clock(), buffer=60 if side == "BUY" else 10)
        exchange, symbol = stock.split(":")
        args = dict(variety="regular", exchange=exchange, tradingsymbol=symbol,
                    transaction_type=side, quantity=quantity, product="CNC", validity="IOC", tag=tag)
        if side == "BUY":
            if limit is None:
                raise ValueError("Entry requires a bounded limit price")
            args.update(order_type="LIMIT", price=float(limit))
        else:
            args.update(order_type="MARKET", market_protection=-1)
        def before_send():
            self.calendar.require_open(stock, self.clock(), buffer=60 if side == "BUY" else 10)
            if on_send is not None:
                on_send()
        def send():
            if isinstance(self.client, SessionClient):
                return self.client.request("place_order", before_send=before_send, **args)
            before_send()
            return self.client.place_order(**args)
        try:
            return str(self._call(send))
        finally:
            self._order_snapshot = None  # includes an ambiguous POST; never reuse an earlier book

    def cancel(self, order_id):
        try:
            return self._call(self.client.cancel_order, variety="regular", order_id=order_id)
        finally:
            self._order_snapshot = None

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
        self.calendar.require_open(spec.stock, self.clock())
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
