"""Persisted deadlines, bounded cancellations and read-only execution health.

Timeouts authorize escalation, NEVER an assumption that an order did not fill.
All replacement sizing still requires authoritative terminal broker state.
"""
from datetime import datetime
from .market import IST, MarketCalendar
from .polling import operational_alerts
from .finance import financial_alerts
from .risk import alerts as risk_alerts

TERMINAL = ("COMPLETE", "CANCELLED", "REJECTED")
POLICY_FIELDS = ("entry_timeout", "exit_timeout", "cancel_timeout", "submission_timeout",
                 "retry_interval", "max_cancel_attempts", "max_exit_attempts", "emergency_exit_policy")


def policy(config, trade):
    return trade.get("execution_policy") or {name: getattr(config, name) for name in POLICY_FIELDS}


class Supervisor:
    def __init__(self, config, store, clock, require_open=None):
        self.config, self.store, self.clock = config, store, clock
        self.require_open = require_open or (lambda trade: None)

    def prepare(self, trade):
        if "execution_policy" not in trade:
            trade["execution_policy"] = policy(self.config, trade)
            self.store.save(trade, "RECOVERY_POLICY_SAVED")
        for order in trade["orders"]:
            self.prepare_order(trade, order)

    def prepare_order(self, trade, order):
        rules = policy(self.config, trade)
        # Prefer authoritative placement time for GTT children discovered after
        # downtime. Observed time is the fallback if the broker omitted it.
        started = min(order.get("placed_at") or order["submitted"], order["submitted"])
        duration = rules["entry_timeout"] if order["side"] == "BUY" else rules["exit_timeout"]
        order["deadline"] = min(order.get("deadline", started + duration), started + duration)
        order.setdefault("submission_deadline", order["submitted"] + rules["submission_timeout"])
        if order["status"] in TERMINAL:
            order.setdefault("terminal_at", self.clock())

    def cancel(self, trade, record, kind, send):
        """Called only after reading a still-active broker order/trigger."""
        self.require_open(trade)
        now, rules = self.clock(), policy(self.config, trade)
        cancellation = record.setdefault("cancel", dict(requested_at=now,
            deadline=now + rules["cancel_timeout"], attempts=0, next_attempt_at=now))
        if now >= cancellation["deadline"] or cancellation["attempts"] >= rules["max_cancel_attempts"]:
            self.store.set("paused", True)
            return  # continue reads; a deadline is not confirmation of cancellation
        if now < cancellation["next_attempt_at"]:
            return
        cancellation["attempts"] += 1
        cancellation["next_attempt_at"] = now + rules["retry_interval"]
        self.store.save(trade, kind + "_CANCEL_INTENT")
        send()
        self.store.save(trade, kind + "_CANCEL_SENT")

    def observe(self, trade):
        now = self.clock()
        if trade.get("error"):
            trade.setdefault("failure_since", now)
        else:
            trade.pop("failure_since", None)
            trade["last_reconciled_at"] = now
        trade["last_attempt_at"] = now
        previous = {(a["code"], a["ref"], a["severity"]) for a in trade.get("alerts", [])}
        trade["alerts"] = trade_alerts(self.config, trade, now)
        current = {(a["code"], a["ref"], a["severity"]) for a in trade["alerts"]}
        if current:
            self.store.set("paused", True)
        if current != previous:
            self.store.save(trade, "EXECUTION_ALERTS_CHANGED")


