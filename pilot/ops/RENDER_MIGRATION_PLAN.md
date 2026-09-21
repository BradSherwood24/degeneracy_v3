# V3.2 on Render -- scope and migration plan

Written 2026-09-21 ~20:30Z on Brad's ask ("Write up that scope and plan. Using Render as the hosting
service"). Status: PLAN. Nothing here is built or provisioned. Every lever in section 6 is Brad's.

## 0. Why move

Every incident that cost windows or money since the 2026-09-14 re-arm traces to the laptop, not to the
strategy or the code:

| date | incident | cost | root cause |
|---|---|---|---|
| 09-15 05:30Z | Windows Update reboot; proxy (plain process) died; task is interactive-logon | 7 windows lost | laptop |
| 09-17 13:00Z | local power outage; 502 on cancel; rest stayed live; unseen fill, unhedged | one naked contract (+44c by luck; could have been -$0.60) | laptop |
| ongoing | proxy not a service; task sleeps when logged out; RAM pressure kills background work | fragility | laptop |

Live scoreboard at writing (main c015339, 961 tests): n = 12, 12/12 positive, mean lock +11.2c per
contract, capture ratio 12/19 = 63%, execution gap -0.6c (live beats shadow), balance $51.997 -> $54.56.

Kalshi's matching engine runs in AWS us-east-2 (Ohio); the public REST/WS hosts are CloudFront edges
(a 1 ms ping measures the edge, not the engine). Render offers an Ohio region. Residential -> Ohio today
is tens of ms per RTT with jitter; Ohio -> Ohio is low single digits. The continuous requoter does
~100 replaces per window, each a cancel -> confirm -> create round trip (amends still 403 at the proxy),
so RTT is directly the size of the "nothing resting" gap. Expect the capture ratio to rise, and expect
the replace-rate alarm (60/min pin) to fire MORE often at lower RTT -- the hysteresis lever becomes live.

## 1. Render facts that shape the design (from the docs, 2026-09-21)

- **Cron Jobs cannot attach a persistent disk** (ephemeral container per run; write to external
  storage). Our journals (~19 MB gz per window, ~460 MB/day, 2.8 GB on disk now) + ledger + ops files
  need a disk -> the pilot runs as a **Background Worker** with a **Persistent Disk** and its own
  in-process scheduler, not as a Render Cron Job. (docs: cronjobs, disks, background-workers)
- **Persistent disks**: mounted at an absolute path; grow-only; daily snapshots kept >= 7 days;
  single instance only; **deploys and restarts of a disk-backed service are NOT zero-downtime** (old
  instance stops, new one starts); build/one-off jobs cannot see the disk; $0.25/GB/month.
- **Private network**: services in the same region + workspace reach each other by a stable internal
  hostname `<name>-<suffix>:<port>`; the listener must bind **0.0.0.0** (ports 18012/18013/19099
  reserved). Private Services get no public URL and are unreachable from the internet.
- **Secret Files**: uploaded plaintext files mounted at runtime (default `/etc/secrets/<name>`),
  excluded from the build; <= 1 MB total. Env vars per service or via Environment Groups.
- **Outbound IPs**: shared static ranges per region, no charge, visible in the dashboard (Connect ->
  Outbound). Dedicated IPs exist ($100/month, Pro workspace+) -- not needed unless Kalshi allowlists.
- **Deploys**: auto-deploy on push is the default -> **turn it OFF** (Settings -> Auto-Deploy Off);
  manual deploys from the dashboard / API / deploy hook; `[skip render]` in a commit message skips.
  Shutdown sequence on deploy/restart: SIGTERM, grace period default 30 s (max 300 s), then SIGKILL.
  A V3.2 window runs ~22 minutes -> a deploy mid-window kills the process. Same rule as the laptop:
  **deploy only inside :02-:33 UTC.**
- **Python**: `PYTHON_VERSION` env var (fully qualified, e.g. `3.12.10`) or a `.python-version` file
  at the repo root; native runtime installs from `requirements.txt` (we have none today -- see 3d).
- **Compute** (background workers and private services share the plan list; **no free plan** for
  either): `0.5c-512mb` (legacy Starter, ~$7/mo), `1c-2g` (Standard, ~$25/mo), `2c-4g` (Pro).
  Prorated per second. Workspace: Hobby (single member, 25 services) is enough; Pro is a flat
  $25/workspace/month (autoscaling, environments, team seats -- none needed). SSH/shell is available on
  paid instances (not on free plans/cron jobs).
- **Regions**: Oregon, Ohio, Virginia, Frankfurt, Singapore; a service's region cannot be changed
  later; private networking is per region. **Pick Ohio.**

## 2. Target architecture (recommended: two services, Ohio)

