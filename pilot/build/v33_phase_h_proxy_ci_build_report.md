# V3.3 Phase H-B build report — proxy env-config patch + requirements + .python-version + Linux CI

Date: 2026-09-22. Branch: `feat/v33-phase-h-proxy-ci` (worktree `dv3_wt_amend`). Builder: Opus 4.8.
Scope: Phase H-B only (PLAN_V33 §1B/§5; RENDER_MIGRATION_PLAN §3c/§3d/§3g). Phase H-A (supervisor,
`DV3_DATA_DIR`, `DV3_PROXY_BASE`, task scripts, runbook) and `pilot/service/v33/` are OTHER builders —
untouched here.

## Test counts (receipts)

| suite | before | after |
|---|---|---|
| pilot on the BOX (`cd pilot && python -m pytest -q`; builder worktree had `sim/out/` present but `historical-data/` ABSENT) | 957 passed, 2 skipped, **2 errors** (961 collected) | **964 passed, 4 skipped, 0 errors** (968 collected) |
| pilot on a CLEAN CLONE (CI-equivalent, both corpora + proxy tree absent) | would ERROR (5 collection errors + 2 quintile) | **900 passed, 10 skipped, 0 errors** |
| proxy scratchpad copy (`python -m pytest -q`) | 111 passed (existing = 86 `test_proxy` + 25 `test_review_probes`) | **133 passed** (111 existing + 22 `test_phase_h`, incl. the wrong-length-token case) |

- The 2 pre-existing pilot **errors** were `test_quintile.py::test_quintile_reproduction_exact` and
  `::test_head_of_corpus_insufficient_tape_is_noquintile`, which raised `FileNotFoundError` on the
  gitignored `historical-data/` corpus (absent in the worktree, absent in CI). They are now clean
  **skips** via fixture guards — the exact CI-green requirement.
- +7 new pilot tests (`test_proxy_writer_token.py`, the client-side `X-DV3-Token` change).
- Worktree run skips 4: `test_box_golden.py` ×2 (already guarded) + `test_quintile.py` ×2 (new guard,
  historical-data absent). `test_quintile.py::test_edges_match_gate_json` still PASSES in the worktree
  because `sim/out/census_train.csv` is present locally; on a fresh CI checkout `sim/out/` is also
  gitignored, so it too skips via the `edges` fixture guard (verified by clean-checkout simulation
  below).

## Clean-checkout (CI-equivalent) simulation — GREEN

A local `git clone` of the branch into a fresh dir (a REAL git repo, so the `git check-ignore` test in
`test_run_v32.py` works; corpora `historical-data/` + `sim/out/` and the separate `degeneracy-proxy/`
tree are all absent because they are gitignored/untracked — exactly the CI condition) + a fresh venv from
`pip install -r requirements.txt -r requirements-dev.txt`, then the EXACT Action steps:

```
python -m compileall pilot            -> OK
cd pilot && python -m pytest -q -rs   -> 900 passed, 10 skipped, 0 failed, 0 errors
```

