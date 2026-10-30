from dataclasses import asdict, dataclass, field
from decimal import Decimal, ROUND_CEILING
import json
import math
import re


def dec(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Money must be finite")
    return result


def positive(value):
    result = dec(value)
    if result <= 0:
        raise ValueError("Value must be positive")
    return result


def dumps(value):
    return json.dumps(value, default=str, sort_keys=True, allow_nan=False)


@dataclass(frozen=True)
class Fees:
    """Explicit ESTIMATES, not a statutory fee schedule. Values are per order.

    A sell fixed charge may include a conservative delivery/DP allowance.
    Commission is separate from fill-price slippage. Configure for your account.
    """
    buy_bps: str = "15"
    sell_bps: str = "15"
    buy_fixed: str = "0"
    sell_fixed: str = "0"

    def __post_init__(self):
        for value in asdict(self).values():
            if dec(value) < 0:
                raise ValueError("Negative fee")
        if dec(self.sell_bps) >= 10000:
            raise ValueError("Sell fee must be below 100%")

    def cost(self, side, value):
        return dec(value) * dec(getattr(self, side.lower() + "_bps")) / 10000 + dec(
            getattr(self, side.lower() + "_fixed"))


@dataclass(frozen=True)
class Config:
    # Exact exchange:symbol whitelist with instrument-master tick sizes.
    stocks: dict
    capital_limit: str
    per_trade_limit: str
    profit_pct: str
    stop_pct: str
    fees: Fees = field(default_factory=Fees)
    max_positions: int = 5
    max_quote_age: float = 15
    max_exit_attempts: int = 3
    entry_timeout: float = 30
    product: str = "CNC"

    def __post_init__(self):
        if not self.stocks:
            raise ValueError("At least one approved stock is required")
        for key, tick in self.stocks.items():
            parts = key.split(":")
            if len(parts) != 2 or parts[0] not in ("NSE", "BSE") or not re.fullmatch(r"[A-Z0-9&_.-]+", parts[1]):
                raise ValueError("Stocks must be NSE:SYMBOL or BSE:SYMBOL")
            positive(tick)
        for value in (self.capital_limit, self.per_trade_limit, self.profit_pct, self.stop_pct):
            positive(value)
        if dec(self.stop_pct) >= 100 or dec(self.per_trade_limit) > dec(self.capital_limit):
            raise ValueError("Invalid stop or capital limit")
        if self.product != "CNC":
            raise ValueError("This version supports fully funded CNC equities only")
        for value in (self.max_positions, self.max_exit_attempts):
            if type(value) is not int or value < 1:
                raise ValueError("Counts must be positive integers")
        for value in (self.max_quote_age, self.entry_timeout):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("Timeouts must be finite and positive")

    @classmethod
    def load(cls, path):
        with open(path) as f:
            data = json.load(f)
        data["fees"] = Fees(**data.get("fees", {}))
        return cls(**data)


@dataclass(frozen=True)
class Quote:
    price: Decimal
    timestamp: float

    def check(self, now, max_age):
        positive(self.price)
        if not math.isfinite(self.timestamp) or not -2 <= now - self.timestamp <= max_age:
            raise ValueError("Stale or future quote")


@dataclass(frozen=True)
class Order:
    id: str
    tag: str
    stock: str
    side: str
    quantity: int
    filled: int
    average: Decimal
    status: str

    @property
    def terminal(self):
        return self.status in ("COMPLETE", "CANCELLED", "REJECTED")

    def check(self):
        if self.quantity < 1 or not 0 <= self.filled <= self.quantity:
            raise ValueError("Invalid broker quantities")
        if self.filled:
            positive(self.average)
        if self.status == "COMPLETE" and self.filled != self.quantity:
            raise ValueError("Incomplete COMPLETE order")


def exit_levels(average, quantity, tick, profit_pct, stop_pct, fees):
    """Profit is net of estimated fees; stop is a gross price-distance rule."""
    average, tick = positive(average), positive(tick)
    invested = average * quantity
    desired = invested * dec(profit_pct) / 100
    proceeds = (invested + fees.cost("BUY", invested) + desired + dec(fees.sell_fixed))
    target = proceeds / (quantity * (1 - dec(fees.sell_bps) / 10000))
    target = (target / tick).to_integral_value(rounding=ROUND_CEILING) * tick
    stop = (average * (1 - dec(stop_pct) / 100) / tick).to_integral_value(
        rounding=ROUND_CEILING) * tick
    if not 0 < stop < average < target:
        raise ValueError("Tick size cannot represent these exit levels")
    return target, stop
