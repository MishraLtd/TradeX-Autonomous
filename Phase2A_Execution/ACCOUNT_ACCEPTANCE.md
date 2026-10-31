# Kite account acceptance

**Current project status: actual-account validation is pending.** Tests in this
repository use synthetic broker responses and the installed SDK. They validate
the code, not your account. No broker credentials, real account captures or live
order results were available for this change.

`validate-account` collects and checks existing broker records using **GET
requests only**. It does not submit/cancel orders, create/delete GTTs, authorize
holdings, obtain tokens, run the engine, or change its journal. Its dedicated SDK
transport rejects POST, PUT and DELETE. Missing observations remain visible.

## Configure the intended account

1. Copy the paper configuration to the ignored `config.live.json` and replace
   the fictional stock list with your approved instruments and verified ticks.
2. Set `live_account_id` to the exact Kite user ID you intend to use. New live
   entries require this ID and recheck account permissions before recording an
   entry intent. Existing position reconciliation remains available when an old
   configuration has no explicit ID; the journal's existing account binding
   still applies. A configured ID mismatch blocks startup.
3. Install `requirements-live.txt`. Configure `KITE_API_KEY` and
   `KITE_ACCESS_TOKEN` through your local environment or secret manager. Do not
   put tokens in configuration, command arguments, Git, screenshots or chat.
   Session acquisition/renewal and CDSL authorization remain account-holder tasks.

No configured credentials produces a `BLOCKED` / `NOT_RUN` result. An account
mismatch stops collection before funds or portfolio records are fetched.

## Capture current evidence

Run from `Phase2A_Execution` on the machine with the intended account configured:

```bash
python -m execution --config config.live.json --live validate-account
```

The default discovery considers engine-tagged orders on approved stocks and
paired GTTs on those stocks. To inspect particular existing broker records, pass
their exact IDs (repeat either option as needed):

```bash
python -m execution --config config.live.json --live validate-account \
  --order-id EXISTING_ENTRY_ID \
  --order-id EXISTING_EXIT_ID \
  --trigger-id EXISTING_GTT_ID \
  --output state/account-validation/baseline.kite-evidence.json
```

Use IDs from activity you independently performed and can identify in the broker
account. This command does not generate test trades or try to force partial
fills. If a scenario has never occurred, keep it pending. Order history/trade
records may become unavailable after the trading day, so capture relevant
observations while the broker still serves them. Trigger-linked child orders are
collected automatically. Limits are 20 explicitly selected orders, 20 triggers,
and 50 total orders including discovered children.

Broker endpoints are read sequentially and are not an atomic account snapshot.
The daily CNC order-fill delta is also compared with the net positions response.
If fills change during collection, order/trade quantities or portfolio totals may
disagree. Keep that report, then collect again after the broker records converge;
do not change expected quantities just to turn a failed check green.

## Evidence required

| Check | What can satisfy it | What does not satisfy it |
|---|---|---|
| Account and permissions | Expected account ID, enabled exchanges, CNC product, LIMIT/MARKET support and a recognized authorization mode. | A valid token for a different account. |
| Available funds | Enabled equity segment and finite raw cash, current available balance and net funds. The smallest available cash bound is used. | Collateral, extra margin, or falling back to raw cash when current funds are missing. |
| Instruments | Approved cash equities with one-share lots and matching tick sizes. | Fictional paper symbols or stale instrument assumptions. |
| LIMIT IOC entry | Terminal CNC buy with actual fills corroborated by the trade endpoint. | An order ID/acknowledgment alone. |
| Protected market exit | Terminal CNC MARKET IOC sell with protection reported and matching fills. | An unprotected sell, or an ambiguous converted LIMIT record without broker review. |
| Partial fills | A partial-fill state in order history plus final quantity/average reconciliation against unique trade records. | Only unit tests, or an unfinished order. |
| Delivery holdings and sale | Settled holdings and an executed sell accompanied by holdings-use evidence. | Only same-day positions or T1 quantities. |
| Sell authorization | Standing authorization reported by the broker, or enough current-session electronic authorization for the observed holdings. | A successful CDSL flow on an earlier day, or assuming future GTT execution is already authorized. |
| Paired GTT execution | Verified two-leg CNC sell-limit contract, authoritative child ID, and matching child-order fills. | GTT registration without observed execution. |
| Overnight settlement | Same-account captures on different IST dates showing carried quantity continuity and migration into settled holdings. | A single snapshot or a move into T1 alone. |