```
Render workspace (Hobby) -- region Ohio
+-- degeneracy-proxy   Private Service   0.5c-512mb   disk 1 GB (order_budget.json)
|     env: KALSHI_ENV=prod, ALLOW_ORDERS=true, MAX_CONTRACTS_PER_ORDER=2, DAILY_ORDER_BUDGET=4000,
|          ORDER_TICKER_PREFIXES=KXBTC15M,KXBTCD,KXBTC, PROXY_PORT=8642, PROXY_HOST=0.0.0.0,
|          PROD_KEYID=<new key id>, PROD_KEYFILE=/etc/secrets/kalshi_render.pem
|     secret file: kalshi_render.pem   (a NEW Kalshi API key, never the laptop's PEM -- see 6)
|     start: python proxy.py
+-- dv3-pilot          Background Worker 1c-2g (measure; maybe 0.5c-512mb)   disk 20 GB at /var/dv3
      env: DV3_DATA_DIR=/var/dv3, DV3_PROXY_BASE=http://degeneracy-proxy-<suffix>:8642,
           PYTHON_VERSION=3.12.10, TZ=UTC
      start: python -m service.run_v32_forever      (new supervisor, see 3a)
      disk layout: /var/dv3/journals_v32  /var/dv3/logs_v32  /var/dv3/ledger  /var/dv3/ops
```

Why two services: the pilot container never holds key material -- the same separation the laptop
proxy was born for ("a key that touches a chat is burned"), now enforced by container boundary rather
than by file ACLs on a single user account. The proxy stays read-only-by-default and keeps every cap.

Alternative B (cheaper, weaker isolation): ONE background worker running the proxy on 127.0.0.1 and
the supervisor in the same container, PEM as a secret file in that container. No proxy code change,
no private network, ~$7-25/month total. Rejected as the default because the pilot process could read
`/etc/secrets`; acceptable only if Brad prefers the smaller bill and accepts that.

Cost: proxy $7 + pilot $7-25 + disks ~$5 = **$19-37/month**. Honest comparison: at the current pace
(~2 sets/day at 2 contracts, ~+22c/set true) V3.2 makes ~$13/month. Hosting costs more than the
strategy earns until size or series count grows; the case for moving is reliability and latency, not
this month's PnL.

## 3. Code changes (the Linux port -- small; nothing in `pilot/service` hard-codes Windows paths)

a. **Supervisor `service/run_v32_forever.py`** (new): loop = compute next UTC :40, sleep, spawn
   `python -m service.run_v32` as a subprocess, wait, append one line to `logs_v32/scheduler.out`,
   repeat. On boot it FIRST runs the stray-order sweep (`executor` startup sweep: cancels every resting
   `v32-*` KXBTC order by shard, skips foreign coids) so a container restart never leaves a rest on the
   venue until the next :40. SIGTERM: if idle, exit; if a window is live, forward SIGTERM to the child
   and let the child's quote-end cancel run (Render's 300 s max grace is shorter than a window, so a
   deploy mid-window still risks a leaked rest until its T-4 `expiration_time` -- hence the :02-:33
   deploy rule and the boot sweep). Unit tests: next-:40 arithmetic, sweep-on-boot, SIGTERM idle/busy.
   Replaces `ops/register_v32_task.ps1` on Render; the laptop keeps the task for the standby role.
b. **Data dir**: the four writable locations (`journals_v32`, `logs_v32`, `ledger`, `ops` incl.
   `v32_mode.txt` and the day-guard files) resolve from `DV3_DATA_DIR` when set, else today's
   `_PILOT_DIR`-relative defaults (behaviour-neutral on the laptop). Read-only inputs (`policy/
   v32_params.json`, `ceremony/v32_falsifier.md`) stay in the repo checkout. Touch points:
   `run_v32.DEFAULT_JOURNAL_DIR/DEFAULT_LOG_DIR/DEFAULT_MODE_PATH`, the two `ops_dir =` lines,
   `v32/ledger.DEFAULT_V32_LEDGER_DIR`. Tests: env override round-trips; defaults unchanged.
c. **Proxy**: `PROXY_HOST` env (default `127.0.0.1`; Render sets `0.0.0.0`), `order_budget.json`
   path from env (disk), `.env` loading optional (env vars come from Render). `/health` unchanged.
   Optional hardening: a shared-secret header (`X-DV3-Token`) the pilot sends and the proxy requires
   for non-GET, so even a same-network neighbour cannot write orders. Tests in `degeneracy-proxy/tests`.
d. **Packaging**: `requirements.txt` at the repo root (pilot: `requests==2.34.2`,
   `websockets==16.0`; proxy: `cryptography`, `python-dotenv`, `requests`), `.python-version` =
   `3.12.10`. The pilot imports sim law via `service/_simlaw.py` (adds `sim/` to `sys.path`) so the
   Render root directory must be the repo root, start command run from `pilot/`.
e. **Journal retention + sync**: the ms journals are the research corpus, not disposable. A nightly
   job (on the laptop, pulling over SSH/scp from the worker) copies new `.gz` files down; the worker
   deletes `.gz` older than N days ONLY after a manifest confirms the copy. 20 GB disk = ~40 days at
   today's rate; grow-only, so start at 20.
