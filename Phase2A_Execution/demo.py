"""Isolated paper walkthrough: automatic profit target and stop-loss exits."""
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
                broker.set_quote("NSE:DEMO", trade[outcome])
                engine.tick()  # submit sell
                trade = engine.tick()[-1]  # confirm sell
                print(dumps({k: trade[k] for k in ("id", "state", "reason", "gross_pnl", "estimated_net_pnl")}))
        finally:
            broker.close()
            store.close()


if __name__ == "__main__":
    main()
