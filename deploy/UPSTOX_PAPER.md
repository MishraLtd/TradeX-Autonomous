# Five-session Upstox intraday paper trial

This trial uses real Upstox market data with an entirely local simulated account.
The trial entry point cannot submit, cancel or modify a broker order. It has no
live-mode flag and does not use the Upstox trading SDK. The existing Kite CNC
engine remains a separate mode; its live-account capabilities are not used here.

## Frozen experiment

| Setting | Value |
| --- | --- |
| Starting virtual cash | ₹4,000, once for the entire trial; no daily reset |
| Maximum entry budget | ₹2,000 including estimated entry fees |
| Maximum simultaneous positions | 3; pending entries reserve a slot and cash |
| Financing | Whole shares; 100% notional reserved on both long and short entries; no leverage |
| Fixed test universe | SBIN, ITC, HDFCBANK; current NSE EQ and MIS eligibility verified |
| Entries, IST | 09:30 SBIN; 10:30 ITC; 11:30 HDFCBANK |
| Session 1 / 3 / 5 directions | Buy–sell SBIN; sell–buy ITC; buy–sell HDFCBANK |
| Session 2 / 4 directions | Sell–buy SBIN; buy–sell ITC; sell–buy HDFCBANK |
| Target | 2% estimated net profit on filled entry notional, after estimated fees |
| Stop | 1% price move against entry, rounded to the instrument tick |
| Daily loss action | At ₹100 accumulated losing-trade/open-position loss, pause entries and request exits; winners do not offset this gate |
| Square-off | Request all remaining exits at 15:00 IST; unresolved at 15:10 fails the trial and blocks later entries |
| Length | Five provider-confirmed regular exchange sessions, frozen at setup |

These symbols and directions are operational test inputs, not investment
recommendations. There is no learned entry strategy. There are at most three
scheduled entries per day, with no automatic re-entry after an exit. Each entry
gets the lesser of ₹2,000 and available cash divided by unused position slots.
The cap does not permit ₹6,000 of positions against ₹4,000. An unaffordable share
is skipped. Short-sale proceeds never increase available funds.

Each slot has a 60-second opportunity window. Late/restarted workers record a
miss instead of chasing a missed entry. The following session reverses the
directions deterministically. Nothing resets the account to hide losses. All
times use `Asia/Kolkata`, regardless of the VM's UTC clock.

## Execution and data assumptions

- The read-only Analytics Token is stored in Key Vault as
  `upstox-analytics-token`. The VM reads it using managed identity. No daily
  OAuth login, API secret, broker-order permissions or public API port is needed.
- A single V3 `full` WebSocket subscription receives the three instrument keys.
  Subscriptions are binary; data uses Upstox's pinned Protobuf schema. The first
  book for each symbol on every connection is a snapshot and cannot execute.
- Both provider and local receipt timestamps must be within 15 seconds, with
  at most two seconds of future clock tolerance. Quotes need fresh last-trade
  time, valid bid/ask quantities and tick-aligned positive prices. Repeated or
  reversed timestamps cannot replenish simulated liquidity.
- A daily instrument/MIS recheck and market-timings check must succeed. The
  stream must indicate `NORMAL_OPEN`; pre-open and closing-auction prices are
  excluded. Changed tick sizes/eligibility block the experiment for inspection.
- Entries/exits execute on a subsequent **live** book after a minimum 250 ms.
  Buys use ask plus 5 bps; sells use bid minus 5 bps, rounded adversely to a tick.
  Each frame offers at most 10% of displayed top-level size. Entry remainder is
  cancelled as simulated IOC; exits can fill partially over subsequent frames.
  The same stock/side/frame capacity is shared across all simulated fills.
- Stops/targets are local virtual controls. They do not operate while this VM
  or receiver is down. On recovery, a stop gap fills at a fresh executable price,
  never at an invented stop price. An exit pending over 60 seconds is unhealthy.
- Fee estimates use the reviewed Upstox Basic NSE intraday schedule: min(₹20,
  0.1%) brokerage per order, exchange, SEBI, IPFT, GST, sell-side STT and buy-side
  stamp duty. No delivery/DP fee. Fees accrue cumulatively across exit fills.
  Estimates use paise rounding; actual account plans and statutory rounding can
  differ. Prices, liquidity, fees and latency do not prove real execution quality.

