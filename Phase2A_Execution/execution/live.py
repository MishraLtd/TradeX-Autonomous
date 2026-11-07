"""Keep the live worker observable while it waits for an operator-renewed session."""
import time
from .engine import Engine
from .kite import KiteBroker
from .market import MarketCalendar
from .sessions import SessionClient
from .supervision import health_snapshot
from .polling import PollMonitor
from .sessions import read_session, key_digest, SessionUnavailable


class LiveRuntime:
    def __init__(self, config, store, api_key, session_file, factory, clock=time.time, environ=None, monotonic=time.monotonic):
        self.config, self.store, self.clock = config, store, clock
        self.calendar = MarketCalendar.load(config.market_calendar)
        self.client = SessionClient(factory, api_key, config.live_account_id, session_file, store, clock, environ,
                                    request_timeout=config.broker_request_timeout)
        self.monitor = PollMonitor(config, store, clock, monotonic)
        if config.cloud_lease_file:
            from .operations import dispatch_guard
            self.client.dispatch_guard = lambda: dispatch_guard(config.cloud_lease_file, config.live_account_id,
                                                                config.broker_request_timeout, self.clock())
        self.monotonic = monotonic
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
                                calendar=self.calendar, clock=self.clock, store=self.store,
                                max_quote_age=self.config.max_quote_age)
            self.engine = Engine(self.config, self.store, broker, self.clock, self.calendar, self.monotonic)
        with self.store.locked():
            self.store.set("live_runtime", dict(ready=True, checked_at=self.clock()))
        return self.engine

    def tick(self):
        with self.store.locked():
            expected = "kite:" + self.config.live_account_id
            if self.store.get("account", expected) != expected:
                raise ValueError("Database belongs to a different broker account/mode")
            previous = self.store.get("polling", {})
            retry = previous.get("next_retry_at")
            if retry is not None and self.clock() < retry and self.clock() >= previous.get("completed_at", 0):
                try:
                    receipt = read_session(self.client.path, self.client.api_key, self.client.account,
                                           self.clock(), self.client.environ)
                    renewed = key_digest(receipt["access_token"]) != self.client.revision
                except SessionUnavailable:
                    renewed = False
                if not renewed:
                    self.store.set("heartbeat", self.clock())
                    return self.store.all(), health_snapshot(self.store, self.config, self.clock())
            self.monitor.begin()
        success, error = False, None
        try:
            if previous.get("recovery_required") or previous.get("running"):
                self.client.reconnect()
            trades = self.prepare().tick()
            with self.store.locked():
                self.store.set("live_runtime", dict(ready=True, checked_at=self.clock()))
                success = (not self.engine.broker.poll_failed and not self.store.get("poll_deferred") and
                           not any(t.get("error") for t in trades if t["state"] not in ("CLOSED", "NO_FILL")) and
                           self.store.get("preflight", {}).get("status") != "FAILED")
                if self.monitor.state.get("recovery_required"):
                    success = success and self.store.get("preflight", {}).get("status") == "READY"
                if not success:
                    error = "Broker/data checks or recovery reconciliation are incomplete"
        except Exception as exc:
            error = type(exc).__name__
            with self.store.locked():
                if self.store.get("account", "kite:" + self.config.live_account_id) != "kite:" + self.config.live_account_id:
                    raise ValueError("Database belongs to a different broker account/mode") from None
                # A fresh worker does not claim fresh broker reconciliation.
                self.store.set("heartbeat", self.clock())
                self.store.set("live_runtime", dict(ready=False, checked_at=self.clock(),
                                                   error=f"{type(exc).__name__}: {exc}"))
                trades = self.store.all()
        with self.store.locked():
            self.monitor.finish(success, any(t["state"] not in ("CLOSED", "NO_FILL") for t in trades), error)
            return trades, health_snapshot(self.store, self.config, self.clock())
