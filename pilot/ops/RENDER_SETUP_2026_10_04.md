# Render setup walkthrough -- 2026-10-04 (Brad: "Main goal is to get hosted today")

Companion to `RENDER_MIGRATION_PLAN.md` (the why, the facts, the gates). This is the DO list, in order, for the
Render dashboard. Everything here is Brad's hand: account, keys, env vars, deploy buttons, mode files. Claude
reads the results (journals, ledger, `/health`) and reports.

State at writing: pilot code is host-ready (Phase H: `service.supervisor`, `service.paths` + `DV3_DATA_DIR`,
`DV3_PROXY_BASE`, `requirements.txt`, `.python-version` 3.12.10, Linux CI green on main). The proxy for Render
lives in its own private repo **github.com/BradSherwood24/degeneracy-proxy** (laptop `proxy.py` 2026-10-01 +
the Phase H env patch, 112 tests; `RENDER.md` in that repo carries the same env table as below). The laptop
keeps running unchanged throughout; Render runs DRY until the gates in section 4 pass. **Never two armed boxes.**

## 0. Before you start (5 minutes)

- Kalshi: create a **NEW API key** for Render (Kalshi -> Account -> API keys). Save the key id and the PEM file
  somewhere only you can see. The laptop PEM never leaves the laptop. (Plan says revoke the laptop key only
  after Render has run a clean week -- not today.)
- Generate a shared secret for the proxy write token, any long random string, e.g. in PowerShell:
  `-join ((48..57)+(97..122) | Get-Random -Count 48 | % {[char]$_})`. You will paste it into BOTH services.
- Render -> Workspace: region choice happens per service; everything below is **Ohio**.
- Connect GitHub to Render once (Render -> Account Settings -> Git Providers) and grant it BOTH repos:
  `degeneracy_v3` and `degeneracy-proxy`.

## 1. Proxy -- Private Service `degeneracy-proxy`

Render -> New -> **Private Service** -> repo `BradSherwood24/degeneracy-proxy`, branch `main`.

| field | value |
|---|---|
| Name | `degeneracy-proxy` |
| Region | Ohio |
| Runtime | Python |
| Root Directory | (blank = repo root) |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `python proxy.py` |
| Instance type | Starter (0.5 CPU / 512 MB) |

Before "Create": **Advanced**:
- **Disk**: name `proxy-state`, mount path `/var/proxy`, size 1 GB.
- **Secret File**: filename `kalshi_render.pem`, contents = the NEW key's PEM. Render mounts it at
  `/etc/secrets/kalshi_render.pem`.
- **Environment variables** (exact names; the proxy reads nothing else):

| var | value | why |
|---|---|---|
| `KALSHI_ENV` | `prod` | |
| `ALLOW_ORDERS` | `false` | READ-ONLY for the dry phase; `true` only at cutover (section 5) |
| `MAX_CONTRACTS_PER_ORDER` | `2` | same cap as the laptop |
| `DAILY_ORDER_BUDGET` | `30000` | same as the laptop since 10-02 |
| `ORDER_TICKER_PREFIXES` | `KXBTC15M,KXBTCD,KXBTC` | |
| `PROXY_PORT` | `8642` | |
| `PROXY_HOST` | `0.0.0.0` | private-network bind; a Private Service has no public URL |
| `PROXY_BUDGET_PATH` | `/var/proxy/order_budget.json` | counter on the disk, survives restarts |
| `PROXY_TOKEN` | the shared secret | writes need header `X-DV3-Token`; GETs stay open |
| `PROD_KEYID` | the NEW key id | |
| `PROD_KEYFILE` | `/etc/secrets/kalshi_render.pem` | absolute path = the Secret File |
| `PYTHON_VERSION` | `3.12.10` | (also in `.python-version`) |

After Create: **Settings -> Build & Deploy -> Auto-Deploy: Off.** Then wait for the first deploy to go live and
open **Shell** on the service:

```
python -c "import urllib.request,json;print(json.dumps(json.load(urllib.request.urlopen('http://127.0.0.1:8642/health')),indent=1))"
```

Expect: `signed: true`, `orders_enabled: false`, `host: 0.0.0.0`, `budget_path: /var/proxy/order_budget.json`,
`token_required: true`, caps 2 / prefixes / budget 30000, and a `key_fingerprint` (that is a fingerprint, not
the key). Note the service's **internal hostname** from the Connect panel (looks like `degeneracy-proxy-xxxx`,
port 8642) -- the pilot needs it next.

## 2. Pilot -- Background Worker `dv3-pilot`

Render -> New -> **Background Worker** -> repo `BradSherwood24/degeneracy_v3`, branch `main`.

