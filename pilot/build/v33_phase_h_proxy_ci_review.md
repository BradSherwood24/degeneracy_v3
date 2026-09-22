# Review — PR #84 "V3.3 Phase H-B: proxy env patch doc, requirements, .python-version, Linux CI"

Reviewer: Opus 4.8. Branch `feat/v33-phase-h-proxy-ci`, head `7de3347`, base `origin/main` `b5059ee`.
Review worktree: `dv3_wt_fix` on `review/v33-phase-h-proxy-ci`. Date 2026-09-22.

## VERDICT: APPROVE WITH NITS

No blocking issues. Behaviour with no env var set is identical to today on both the proxy and the pilot,
verified empirically — safe to merge into the live (armed size-2) tree. Five NITs + one QUESTION below,
all cosmetic or out-of-scope-for-Render.

---

## Receipts (measured, not trusted)

- **Pilot suite in this worktree**: `cd pilot && python -m pytest -q -rs` -> **911 passed, 9 skipped**
  in 11.75s. `python -m compileall pilot` -> exit 0. (This worktree has NO corpus, so it reproduces a
  near-CI condition; `test_orders_proxy_compat` still ran because `_PROXY` resolves to the live
  `Python_stuff/degeneracy-proxy/proxy.py`, which exists. In a true clean clone that also skips, giving
  the builder's 900 passed / 10 skipped.)
- **Pinned versions match installed**: `requests 2.34.2`, `websockets 16.0`, `pytest 9.1.1` — exact.
- **proxy_writer unset-token is byte-identical** (empirical): `requests.Request(...).prepare()` headers
  with `headers={}` == with no `headers` arg (`True`); no `X-DV3-Token` present in the empty case. So the
  `headers=_dv3_token_headers()` addition is a true no-op when `DV3_PROXY_TOKEN` is unset. No empty
  header is sent.
- **Proxy patch applies + passes** in a scratchpad copy of exactly `proxy.py`, `README.md`, `run.ps1`,
  `tests/{conftest,test_proxy,test_review_probes}.py` (no `.env`, no `*.pem`, no logs, no budget file
  copied):
  - `git apply --check` OK for all four Phase-H diffs (proxy.py, README.md, tests/test_proxy.py,
    tests/test_review_probes.py).
  - Full copy suite after apply: **132 passed** (pristine 111 = 86 `test_proxy` + 25
    `test_review_probes`; `test_phase_h.py` adds 21). Token 401 without/with-wrong header, GET open,
    right-header reaches caps, valid create forwards with `X-DV3-Token` **stripped upstream**, `/health`
    never leaks the token, bind defaults 127.0.0.1, `.env` optional, `PROXY_BUDGET_PATH` -> `OrderBudget._path`
    — all asserted and green.
- **Composition with `proxy_amend_cap.md`** confirmed both orders — see QUESTION 1.
- **Live tree corpus present** (list-only, no read of `sim/out/sealed_eval/**`):
  `degeneracy_v3/sim/out/census_train.csv` and `degeneracy_v3/historical-data/` both exist, so every
  corpus-guarded test RUNS on the box — the guards are no-ops there and the merge hides nothing.

---

## Baseline puzzle — answered

The differing baselines are entirely explained by the **gitignored corpora being per-working-directory**
(`historical-data/` and `sim/out/` are untracked, so each worktree has its own or none):

- The tree that reported **"961 passed on main"** had BOTH corpora present.
- The builder's worktree (`dv3_wt_amend`) reported **"957 passed, 2 skipped, 2 errors" (961 collected)**:
  it had `sim/out/census_train.csv` present (census tests ran) but `historical-data/` ABSENT — the 2
  errors were `test_quintile::test_quintile_reproduction_exact` and
  `::test_head_of_corpus_insufficient_tape_is_noquintile` raising `FileNotFoundError`.
- After the PR: 964 passed / 4 skipped / 0 errors = +7 new token tests passed, the 2 quintile errors
  converted to clean skips (+2 skipped), 0 errors.

**Does the answer matter for the live tree? No.** The live tree has both corpora, so the guards never
trigger there; a merge + pull runs all previously-running tests plus the 7 new token tests, and the
2 converted-to-skip quintile tests still RUN on the box (corpus present). The guards only skip when a
corpus is genuinely absent (a fresh CI checkout), so nothing is hidden on the box.

---

## Findings

### QUESTION 1 — `proxy_amend_cap.md` is prose, not a literal diff; composition verified structurally
`pilot/ops/proxy_amend_cap.md` describes its edits as prose + code snippets at approximate line numbers
(`~L168`, `~L464-478`, `~L483-517`), not a unified `--- a/ +++ b/` diff, so it cannot be `git apply`-ed;
Brad hand-applies it (the doc already says so). I confirmed the two proxy changes **compose in either
order**:
- **Phase H first, then amend_cap (hand-applied on top)**: `import proxy` OK; all 21 Phase-H tests +
  token gate stay green. The only 2 failures are `test_review_probes::test_amend_post_is_blocked` and
  `::test_events_amend_post_is_blocked_uncapped_write` — these are amend_cap's OWN pre-existing "amends
  are blocked" tests flipping because amend_cap intentionally makes amends capped-not-blocked (the
  amend_cap doc itself says to add mirror tests). Not a Phase-H regression.
