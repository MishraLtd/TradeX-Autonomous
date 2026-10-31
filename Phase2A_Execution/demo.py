"""Paper GTT exits execute while the engine is stopped, then reconcile on restart."""
from pathlib import Path
import tempfile
from execution import Engine, Config
from execution.brokers import PaperBroker
from execution.models import dumps
from execution.store import Store


def main():
    cfg = Config.load(Path(__file__).with_name("config.paper.json"))
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / "engine.sqlite")
        broker = PaperBroker(Path(directory) / "broker.sqlite", fees=cfg.fees)
        try:
            engine = Engine(cfg, store, broker)
            for number, outcome in enumerate(("target", "stop"), 1):
                broker.set_quote("NSE:DEMO", "100")
                engine.request_entry(f"demo-{number}", "NSE:DEMO", "1000")
                trade = engine.tick()[-1]
                print(f"Filled {trade['filled_quantity']} at {trade['entry_price']}; target={trade['target']}; stop={trade['stop']}")
                print(f"Verified broker protection: {trade['protection']['health']}")
                store.close()
                engine = None  # worker offline; the separate broker still receives prices
                broker.set_quote("NSE:DEMO", trade[outcome])
                protection = broker.protection(trade["protection"]["id"])
                child = broker.order(protection.child_ids[0])
                print(f"Engine offline: broker sold {child.filled} shares ({child.status})")
                store = Store(Path(directory) / "engine.sqlite")
                engine = Engine(cfg, store, broker)
                trade = engine.tick()[-1]  # adopt and reconcile; no competing sell
                print(dumps({k: trade[k] for k in ("id", "state", "reason", "gross_pnl", "estimated_net_pnl")}))
        finally:
            broker.close()
            store.close()


if __name__ == "__main__":
    main()
