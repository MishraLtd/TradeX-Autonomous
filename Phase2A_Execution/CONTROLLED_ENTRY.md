# Controlled manual and API entry

An operator decides when to buy. The engine chooses the quantity within the
budget, derives target/stop levels from fills, and manages exits. Approved stocks
are the exact `exchange:symbol` keys in the operator-owned config's `stocks` map.
Requests cannot change that map, limits, prices, exit rules, or trading mode.
The example config contains fictitious DEMO stocks for paper testing only.

Both the CLI `entry` command and HTTP endpoint use the same entry service. A
fresh worker heartbeat is required for new entries. Run `run` continuously using
the **same config, engine DB, paper DB and live/paper mode** as the entry process.
Direct Python callers of `Engine.request_entry` retain the existing lower-level
interface and are trusted application code, not an external API.

## Paper setup

From `Phase2A_Execution`, start the exit worker in one terminal:

```bash
python -m execution run
```

In another terminal, set a fresh paper price and submit a manual request:

```bash
python -m execution quote NSE:DEMO 100
python -m execution entry manual-001 NSE:DEMO 1000
```

The response now contains `replayed` and `trade`. Repeating the same ID with the
same stock and budget returns the saved trade. Changing its inputs is rejected.
Use a new ID only for a genuinely new entry, never to retry a timed-out request.
Paper quotes must be refreshed; the engine deliberately rejects stale prices.

## Private HTTP API

Generate a token with `python -c 'import secrets; print(secrets.token_urlsafe(48))'`
in a private terminal. Store it securely and export it as `TRADEX_API_TOKEN` in
the API process environment. Never commit it or put it in a URL. Then run:

```bash
python -m execution serve-api --port 8765
```

The listener binds only to `127.0.0.1`. There is no public bind option, CORS,
cookie authentication, or endpoint to change configuration/resume/flatten.
This is an operator API, not an internet-facing web service. Do not expose it
with a public reverse proxy or open an Azure inbound port for it. For remote
access, use an authenticated SSH tunnel:

```bash
ssh -N -L 8765:127.0.0.1:8765 tradexadmin@YOUR_VM_IP
```

Read `GET /v1/universe` or submit `POST /v1/entries` with
`Authorization: Bearer <token>` and `Content-Type: application/json`.

```json
{"request_id":"operator-20261109-001","stock":"NSE:DEMO","budget":"1000.00"}
```

Example Python client (token stays out of command arguments):

```python
import json, os, urllib.request
payload = {"request_id": "operator-20261109-001", "stock": "NSE:DEMO", "budget": "1000.00"}
request = urllib.request.Request(
    "http://127.0.0.1:8765/v1/entries", data=json.dumps(payload).encode(),
    headers={"Authorization": "Bearer " + os.environ["TRADEX_API_TOKEN"],
             "Content-Type": "application/json"})
with urllib.request.urlopen(request, timeout=60) as response:
    print(response.read().decode())
```

| Response | Meaning |
| --- | --- |
| 202 | Request recorded/processed; inspect `trade.state`, `error`, orders and protection. Does not guarantee a fill. |
| 200 | Saved request replay, or universe response. |
| 401 / 403 | Authentication failed / browser-origin request refused. |
| 400 / 413 / 415 | Invalid JSON/framing, oversized body, or wrong content type. |
| 409 | Invalid inputs, conflicting ID, or an entry gate blocked the request. |
| 503 or connection timeout | Service unavailable or outcome unknown. Retry the **same ID and payload**, then inspect health/journal. |

Replay survives service/database reopen and does not contact the broker. It can
return a pending/unknown trade; it is not evidence that no order was placed.
The usual engine limits and safeguards apply to new submissions. Existing exits
continue in the separate worker when the API is stopped or receives bad traffic.
Do not edit config while processes run: pause entries, update the private config,
restart both processes, reconcile existing positions, then explicitly resume.

## Azure service

The deployment bootstrap installs `tradex-api.service` but does not enable it.
Configure `/etc/tradex/api.env` with `TRADEX_API_TOKEN=<generated token>`, owned by
root and mode 0600. It is read by systemd; do not include the token in `worker.env`.
The launcher reuses the worker's config, mode, databases, account session and
managed-identity Key Vault access. In live cloud mode the existing lease receipt
guards broker mutations, including API orders. Configure the worker as described
in `deploy/README.md` before starting either service.

```bash
sudo systemctl enable --now tradex.service tradex-api.service
```

The API restarts with the worker and stops when the worker is stopped. After a
deployment/bootstrap or explicit worker stop, start both services again. Rotate
the token by replacing the private environment file and restarting the API.
The API adds up to a 250 MiB service memory allowance; measure total RAM on a
1 GiB VM and upgrade if needed. It does not perform broker login automatically.

Deployment, broker credentials and a live-money pilot are separate from pushing
this code. Verify paper entry, target exit, rejected stock, replay, worker stop,
and restart on the actual VM before enabling live requests.
