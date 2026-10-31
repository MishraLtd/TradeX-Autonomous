"""Broker contract and persistent deterministic paper execution."""
from dataclasses import asdict, replace
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import time
from typing import Protocol
import uuid
from .models import Fees, Quote, Order, dec, positive, dumps
from .protection import Protection, ProtectionSpec


class Broker(Protocol):
    identity: str
    protection_required: bool
    supports_emergency_exit: bool
    def quote(self, stock) -> Quote: ...
    def available_cash(self) -> Decimal: ...
    def assert_inventory(self, stock, expected, own_order_ids): ...
    def submit(self, stock, side, quantity, tag, limit=None) -> str: ...
    def order(self, order_id) -> Order: ...
    def find_by_tag(self, tag) -> Order | None: ...
    def cancel(self, order_id): ...
    def assert_no_protection(self, stock, own_ids): ...
    def protection_ids(self, stock): ...
    def create_protection(self, spec, last_price) -> str: ...
    def protection(self, trigger_id) -> Protection: ...
    def cancel_protection(self, trigger_id): ...


class PaperBroker:
    """IOC fills at the current paper quote; optional partial fills and slippage.

    Prices are supplied explicitly, never generated or silently refreshed.
    Broker orders survive engine crashes independently of the engine database.
    """
    protection_required = False
    supports_emergency_exit = False  # paper fills still require a fresh injected price

    def __init__(self, path, cash="10000", fees=None, clock=time.time, max_fill=None, slippage_bps="0"):
        path = Path(path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.identity = "paper:" + str(path)
        self.clock, self.max_fill = clock, max_fill
        self.slippage = dec(slippage_bps)
        if self.slippage < 0 or (max_fill is not None and (type(max_fill) is not int or max_fill < 0)):
            raise ValueError("Invalid fill simulation")
        self.fees = fees or Fees()
        self.db = sqlite3.connect(path, timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY, tag TEXT UNIQUE, data TEXT);
            CREATE TABLE IF NOT EXISTS quotes(stock TEXT PRIMARY KEY, price TEXT, at REAL);
            CREATE TABLE IF NOT EXISTS account(id INTEGER PRIMARY KEY CHECK(id=1), cash TEXT, fees TEXT);
            CREATE TABLE IF NOT EXISTS protections(id TEXT PRIMARY KEY, data TEXT);
        """)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO account VALUES (1,?,?)", (str(positive(cash)), dumps(asdict(self.fees))))
        if self.db.execute("SELECT fees FROM account").fetchone()[0] != dumps(asdict(self.fees)):
            raise ValueError("Paper account fee profile changed; use a new paper account database")

    def set_quote(self, stock, price, timestamp=None):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("INSERT OR REPLACE INTO quotes VALUES (?,?,?)", (
                stock, str(positive(price)), self.clock() if timestamp is None else timestamp))
            # Broker-side triggers advance on market events WITHOUT Engine.tick().
            # An old replay event never triggers an order as if it were live.
            self.quote(stock).check(self.clock(), 15)
            self._process_protections(stock)

    def quote(self, stock):
        row = self.db.execute("SELECT price,at FROM quotes WHERE stock=?", (stock,)).fetchone()
        if not row:
            raise ValueError("No paper quote for " + stock)
        return Quote(dec(row[0]), row[1])

    def available_cash(self):
        return dec(self.db.execute("SELECT cash FROM account").fetchone()[0])

    def _orders(self):
        return [self._decode(r[0]) for r in self.db.execute("SELECT data FROM orders")]

    @staticmethod
    def _decode(data):
        value = json.loads(data)
        value["average"] = dec(value["average"])
        return Order(**value)

    def assert_inventory(self, stock, expected, own_order_ids):
        owned = sum(o.filled * (1 if o.side == "BUY" else -1) for o in self._orders() if o.stock == stock)
        if owned != expected:
            raise ValueError(f"Inventory mismatch for {stock}: broker={owned}, engine={expected}")

    def submit(self, stock, side, quantity, tag, limit=None):
        if side not in ("BUY", "SELL") or type(quantity) is not int or quantity < 1:
            raise ValueError("Invalid order")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            existing = self.find_by_tag(tag)
            if existing:
                return existing.id
            quote = self.quote(stock)
            quote.check(self.clock(), 15)
            price = quote.price * (1 + self.slippage / 10000 * (1 if side == "BUY" else -1))
            positive(price)
            filled = quantity if self.max_fill is None else min(quantity, self.max_fill)
            status = "COMPLETE"
            if side == "BUY" and (limit is None or price > dec(limit)):
                filled = 0
            if side == "SELL":
                owned = sum(o.filled * (1 if o.side == "BUY" else -1) for o in self._orders() if o.stock == stock)
                if quantity > owned:
                    filled, status = 0, "REJECTED"
            value = price * filled
            charges = self.fees.cost(side, value) if filled else Decimal(0)
            cash = self.available_cash()
            if side == "BUY" and value + charges > cash:
                filled, status, value, charges = 0, "REJECTED", Decimal(0), Decimal(0)
            if status != "REJECTED" and filled < quantity:
                status = "CANCELLED"
            cash += (-value if side == "BUY" else value) - charges
            order = Order(uuid.uuid4().hex, tag, stock, side, quantity, filled, price if filled else Decimal(0),
                          status, self.clock(), "Paper inventory/cash check failed" if status == "REJECTED" else None)
            self.db.execute("INSERT INTO orders VALUES (?,?,?)", (order.id, tag, dumps(asdict(order))))
            self.db.execute("UPDATE account SET cash=? WHERE id=1", (str(cash),))
            return order.id

    def order(self, order_id):
        row = self.db.execute("SELECT data FROM orders WHERE id=?", (order_id,)).fetchone()
        if not row:
            raise ValueError("Unknown paper order")
        return self._decode(row[0])

    def find_by_tag(self, tag):
        row = self.db.execute("SELECT data FROM orders WHERE tag=?", (tag,)).fetchone()
        return self._decode(row[0]) if row else None

    def cancel(self, order_id):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            order = self.order(order_id)
            if not order.terminal:
                self._save_order(replace(order, status="CANCELLED"))
        return self.order(order_id)

    def _save_order(self, order):
        self.db.execute("INSERT OR REPLACE INTO orders VALUES (?,?,?)",
                        (order.id, order.tag, dumps(asdict(order))))

    def _save_protection(self, protection):
        self.db.execute("INSERT OR REPLACE INTO protections VALUES (?,?)",
                        (protection.id, dumps(asdict(protection))))

    def protection_ids(self, stock):
        return [row[0] for row in self.db.execute("SELECT id FROM protections")
                if self.protection(row[0]).spec.stock == stock]

    def assert_no_protection(self, stock, own_ids):
        for trigger_id in self.protection_ids(stock):
            p = self.protection(trigger_id)
            if trigger_id not in own_ids and p.status not in ("deleted", "cancelled", "expired", "rejected"):
                raise ValueError("Unmanaged GTT on managed stock")

    def create_protection(self, spec, last_price):
        if not dec(spec.stop) < dec(last_price) < dec(spec.target):
            raise ValueError("Market price already outside protection levels")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            p = Protection(uuid.uuid4().hex, "active", spec)
            self._save_protection(p)
            return p.id

    def protection(self, trigger_id):
        row = self.db.execute("SELECT data FROM protections WHERE id=?", (trigger_id,)).fetchone()
        if not row:
            raise ValueError("Missing broker protection record")
        data = json.loads(row[0])
        data["spec"] = ProtectionSpec(**data["spec"])
        data["child_ids"] = tuple(data["child_ids"])
        return Protection(**data)

    def cancel_protection(self, trigger_id):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            p = self.protection(trigger_id)
            if p.status in ("active", "disabled"):
                self._save_protection(Protection(p.id, "deleted", p.spec, p.child_ids,
                                                p.triggered_leg, p.submission_failed))

    def _process_protections(self, stock):
        price = self.quote(stock).price
        for trigger_id in self.protection_ids(stock):
            p = self.protection(trigger_id)
            if p.status == "active" and (price <= dec(p.spec.stop) or price >= dec(p.spec.target)):
                leg = "STOP_LOSS" if price <= dec(p.spec.stop) else "PROFIT_TARGET"
                child = Order(uuid.uuid4().hex, "gtt:" + p.id, stock, "SELL", p.spec.quantity,
                              0, Decimal(0), "OPEN", self.clock())
                self._save_order(child)
                p = Protection(p.id, "triggered", p.spec, (child.id,), leg)
                self._save_protection(p)
            if p.status == "triggered" and p.child_ids:
                order = self.order(p.child_ids[0])
                if order.terminal:
                    continue
                limit = dec(p.spec.stop_limit if p.triggered_leg == "STOP_LOSS" else p.spec.target_limit)
                # DAY limit order: a gap below its price leaves it OPEN. OCO
                # consumes the trigger even if this child doesn't fill.
                if price >= limit:
                    self._fill_gtt_child(order, max(limit, price * (1 - self.slippage / 10000)))

    def _fill_gtt_child(self, order, price):
        remaining = order.quantity - order.filled
        owned = sum(o.filled * (1 if o.side == "BUY" else -1)
                    for o in self._orders() if o.stock == order.stock)
        if owned < remaining:
            self._save_order(replace(order, status="REJECTED", message="Paper inventory check failed"))
            return
        quantity = remaining if self.max_fill is None else min(remaining, self.max_fill)
        if not quantity:
            return
        value = price * quantity
        fees = value * dec(self.fees.sell_bps) / 10000 + (dec(self.fees.sell_fixed) if not order.filled else 0)
        average = (order.average * order.filled + value) / (order.filled + quantity)
        self._save_order(replace(order, filled=order.filled + quantity, average=average,
                                 status="COMPLETE" if quantity == remaining else "OPEN"))
        self.db.execute("UPDATE account SET cash=? WHERE id=1", (str(self.available_cash() + value - fees),))

    def close(self):
        self.db.close()