A target or loss limit is not a guaranteed final P&L. If the feed is unavailable,
no stale-price fills occur. If the intraday deadline passes with a position open,
the position remains explicitly unresolved in the report; the engine does not
pretend it closed or carry on opening trades the next day.

## 1. Store the Analytics Token in Azure Cloud Shell

Generate **Analytics**, not an order-capable access token, under
[Upstox Developer Apps](https://account.upstox.com/developer/apps). The provider
documents it as free, read-only and valid for one year. Never paste it into chat.

Set `TRADEX_RELEASE` to the full reviewed commit SHA supplied with this release.
Use a fresh directory so local edits in any previous checkout are preserved:

```bash
export TRADEX_RELEASE='FULL_REVIEWED_COMMIT_SHA'
git clone https://github.com/MishraLtd/TradeX-Autonomous.git "tradex-paper-$TRADEX_RELEASE"
cd "tradex-paper-$TRADEX_RELEASE"
git checkout --detach "$TRADEX_RELEASE"
az account set --subscription 'YOUR_EXISTING_SUBSCRIPTION_ID'
bash deploy/azure/store-upstox-token.sh YOUR_EXISTING_VAULT_NAME
```

The script prompts with hidden input, uploads via a temporary private file,
deletes that file, and does not print the token or put it in process arguments.
If Azure returns a Key Vault permission error, the signed-in operator needs a
secret-write role on this vault (for example **Key Vault Secrets Officer**).
The VM's existing **Key Vault Secrets User** role is sufficient to read it.
Do not grant the GitHub monitor secret access; it only needs blob read access.

## 2. Install on the existing VM

SSH from Cloud Shell using the existing key and VM IP. Then, **on the VM**:

```bash
export TRADEX_RELEASE='THE_SAME_FULL_REVIEWED_COMMIT_SHA'
git clone https://github.com/MishraLtd/TradeX-Autonomous.git "$HOME/tradex-paper-$TRADEX_RELEASE"
cd "$HOME/tradex-paper-$TRADEX_RELEASE"
git checkout --detach "$TRADEX_RELEASE"
sudo bash deploy/azure/start-upstox-paper.sh "$TRADEX_RELEASE"
```

If `git` is absent, install it with `sudo apt-get update` and
`sudo apt-get install -y git` before cloning. The installer:

1. Stages an immutable release and installs pinned direct dependencies.
2. Checks managed-identity token access and stream authorization, resolves the
   three stocks and freezes the next five regular sessions. Preparation after
   09:25 IST starts on a later session, so the first slot is not already missed.
3. Refuses to switch a live worker or reset an existing trial ledger.
4. Stops the previous paper API/worker, verifies its positions are flat, and
   takes a consistent stopped-worker backup of **both** old DEMO databases and
   config. Original DEMO files are left in place.
5. Switches `TRADEX_MODE` to `upstox-paper`, starts the fenced worker, disables
   manual API entries for this fixed experiment, and runs backup/health checks.
6. Restores the previous worker config/release if activation fails. A partially
   created trial DB is preserved for diagnosis; it is never silently reset.

The existing lease anchor, managed identity, storage, health timer and backup
timer are reused. No extra VM is provisioned, no public port is opened, and no
Mac process is required. A prepared calendar and successful install are not yet
proof of an actual Upstox data session. Check the following during market hours.

## 3. Verify the first session

```bash
cd /opt/tradex/current/Phase2A_Execution
sudo systemctl status tradex.service --no-pager
sudo journalctl -u tradex.service -n 30 --no-pager
sudo -u tradex env TRADEX_MODE=upstox-paper \
  /opt/tradex/current/venv/bin/python -m execution.paper_trial status
sudo systemctl status tradex-maintain@backup.service --no-pager
```

Require a fresh heartbeat, `market.connected=true`, `market.normal=true`,
`market.calendar_valid=true`, increasing `live_quote_updates` and quote ages
below 15 seconds. The first slot should create a paper trade with target/stop;
it must not appear in Upstox's order book. Status includes only this local
simulator's trades, not a claim to have inspected the real broker account.

At 15:10 IST verify zero open positions, or investigate an explicit failure.
Keep the code/config frozen for the five sessions. A missed session is counted
and reported, not replaced with a better-looking sixth session. Use GitHub's
**Cloud worker external health** workflow to verify its next scheduled run. The
existing workflow consumes the same sanitized `health.json` and now detects
stale feed, stalled exits, missing backups and worker failure for this mode.
GitHub cron timing is best-effort; inbox/device delivery still needs an actual
operator notification test. No message-delivery guarantee is made by the tests.

## 4. Reports and backup recovery

```bash
cd /opt/tradex/current/Phase2A_Execution
sudo -u tradex env TRADEX_MODE=upstox-paper \
  /opt/tradex/current/venv/bin/python -m execution.paper_trial report
```

Private local reports are in `/var/lib/tradex/upstox-trial/reports/`:
`latest.json`, a day-scoped JSON file for each completed date, and `final.json`
after the last session. They contain starting/equity/available funds, long/short
entries and exits, net estimates, win/loss counts, drawdown sampled every minute,
fee/slippage assumptions, fill latency, feed coverage, reconnects, skipped slots
and unresolved exits. P&L from stale marks is identified as stale.

The private `operations` container receives an hourly
`backups/upstox-trial/latest-report.json` (also when the trial completes) and
timestamped `backups/upstox-trial/` sets.
Each set contains one SQLite file (ledger, cash, schedule, fills, quotes and
watermarks), report and checksum/config manifest. The manifest uploads last.
Health checks the successful-backup receipt and reports backups older than
27 hours. Local copies are retained for seven days. These blobs reuse the
existing `operations/backups/` lifecycle policy: 30-day base-blob retention and
seven-day version retention, without a new cloud-resource deployment.

For an isolated restore drill, copy/download a snapshot **and its `.sqlite.json`
manifest** to a private directory, then restore to a new file, never the running
ledger:

```bash
sudo -u tradex env TRADEX_MODE=upstox-paper \
  /opt/tradex/current/venv/bin/python -m execution.paper_trial \
  --db /var/lib/tradex/upstox-trial/restore-check.sqlite \
  restore /var/lib/tradex/upstox-trial/backups/CHOSEN_SNAPSHOT.sqlite
```

Restore verifies checksum, SQLite integrity and the frozen config, and sets
`RESTORED_TRIAL_REQUIRES_REVIEW`. Restored history may lack later ticks/fills; it
does not silently continue an experiment or recreate missed market data. The
restore command is not a way to reset capital for this week.

To stop the trial, use `sudo systemctl stop tradex.service`; a stop does not
fabricate exits. Prefer waiting until the daily status is flat. Inspect the
preserved history before any new experiment. Starting another trial is a new
configuration/ledger decision, not an automatic weekly reset.

## Evidence and boundaries

The earlier Azure DEMO round trip, private entry API and maintenance success
were reported by the operator on 9 November 2026. This implementation adds
credential-free automated tests for the new behavior. Activation on the VM,
real streaming data, first scheduled fills, actual cloud restore and delivered
notifications require operator-environment evidence. Do not describe them as
verified solely because CI passes. A five-session fixed-schedule paper trial
measures operational behavior and estimated results, not investment performance
or readiness to risk real money.

Official references reviewed 10 November 2026:

- [Analytics Token](https://upstox.com/developer/api-documentation/analytics-token/)
- [V3 full market feed](https://upstox.com/developer/api-documentation/v3/get-market-data-feed/)
- [Feed authorization](https://upstox.com/developer/api-documentation/get-market-data-feed-authorize-v3/)
- [Instrument and MIS masters](https://upstox.com/developer/api-documentation/instruments/)
- [Market timings](https://upstox.com/developer/api-documentation/get-market-timings/)
- [Market and CAS statuses](https://upstox.com/developer/api-documentation/appendix/market-status/)
- [Upstox charge schedule](https://upstox.com/brokerage-charges/)

The unchanged `MarketDataFeed.proto` is supplied by Upstox at
`https://assets.upstox.com/feed/market-data-feed/v3/MarketDataFeed.proto`.
SHA-256: `3e1c939dce2c83a3fef91405c5ca268dfa089aae973319f56685f12a5afcf612`.
`MarketDataFeed_pb2.py` was generated with `grpcio-tools==1.76.0`; only the
Protobuf runtime and websocket-client are needed on the VM.