Coverage is representative account evidence for the configured universe, not an
exhaustive certification of every symbol or market condition. Readiness may
change after capture. Even a complete report does not authorize or enable trades.

## Compare settlement across captures

Keep a capture showing a same-day CNC position or T1 stock. After those shares
appear as settled delivery holdings, collect another report with that baseline:

```bash
python -m execution --config config.live.json --live validate-account \
  --previous state/account-validation/baseline.kite-evidence.json \
  --order-id EXISTING_ENTRY_ID \
  --order-id EXISTING_EXIT_ID \
  --trigger-id EXISTING_GTT_ID
```

The baseline must have the same account fingerprint and exact stock/tick universe,
and an earlier capture date in `Asia/Kolkata`. Compare the report's inventory
components with the broker UI/records. Review any intervening trades, corporate
actions, holidays, or adjustments; the tool does not infer a settlement calendar
or certify the cause of an unexplained quantity change. It compares observed
quantity continuity rather than assuming a fixed settlement delay.

All required execution scenarios must remain available in the current capture
for `EVIDENCE_COMPLETE`. Daily order-history retention can make this impractical
in a single report. Keep the separate reports for operator/broker review rather
than treating missing historical records as passed. Offline replay does not
promote earlier evidence into live acceptance or unlock trading.

## Inventory and authorization boundaries

The adapter reconstructs opening stock as
`quantity + t1_quantity + used_quantity`, verifies it against `opening_quantity`,
then adds today's net CNC quantity. This avoids subtracting a holdings sale twice
when both holdings and positions reflect it. For example, 100 opening shares,
60 remaining holdings, 40 used shares and a -40 CNC position reconcile to 60.

Missing/inconsistent fields, duplicate holdings, cross-exchange holdings under
the same symbol, non-CNC exposure, overnight CNC positions, pledged/MTF holdings,
short quantities and broker discrepancy flags fail closed for managed symbols.
These cases require account-specific reconciliation; the adapter does not guess
which shares are available to sell. Fully sold holdings that disappear while a
negative CNC position remains can also require a fresh broker snapshot/review.

Electronic authorization checks are deliberately conservative: the date must match
the capture's IST session, unused authorized units must cover observed holdings,
and captures at/after 17:30 do not pass that session check. This is metadata
verification, not depository approval. A standing-authorization mode is likewise
not a guarantee that a later sell or GTT child will be accepted. Authorization
checks report issues; they never initiate or bypass the depository flow.

## Results, privacy and replay

- `BLOCKED`: at least one contract/read/account check failed. Inspect the named
  checks; no account validation has been established.
- `INCOMPLETE`: required scenarios are missing, or data is synthetic/offline.
  `NOT_OBSERVED` is not a pass.
- `EVIDENCE_COMPLETE`: a fresh read-only collection satisfied the implemented
  checks and all scenario observations. Review its limits above; it is not a
  production certificate or an execution guarantee.

Exit code is **0** only for `EVIDENCE_COMPLETE`, **2** for blocked/incomplete
reports, and **1** for configuration or command failures. `live_account_validated`
refers only to the implemented observational checks in a live collection. It is
always false for replay and synthetic data.

Default reports go to ignored `state/account-validation/` with unique names.
Files are written atomically with mode `0600`; newly created output directories
use `0700`. `*.kite-evidence.json` is also ignored by Git. Collection allowlists
fields, hashes the account ID, and excludes names, email, API keys, tokens,
unrelated-symbol holdings, and free-text HTTP errors. Reports still contain
private positions, order IDs and financial amounts: keep them local and out of
public issues/commits. Choose a private path if overriding `--output`.

To reproduce analysis without any broker connection:

```bash
python -m execution --config config.live.json validate-account \
  --snapshot state/account-validation/baseline.kite-evidence.json \
  --output state/account-validation/replay.kite-evidence.json
```

Replay returns `OFFLINE_REPLAY` and cannot claim live validation, even if its
input file was previously a live capture. Evidence files are not signed
certificates; never accept an edited file as independent proof of broker state.

Official references: [account profile and funds](https://kite.trade/docs/connect/v3/user/),
[holdings, positions and authorization](https://kite.trade/docs/connect/v3/portfolio/),
[orders and trades](https://kite.trade/docs/connect/v3/orders/),
[GTT](https://kite.trade/docs/connect/v3/gtt/),
[broker explanation of holdings after a sale](https://kite.trade/forum/discussion/comment/52974/).
