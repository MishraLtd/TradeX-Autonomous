"""Keep the live worker observable while it waits for an operator-renewed session."""
import time
from .engine import Engine
from .kite import KiteBroker
from .market import MarketCalendar
from .sessions import SessionClient
from .supervision import health_snapshot


class LiveRuntime:
    def __init__(self, config, store, api_key, session_file, factory, clock=time.time, environ=None):
        self.config, self.store, self.clock = config, store, clock
        self.calendar = MarketCalendar.load(config.market_calendar)
        self.client = SessionClient(factory, api_key, config.live_account_id, session_file, store, clock, environ)
        self.engine = None

    def prepare(self):
        with self.store.locked():
            expected = "kite:" + self.config.live_account_id
            if self.store.get("account", expected) != expected:
                raise ValueError("Database belongs to a different broker account/mode")
            self.store.set("account", expected)
            self.store.set("calendar", self.calendar.data)
            stocks = set(self.config.stocks) | {t["stock"] for t in self.store.all() if t["state"] not in ("CLOSED", "NO_FILL")}
            self.store.set("market", self.calendar.snapshot(stocks, self.clock()))
            self.client.ensure()
        if self.engine is None:
            broker = KiteBroker(self.client, self.config.stocks, live=True,
                                expected_account=self.config.live_account_id,
                                calendar=self.calendar, clock=self.clock)
            self.engine = Engine(self.config, self.store, broker, self.clock, self.calendar)
        with self.store.locked():
            self.store.set("live_runtime", dict(ready=True, checked_at=self.clock()))
        return self.engine

    def tick(self):
        try:
            trades = self.prepare().tick()
            with self.store.locked():
                self.store.set("live_runtime", dict(ready=True, checked_at=self.clock()))
        except Exception as exc:
            with self.store.locked():
                if self.store.get("account", "kite:" + self.config.live_account_id) != "kite:" + self.config.live_account_id:
                    raise ValueError("Database belongs to a different broker account/mode") from None
                # A fresh worker does not claim fresh broker reconciliation.
                self.store.set("heartbeat", self.clock())
                self.store.set("live_runtime", dict(ready=False, checked_at=self.clock(),
                                                   error=f"{type(exc).__name__}: {exc}"))
                trades = self.store.all()
        with self.store.locked():
            return trades, health_snapshot(self.store, self.config, self.clock())
