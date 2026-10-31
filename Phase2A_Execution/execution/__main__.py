"""Run with python -m execution; paper mode is the default."""
import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from .models import Config, dumps
from .store import Store
from .brokers import PaperBroker
from .engine import Engine
from .supervision import health_snapshot


def emit_alerts(health, previous):
    """Structured local events for a supervisor/log collector; no external sends."""
    current = set()
    for alert in health["alerts"]:
        key = (alert["trade_id"], alert["code"], alert["ref"], alert["severity"])
        current.add(key)
        if key not in previous:
            print(dumps(dict(event="execution_alert", at=health["checked_at"], **alert)), file=sys.stderr, flush=True)
    return current


def validate_account(args, cfg):
    from .acceptance import analyze, collect, readonly_client, save_private
    expected = args.expected_account or cfg.live_account_id
    if not expected:
        raise ValueError("Set live_account_id or --expected-account before account validation")
    if args.expected_account and cfg.live_account_id and args.expected_account != cfg.live_account_id:
        raise ValueError("Expected account conflicts with live_account_id")
    previous = None
    if args.previous:
        with open(args.previous) as handle:
            previous = json.load(handle)
    if args.snapshot:
        if args.live:
            raise ValueError("Use either --live collection or --snapshot replay")
        with open(args.snapshot) as handle:
            saved = json.load(handle)
        snapshot = saved.get("snapshot", saved)
    else:
        if not args.live:
            raise ValueError("Account collection requires --live; offline analysis requires --snapshot")
        missing = [name for name in ("KITE_API_KEY", "KITE_ACCESS_TOKEN") if not os.environ.get(name)]
        if missing:
            print(dumps(dict(status="BLOCKED", source="NOT_RUN", live_account_validated=False,
                             missing_environment=missing, detail="Configure credentials securely in the local environment; do not paste them into chat.")))
            return 2
        client = readonly_client(os.environ["KITE_API_KEY"], os.environ["KITE_ACCESS_TOKEN"])
        snapshot = collect(client, cfg, expected, order_ids=args.order_id, trigger_ids=args.trigger_id)
    report = analyze(snapshot, cfg, expected, previous=previous, replay=bool(args.snapshot))
    path = args.output or ("state/account-validation/" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") +
                           str(time.time_ns()) + ".kite-evidence.json")
    save_private(path, report)
    print(dumps(dict({k: v for k, v in report.items() if k != "snapshot"}, report_path=path)))
    return 0 if report["status"] == "EVIDENCE_COMPLETE" else 2


