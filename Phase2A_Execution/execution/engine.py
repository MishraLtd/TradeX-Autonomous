"""Persistent, single-account execution state machine. No entry signal generation."""
from dataclasses import asdict
from decimal import Decimal, ROUND_CEILING
import time
import uuid

from .models import Order, dec, positive, exit_levels
from .protection import ProtectionManager
from .supervision import Supervisor, health_snapshot, policy, trade_alerts
from .market import MarketCalendar, MarketClosed
from .sessions import SessionUnavailable
from .polling import exit_response, mark_exit_sent, operational_alerts
from .costs import affordable_quantity, load_plan, trade_fees
from .finance import financial_alerts
from . import risk


class Engine:
    def __init__(self, config, store, broker, clock=time.time, calendar=None, monotonic=time.monotonic):
        self.config, self.store, self.broker, self.clock = config, store, broker, clock
        self.monotonic = monotonic
        self.calendar = calendar
        if getattr(broker, "requires_market_hours", False):
            self.calendar = calendar or (MarketCalendar.load(config.market_calendar) if config.market_calendar else broker.calendar)
            broker.calendar = self.calendar
        self.supervisor = Supervisor(config, store, clock, self._require_open)
        self.protection = ProtectionManager(broker, store, clock, self.supervisor)
        if getattr(broker, "protection_required", False) and config.exit_mode != "BROKER_OCO":
            raise ValueError("Live execution requires BROKER_OCO protection")
        with store.locked():
            identity = broker.identity
            if store.get("account", identity) != identity:
                raise ValueError("Database belongs to a different broker account/mode")
            store.set("account", identity)

    def _market_snapshot(self):
        if self.calendar is not None:
            self.store.set("calendar", self.calendar.data)
            stocks = set(self.config.stocks) | {t["stock"] for t in self.store.all()
                                               if t["state"] not in ("CLOSED", "NO_FILL")}
            self.store.set("market", self.calendar.snapshot(stocks, self.clock()))

    def preflight(self):
        """Read-only broker checks; safe before the open and after manual renewal."""
        with self.store.locked():
            return self._preflight(force=True)

    def _preflight(self, force=False):
        if self.calendar is None:
            return dict(status="NOT_REQUIRED")
        self._market_snapshot()
        states = self.store.get("market")
        now = self.clock()
        day = next(iter(states.values()))["trading_date"]
        revision = self.store.get("session", {}).get("revision")
        saved = self.store.get("preflight", {})
        identity = dict(trading_date=day, session_revision=revision,
                        stocks=self.config.stocks, calendar=self.calendar.data)
        if not force and saved.get("identity") == identity:
            if saved.get("status") == "READY" or 0 <= now - saved["checked_at"] < 5:
                return saved
        if any(s["state"] == "UNKNOWN" for s in states.values()):
            result = dict(status="FAILED", error="Calendar coverage is unverified")
        elif not any(s["state"] in ("PREMARKET", "OPEN") for s in states.values()):
            result = dict(status="WAITING", error=None)
        else:
            try:
                checks = self.broker.preflight() if hasattr(self.broker, "preflight") else {
                    "cash_available": str(self.broker.available_cash())}
                active = [t for t in self.store.all() if t["state"] not in ("CLOSED", "NO_FILL")]
                quantities, order_ids = {}, {}
                for t in active:
                    self.supervisor.prepare(t)
                    self.protection.sync(t)
                    self._refresh(t)
                    if any(o["side"] == "BUY" and o["status"] not in ("COMPLETE", "CANCELLED", "REJECTED")
                           for o in t["orders"]):
                        raise ValueError("Prior entry still awaits authoritative terminal state")
                    quantity = sum(o["filled"] * (1 if o["side"] == "BUY" else -1) for o in t["orders"])
                    if quantity < 0:
                        raise ValueError("Broker reports an oversell")
                    quantities[t["stock"]] = quantities.get(t["stock"], 0) + quantity
                    order_ids.setdefault(t["stock"], []).extend(o["id"] for o in t["orders"])
                expected = {stock: quantities.get(stock, 0) for stock in set(self.config.stocks) | quantities.keys()}
                triggers = {stock: self.protection.owned_ids(stock) for stock in expected}
                if hasattr(self.broker, "preflight_inventory"):
                    self.broker.preflight_inventory(expected, order_ids, triggers)
                else:
                    for stock, quantity in expected.items():
                        self.broker.assert_inventory(stock, quantity, order_ids.get(stock, []))
                        self.broker.assert_no_protection(stock, triggers[stock])
                result = dict(status="READY", error=None, checks=checks)
            except Exception as exc:
                result = dict(status="FAILED", error=f"{type(exc).__name__}: {exc}")
        result.update(identity=identity, checked_at=now)
        self.store.set("preflight", result)
        return result

    def _can_act(self, trade):
        if self.calendar is None:
            return True
        state = self.calendar.status(trade["stock"].split(":")[0], self.clock())
        trade["market"] = state
        allowed = state["state"] == "OPEN" and state["execution_close"] - self.clock() > 10
        if not allowed:
            trade["exit_feed"] = dict(state="MARKET_CLOSED" if state["state"] != "UNKNOWN" else "CALENDAR_UNVERIFIED",
                                      checked_at=self.clock())
        return allowed

    def _require_open(self, trade):
        if self.calendar is not None:
            self.calendar.require_open(trade["stock"], self.clock())

    def _broker_exits(self, trade):
        # Existing live positions migrate on their next cycle. Once a GTT exists,
        # a config change can never bypass its ownership/reconciliation protocol.
        return (bool(trade.get("protection")) or getattr(self.broker, "protection_required", False) or
                trade.get("exit_mode", self.config.exit_mode) == "BROKER_OCO")

    def request_entry(self, request_id, stock, budget):
        """Future entry trigger calls this once with a stable id. No prices required.

        Buys with an IOC limit up to 0.2% above the fresh reference quote.
        Budget includes estimated buy charges; no leverage is used.
        """
        if not isinstance(request_id, str) or not request_id or len(request_id) > 200:
            raise ValueError("A stable request id (1..200 characters) is required")
        budget = positive(budget)
        cfg = self.config
        with self.store.locked():
            trades = self.store.all()
            old = next((t for t in trades if t["id"] == request_id), None)
            if old:
                if old["stock"] != stock or dec(old["budget"]) != budget:
                    raise ValueError("Request id already used with different inputs")
                return old
            self._risk_check()
            if risk.alerts(self.store, cfg, self.clock()):
                raise ValueError("Account daily loss controls block new entries")
            if self.store.get("paused", False):
                raise ValueError("New entries are paused; existing exits continue")
            if operational_alerts(self.store, cfg, self.clock()):
                raise ValueError("Restore fresh data and complete polling recovery before new entries")
            if financial_alerts(self.store, cfg, self.clock()):
                raise ValueError("Resolve account costs and financial reconciliation alerts before new live entries")
            if stock not in cfg.stocks:
                raise ValueError("Stock is outside the approved universe")
            if self.calendar is not None:
                self._market_snapshot()
                self.calendar.require_open(stock, self.clock(), buffer=max(60, cfg.entry_timeout + cfg.cancel_timeout + 30))
                if self._preflight()["status"] != "READY":
                    raise ValueError("Daily/session preflight has not passed; inspect health and run preflight")
                trades = self.store.all()  # preflight may have reconciled prior fills
            if hasattr(self.broker, "assert_entry_ready"):
                self.broker.assert_entry_ready()
            active = [t for t in trades if t["state"] not in ("CLOSED", "NO_FILL")]
            if any(t["stock"] == stock for t in active):
                raise ValueError("Stock already has an active entry or position")
            if any(t.get("error") or any(o["status"] == "UNKNOWN" for o in t["orders"]) for t in active):
                raise ValueError("Resolve outstanding broker uncertainty before new entries")
            if any(trade_alerts(cfg, t, self.clock()) for t in active):
                raise ValueError("Resolve execution alerts before new entries")
            for t in active:
                p = t.get("protection", {})
                if self._broker_exits(t) and (p.get("health") != "ACTIVE" or
                        self.clock() - p.get("verified_at", 0) > cfg.max_quote_age):
                    raise ValueError("Reconcile and protect existing exposure before new entries")
            if len(active) >= cfg.max_positions:
                raise ValueError("Maximum positions reached")
            if budget > dec(cfg.per_trade_limit):
                raise ValueError("Per-trade budget exceeded")
            reserved = sum((dec(t["reserved"]) for t in active), Decimal(0))
            if reserved + budget > dec(cfg.capital_limit):
                raise ValueError("Total capital limit exceeded")
            self.broker.assert_no_protection(stock, self.protection.owned_ids(stock))
            self.broker.assert_inventory(stock, 0, [])
            if budget > self.broker.available_cash():
                raise ValueError("Insufficient broker cash")
            quote = self.broker.quote(stock)
            quote.check(self.clock(), cfg.max_quote_age)
            tick = positive(cfg.stocks[stock])
            limit = (dec(quote.price) * Decimal("1.002") / tick).to_integral_value(
                rounding=ROUND_CEILING) * tick
            fees = cfg.fees
            if cfg.cost_profile:
                account = self.broker.identity[5:] if self.broker.identity.startswith("kite:") else cfg.live_account_id
                fees = load_plan(cfg.cost_profile, account, stock, self.clock(), cfg.max_exit_attempts)
            quantity = affordable_quantity(budget, limit, fees)
            if quantity < 1:
                raise ValueError("Budget cannot afford one share including estimated fees")
            exit_levels(limit, quantity, tick, cfg.profit_pct, cfg.stop_pct, fees)
            trade = dict(id=request_id, stock=stock, budget=str(budget), reserved=str(budget),
                         state="ENTERING", quantity=quantity, tick=str(tick),
                         profit_pct=cfg.profit_pct, stop_pct=cfg.stop_pct,
                         fees=asdict(cfg.fees), orders=[], reason=None, error=None,
                         exit_mode=cfg.exit_mode, stop_limit_buffer_pct=cfg.stop_limit_buffer_pct,
                         created=self.clock(), target=None, stop=None)
            if cfg.cost_profile:
                trade["cost_plan"] = fees.data
            self.store.save(trade, "ENTRY_ACCEPTED")
            self.supervisor.prepare(trade)
            self._place(trade, "BUY", quantity, limit)
            # Reduce the fill-to-protection gap for immediate IOC fills; later
            # broker updates are still reconciled by the worker.
            if cfg.exit_mode == "BROKER_OCO":
                try:
                    self._progress(trade)
                except Exception as exc:
                    trade["error"] = f"{type(exc).__name__}: {exc}"
                    self.store.set("paused", True)
            self.supervisor.observe(trade)
            self.store.save(trade, "ENTRY_PROTECTION_CHECK")
            return trade

    def _place(self, trade, side, quantity, limit=None):
        if self.calendar is not None:
            self.calendar.require_open(trade["stock"], self.clock(), buffer=60 if side == "BUY" else 10)
        if side == "SELL":
            exit_response(trade, self.config, self.clock(), can_act=True)
            if trade.get("protection") and not trade["protection"].get("released"):
                raise RuntimeError("Broker protection still owns the exit; competing sell blocked")
            if any(o["side"] == "SELL" and o["status"] not in ("COMPLETE", "CANCELLED", "REJECTED")
                   for o in trade["orders"]):
                raise RuntimeError("Another exit order is still unresolved")
        tag = "tx" + uuid.uuid4().hex[:18]
        record = dict(id="", tag=tag, stock=trade["stock"], side=side,
                      quantity=quantity, filled=0, average="0", status="UNKNOWN",
                      submitted=self.clock())
        self.supervisor.prepare_order(trade, record)
        trade["orders"].append(record)
        # Persist intent BEFORE the network call. An ambiguous call is NEVER blindly retried.
        self.store.save(trade, "ORDER_INTENT")
        def sent():
            record["sent_at"] = self.clock()
            if side == "SELL":
                mark_exit_sent(trade, self.clock())
            self.store.save(trade, "ORDER_DISPATCHED")
        try:
            if getattr(self.broker, "supports_submission_timing", False):
                record["id"] = self.broker.submit(trade["stock"], side, quantity, tag, limit, on_send=sent)
            else:
                sent()  # paper/custom adapters: measured at adapter invocation
                record["id"] = self.broker.submit(trade["stock"], side, quantity, tag, limit)
            record["status"] = "OPEN"
            record["acknowledged_at"] = self.clock()
            if side == "SELL" and trade.get("exit_response"):
                trade["exit_response"]["acknowledged_at"] = self.clock()
            self.store.save(trade, "ORDER_SUBMITTED")
        except MarketClosed:
            # The adapter's local guard rejected BEFORE sending any request.
            trade["orders"].remove(record)
            if side == "SELL" and trade.get("exit_response"):
                trade["exit_response"].update(sent_at=None, acknowledged_at=None,
                                              elapsed_to_send=None, source_to_send=None)
            self.store.save(trade, "ORDER_DEFERRED_MARKET_CLOSED")
            raise
        except Exception as exc:
            trade["error"] = "Submission outcome unknown: " + type(exc).__name__
            self.store.save(trade, "SUBMISSION_UNKNOWN")
            self.store.set("paused", True)

    def _refresh(self, trade):
        for record in trade["orders"]:
            if record["status"] in ("COMPLETE", "CANCELLED", "REJECTED"):
                continue
            order = (self.broker.order(record["id"]) if record["id"] else
                     self.broker.find_by_tag(record["tag"]))
            if order is None:
                raise RuntimeError("Unknown submission; reconcile tag " + record["tag"])
            order.check()
            if record["id"] and record["id"] != order.id:
                raise RuntimeError("Broker returned a different order ID")
            if record.get("updated_at") is not None and order.updated_at is None:
                raise RuntimeError("Broker order update timestamp disappeared")
            if order.updated_at is not None:
                if order.updated_at > self.clock() + 2:
                    raise RuntimeError("Broker order update is in the future")
                if record.get("updated_at") is not None and order.updated_at < record["updated_at"]:
                    raise RuntimeError("Broker order update moved backwards")
            if (order.tag, order.stock, order.side, order.quantity) != (
                    record["tag"], record["stock"], record["side"], record["quantity"]):
                raise RuntimeError("Broker order identity mismatch")
            if order.filled < record["filled"]:
                raise RuntimeError("Broker fill quantity moved backwards")
            record.update(asdict(order))
            self.supervisor.prepare_order(trade, record)
            self.store.save(trade, "ORDER_RECONCILED")

    def _exit_quote(self, trade):
        try:
            quote = self.broker.quote(trade["stock"])
            quote.check(self.clock(), self.config.max_quote_age)
        except (MarketClosed, SessionUnavailable):
            raise  # closure/expired authentication cannot authorize an emergency sell
        except Exception as exc:
            allowed = (bool(trade["reason"]) and
                       policy(self.config, trade)["emergency_exit_policy"] == "BROKER_PROTECTED_MARKET" and
                       getattr(self.broker, "supports_emergency_exit", False))
            trade["exit_feed"] = dict(state="UNAVAILABLE", checked_at=self.clock(),
                                      error=f"{type(exc).__name__}: {exc}", emergency_allowed=allowed)
            if not allowed:
                raise
            # This path sends a broker-protected market order, never a limit
            # calculated from an old price. Ownership checks already succeeded.
            self.store.save(trade, "EMERGENCY_EXIT_WITHOUT_LOCAL_QUOTE")
            return None
        trade["exit_feed"] = dict(state="FRESH", checked_at=self.clock(), quote_at=quote.timestamp)
        trade["last_price"] = str(quote.price)
        return quote

    def _progress(self, trade):
        self.supervisor.prepare(trade)
        rules = policy(self.config, trade)
        remote = self.protection.sync(trade)
        self._refresh(trade)
        can_act = self._can_act(trade)
        buy = trade["orders"][0]
        if buy["status"] not in ("COMPLETE", "CANCELLED", "REJECTED"):
            if not can_act:
                return
            if buy["filled"] or self.clock() >= buy["deadline"] or trade["reason"]:
                self.supervisor.cancel(trade, buy, "ORDER", lambda: self.broker.cancel(buy["id"]))
            return  # cancellation must be confirmed before any sell can be sized
        if not buy["filled"]:
            trade["state"], trade["reserved"] = "NO_FILL", "0"
            return
        bought = buy["filled"]
        sold = sum(o["filled"] for o in trade["orders"] if o["side"] == "SELL")
        if sold > bought:
            raise RuntimeError("Broker reports an oversell")
        remaining = bought - sold
        trade["filled_quantity"], trade["remaining"] = bought, remaining
        trade["entry_price"] = str(buy["average"])
        if remaining == 0:
            if remote is not None and not remote.released:
                if not can_act:
                    return
                self.protection.cancel(trade)
                return  # never close a trade while a GTT could still sell later
            gross = sum((dec(o["average"]) * o["filled"] for o in trade["orders"]
                         if o["side"] == "SELL"), Decimal(0)) - dec(buy["average"]) * bought
            fees = trade_fees(trade)
            charges = sum((fees.cost(o["side"], dec(o["average"]) * o["filled"])
                           for o in trade["orders"] if o["filled"]), Decimal(0))
            trade.setdefault("closed_at", self.clock())
            trade.update(state="CLOSED", reserved="0", gross_pnl=str(gross),
                         estimated_fees=str(charges), estimated_net_pnl=str(gross-charges))
            return
        if trade["target"] is None:
            try:
                target, stop = exit_levels(buy["average"], bought, trade["tick"],
                                          trade["profit_pct"], trade["stop_pct"], trade_fees(trade))
                trade.update(target=str(target), stop=str(stop))
            except ValueError:
                trade["reason"] = "INVALID_EXIT_LEVELS"
            self.store.save(trade, "EXITS_CALCULATED")
        exits = [o for o in trade["orders"] if o["side"] == "SELL"]
        exit_response(trade, self.config, self.clock(), can_act=can_act)
        if any(o["status"] not in ("COMPLETE", "CANCELLED", "REJECTED") for o in exits):
            trade["state"] = "EXITING"
            if not can_act:
                return
            for o in exits:
                if o["status"] not in ("COMPLETE", "CANCELLED", "REJECTED"):
                    if trade["reason"] == "OPERATOR_EXIT" or self.clock() >= o["deadline"]:
                        trade["exit_escalated"] = True
                        self.store.set("paused", True)
                        self.supervisor.cancel(trade, o, "ORDER", lambda: self.broker.cancel(o["id"]))
            return
        if not can_act:
            # Continue authoritative reads, retaining the existing GTT and all
            # intent/deadline/attempt state. Never spend retries on a closure.
            self.broker.assert_inventory(trade["stock"], remaining, [o["id"] for o in trade["orders"]])
            self.broker.assert_no_protection(trade["stock"], self.protection.owned_ids(trade["stock"]))
            return
        if remote is not None:
            if remote.status == "active":
                if trade["reason"]:
                    self.protection.cancel(trade)
                else:
                    if hasattr(self.broker, "cached_quote"):
                        quote = self.broker.cached_quote(trade["stock"])
                        if quote and (quote.price <= dec(trade["stop"]) or quote.price >= dec(trade["target"])):
                            trade.setdefault("broker_trigger_watch", dict(observed_at=self.clock(),
                                source_at=quote.timestamp, deadline=self.clock() + self.config.exit_response_timeout,
                                child_seen_at=None, submitted_at=None))
                    self.broker.assert_inventory(trade["stock"], remaining, [o["id"] for o in trade["orders"]])
                    self.broker.assert_no_protection(trade["stock"], self.protection.owned_ids(trade["stock"]))
                    trade["state"] = "OPEN"
                return  # broker owns target/stop; no software-trigger sell
            if remote.status == "disabled":
                self.store.set("paused", True)
                trade["reason"] = trade["reason"] or "PROTECTION_LOST"
                self.protection.cancel(trade)
                return
            if not remote.released:
                raise RuntimeError("Protection outcome unresolved; competing exit blocked")
            if not trade["reason"]:
                self.store.set("paused", True)
                trade["reason"] = "PROTECTION_LOST"
                exit_response(trade, self.config, self.clock(), can_act=True)
        if trade["reason"] and exits:
            if len(exits) >= rules["max_exit_attempts"] + trade.get("extra_attempts", 0):
                raise RuntimeError("Exit attempts exhausted; reconcile broker rejection before retry-exit")
            trade["next_exit_at"] = max(o["terminal_at"] for o in exits) + rules["retry_interval"]
            if self.clock() < trade["next_exit_at"]:
                trade["state"] = "EXITING"
                return
        # Check broker ownership immediately before deciding to sell. Never sell unrelated holdings.
        self.broker.assert_inventory(trade["stock"], remaining, [o["id"] for o in trade["orders"]])
        self.broker.assert_no_protection(trade["stock"], self.protection.owned_ids(trade["stock"]))
        quote = self._exit_quote(trade)
        if not trade["reason"]:
            if dec(quote.price) <= dec(trade["stop"]):
                trade["reason"] = "STOP_LOSS"
            elif dec(quote.price) >= dec(trade["target"]):
                trade["reason"] = "PROFIT_TARGET"
            if trade["reason"]:
                exit_response(trade, self.config, self.clock(), can_act=True, source_at=quote.timestamp)
                self.store.save(trade, "EXIT_TRIGGER_OBSERVED")
        if self._broker_exits(trade) and not trade["reason"] and not trade.get("protection"):
            trade.setdefault("stop_limit_buffer_pct", self.config.stop_limit_buffer_pct)
            protected = self.protection.arm(trade, remaining, quote)
            if protected.status not in ("active", "triggered"):
                raise RuntimeError("Broker did not activate protection")
            trade["state"] = "OPEN" if protected.status == "active" else "EXITING"
            return
        if trade["reason"]:
            if len(exits) >= rules["max_exit_attempts"] + trade.get("extra_attempts", 0):
                raise RuntimeError("Exit attempts exhausted; reconcile broker rejection before retry-exit")
            trade["state"] = "EXITING"
            self._place(trade, "SELL", remaining)
        else:
            trade["state"] = "OPEN"

    def tick(self):
        """One polling cycle. A failed symbol never stops protection of other symbols."""
        with self.store.locked():
            started = self.monotonic()
            self._preflight(force=self.store.get("polling", {}).get("recovery_required", False))
            trades = [t for t in self.store.all() if t["state"] not in ("CLOSED", "NO_FILL")]
            cursor = self.store.get("poll_cursor")
            if cursor in [t["id"] for t in trades]:
                index = next(i for i, t in enumerate(trades) if t["id"] == cursor)
                trades = trades[index:] + trades[:index]
            if hasattr(self.broker, "begin_poll"):
                self.broker.begin_poll(trades)
            self.store.set("poll_deferred", [])
            try:
                self._poll_trades(trades, started)
            finally:
                if hasattr(self.broker, "end_poll"):
                    self.broker.end_poll()
            self.store.set("heartbeat", self.clock())
            self._risk_check()
            return self.store.all()

    def _risk_check(self):
        if self.config.daily_loss_limit is None:
            return
        def quote(stock):
            if hasattr(self.broker, 'cached_quote'):
                value = self.broker.cached_quote(stock)
                if value is None:
                    raise ValueError('No fresh cached quote')
                return value
            return self.broker.quote(stock)
        state = risk.assess(self.config, self.store.all(), quote, self.clock())
        previous = self.store.get('daily_risk', {})
        state['breached'] = state['breached'] or (previous.get('day') == state['day'] and previous.get('breached', False))
        self.store.set('daily_risk', state)
        if state['breached']:
            self.store.set('paused', True)
            if self.config.daily_loss_action == 'FLATTEN':
                for trade in self.store.all():
                    if trade['state'] not in ('CLOSED', 'NO_FILL') and not trade.get('reason'):
                        trade['reason'] = 'DAILY_LOSS_LIMIT'
                        self.store.save(trade, 'DAILY_LOSS_EXIT_REQUESTED')

    def _poll_trades(self, trades, started):
        for index, trade in enumerate(trades):
            if index and self.monotonic() - started >= self.config.poll_cycle_budget:
                self.store.set("poll_deferred", [t["id"] for t in trades[index:]])
                self.store.set("poll_cursor", trade["id"])
                return  # the next cycle starts with the first unprocessed trade
            try:
                trade["error"] = None
                if not trade["orders"]:
                    # Crash after saving request, before writing intent: definitely not submitted.
                    trade.update(state="NO_FILL", reserved="0")
                else:
                    self._progress(trade)
            except MarketClosed:
                self._can_act(trade)  # session boundary crossed during network calls
                trade["error"] = None
            except Exception as exc:
                trade["error"] = f"{type(exc).__name__}: {exc}"
                self.store.set("paused", True)
            self.supervisor.observe(trade)
            self.store.save(trade, "TICK")
        self.store.set("poll_cursor", trades[0]["id"] if trades else None)

    def pause(self, paused=True):
        with self.store.locked():
            if not paused:
                self._risk_check()
                if risk.alerts(self.store, self.config, self.clock()):
                    raise ValueError("Daily loss controls block resume")
            if not paused and operational_alerts(self.store, self.config, self.clock()):
                raise ValueError("Complete data/polling recovery before resuming entries")
            if not paused and financial_alerts(self.store, self.config, self.clock()):
                raise ValueError("Resolve financial reconciliation alerts before resuming entries")
            if not paused and any(t.get("error") or trade_alerts(self.config, t, self.clock()) for t in self.store.all()
                                  if t["state"] not in ("CLOSED", "NO_FILL")):
                raise ValueError("Resolve active errors and execution alerts before resuming")
            self.store.set("paused", paused)

    def health(self):
        with self.store.locked():
            return health_snapshot(self.store, self.config, self.clock())

    def flatten(self, request_id=None):
        """Pause new entries and request exits; pending buys are cancelled first."""
        with self.store.locked():
            self.store.set("paused", True)
            for trade in self.store.all():
                if trade["state"] not in ("CLOSED", "NO_FILL") and (request_id is None or trade["id"] == request_id):
                    trade["reason"] = "OPERATOR_EXIT"
                    exit_response(trade, self.config, self.clock(), can_act=self._can_act(trade))
                    self.store.save(trade, "EXIT_REQUESTED")

    def retry_exit(self, request_id):
        """Explicitly allow one further sell attempt after an operator fixes a rejection."""
        with self.store.locked():
            trade = next(t for t in self.store.all() if t["id"] == request_id)
            self._refresh(trade)
            exits = [o for o in trade["orders"] if o["side"] == "SELL"]
            if not exits or any(o["status"] not in ("COMPLETE", "CANCELLED", "REJECTED") for o in exits):
                raise ValueError("All previous exits must have a confirmed terminal status")
            trade["extra_attempts"] = trade.get("extra_attempts", 0) + 1
            self.store.save(trade, "EXIT_RETRY_AUTHORIZED")

    def resolve_absent(self, request_id, tag, *, confirmed_not_placed=False):
        """Operator recovery ONLY after checking broker order book/contract records.

        Tag absence alone is not proof of failure (Kite's order book is daily).
        Therefore this command requires an explicit operator attestation.
        """
        if not confirmed_not_placed:
            raise ValueError("Broker records must first confirm this intent was never placed")
        with self.store.locked():
            trade = next(t for t in self.store.all() if t["id"] == request_id)
            record = next(o for o in trade["orders"] if o["tag"] == tag)
            if record["id"] or record["status"] != "UNKNOWN" or record["filled"]:
                raise ValueError("Only an unacknowledged, unfilled intent can be resolved absent")
            if self.broker.find_by_tag(tag) is not None:
                raise ValueError("Broker has this order; run tick to reconcile it")
            record["status"] = "REJECTED"
            trade["error"] = None
            self.store.save(trade, "OPERATOR_CONFIRMED_NOT_PLACED")

    def adopt_protection(self, request_id, trigger_id):
        """Operator identifies the exact GTT after a lost creation response."""
        with self.store.locked():
            trade = next(t for t in self.store.all() if t["id"] == request_id)
            self.protection.adopt(trade, trigger_id)

    def resolve_protection_absent(self, request_id, *, confirmed_not_created=False):
        """After broker verification, permit closing an unprotected position."""
        if not confirmed_not_created:
            raise ValueError("Broker records must confirm the GTT was never created")
        with self.store.locked():
            trade = next(t for t in self.store.all() if t["id"] == request_id)
            saved = trade.get("protection")
            if not saved or saved["id"] or saved.get("confirmed_absent"):
                raise ValueError("Only an unidentified protection intent can be resolved absent")
            self.broker.assert_no_protection(trade["stock"], self.protection.owned_ids(trade["stock"]))
            saved.update(confirmed_absent=True, released=True, health="INACTIVE", status="rejected")
            trade["reason"] = trade["reason"] or "PROTECTION_LOST"
            self.store.set("paused", True)
            self.store.save(trade, "OPERATOR_CONFIRMED_GTT_ABSENT")
