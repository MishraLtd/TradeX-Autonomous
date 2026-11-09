"""Run with python -m execution; paper mode is the default."""
import argparse
import getpass
from contextlib import nullcontext
import json
import os
import sys
import time
from datetime import datetime, timezone
from dataclasses import replace
from .models import Config, dumps
from .store import Store
from .brokers import PaperBroker
from .engine import Engine
from .supervision import health_snapshot
from .polling import Cadence, PollMonitor


def login(args, cfg):
    from kiteconnect import KiteConnect
    from .sessions import save_session
    api_key = os.environ.get("KITE_API_KEY")
    if not api_key:
        raise ValueError("Set KITE_API_KEY in the local environment")
    client = KiteConnect(api_key=api_key, timeout=10)
    if args.command == "login-url":
        print(client.login_url())
        return 0
    if not cfg.live_account_id:
        raise ValueError("Configure live_account_id before login")
    if not sys.stdin.isatty():
        raise ValueError("Run login in an interactive terminal; do not pass tokens in command arguments")
    secret = os.environ.get("KITE_API_SECRET") or getpass.getpass("Kite API secret: ")
    request_token = getpass.getpass("Request token from your registered redirect URL: ")
    try:
        response = client.generate_session(request_token, api_secret=secret)
        if client.profile().get("user_id") != cfg.live_account_id:
            raise ValueError("Wrong account")
    except Exception as exc:
        raise RuntimeError("Login exchange failed (" + type(exc).__name__ + "); obtain a new request token before retrying") from None
    metadata = save_session(args.session_file, response, api_key, cfg.live_account_id, time.time())
    print(dumps(dict(status="SESSION_SAVED", session_file=args.session_file, **metadata)))
    return 0


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
        api_key, token = os.environ.get("KITE_API_KEY"), os.environ.get("KITE_ACCESS_TOKEN")
        if api_key and not token and os.path.exists(args.session_file):
            from .sessions import read_session
            token = read_session(args.session_file, api_key, expected, time.time())["access_token"]
        missing = [name for name, value in (("KITE_API_KEY", api_key), ("KITE_ACCESS_TOKEN", token)) if not value]
        if missing:
            print(dumps(dict(status="BLOCKED", source="NOT_RUN", live_account_validated=False,
                             missing_environment=missing, detail="Configure credentials securely in the local environment; do not paste them into chat.")))
            return 2
        client = readonly_client(api_key, token)
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
    parser.add_argument("--live", action="store_true", help="Use Kite with a private session receipt")
    parser.add_argument("--session-file", default="state/live.kite-session.json")
    sub = parser.add_subparsers(dest="command", required=True)
    quote = sub.add_parser("quote", help="Set a paper quote")
    quote.add_argument("stock")
    quote.add_argument("price")
    enter = sub.add_parser("entry", help="Explicit entry request; future strategy calls the same interface")
    enter.add_argument("request_id")
    enter.add_argument("stock")
    enter.add_argument("budget")
    api = sub.add_parser("serve-api", help="Private authenticated entry API; requires a running exit worker")
    api.add_argument("--port", type=int, default=8765)
    for name in ("tick", "status", "health", "pause", "resume", "journal", "preflight", "login-url", "login"):
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
    run.add_argument("--interval", type=float, help="Override config poll_interval (seconds between cycle starts)")
    validate = sub.add_parser("validate-account", help="Collect read-only Kite account evidence, or replay a private capture")
    validate.add_argument("--expected-account")
    validate.add_argument("--order-id", action="append", default=[])
    validate.add_argument("--trigger-id", action="append", default=[])
    validate.add_argument("--previous", help="Earlier private capture for settlement comparison")
    validate.add_argument("--snapshot", help="Offline replay only; cannot validate a live account")
    validate.add_argument("--output", help="Private JSON report path; defaults to ignored state/account-validation")
    financial_template = sub.add_parser("finance-template", help="Write an incomplete private evidence template and engine order reference")
    financial_template.add_argument("--output", required=True)
    financial = sub.add_parser("reconcile-finances", help="Compare cumulative broker statements with engine records; no broker requests")
    financial.add_argument("--evidence", required=True, help="Normalized financial evidence JSON and its source files")
    financial.add_argument("--output", help="Private report path; defaults to ignored state/finance")
    financial.add_argument("--replay", action="store_true", help="Analyze only; do not record a receipt or change entry gates")
    args = parser.parse_args(argv)
    cfg = Config.load(args.config)
    if args.command in ("login-url", "login"):
        return login(args, cfg)
    if args.command == "run" and args.interval is not None:
        cfg = replace(cfg, poll_interval=args.interval)
    if args.command == "validate-account":
        return validate_account(args, cfg)  # no Engine/Store initialization or trading mutation
    store = Store(args.db)
    broker = None
    runtime = None
    try:
        if args.command == 'run':
            store.acquire_worker()
        if args.command in ("finance-template", "reconcile-finances"):
            from pathlib import Path
            from .acceptance import save_private
            from .finance import read_json, reconcile, record_report, template
            with store.locked():
                if args.command == "finance-template":
                    save_private(args.output, template(store, cfg, time.time()))
                    print(dumps(dict(status="INCOMPLETE_TEMPLATE", path=args.output)))
                    return 0
                evidence = read_json(args.evidence)
                report = reconcile(evidence, store.all(), store.get("account"), cfg,
                                   Path(args.evidence).resolve().parent, time.time())
                if not args.replay:
                    record_report(store, report)
                path = args.output or "state/finance/" + report["report_id"] + ".financial-report.json"
                save_private(path, dict(report, replay=args.replay))
                print(dumps(dict(status=report["status"], report_id=report["report_id"], report_path=path,
                                 replay=args.replay, financial_match=report["financial_match"],
                                 issues=report["issues"], warnings=report["warnings"])))
                return 0 if report["status"] == "MATCHED" else 2
        # Read-only status/health/journal and pause work without broker credentials or connectivity.
        if args.command in ("status", "health", "journal", "pause"):
            with store.locked() if args.command == "pause" else nullcontext():
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
            from .live import LiveRuntime
            runtime = LiveRuntime(cfg, store, os.environ.get("KITE_API_KEY"), args.session_file, KiteConnect)
            engine = None if args.command in ("run", "tick", "serve-api", "entry") else runtime.prepare()
        else:
            broker = PaperBroker(args.paper_db, cash=cfg.capital_limit, fees=cfg.fees)
            engine = Engine(cfg, store, broker)
        if args.command == "serve-api":
            from .api import EntryService, serve
            serve(EntryService(cfg, store, runtime.prepare if runtime is not None else lambda: engine), args.port)
        elif args.command == "quote":
            broker.set_quote(args.stock, args.price)
        elif args.command == "entry":
            from .api import EntryService
            service = EntryService(cfg, store, runtime.prepare if runtime is not None else lambda: engine)
            print(dumps(service.submit(dict(request_id=args.request_id, stock=args.stock, budget=args.budget))))
        elif args.command == "tick":
            trades, health = runtime.tick() if runtime is not None else (engine.tick(), engine.health())
            print(dumps(trades))
            emit_alerts(health, set())
        elif args.command == "preflight":
            result = engine.preflight()
            print(dumps(result))
            return 0 if result["status"] in ("READY", "NOT_REQUIRED") else 2
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
            previous_alerts = set()
            cadence = Cadence(cfg.poll_interval)
            monitor = PollMonitor(cfg, store)
            while True:
                cadence.begin()
                try:
                    if runtime is not None:
                        trades, health = runtime.tick()
                    else:
                        with store.locked():
                            monitor.begin()
                        trades = engine.tick()
                        with store.locked():
                            monitor.finish(not any(t.get("error") for t in trades),
                                           any(t["state"] not in ("CLOSED", "NO_FILL") for t in trades))
                        health = engine.health()
                    print(dumps(dict(at=time.time(), trades=trades, health=health)), flush=True)
                    previous_alerts = emit_alerts(health, previous_alerts)
                except Exception as exc:
                    # Keep the worker alive after transient connectivity/lock failures.
                    print(dumps(dict(event="worker_error", at=time.time(), error=str(exc))), file=sys.stderr, flush=True)
                time.sleep(cadence.delay())
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