def main(argv=None):
    parser = argparse.ArgumentParser(description="TradeX execution controls (entry strategy supplied separately)")
    parser.add_argument("--config", default="config.paper.json")
    parser.add_argument("--db", default="state/engine.sqlite")
    parser.add_argument("--paper-db", default="state/paper.sqlite")
    parser.add_argument("--live", action="store_true", help="Use Kite with credentials from environment")
    sub = parser.add_subparsers(dest="command", required=True)
    quote = sub.add_parser("quote", help="Set a paper quote")
    quote.add_argument("stock")
    quote.add_argument("price")
    enter = sub.add_parser("entry", help="Explicit entry request; future strategy calls the same interface")
    enter.add_argument("request_id")
    enter.add_argument("stock")
    enter.add_argument("budget")
    for name in ("tick", "status", "health", "pause", "resume", "journal"):
        sub.add_parser(name)
    flatten = sub.add_parser("flatten", help="Pause entries and request all positions closed")
    flatten.add_argument("--request-id")
    retry = sub.add_parser("retry-exit", help="Permit one additional exit after fixing a broker rejection")
    retry.add_argument("request_id")
    absent = sub.add_parser("resolve-absent", help="Resolve unknown submission after manual broker verification")
    absent.add_argument("request_id")
    absent.add_argument("tag")
    absent.add_argument("--confirmed-not-placed", action="store_true")
    adopt = sub.add_parser("adopt-protection", help="Adopt the verified GTT ID after a lost create response")
    adopt.add_argument("request_id")
    adopt.add_argument("trigger_id")
    missing = sub.add_parser("resolve-protection-absent", help="Permit exit after verifying a GTT was never created")
    missing.add_argument("request_id")
    missing.add_argument("--confirmed-not-created", action="store_true")
    run = sub.add_parser("run", help="Poll orders and manage exits until interrupted")
    run.add_argument("--interval", type=float, default=5)
    validate = sub.add_parser("validate-account", help="Collect read-only Kite account evidence, or replay a private capture")
    validate.add_argument("--expected-account")
    validate.add_argument("--order-id", action="append", default=[])
    validate.add_argument("--trigger-id", action="append", default=[])
    validate.add_argument("--previous", help="Earlier private capture for settlement comparison")
    validate.add_argument("--snapshot", help="Offline replay only; cannot validate a live account")
    validate.add_argument("--output", help="Private JSON report path; defaults to ignored state/account-validation")
    args = parser.parse_args(argv)
    cfg = Config.load(args.config)
    if args.command == "validate-account":
        return validate_account(args, cfg)  # no Engine/Store initialization or trading mutation
    store = Store(args.db)
    broker = None
    try:
        # Read-only status/health/journal and pause work without broker credentials or connectivity.
        if args.command in ("status", "health", "journal", "pause"):
            with store.locked():
                if args.command == "pause":
                    store.set("paused", True)
                if args.command == "journal":
                    rows = store.db.execute("SELECT at,trade_id,kind,data FROM events ORDER BY id")
                    for at, trade_id, kind, data in rows:
                        print(dumps(dict(at=at, trade_id=trade_id, kind=kind, data=json.loads(data))))
                elif args.command == "health":
                    health = health_snapshot(store, cfg, time.time())
                    print(dumps(health))
                    return 0 if health["status"] == "HEALTHY" else 2
                else:
                    print(dumps(dict(paused=store.get("paused", False), heartbeat=store.get("heartbeat"),
                                     health=health_snapshot(store, cfg, time.time()), trades=store.all())))
            return 0
        if args.live:
            if args.command == "quote":
                raise ValueError("Cannot inject quotes into a live broker")
            from kiteconnect import KiteConnect
            from .kite import KiteBroker
            client = KiteConnect(api_key=os.environ["KITE_API_KEY"], timeout=10)
            client.set_access_token(os.environ["KITE_ACCESS_TOKEN"])
            broker = KiteBroker(client, cfg.stocks, live=True, expected_account=cfg.live_account_id)
        else:
            broker = PaperBroker(args.paper_db, cash=cfg.capital_limit, fees=cfg.fees)
        engine = Engine(cfg, store, broker)
        if args.command == "quote":
            broker.set_quote(args.stock, args.price)
        elif args.command == "entry":
            print(dumps(engine.request_entry(args.request_id, args.stock, args.budget)))
        elif args.command == "tick":
            print(dumps(engine.tick()))
            emit_alerts(engine.health(), set())
        elif args.command == "resume":
            engine.pause(False)
        elif args.command == "flatten":
            engine.flatten(args.request_id)
            print(dumps(engine.tick()))
        elif args.command == "retry-exit":
            engine.retry_exit(args.request_id)
        elif args.command == "resolve-absent":
            engine.resolve_absent(args.request_id, args.tag, confirmed_not_placed=args.confirmed_not_placed)
        elif args.command == "adopt-protection":
            engine.adopt_protection(args.request_id, args.trigger_id)
        elif args.command == "resolve-protection-absent":
            engine.resolve_protection_absent(args.request_id, confirmed_not_created=args.confirmed_not_created)
        elif args.command == "run":
            if not math.isfinite(args.interval) or args.interval < 3:
                raise ValueError("Polling interval must be finite and at least 3 seconds")
            previous_alerts = set()
            while True:
                try:
                    trades = engine.tick()
                    health = engine.health()
                    print(dumps(dict(at=time.time(), trades=trades, health=health)), flush=True)
                    previous_alerts = emit_alerts(health, previous_alerts)
                except Exception as exc:
                    # Keep the worker alive after transient connectivity/lock failures.
                    print(dumps(dict(event="worker_error", at=time.time(), error=str(exc))), file=sys.stderr, flush=True)
                time.sleep(args.interval)
        return 0
    finally:
        store.close()
        if hasattr(broker, "close"):
            broker.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("Worker stopped. Persisted positions will resume on the next run.", file=sys.stderr)
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