| field | value |
|---|---|
| Name | `dv3-pilot` |
| Region | Ohio (same as the proxy -- private networking is per region) |
| Runtime | Python |
| Root Directory | (blank = repo root; the pilot imports `sim/` via `service/_simlaw.py`) |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `cd pilot && python -m service.supervisor --roster v33` |
| Instance type | Standard (1 CPU / 2 GB) to start; the laptop window process peaks well under 1 GB, Starter may do |

**Advanced**:
- **Disk**: name `dv3-data`, mount path `/var/dv3`, size 20 GB (grow-only; ~40 days of gz journals).
- **Environment variables**:

| var | value | why |
|---|---|---|
| `DV3_DATA_DIR` | `/var/dv3` | relocates journals_v33 / logs_v33 / ledger / ops (mode file + day-guard) to the disk |
| `DV3_PROXY_BASE` | `http://<proxy internal hostname>:8642` | from section 1's Connect panel |
| `DV3_PROXY_TOKEN` | the shared secret | sent as `X-DV3-Token` on writes only |
| `DV3_V33_ASYNC_WRITER` | `1` | same as the laptop supervisor |
| `PYTHON_VERSION` | `3.12.10` | |
| `TZ` | `UTC` | |

After Create: **Auto-Deploy: Off.** Shutdown grace: Settings -> **max (300 s)** if offered.

The worker will boot, run the startup cancel sweep (read-only proxy -> "found 0"), and sleep to the next UTC
:40. Before that first :40, open **Shell** on the worker and create the mode file ON THE DISK (absent = the
window runs `shakedown`, which places nothing and is useless as a shakedown of the dry path):

```
mkdir -p /var/dv3/ops && echo dry > /var/dv3/ops/v33_mode.txt && cat /var/dv3/ops/v33_mode.txt
```

Then check the proxy is reachable from the worker (same shell):

```
python -c "import os,urllib.request;print(urllib.request.urlopen(os.environ['DV3_PROXY_BASE']+'/health').read()[:200])"
```

and that the pilot's Linux suite is green in the container (gate 6, one-off, ~2 min):

```
cd pilot && python -m pytest -q -rs 2>&1 | tail -3
```

## 3. What Claude reads after the first Render window (:40 wake, :00 close)

On the worker: `/var/dv3/logs_v33/supervisor.scheduler.out` (one JSON line per window: wake, pid, exit code,
duration), `/var/dv3/journals_v33/summary.jsonl` (the ledger row: `resolved_mode dry`, `would_places`,
`dry_sim_fills`, `records`, lag stats), `/var/dv3/journals_v33/<close>.jsonl.gz`. Compare the same close's row
on the laptop (both run dry-vs-armed on the same tape): data-age and feed lag should be visibly lower from Ohio.

## 4. Dry gates (from the plan, section 4 Phase 2) -- all must pass before section 5

1. `/health` from the worker: proxy reachable, `orders_enabled false`, caps right, `token_required true`.
2. Read-only Kalshi GETs succeed from Render's Ohio egress (the pilot's own market/orderbook/balance reads in
   the first window prove it; a 403 from Kalshi = datacenter IP problem, stop and report).
3. Dry windows writing rows every armed hour (plan said >= 48 h; Brad can shorten -- his call), zero discovery
   errors, no `rest_invariant_*`, feed lag p99 below the laptop's.
4. Restart the worker from the dashboard (in a :02-:33 band): ledger/journals/mode file still there.
5. Boot sweep in the log on that restart (`startup_cancel_sweep ... found 0`).
6. Linux suite green in the worker shell (section 2).

## 5. Cutover (later; ONE :02-:33 UTC band; never two armed)

1. Laptop: `Set-Content pilot\ops\v33_mode.txt dry` (Brad), confirm the next laptop row says dry.
2. Render proxy: `ALLOW_ORDERS=true` -> save -> Manual Deploy (restart). Shell `/health` -> `orders_enabled true`.
3. Render worker shell: `echo armed > /var/dv3/ops/v33_mode.txt`.
4. Registration MECHANICS note (host move, before/after execution gap) in `ceremony/v33_falsifier.md`, Brad's words.
5. First armed Render window gets the MUST CONFIRM read + hand reconciliation against `/portfolio/fills`.

## 6. Rules that carry over unchanged

Deploys/restarts of either service only in :02-:33 UTC when anything is armed (a deploy mid-window kills the
window process; the boot sweep and T-4 expiry bound the damage). Auto-deploy stays OFF on both. Journals are the
research corpus: nothing deletes a `.gz` on the disk until the laptop sync (plan 3e, not built yet) confirms a
copy. The frozen falsifier, params sha and pins do not change with the host.
