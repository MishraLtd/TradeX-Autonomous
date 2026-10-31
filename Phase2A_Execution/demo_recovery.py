"""Deterministic gap/timeout recovery; no credentials, network, or real waiting."""
from pathlib import Path
import tempfile

from execution import Config, Engine
from execution.brokers import PaperBroker
from execution.models import dumps
from execution.store import Store


def main():
    cfg = Config.load(Path(__file__).with_name("config.paper.json"))
    now = [1000.0]
    clock = lambda: now[0]
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / "engine.sqlite")
        broker = PaperBroker(Path(directory) / "broker.sqlite", fees=cfg.fees, clock=clock)
        try:
            engine = Engine(cfg, store, broker, clock)
            broker.set_quote("NSE:DEMO", "100")
            engine.request_entry("gap-demo", "NSE:DEMO", "1000")
            broker.set_quote("NSE:DEMO", "90")
            trade = engine.tick()[0]
            print(dumps(dict(stage="gap_below_stop_limit", state=trade["state"],
                             child_status=trade["orders"][-1]["status"], remaining=trade["remaining"])))
            now[0] += cfg.exit_timeout
            engine.tick()  # request cancellation, never assume its outcome
            trade = engine.tick()[0]  # verify cancellation and begin retry cooldown
            print(dumps(dict(stage="cancellation_confirmed", child_status=trade["orders"][-1]["status"],
                             health=engine.health()["status"], next_exit_at=trade["next_exit_at"])))
            now[0] += cfg.retry_interval
            broker.set_quote("NSE:DEMO", "90")
            engine.tick()  # submit residual sell
            trade = engine.tick()[0]
            sells = [o for o in broker._orders() if o.side == "SELL"]
            assert trade["state"] == "CLOSED" and sum(o.filled for o in sells) == trade["filled_quantity"]
            print(dumps(dict(stage="recovered", state=trade["state"], remaining=trade["remaining"],
                             sell_attempts=len(sells), health=engine.health()["status"], paused=store.get("paused"))))
        finally:
            broker.close()
            store.close()


if __name__ == "__main__":
    main()
