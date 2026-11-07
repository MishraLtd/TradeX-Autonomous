"""Measured polling and durable exit response deadlines, not fill guarantees."""
import math
import time

TERMINAL = ("COMPLETE", "CANCELLED", "REJECTED")


class Cadence:
    """Start on interval boundaries; skip missed slots instead of building a backlog."""
    def __init__(self, interval, monotonic=time.monotonic):
        self.interval, self.monotonic = interval, monotonic
        self.started = None

    def begin(self):
        self.started = self.monotonic()

    def delay(self):
        elapsed = max(0, self.monotonic() - self.started)
        slots = max(1, math.ceil(elapsed / self.interval))
        return max(0, slots * self.interval - elapsed)


class PollMonitor:
    def __init__(self, config, store, clock=time.time, monotonic=time.monotonic):
        self.config, self.store, self.clock, self.monotonic = config, store, clock, monotonic
        self.started_mono = None

    def begin(self):
        now = self.clock()
        saved = self.store.get("polling", {})
        self.started_mono = self.monotonic()
        interrupted = saved.get("running", False)
        gap = now - saved["started_at"] if "started_at" in saved else None
        self.state = dict(saved, running=True, started_at=now,
                          start_gap=gap, max_start_gap=max(0, gap or 0, saved.get("max_start_gap", 0)),
                          interval=self.config.poll_interval, cycle_budget=self.config.poll_cycle_budget,
                          recovery_required=saved.get("recovery_required", False) or interrupted,
                          interrupted=interrupted, status="POLLING")
        self.store.set("polling", self.state)

    def finish(self, success, active, error=None):
        now = self.clock()
        elapsed = self.monotonic() - self.started_mono
        skew = elapsed < 0 or abs((now - self.state["started_at"]) - elapsed) > 2
        overrun = elapsed >= self.config.poll_cycle_budget
        good = success and not skew and not overrun
        failures = 0 if good else self.state.get("consecutive_failures", 0) + 1
        # Keep reconciling exposure every interval during outages. A flat account
        # uses capped backoff to avoid hammering an unavailable broker.
        wait = 0
        if failures:
            wait = self.config.poll_interval if active else min(
                self.config.reconnect_max_delay, self.config.poll_interval * 2 ** min(failures - 1, 6))
        self.state.update(running=False, completed_at=now, duration=max(0, elapsed),
                          max_duration=max(max(0, elapsed), self.state.get("max_duration", 0)),
                          clock_changed=skew, overrun=overrun, consecutive_failures=failures,
                          recovery_required=not good, status="HEALTHY" if good else "RECOVERING",
                          error=error, next_retry_at=now + wait if not good else None,
                          cycles=self.state.get("cycles", 0) + 1)
        if good:
            self.state["last_success_at"] = now
        else:
            self.state["last_failure_at"] = now
            self.store.set("paused", True)
        self.store.set("polling", self.state)
        return self.state


def exit_response(trade, config, now, *, can_act, source_at=None):
    """One episode for each required sell. Existing/pending orders retain ownership."""
    sells = [o for o in trade["orders"] if o["side"] == "SELL"]
    if not trade.get("reason") or any(o["status"] not in TERMINAL for o in sells):
        return
    remaining = sum(o["filled"] * (1 if o["side"] == "BUY" else -1) for o in trade["orders"])
    if remaining <= 0:
        return
    number = len(sells) + 1
    response = trade.get("exit_response")
    if response is None or response["attempt"] != number:
        response = dict(attempt=number, reason=trade["reason"], observed_at=now,
                        source_at=source_at, budget=config.exit_response_timeout,
                        started_at=None, deadline=None, sent_at=None, acknowledged_at=None)
        trade["exit_response"] = response
    if can_act and response["started_at"] is None:
        response.update(started_at=now, deadline=now + response["budget"])


def mark_exit_sent(trade, now):
    response = trade.get("exit_response")
    if response and response["sent_at"] is None:
        response["sent_at"] = now
        started = response["started_at"]
        response["elapsed_to_send"] = now - started if started is not None else None
        response["source_to_send"] = now - response["source_at"] if response.get("source_at") is not None else None
        response["breached"] = started is None or now < started or now > response["deadline"]


def operational_alerts(store, config, now):
    alerts = []
    def add(code, message, action, ref="polling", stock=None):
        alerts.append(dict(code=code, severity="CRITICAL", trade_id=None, stock=stock, ref=ref,
                           message=message, action=action, deadline=None))
    poll = store.get("polling")
    if poll:
        if poll.get("recovery_required"):
            add("POLL_RECOVERY_REQUIRED", "Polling has not completed a verified recovery cycle",
                "Restore connectivity and fresh data; reconcile account/orders/GTTs before resuming entries.")
        if poll.get("overrun") or (poll.get("running") and now - poll["started_at"] > poll["cycle_budget"]):
            add("POLL_CYCLE_OVERRUN", "Broker polling exceeded its configured duration budget",
                "Inspect broker latency and workload; existing exits retain all ownership checks.")
        if poll.get("clock_changed") or now < poll["started_at"]:
            add("POLL_CLOCK_CHANGED", "Wall-clock and monotonic timing disagree",
                "Correct clock synchronization and complete a clean recovery cycle.")
        last = poll.get("last_success_at")
        if last is None or now - last > config.poll_interval + config.poll_cycle_budget:
            add("POLL_STALE", "No recent successful complete polling cycle",
                "Restore the worker/data connection; heartbeat alone is insufficient.")
    market = store.get("market", {})
    calendar = None
    if store.get("calendar"):
        from .market import MarketCalendar
        calendar = MarketCalendar(store.get("calendar"))
    watched = {t["stock"] for t in store.all() if t["state"] not in ("CLOSED", "NO_FILL")}
    for stock, data in store.get("market_data", {}).items():
        if stock not in watched:
            continue
        exchange = stock.split(":")[0]
        # Compute present market state independently of an old worker snapshot.
        if calendar is not None:
            state = calendar.status(exchange, now)["state"]
        else:
            state = market.get(exchange, {}).get("state", "OPEN")
        if state != "OPEN":
            continue
        stamp = data.get("trade_at")
        if (data["state"] != "FRESH" or stamp is None or not -2 <= now - stamp <= config.max_quote_age or
                not 0 <= now - data.get("received_at", 0) <= config.max_quote_age):
            add("MARKET_DATA_UNAVAILABLE", "No fresh validated market quote for " + stock,
                "Obtain fresh timestamps and complete reconciliation; never reuse a cached failed quote.", stock, stock)
    return alerts
