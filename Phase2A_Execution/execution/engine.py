"""Persistent, single-account execution state machine. No entry signal generation."""
from dataclasses import asdict
from decimal import Decimal, ROUND_CEILING
import time
import uuid

from .models import Fees, Order, dec, positive, exit_levels


class Engine:
    def __init__(self, config, store, broker, clock=time.time):
        self.config, self.store, self.broker, self.clock = config, store, broker, clock
        with store.locked():
            identity = broker.identity
            if store.get("account", identity) != identity:
                raise ValueError("Database belongs to a different broker account/mode")
            store.set("account", identity)

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
            active = [t for t in trades if t["state"] not in ("CLOSED", "NO_FILL")]
            if any(t["stock"] == stock for t in active):
                raise ValueError("Stock already has an active entry or position")
            if any(t.get("error") or any(o["status"] == "UNKNOWN" for o in t["orders"]) for t in active):
                raise ValueError("Resolve outstanding broker uncertainty before new entries")
            if len(active) >= cfg.max_positions:
                raise ValueError("Maximum positions reached")
            if budget > dec(cfg.per_trade_limit):
                raise ValueError("Per-trade budget exceeded")
            reserved = sum((dec(t["reserved"]) for t in active), Decimal(0))
            if reserved + budget > dec(cfg.capital_limit):
                raise ValueError("Total capital limit exceeded")
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
                         created=self.clock(), target=None, stop=None)
            self.store.save(trade, "ENTRY_ACCEPTED")
            self._place(trade, "BUY", quantity, limit)
            return trade

    def _place(self, trade, side, quantity, limit=None):
        tag = "tx" + uuid.uuid4().hex[:18]
        record = dict(id="", tag=tag, stock=trade["stock"], side=side,
                      quantity=quantity, filled=0, average="0", status="UNKNOWN",
                      submitted=self.clock())
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
            self.store.save(trade, "ORDER_RECONCILED")

    def _progress(self, trade):
        self._refresh(trade)
        buy = trade["orders"][0]
        if buy["status"] not in ("COMPLETE", "CANCELLED", "REJECTED"):
            if buy["filled"] or self.clock() - buy["submitted"] >= self.config.entry_timeout or trade["reason"]:
                self.broker.cancel(buy["id"])
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
            return
        # Check broker ownership immediately before deciding to sell. Never sell unrelated holdings.
        self.broker.assert_inventory(trade["stock"], remaining, [o["id"] for o in trade["orders"]])
        quote = self.broker.quote(trade["stock"])
        quote.check(self.clock(), self.config.max_quote_age)
        trade["last_price"] = str(quote.price)
        if not trade["reason"]:
            if dec(quote.price) <= dec(trade["stop"]):
                trade["reason"] = "STOP_LOSS"
            elif dec(quote.price) >= dec(trade["target"]):
                trade["reason"] = "PROFIT_TARGET"
        if trade["reason"]:
            if len(exits) >= self.config.max_exit_attempts + trade.get("extra_attempts", 0):
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
                    trade["error"] = str(exc)
                    self.store.set("paused", True)
                self.store.save(trade, "TICK")
            self.store.set("heartbeat", self.clock())
            return self.store.all()

    def pause(self, paused=True):
        with self.store.locked():
            if not paused and any(t.get("error") for t in self.store.all()
                                  if t["state"] not in ("CLOSED", "NO_FILL")):
                raise ValueError("Resolve active errors before resuming")
            self.store.set("paused", paused)

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
