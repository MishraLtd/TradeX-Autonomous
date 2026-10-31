"""Persistent, single-account execution state machine. No entry signal generation."""
from dataclasses import asdict
from decimal import Decimal, ROUND_CEILING
import time
import uuid

from .models import Fees, Order, dec, positive, exit_levels
from .protection import ProtectionManager
from .supervision import Supervisor, health_snapshot, policy, trade_alerts


class Engine:
    def __init__(self, config, store, broker, clock=time.time):
        self.config, self.store, self.broker, self.clock = config, store, broker, clock
        self.supervisor = Supervisor(config, store, clock)
        self.protection = ProtectionManager(broker, store, clock, self.supervisor)
        if getattr(broker, "protection_required", False) and config.exit_mode != "BROKER_OCO":
            raise ValueError("Live execution requires BROKER_OCO protection")
        with store.locked():
            identity = broker.identity
            if store.get("account", identity) != identity:
                raise ValueError("Database belongs to a different broker account/mode")
            store.set("account", identity)

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
            if self.store.get("paused", False):
                raise ValueError("New entries are paused; existing exits continue")
            if stock not in cfg.stocks:
                raise ValueError("Stock is outside the approved universe")
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
            quantity = int((budget - dec(cfg.fees.buy_fixed)) /
                           (limit * (1 + dec(cfg.fees.buy_bps) / 10000)))
            if quantity < 1:
                raise ValueError("Budget cannot afford one share including estimated fees")
            exit_levels(limit, quantity, tick, cfg.profit_pct, cfg.stop_pct, cfg.fees)
            trade = dict(id=request_id, stock=stock, budget=str(budget), reserved=str(budget),
                         state="ENTERING", quantity=quantity, tick=str(tick),
                         profit_pct=cfg.profit_pct, stop_pct=cfg.stop_pct,
                         fees=asdict(cfg.fees), orders=[], reason=None, error=None,
                         exit_mode=cfg.exit_mode, stop_limit_buffer_pct=cfg.stop_limit_buffer_pct,
                         created=self.clock(), target=None, stop=None)
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
        if side == "SELL":
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
        try:
            record["id"] = self.broker.submit(trade["stock"], side, quantity, tag, limit)
            record["status"] = "OPEN"
            self.store.save(trade, "ORDER_SUBMITTED")
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
        buy = trade["orders"][0]
        if buy["status"] not in ("COMPLETE", "CANCELLED", "REJECTED"):
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
                self.protection.cancel(trade)
                return  # never close a trade while a GTT could still sell later
            gross = sum((dec(o["average"]) * o["filled"] for o in trade["orders"]
                         if o["side"] == "SELL"), Decimal(0)) - dec(buy["average"]) * bought
            fees = Fees(**trade["fees"])
            charges = sum((fees.cost(o["side"], dec(o["average"]) * o["filled"])
                           for o in trade["orders"] if o["filled"]), Decimal(0))
            trade.update(state="CLOSED", reserved="0", gross_pnl=str(gross),
                         estimated_fees=str(charges), estimated_net_pnl=str(gross-charges))
            return
        if trade["target"] is None:
            try:
                target, stop = exit_levels(buy["average"], bought, trade["tick"],
                                          trade["profit_pct"], trade["stop_pct"], Fees(**trade["fees"]))
                trade.update(target=str(target), stop=str(stop))
            except ValueError:
                trade["reason"] = "INVALID_EXIT_LEVELS"
            self.store.save(trade, "EXITS_CALCULATED")
        exits = [o for o in trade["orders"] if o["side"] == "SELL"]
        if any(o["status"] not in ("COMPLETE", "CANCELLED", "REJECTED") for o in exits):
            trade["state"] = "EXITING"
            for o in exits:
                if o["status"] not in ("COMPLETE", "CANCELLED", "REJECTED"):
                    if trade["reason"] == "OPERATOR_EXIT" or self.clock() >= o["deadline"]:
                        trade["exit_escalated"] = True
                        self.store.set("paused", True)
                        self.supervisor.cancel(trade, o, "ORDER", lambda: self.broker.cancel(o["id"]))
            return
        if remote is not None:
            if remote.status == "active":
                if trade["reason"]:
                    self.protection.cancel(trade)
                else:
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
            for trade in self.store.all():
                if trade["state"] in ("CLOSED", "NO_FILL"):
                    continue
                try:
                    trade["error"] = None
                    if not trade["orders"]:
                        # Crash after saving request, before writing intent: definitely not submitted.
                        trade.update(state="NO_FILL", reserved="0")
                    else:
                        self._progress(trade)
                except Exception as exc:
                    trade["error"] = f"{type(exc).__name__}: {exc}"
                    self.store.set("paused", True)
                self.supervisor.observe(trade)
                self.store.save(trade, "TICK")
            self.store.set("heartbeat", self.clock())
            return self.store.all()

    def pause(self, paused=True):
        with self.store.locked():
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
