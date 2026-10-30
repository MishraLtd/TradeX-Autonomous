"""Broker contract and persistent deterministic paper execution."""
from dataclasses import asdict
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import time
from typing import Protocol
import uuid
from .models import Fees, Quote, Order, dec, positive, dumps


class Broker(Protocol):
    identity: str
    def quote(self, stock) -> Quote: ...
    def available_cash(self) -> Decimal: ...
    def assert_inventory(self, stock, expected, own_order_ids): ...
    def submit(self, stock, side, quantity, tag, limit=None) -> str: ...
    def order(self, order_id) -> Order: ...
    def find_by_tag(self, tag) -> Order | None: ...
    def cancel(self, order_id): ...


class PaperBroker:
    """IOC fills at the current paper quote; optional partial fills and slippage.

    Prices are supplied explicitly, never generated or silently refreshed.
    Broker orders survive engine crashes independently of the engine database.
    """
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
        """)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO account VALUES (1,?,?)", (str(positive(cash)), dumps(asdict(self.fees))))
        if self.db.execute("SELECT fees FROM account").fetchone()[0] != dumps(asdict(self.fees)):
            raise ValueError("Paper account fee profile changed; use a new paper account database")

    def set_quote(self, stock, price, timestamp=None):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO quotes VALUES (?,?,?)", (
                stock, str(positive(price)), self.clock() if timestamp is None else timestamp))

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
            order = Order(uuid.uuid4().hex, tag, stock, side, quantity, filled, price if filled else Decimal(0), status)
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
        return self.order(order_id)  # IOC simulator has no resting orders

    def close(self):
        self.db.close()