def trade_alerts(config, trade, now):
    if trade["state"] in ("CLOSED", "NO_FILL"):
        return []
    rules, alerts = policy(config, trade), []
    def add(code, severity, ref, message, action, deadline=None):
        alerts.append(dict(code=code, severity=severity, trade_id=trade["id"], stock=trade["stock"],
                           ref=ref, message=message, action=action, deadline=deadline))
    def uncertain(code, ref, deadline, message, action):
        add(code, "CRITICAL" if now >= deadline else "WARNING", ref, message, action, deadline)
    def cancellation(record, kind):
        c = record.get("cancel")
        if c:
            exhausted = now >= c["deadline"] or c["attempts"] >= rules["max_cancel_attempts"]
            add("CANCEL_STALLED" if exhausted else "CANCEL_PENDING", "CRITICAL" if exhausted else "WARNING",
                record.get("id") or kind, f"{kind} cancellation awaits broker confirmation",
                "Inspect broker state; replacement is blocked until terminal status and fills are confirmed."
                if exhausted else "Continue reconciliation; cancellation requests are rate-limited.", c["deadline"])

    orders = trade["orders"]
    buys = sum(o["filled"] for o in orders if o["side"] == "BUY")
    sells = [o for o in orders if o["side"] == "SELL"]
    remaining = buys - sum(o["filled"] for o in sells)
    for o in orders:
        if o["status"] in TERMINAL:
            continue
        if not o["id"]:
            deadline = o.get("submission_deadline", o["submitted"] + rules["submission_timeout"])
            uncertain("SUBMISSION_UNRESOLVED", o["tag"], deadline, "Order submission outcome is unknown",
                      "Verify the broker order by tag; never resubmit based on timeout or an empty daily book.")
        elif now >= o.get("deadline", o["submitted"] + rules["exit_timeout" if o["side"] == "SELL" else "entry_timeout"]):
            if o["side"] == "SELL":
                add("EXIT_TIMEOUT", "WARNING", o["id"], "Sell exceeded its execution deadline",
                    "Cancel, confirm final fills, then retry only the residual within the attempt limit.", o.get("deadline"))
            else:
                add("ENTRY_TIMEOUT", "WARNING", o["id"], "Entry exceeded its execution deadline",
                    "Reconcile and cancel in a supported session; no exit can be sized before final fills are confirmed.", o.get("deadline"))
        cancellation(o, "ORDER")
    p = trade.get("protection", {})
    if p and not p.get("confirmed_absent"):
        if not p.get("id"):
            uncertain("GTT_REGISTRATION_UNRESOLVED", "GTT", p["created"] + rules["submission_timeout"],
                      "GTT registration outcome is unknown", "Identify the exact broker trigger, then use adopt-protection.")
        elif not p.get("released"):
            cancellation(p, "GTT")
        if p.get("health") == "ACTIVE" and now - p.get("verified_at", 0) > config.health_max_age:
            add("PROTECTION_CHECK_STALE", "CRITICAL", p["id"], "Broker protection has not been verified recently",
                "Restore broker connectivity and run reconciliation; do not create a competing exit.")
    pending = any(o["status"] not in TERMINAL for o in sells)
    if remaining > 0 and sells and not pending:
        if len(sells) >= rules["max_exit_attempts"] + trade.get("extra_attempts", 0):
            add("EXIT_ATTEMPTS_EXHAUSTED", "CRITICAL", sells[-1]["id"], "Exit attempts exhausted with shares remaining",
                "Fix the broker rejection or execution issue, then explicitly authorize retry-exit.")
        elif sells[-1]["status"] == "REJECTED":
            add("EXIT_REJECTED", "WARNING", sells[-1]["id"], sells[-1].get("message") or "Broker rejected the exit",
                "Bounded retry waits for its cooldown; inspect authorization, funds and market restrictions.")
    if remaining > 0 and (trade.get("exit_mode", config.exit_mode) == "BROKER_OCO" or p):
        if p.get("health") != "ACTIVE" and not pending:
            add("POSITION_UNPROTECTED", "CRITICAL", p.get("id", ""), "Exposure has no verified active GTT or pending exit",
                "Reconcile broker protection and orders; never clear uncertainty by issuing another sell.")
    if trade.get("error"):
        since = trade.get("failure_since", now)
        uncertain("RECONCILIATION_FAILED", "trade", since + rules["submission_timeout"], trade["error"],
                  "Resolve the reported broker/feed/state failure; existing orders must still be reconciled.")
    feed = trade.get("exit_feed", {})
    if feed.get("state") == "UNAVAILABLE":
        add("EXIT_FEED_UNAVAILABLE", "WARNING", "quote", feed["error"],
            "Broker-protected emergency exit is permitted only after ownership and terminal-state checks."
            if feed.get("emergency_allowed") else "Restore a fresh quote; the configured policy blocks quote-free exits.")
    last = trade.get("last_reconciled_at", trade["created"])
    response = trade.get("exit_response", {})
    if response.get("deadline") is not None:
        end = response.get("sent_at") or now
        if response.get("breached") or end > response["deadline"] or end < response["started_at"]:
            add("EXIT_RESPONSE_LATE", "CRITICAL", "response", "Exit response exceeded its submission deadline",
                "Inspect the blocking order, protection, market or connection state; a deadline never authorizes a competing sell.",
                response["deadline"])
    watch = trade.get("broker_trigger_watch", {})
    if watch and (watch.get("submitted_at") or watch.get("child_seen_at") or now) > watch["deadline"]:
        add("BROKER_TRIGGER_DELAY", "CRITICAL", "GTT", "Observed price trigger lacks timely broker submission evidence",
            "Inspect the GTT and broker order; reconcile ownership before any replacement. Missing broker timestamps are not proof of timely submission.",
            watch["deadline"])
    if now - last > config.health_max_age or now < last:
        add("RECONCILIATION_STALE", "CRITICAL", "trade", "No recent successful reconciliation for this trade",
            "Restore the worker/broker connection and inspect outstanding orders before resuming entries.")
    return alerts