The 10 skips (all with reason strings): 5 census-import modules (`reference_impl_review.py`,
`test_parity.py`, `test_review_probes2.py`, `test_shakedown.py`, plus `test_quintile.py`'s edges test),
`test_orders_proxy_compat.py` (proxy source absent), `test_box_golden.py` ×2 and `test_quintile.py` ×2
(historical-data absent). Note: an earlier `git archive` attempt (no `.git`) falsely failed only the
`git check-ignore` mode-file test — that test needs a git repo, which `actions/checkout` provides, so the
`git clone` run above is the faithful CI simulation (and `git check-ignore pilot/ops/v32_mode.txt`
returns 0 in the real repo).

## What changed — files

Repo (in git, merges normally; Brad merges the PR):
- `requirements.txt` (new, repo root) — `requests==2.34.2`, `websockets==16.0` (the only third-party
  runtime imports across `pilot/service/*` and `tools/*`; `sim/*` has none). Proxy deps are NOT here
  (separate service — see the patch doc).
- `requirements-dev.txt` (new, repo root) — `-r requirements.txt` + `pytest==9.1.1`.
- `.python-version` (new, repo root) — `3.12.10` (matches local `python --version`).
- `.github/workflows/pilot-tests.yml` (new) — ubuntu-latest; `actions/setup-python@v5` with
  `python-version-file: .python-version`; install both requirements files; `python -m compileall pilot`;
  `cd pilot && python -m pytest -q -rs` (the `-rs` prints the skip list + count).
- `pilot/tests/test_quintile.py` — fixture guards: `market_inputs` skips on absent `historical-data/`,
  `edges` skips on absent `sim/out/census_train.csv`. The 3 pure `test_live_pairing_*` tests use neither
  fixture and always run. Minimal, reason strings included.
- `pilot/tests/{reference_impl_review,test_parity,test_review_probes2,test_shakedown}.py` — module-level
  `pytest.skip(..., allow_module_level=True)` when `sim/out/census_train.csv` is absent (these call
  `load_ev_curve()` at IMPORT, so a fixture cannot save them — they errored at collection on the clean
  clone). No assertion or behaviour weakened.
- `pilot/tests/test_orders_proxy_compat.py` — module-level skip when `degeneracy-proxy/proxy.py` (the
  separate, non-git service tree) is absent, e.g. on CI. The cross-check still runs on the box.
- `pilot/service/proxy_writer.py` — client-side shared secret: `_dv3_token_headers()` reads
  `DV3_PROXY_TOKEN` and returns `{"X-DV3-Token": <tok>}` or `{}`; attached to `_default_post` and
  `_default_delete` (WRITES ONLY — GETs stay unauthenticated to match the proxy). Absent env var → no
  header → identical to today. Read at call time; never logged.
- `pilot/tests/test_proxy_writer_token.py` (new) — 7 tests for the helper and the two default transports.
- `pilot/ops/proxy_phase_h.md` (new) — the proxy patch DOCUMENT (Brad's manual apply; see below).

Proxy (NOT in git; delivered only as the patch doc — Brad applies to the live tree):
- `proxy.py`: `PROXY_HOST`, `PROXY_BUDGET_PATH`, `.env` optional, `PROXY_TOKEN` (+`X-DV3-Token` non-GET
  gate returning 401), `/health` gains `host`/`budget_path`/`token_required`, `main()` binds
  `CONFIG.host`, and the upstream header filter now strips `X-DV3-Token` so the internal secret never
  reaches Kalshi. The token compare uses `hmac.compare_digest` on utf-8 bytes (constant-time; Round 2).
  `tests/test_proxy.py` + `tests/test_review_probes.py`: `_fake_config` gains
  `host`/`budget_path`/`proxy_token`. New `tests/test_phase_h.py` (22 tests).

## Proxy diff summary (full unified diffs live in `pilot/ops/proxy_phase_h.md`)

- Behaviour-neutral on the laptop: every new env var defaults to today's behaviour; `PROXY_TOKEN` unset
  makes the token gate a pure no-op (a test asserts the read-only 403 path is unchanged when unset).
- Key path is ALREADY generic (`PROD_KEYFILE`/`DEMO_KEYFILE` accept an absolute path → a Render Secret
  File works with no code change). No `KALSHI_KEY_PATH` alias added; documented instead. No key material
  is printed or returned anywhere.
- The `X-DV3-Token` upstream-strip was a genuine finding during testing (the existing filter only removed
  hop-by-hop + `KALSHI-ACCESS-*`, so it would have forwarded the internal secret to Kalshi). Fixed +
  test-locked.

## What could NOT be verified

- **The Action run itself** — GitHub Actions cannot run on this box. Mitigations: the workflow YAML was
  parsed valid (PyYAML in a scratchpad install); the exact commands were run locally on Windows AND on a
  clean `git archive` checkout with both corpora absent (the CI condition); `python -m compileall pilot`
  runs clean.
- **The live proxy is untouched** — read-only throughout. The proxy change is developed/tested against a
  scratchpad COPY (`proxy.py` + `README.md` + `run.ps1` + `tests/` only; never `.env`/`*.pem`/budget/
  logs). Applying + restarting is Brad's lever (patch doc has the steps, the :02–:33 window rule, and
  rollback).
- **Render-side `0.0.0.0` bind / Secret File path** — exercised only by config unit assertions, not a
  real datacenter deploy.

## Open questions / coordination notes

- `DV3_PROXY_BASE` default wiring for the proxy client constructor is owned by Phase H-A — NOT
  implemented here (task instruction). If H-A adds it to `ProxyAuth`/`ProxyWriter`, it composes cleanly
  with this token change (independent code paths).
- Whether to also count/gate the token on the amend path is moot: the token gate covers EVERY non-GET
  (POST create, POST amend, DELETE cancel) before any create/amend-specific handling, so it composes with
  `proxy_amend_cap.md` regardless of apply order (documented in the patch doc).
- README update for `0.0.0.0`/Secret Files/`PROXY_TOKEN` is a real `README.md` diff in the patch doc (a
  new "Hosting / env configuration (Phase H)" section), alongside the `proxy.py` module-docstring update.

## Round 2 (2026-09-22) — review PR #84 APPROVE WITH NITS

Addressed the nits from `pilot/build/v33_phase_h_proxy_ci_review.md`:

- **NIT 1 (CI triggers):** `.github/workflows/pilot-tests.yml` `on.push.branches` is now `[main]` (post-
  merge signal only) + `pull_request` (pre-merge). No more run on every push of every branch.
- **NIT 2 (concurrency + timeout):** added `concurrency: {group: pilot-tests-${{ github.ref }},
  cancel-in-progress: true}` and `timeout-minutes: 20` on the job. YAML re-validated (PyYAML).
- **NIT 3 (constant-time compare):** the proxy token check now uses `hmac.compare_digest` on utf-8 bytes
  (missing header → `b""`, rejected without raising). Regenerated the `proxy.py` diff in
  `pilot/ops/proxy_phase_h.md` (adds `import hmac`); added `test_token_wrong_length_header_401`. Proxy
  copy suite: **133 passed** (was 132).
- **NIT 5 (count labels):** corrected the split everywhere — pristine proxy copy is **111** (86
  `test_proxy` + 25 `test_review_probes`), `test_phase_h.py` adds **22**, total **133**. Also fixed the
  "corpora present" imprecision: the builder worktree had `sim/out/` present but `historical-data/` ABSENT
  (that is what produced the two quintile errors).
- **NIT 4 (box path):** added a "Known gaps" note in `proxy_phase_h.md` — `executor.py:95` `_default_post`
  does not attach `X-DV3-Token`; the V3.2 `run_v32` path (Render target) IS covered; the box path is out
  of scope and would 401 against a token-gated proxy.
- **QUESTION 1 / NITs not code-changed:** the reviewer already confirmed composition with
  `proxy_amend_cap.md` in both orders; no change needed there.

Round 2 test receipts: pilot suite on the box (corpora present) **964 passed, 4 skipped, 0 errors**
(unchanged — only the workflow, the scratchpad proxy copy, and docs changed; no pilot runtime code
touched in Round 2). Proxy scratchpad copy **133 passed**. Workflow YAML valid.
