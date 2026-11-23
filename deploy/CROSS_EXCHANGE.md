# NSE/BSE paper trading for 16–20 November 2026

From activation through Friday 20 November, the continuous portfolio admits only a matched pair
on opposite exchanges for the same ISIN. The window is fixed in Asia/Kolkata. It collects data
from the regular open and admits pairs from 09:30 until 15:00 IST. The next verified trading
session resumes NSE trend entries across all 13 symbols. New fills continue using Zerodha
fees. The trend profit exit becomes strictly positive net P&L and the former ₹10 rotation
filter is removed. Three consecutive losses can still rotate a trend position.

| Addition | NSE symbol | BSE code | ISIN |
|---|---|---:|---|
| Bosch | BOSCHLTD | 500530 | INE323A01026 |
| Hitachi Energy India | POWERINDIA | 543187 | INE07Y701011 |
| MRF | MRF | 500290 | INE883A01011 |
| PTC Industries | PTCIL | 539006 | INE596F01018 |

The worker resolves both venue keys from the daily Upstox cash-equity and intraday-eligibility
files. It verifies ISIN, exchange, lot size, tick size and the BSE A/B tariff group.
Each exchange must confirm normal trading on the live stream as well as a regular calendar session.

## Entry calculation

For each direction, use the best ask at the buying venue and best bid at the selling venue.
Apply the configured 5 basis points of adverse slippage per leg, then round the buy price up
and the sell price down to each venue's tick. For matched quantity `q`,

    estimated net INR = q × (simulated sell price − simulated buy price) − buy charges − sell charges

Each leg pays the lower of 0.03% of its turnover or ₹20 brokerage. Costs include buy-side
stamp duty, sell-side STT, venue transaction fees, SEBI fees and applicable GST.
The NSE transaction fee includes IPFT. Costs are rounded to paise per simulated order.
Broker contract-note aggregation can change small realized amounts. [Zerodha charges](https://zerodha.com/charges/)

Books must be live, at most one second old and within half a second of each other. The worker
compares both directions and prioritises the highest estimated net INR among opportunities
updated together. It reserves full cash for the larger notional plus both fees, limits quantity
to 10% of displayed top-level depth on each side and allows up to three distinct pending pairs.
A stock whose first whole share exceeds free cash is skipped.

After 250 ms, both venues must supply newer books on the same connection. The worker recalculates
the matched quantity and costs. Only a strictly positive result records fills. A pair expires
after two seconds. Both legs, fees and cash commit together in SQLite. This local matching
assumption omits live legging and queue risk. Repeated timestamps or unchanged consumed depth
cannot generate repeated matches. A session can finish with zero trades.

## Existing Azure VM

On the worker, use a clean checkout of the reviewed `profitup` commit. Activate before 09:10 or
after 15:15 IST while the account is flat. The existing upgrade writes a backup, verifies the
release and preserves the ledger, cash and pause marker.

```bash
sudo bash deploy/azure/upgrade-upstox-greedy.sh "$TRADEX_RELEASE" --cross-exchange-week
```

To review the result on the worker, run a fresh report.

```bash
cd /opt/tradex/current/Phase2A_Execution
sudo -u tradex env TRADEX_MODE=upstox-paper /opt/tradex/current/venv/bin/python \
  -m execution.paper_trial report | python3 -c '
import json, sys
d = json.load(sys.stdin)
print(d["release"], d["entry_policy"], d["fee_model"], d["status"], d["codes"])
for t in d["trades"]:
    p = t.get("spread_match")
    if p:
        print(t["day"], t["symbol"], p["quantity"], "buy", p["buy_venue"], p["buy_price"],
              "sell", p["sell_venue"], p["sell_price"], "net INR", p["net"])
'
```

During the window, status shows `CROSS_EXCHANGE` and `MATCHED_PAIR`. The dated spread statistics
include pairs checked and matched. Each signal records both quote times, observed gap and
expected net. Each matched trade records both prices and fees, with one fill event per venue.
The NSE leg is the reporting reference for `LONG` or `SHORT`. The pair itself closes flat.
