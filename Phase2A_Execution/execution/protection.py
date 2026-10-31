"""Broker-held OCO ownership, durable registration, and child-order reconciliation.

Kite GTT creation has no client idempotency key. An ambiguous create must be
resolved explicitly; matching prices alone never justify retrying or adopting.
"""
from dataclasses import asdict, dataclass
from decimal import ROUND_FLOOR

from .models import dec, positive


@dataclass(frozen=True)
class ProtectionSpec:
    stock: str
    quantity: int
    stop: str
    target: str
    stop_limit: str
    target_limit: str

    def __post_init__(self):
        if type(self.quantity) is not int or self.quantity < 1:
            raise ValueError("Invalid protection quantity")
        for name in ("stop", "target", "stop_limit", "target_limit"):
            object.__setattr__(self, name, str(positive(getattr(self, name))))
        if not dec(self.stop_limit) <= dec(self.stop) < dec(self.target) <= dec(self.target_limit):
            raise ValueError("Invalid protection prices")

    def matches(self, other):
        return (self.stock == other.stock and self.quantity == other.quantity and
                all(dec(getattr(self, n)) == dec(getattr(other, n))
                    for n in ("stop", "target", "stop_limit", "target_limit")))


@dataclass(frozen=True)
class Protection:
    id: str
    status: str
    spec: ProtectionSpec
    child_ids: tuple = ()
    triggered_leg: str | None = None
    submission_failed: bool = False
    submission_pending: bool = False

    @property
    def released(self):
        # A consumed GTT is safe to replace only if its child is known, or the
        # broker explicitly reports failed submission. Child orders still need
        # a terminal status before a replacement sell is allowed.
        return not self.submission_pending and (self.status in ("deleted", "cancelled", "expired", "rejected") or
                (self.status == "triggered" and (bool(self.child_ids) or self.submission_failed)))