- **amend_cap first, then `git apply` Phase H `proxy.py` diff**: applies cleanly ("PHASE H proxy.py
  APPLIES CLEANLY ON TOP OF AMEND_CAP"); `import proxy` OK; both `is_order_amend` and `proxy_token`
  present.
Regions are disjoint in the patched file: the token gate sits in `_handle` right after the
`/trade-api/` 404 guard (patched L470–486), before the read-only check (L489); amend_cap touches
`is_order_create`'s neighbourhood (L177), the non-create refusal block (L505–517), and after the
create-caps block (L527+). The patch doc's ordering claim is correct. No action required.

### NIT 1 — CI runs on every push of every branch (noise/minutes)
`.github/workflows/pilot-tests.yml:9-13` — `on: push: branches: ["**"]` plus `pull_request` plus
`workflow_dispatch`. Every push of every feature branch triggers a run in addition to the PR run
(duplicate work, burns Actions minutes). Recommend `push: branches: [main]` (post-merge signal only) +
`pull_request` (pre-merge signal). `permissions: contents: read` is correctly present; no secret is used
and nothing touches the network beyond `pip`.

### NIT 2 — Workflow has no `concurrency` and no `timeout-minutes`
`.github/workflows/pilot-tests.yml` — a superseded push isn't auto-cancelled and a hung job isn't
bounded. Recommend adding:
```yaml
concurrency:
  group: pilot-tests-${{ github.ref }}
  cancel-in-progress: true
```
and `timeout-minutes: 15` on the job.

### NIT 3 — Token comparison is not constant-time
`pilot/ops/proxy_phase_h.md` proxy.py diff (patched `proxy.py:479`):
`if self.headers.get("X-DV3-Token") != CONFIG.proxy_token:` uses `!=`, not `hmac.compare_digest`. Not
blocking on a private network (Render Private Service, no public URL), but a constant-time compare is
cheap defence-in-depth against a timing oracle. NIT only.

### NIT 4 — The box path (`executor.py`) does NOT attach `X-DV3-Token`
`pilot/service/executor.py:95` — the wide-box runner's own `_default_post` posts without the token
header (it bypasses `ProxyWriter`). Out of scope: V3.2 (`run_v32.py` -> `ProxyWriter`) is the Render
migration target and IS covered (create/batch-create/amend via `rest_post`, cancel via `rest_delete`,
all -> `_default_post`/`_default_delete` -> token). But if the box runner (`run_window.py`) is ever
pointed at a token-gated proxy, its creates/cancels would silently 401. Noting per the task; no fix
needed for this PR.

### NIT 5 — Report/doc count labels are off (totals reproduce)
`pilot/build/v33_phase_h_proxy_ci_build_report.md:14` and `proxy_phase_h.md:636` say "101 existing + 31
Phase H = 132". Measured: pristine proxy copy is **111** existing (86 + 25) and `test_phase_h.py` adds
**21**, total **132**. The headline **132 reproduces exactly**; only the split (101/31) is mislabelled.
Also build report line 12 "corpora present" is imprecise for the builder's worktree (only `sim/out` was
present; `historical-data/` was absent — that is what produced the 2 quintile errors). Cosmetic.

---

## Checklist confirmations (no finding)

- **Every non-GET pilot write carries the token; no GET does.** V3.2: create `writer.rest_post`
  (`executor.py:394`), amend `writer.rest_post(amend_path...)` (`887`), cancel `writer.rest_delete`
  (`611/713/761`) — all route through `_default_post`/`_default_delete` -> `_dv3_token_headers()`. GETs
  (`rest_get`, and `ProxyAuth.ws_connect_params` which is a **GET** to `/ws-auth`) add no token; the WS
  client (`ws_client.py`) uses `ws_connect_params()` so it is unaffected by the non-GET gate.
- **`/ws-auth` is GET -> stays open** under the proxy gate; the "should the WS auth be gated" concern is
  moot (it is not a POST).
- **requirements.txt scope is correct.** `numpy`/`plotly` appear only in `pilot/build/mc/{v32_mc,v32_mc_html}.py`
  (study scripts), which pytest does not collect (`pytest.ini` `testpaths = tests`) and which are not
  runtime for the service; `python -m compileall pilot` only byte-compiles them (no import), so CI does
  not need them. `dotenv` is the proxy's dep (referenced only in a `test_orders_proxy_compat.py`
  docstring, not imported), correctly excluded. `tools/fetch_history.py` uses `requests` (covered);
  `sim/*` and `pilot/service/*` add nothing beyond requests+websockets.
- **Corpus guards are precise** — each condition is exactly "corpus absent"
  (`not os.path.exists(DEFAULT_CENSUS_CSV)`, `not os.path.isdir(_HISTORICAL_DATA)`,
  `not os.path.exists(_PROXY)`); no skip hides a logic failure (the converted quintile errors are genuine
  can't-run-without-data cases).
- **House law respected in the compat test**: `test_orders_proxy_compat._load_proxy_parser` reads the
  live `proxy.py` read-only and execs ONLY named pure defs (`_WANT` excludes `Config`/`Signer`/
  `OrderBudget`/handler), so no `.env`/`*.pem`/`CONFIG = Config()` runs. The PR only ADDS a skip guard;
  behaviour unchanged.
- **`run.ps1` needs no change** — it is `python proxy.py`; host/budget/token all default to today's
  behaviour on the laptop, and Render sets env vars around `python proxy.py` in a container.
- **No live proxy file was modified or key material read** — testing used a scratchpad copy of the four
  allowed files only.