f. **Pilot -> proxy URL**: `run_v32 --proxy-base` exists; add `DV3_PROXY_BASE` env as its default so
   the supervisor needs no flags. `proxy_auth.DEFAULT_PROXY_BASE` likewise.
g. **Linux CI**: run `python -m pytest -q` on ubuntu (GitHub Action) so the 961-test suite is proven
   on Linux before the first Render deploy. The corpus-dependent tests need `historical-data/` (absent
   on Render) and skip/error there today -- mark them `skipif` on absence so the suite is green on both.

Estimated size: ~400 lines + tests, two PRs (pilot: a/b/d/e/f/g; proxy: c), each Opus 4.8 build +
review, Brad merges. Behaviour-neutral for the laptop until env vars are set.

## 4. Migration sequence (gates, never two armed boxes)

**Phase 0 -- harden the laptop now (Brad, free, this week).** UPS; proxy as a logon task or service;
task "run whether user is logged on or not"; Windows auto-update off / active hours. Measure the pilot
process RSS during one live window (PowerShell: sample `Get-Process python` WorkingSet64 every 30 s
from :41 to :02) to choose `0.5c-512mb` vs `1c-2g`.

**Phase 1 -- code (PRs).** Section 3. Merge. Laptop keeps running unchanged.

**Phase 2 -- Render in DRY, in parallel.** Brad creates the workspace (Ohio), both services, disk,
env vars, secret file with a **new** Kalshi key (read-only first: `ALLOW_ORDERS=false`), auto-deploy
OFF, mode file `dry`. Gates before Phase 3, all read from the Render journals/ledger:

1. `/health` from the worker shows the proxy reachable, `orders_enabled` false, caps correct.
2. Read-only Kalshi GETs succeed from Render's Ohio egress (markets, orderbook, balance) --
   confirms Kalshi accepts datacenter IPs; rate limits are per key, not per IP.
3. >= 48 h of dry windows: `would_place_rest` + shadow records every armed hour, zero discovery
   errors, data-age p99 and replace RTT visibly below the laptop's (log both side by side).
4. Ledger/journal/ops writes land on the disk and survive a manual restart.
5. Boot sweep verified: restart the worker mid-window in DRY and confirm `startup_cancel_sweep`
   runs at boot (found 0 in dry).
6. Linux suite green in the Render shell (`cd pilot && python -m pytest -q`).

**Phase 3 -- cutover (one :02-:33 window).** Laptop `v32_mode.txt` -> `dry`, confirm its :40 run
writes a dry row; Render proxy `ALLOW_ORDERS=true` + restart; Render `v32_mode.txt` -> `armed`. First
Render armed window gets the MUST CONFIRM treatment (V32_ARMING.md section D) and a hand reconciliation
of its first set against `/portfolio/fills`. Falsifier: the roster, params sha and pins do not change,
so no amendment -- but append a dated **MECHANICS note** under Registration (Brad's words) recording
the host move and the before/after execution gap, so the pooled n stays honest about what changed.

**Phase 4 -- after.** Laptop = research box + cold standby (task disabled, mode `dry`); nightly journal
sync down; revoke the laptop's Kalshi key once Render has run a clean week.

## 5. Risks and open questions

- **Mid-window kill** (deploy, platform maintenance, crash): the rest can stay on the venue until its
  T-4 expiry; a fill in that gap is unhedged (the 09-17 class). Mitigations: boot sweep (3a),
  :02-:33 deploy rule, T-4 expiry, reconcile-first refusing to arm on positions. Render platform
  maintenance restarts are not on our schedule -- read the maintenance docs and set the shutdown grace
  to 300 s; accept the residual.
- **Kalshi and datacenter egress**: verify with read-only GETs (Phase 2 gate 2). Shared Render IPs mean
  other tenants' behaviour could in theory taint the range; dedicated IPs are the $100/month answer if
  it ever bites.
- **Faster RTT -> more replaces/min** -> the 60/min alarm stands hours down more often. Brad's
  params lever (hysteresis / replace budget); proposal with numbers on request.
- **Disk is single-instance and grow-only**; snapshots cover 7 days; the laptop sync is the real
  backup of the corpus.
- **Time**: containers run UTC -- the DST hack in the task script disappears.
- **Cost vs edge** (section 2): hosting exceeds current PnL at 2 contracts.

## 6. Brad's levers (nothing here is Claude's to pull)

Workspace and services creation; region; plan sizes; the new Kalshi API key and its Secret File;
`ALLOW_ORDERS`; every env var; `v32_mode.txt` on both boxes; auto-deploy off; the deploy button;
revoking the old key; the Registration note wording.

## Sources (Render docs, read 2026-09-21)

cronjobs, disks, background-workers, private-services, private-network, configure-environment-variables,
outbound-ip-addresses, dedicated-ips, deploys, python-version, compute-plans, new-workspace-plans, ssh,
regions -- all under https://render.com/docs/. Kalshi engine region: docs.kalshi.com/fix/connectivity
and third-party latency write-ups (Chicago -> us-east-2 ~10 ms; the REST host is a CloudFront edge).