class ProtectionManager:
    def __init__(self, broker, store, clock, supervisor):
        self.broker, self.store, self.clock = broker, store, clock
        self.supervisor = supervisor

    def owned_ids(self, stock):
        return [t["protection"]["id"] for t in self.store.all()
                if t["stock"] == stock and t.get("protection", {}).get("id")]

    def sync(self, trade):
        saved = trade.get("protection")
        if not saved:
            return None
        if saved.get("confirmed_absent"):
            return Protection("", "rejected", ProtectionSpec(**saved["spec"]))
        saved["health"] = "UNCONFIRMED"
        saved["released"] = False
        if not saved["id"]:
            raise RuntimeError("GTT registration outcome unknown; inspect broker triggers and use adopt-protection")
        remote = self.broker.protection(saved["id"])
        if remote.id != saved["id"] or not remote.spec.matches(ProtectionSpec(**saved["spec"])):
            raise RuntimeError("Broker protection identity/price/quantity mismatch")
        if len(remote.child_ids) > 1 or len(set(remote.child_ids)) != len(remote.child_ids):
            raise RuntimeError("Unexpected multiple OCO child orders; manual reconciliation required")
        if remote.status in ("active", "disabled") and (remote.child_ids or remote.submission_failed):
            raise RuntimeError("Inconsistent GTT status and child results")
        for order_id in remote.child_ids:
            existing = next((o for o in trade["orders"] if o["id"] == order_id), None)
            if existing:
                if existing.get("protection_id") != remote.id:
                    raise RuntimeError("GTT child conflicts with another order intent")
                continue
            # The GTT result provides the authoritative child ID. Never infer a
            # child from order timestamps, tags or symbol matching alone.
            child = self.broker.order(order_id)
            child.check()
            if (child.stock, child.side, child.quantity) != (trade["stock"], "SELL", remote.spec.quantity):
                raise RuntimeError("GTT child identity mismatch")
            record = asdict(child)
            record.update(protection_id=remote.id, submitted=self.clock())
            self.supervisor.prepare_order(trade, record)
            trade["orders"].append(record)
            self.store.save(trade, "GTT_CHILD_ADOPTED")
        # Once a child is known, the association must never disappear from the
        # authoritative trigger response. Fail closed on inconsistent snapshots.
        known = {o["id"] for o in trade["orders"] if o.get("protection_id") == remote.id}
        if not known.issubset(set(remote.child_ids)):
            raise RuntimeError("Broker GTT child association disappeared")
        if remote.status == "triggered":
            trade["reason"] = trade["reason"] or remote.triggered_leg or "BROKER_TRIGGER"
        saved.update(status=remote.status, released=remote.released,
                     health="ACTIVE" if remote.status == "active" else "TRIGGERED" if remote.child_ids else "INACTIVE",
                     verified_at=self.clock(), child_ids=list(remote.child_ids))
        self.store.save(trade, "GTT_RECONCILED")
        if remote.submission_pending:
            saved["health"] = "UNCONFIRMED"
            raise RuntimeError("GTT child submission is unresolved; no competing sell allowed")
        if remote.status == "triggered" and not remote.released:
            saved["health"] = "UNCONFIRMED"
            raise RuntimeError("GTT triggered but child submission is unresolved; no competing sell allowed")
        if remote.status not in ("active", "disabled", "triggered", "deleted", "cancelled", "expired", "rejected"):
            saved["health"] = "UNCONFIRMED"
            raise RuntimeError("Unknown broker GTT status")
        return remote

    def arm(self, trade, quantity, quote):
        tick = positive(trade["tick"])
        stop = dec(trade["stop"])
        limit = (stop * (1 - dec(trade["stop_limit_buffer_pct"]) / 100) / tick).to_integral_value(
            rounding=ROUND_FLOOR) * tick
        spec = ProtectionSpec(trade["stock"], quantity, trade["stop"], trade["target"],
                              str(max(tick, limit)), trade["target"])
        self.broker.assert_no_protection(trade["stock"], self.owned_ids(trade["stock"]))
        # Capture existing IDs to prevent adopting an older/unrelated trigger
        # after a timed-out create. There is only one creation attempt.
        previous = self.broker.protection_ids(trade["stock"])
        trade["protection"] = dict(id="", spec=asdict(spec), status="unknown", health="UNCONFIRMED",
                                   released=False, created=self.clock(), previous_ids=previous,
                                   cancel_requested=False)
        self.store.save(trade, "GTT_CREATE_INTENT")
        result = self.broker.create_protection(spec, quote.price)
        if not result:
            raise RuntimeError("Broker returned no GTT ID")
        trade["protection"]["id"] = str(result)
        self.store.save(trade, "GTT_CREATED")
        # Success requires a read-back of the exact levels and quantity, not
        # simply an accepted API request.
        return self.sync(trade)

    def cancel(self, trade):
        saved = trade["protection"]
        if not saved["id"]:
            raise RuntimeError("Cannot cancel an unidentified GTT")
        saved["cancel_requested"] = True
        saved["health"] = "CANCELLING"
        self.supervisor.cancel(trade, saved, "GTT", lambda: self.broker.cancel_protection(saved["id"]))
        # The next cycle MUST fetch status and any racing child before selling.

    def adopt(self, trade, trigger_id):
        saved = trade.get("protection")
        if not saved or saved["id"]:
            raise ValueError("Only an unidentified GTT create intent can be adopted")
        trigger_id = str(trigger_id)
        if trigger_id in saved["previous_ids"]:
            raise ValueError("Trigger existed before this intent")
        if any(t.get("protection", {}).get("id") == trigger_id for t in self.store.all()):
            raise ValueError("Trigger already belongs to another trade")
        remote = self.broker.protection(trigger_id)
        if not remote.spec.matches(ProtectionSpec(**saved["spec"])):
            raise ValueError("Trigger does not match the saved protection intent")
        self.broker.assert_no_protection(trade["stock"], self.owned_ids(trade["stock"]) + [trigger_id])
        saved["id"] = trigger_id
        self.store.save(trade, "OPERATOR_ADOPTED_GTT")
        self.sync(trade)
