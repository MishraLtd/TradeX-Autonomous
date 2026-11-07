"""Validated quote snapshots. Failed reads never fall back to a last-good price."""
from datetime import datetime
from .market import IST
from .models import Quote, dec


class QuoteBook:
    def __init__(self, clock, max_age=15, store=None):
        self.clock, self.max_age, self.store = clock, max_age, store
        self.states = store.get("market_data", {}) if store else {}
        self.current = {}

    def _save(self):
        if self.store:
            self.store.set("market_data", self.states)

    def clear(self):
        self.current.clear()  # retain validated watermarks across reconnect/restart

    def fail(self, stock, reason):
        self.current.pop(stock, None)
        previous = self.states.get(stock, {})
        self.states[stock] = dict(previous, state="UNAVAILABLE", checked_at=self.clock(), error=reason)
        self._save()

    @staticmethod
    def epoch(value):
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if not isinstance(value, datetime):
            raise ValueError("Missing exchange/last-trade timestamp")
        return (value.replace(tzinfo=IST) if value.tzinfo is None else value).timestamp()

    def accept(self, stock, row):
        now = self.clock()
        try:
            if not isinstance(row, dict):
                raise ValueError("Missing quote in broker snapshot")
            exchange = self.epoch(row.get("timestamp"))
            traded = self.epoch(row.get("last_trade_time"))
            price = dec(row["last_price"])
            # Validate BOTH clocks: taking their minimum can conceal a future timestamp.
            Quote(price, exchange).check(now, self.max_age)
            Quote(price, traded).check(now, self.max_age)
            if traded > exchange + 2:
                raise ValueError("Last trade is newer than the exchange quote")
            previous = self.states.get(stock, {})
            if (exchange < previous.get("exchange_at", exchange) or traded < previous.get("trade_at", traded)):
                raise ValueError("Quote timestamps moved backwards")
            quote = Quote(price, min(exchange, traded))
        except Exception as exc:
            # Store fixed descriptions, never broker response payloads or SDK URLs.
            self.fail(stock, "Invalid, missing, stale, future or regressive quote (" + type(exc).__name__ + ")")
            raise ValueError(self.states[stock]["error"]) from None
        self.current[stock] = quote
        self.states[stock] = dict(state="FRESH", exchange_at=exchange, trade_at=traded,
                                  received_at=now, checked_at=now, error=None)
        self._save()
        return quote

    def get(self, stock):
        quote = self.current.get(stock)
        if quote is None:
            raise ValueError("No validated quote in this polling cycle")
        try:
            quote.check(self.clock(), self.max_age)
            received = self.states[stock]["received_at"]
            if not 0 <= self.clock() - received <= self.max_age:
                raise ValueError("Quote receipt is stale or the local clock moved backwards")
        except ValueError:
            self.fail(stock, "Quote expired before use")
            raise
        return quote
