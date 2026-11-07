"""Synthetic accounting evidence. None of this validates a real broker account."""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from execution.market import IST


def instant(value):
    return datetime.fromisoformat(value).replace(tzinfo=IST).timestamp()


def profile(root, stocks=("NSE:DEMO", "NSE:OTHER")):
    delivery = dict(brokerage_bps="0", brokerage_cap=None, brokerage_min="0.01",
        exchange_bps="0.307", sebi_bps="0.01", ipft_bps="0.00001", gst_pct="18",
        stt_buy_bps="10", stt_sell_bps="10", stamp_buy_bps="1.5", stamp_sell_bps="0",
        buy_fixed="0", sell_fixed="0", dp_sell="15.34", rounding_buffer="1")
    intraday = dict(delivery, brokerage_bps="3", brokerage_cap="20", brokerage_min="0",
                   stt_buy_bps="0", stt_sell_bps="2.5", stamp_buy_bps="0.3", dp_sell="0")
    data = dict(schema=1, example=False, account_id="TEST", profile_id="SYNTHETIC-TEST-ONLY",
                valid_from="2026-11-01", valid_until="2026-12-31", reviewed_by="synthetic-test",
                source_reference="SYNTHETIC fixture, not an account tariff or broker validation",
                stocks={s: dict(DELIVERY=deepcopy(delivery), INTRADAY=deepcopy(intraday)) for s in stocks})
    path = root / "test.cost-profile.json"
    path.write_text(json.dumps(data))
    return str(path), data


def trade_and_evidence(root):
    start = instant("2026-11-05 08:00")
    at = instant("2026-11-06 10:00")
    end = instant("2026-11-07 18:00")
    def order(oid, side, qty, filled, average, status, seconds):
        return dict(id=oid, tag=oid, stock="NSE:DEMO", side=side, quantity=qty, filled=filled,
                    average=average, status=status, submitted=at + seconds, placed_at=at + seconds)
    trade = dict(id="one", stock="NSE:DEMO", created=at, state="CLOSED", budget="1500", reserved="0",
                 orders=[order("B1", "BUY", 12, 10, "100", "CANCELLED", 0),
                         order("S1", "SELL", 4, 4, "110", "COMPLETE", 30),
                         order("S2", "SELL", 6, 6, "105", "COMPLETE", 60)],
                 fees=dict(buy_bps="15", sell_bps="15", buy_fixed="0", sell_fixed="0"),
                 gross_pnl="70", estimated_fees="3.105", estimated_net_pnl="66.895")
    sources = []
    for kind in ("trades", "charges", "holdings", "funds", "pnl"):
        content = json.dumps(dict(kind=kind, account="TEST", synthetic=True)).encode()
        path = root / (kind + ".json")
        path.write_bytes(content)
        sources.append(dict(id=kind, kind=kind, path=path.name, sha256=hashlib.sha256(content).hexdigest()))
    def fill(fid, oid, side, qty, price, seconds):
        return dict(id=fid, order_id=oid, stock="NSE:DEMO", side=side, quantity=qty, price=price,
                    at=at + seconds, source="trades")
    evidence = dict(schema=1, account_id="TEST", period_start=start, as_of=end, sources=sources,
        fills=[fill("F1", "B1", "BUY", 5, "99", 1), fill("F2", "B1", "BUY", 5, "101", 2),
               fill("F3", "S1", "SELL", 4, "110", 31), fill("F4", "S2", "SELL", 6, "105", 61)],
        charge_groups=[dict(id="C1", order_ids=["B1", "S1", "S2"], sources=["charges"],
            components=dict(brokerage="1", stt="3", gst="1", dp="15", stamp="0", exchange="0", sebi="0", ipft="0", other="0"))],
        holdings=dict(source="holdings", opening={"BSE:LEGACY": 3}, closing={"BSE:LEGACY": 3}),
        cash=dict(source="funds", opening="1000", closing="1050", unsettled_opening="0", unsettled_closing="0", movements=[]),
        pnl=dict(source="pnl", realized_gross="70", net_after_costs="50"))
    return trade, evidence
