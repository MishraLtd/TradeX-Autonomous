"""Run with python -m execution; paper mode is the default."""
import argparse
import json
import math
import os
import sys
import time
from .models import Config, dumps
from .store import Store
from .brokers import PaperBroker
from .engine import Engine


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
    for name in ("tick", "status", "pause", "resume", "journal"):
        sub.add_parser(name)
    flatten = sub.add_parser("flatten", help="Pause entries and request all positions closed")
    flatten.add_argument("--request-id")
    retry = sub.add_parser("retry-exit", help="Permit one additional exit after fixing a broker rejection")
    retry.add_argument("request_id")
    absent = sub.add_parser("resolve-absent", help="Resolve unknown submission after manual broker verification")
    absent.add_argument("request_id")
    absent.add_argument("tag")
    absent.add_argument("--confirmed-not-placed", action="store_true")
    run = sub.add_parser("run", help="Poll orders and manage exits until interrupted")
    run.add_argument("--interval", type=float, default=5)
    args = parser.parse_args(argv)
    cfg = Config.load(args.config)
    store = Store(args.db)
    broker = None
    try:
        # Read-only status/journal and pause work without broker credentials or connectivity.
        if args.command in ("status", "journal", "pause"):
            with store.locked():
                if args.command == "pause":
                    store.set("paused", True)
                if args.command == "journal":
                    rows = store.db.execute("SELECT at,trade_id,kind,data FROM events ORDER BY id")
                    for at, trade_id, kind, data in rows:
                        print(dumps(dict(at=at, trade_id=trade_id, kind=kind, data=json.loads(data))))
                else:
                    print(dumps(dict(paused=store.get("paused", False), heartbeat=store.get("heartbeat"), trades=store.all())))
            return 0
        if args.live:
            if args.command == "quote":
                raise ValueError("Cannot inject quotes into a live broker")
            from kiteconnect import KiteConnect
            from .kite import KiteBroker
            client = KiteConnect(api_key=os.environ["KITE_API_KEY"], timeout=10)
            client.set_access_token(os.environ["KITE_ACCESS_TOKEN"])
            broker = KiteBroker(client, cfg.stocks, live=True)
        else:
            broker = PaperBroker(args.paper_db, cash=cfg.capital_limit, fees=cfg.fees)
        engine = Engine(cfg, store, broker)
        if args.command == "quote":
            broker.set_quote(args.stock, args.price)
        elif args.command == "entry":
            print(dumps(engine.request_entry(args.request_id, args.stock, args.budget)))
        elif args.command == "tick":
            print(dumps(engine.tick()))
        elif args.command == "resume":
            engine.pause(False)
        elif args.command == "flatten":
            engine.flatten(args.request_id)
            print(dumps(engine.tick()))
        elif args.command == "retry-exit":
            engine.retry_exit(args.request_id)
        elif args.command == "resolve-absent":
            engine.resolve_absent(args.request_id, args.tag, confirmed_not_placed=args.confirmed_not_placed)
        elif args.command == "run":
            if not math.isfinite(args.interval) or args.interval < 3:
                raise ValueError("Polling interval must be finite and at least 3 seconds")
            while True:
                try:
                    print(dumps(dict(at=time.time(), trades=engine.tick())), flush=True)
                except Exception as exc:
                    # Keep the worker alive after transient connectivity/lock failures.
                    print(dumps(dict(at=time.time(), error=str(exc))), file=sys.stderr, flush=True)
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