def health_snapshot(store, config, now):
    trades = store.all()
    heartbeat = store.get("heartbeat")
    fresh = heartbeat is not None and 0 <= now - heartbeat <= config.health_max_age
    alerts = ([a for t in trades for a in trade_alerts(config, t, now)] + operational_alerts(store, config, now)
              + financial_alerts(store, config, now) + risk_alerts(store, config, now))
    def operational(code, severity, message, action):
        alerts.append(dict(code=code, severity=severity, trade_id=None, ref="runtime", deadline=None,
                           message=message, action=action))
    session = store.get("session")
    if session:
        state = session["state"]
        if state == "VALID" and now >= session["expires_at"]:
            state = "EXPIRED"
        if state != "VALID":
            operational("SESSION_" + state, "CRITICAL", "Live broker session is " + state.lower(),
                        "Complete login-url/login and replace the private session file; the worker reloads it automatically.")
        elif session["expires_at"] - now <= 3600:
            operational("SESSION_EXPIRING", "WARNING", "Broker session expires within one hour",
                        "Arrange the next supported Kite login; expiry is not automatically refreshable.")
        elif now - session["verified_at"] > 90 or now < session["verified_at"]:
            operational("SESSION_CHECK_STALE", "CRITICAL", "Session verification is stale",
                        "Restore the worker and broker connectivity.")
        session = dict(session, state=state)
    market = store.get("market")
    if store.get("calendar"):
        calendar = MarketCalendar(store.get("calendar"))
        stocks = set(config.stocks) | {t["stock"] for t in trades if t["state"] not in ("CLOSED", "NO_FILL")}
        market = calendar.snapshot(stocks, now)
        if any(s["state"] == "UNKNOWN" for s in market.values()):
            operational("CALENDAR_UNVERIFIED", "CRITICAL", "An exchange/date has no reviewed execution schedule",
                        "Install a verified calendar covering the required exchanges and dates, then restart the worker.")
        elif (calendar.end - datetime.fromtimestamp(now, IST).date()).days <= 7:
            operational("CALENDAR_EXPIRING", "WARNING", "Exchange calendar coverage expires within seven days",
                        "Review exchange circulars and install the next calendar before its coverage expires.")
    preflight = store.get("preflight")
    if preflight and preflight["status"] == "FAILED":
        operational("PREFLIGHT_FAILED", "CRITICAL", preflight["error"],
                    "Correct the account, instruments, calendar or inventory discrepancy and run preflight again.")
    runtime = store.get("live_runtime")
    if runtime and not runtime["ready"]:
        operational("LIVE_RUNTIME_BLOCKED", "CRITICAL", runtime["error"],
                    "Restore the session/connectivity/configuration; the running worker retries initialization.")
    if not fresh:
        alerts.append(dict(code="WORKER_STALE", severity="CRITICAL", trade_id=None, ref="worker", deadline=None,
                           message="Worker heartbeat is missing, stale or in the future",
                           action="Restore the worker and confirm per-trade reconciliation/protection health."))
    severity = "CRITICAL" if any(a["severity"] == "CRITICAL" for a in alerts) else "DEGRADED" if alerts else "HEALTHY"
    return dict(status=severity, checked_at=now, worker=dict(heartbeat=heartbeat, fresh=fresh),
                paused=store.get("paused", False), alerts=alerts,
                session=session, market=market, preflight=preflight,
                polling=store.get("polling"), market_data=store.get("market_data", {}),
                financial_reconciliation=store.get("financial_reconciliation"),
                daily_risk=store.get("daily_risk"),
                active_trades=sum(t["state"] not in ("CLOSED", "NO_FILL") for t in trades))
