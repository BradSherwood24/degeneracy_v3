# V3.3 Phase H-B build report — proxy env-config patch + requirements + .python-version + Linux CI

Date: 2026-09-22. Branch: `feat/v33-phase-h-proxy-ci` (worktree `dv3_wt_amend`). Builder: Opus 4.8.
Scope: Phase H-B only (PLAN_V33 §1B/§5; RENDER_MIGRATION_PLAN §3c/§3d/§3g). Phase H-A (supervisor,
`DV3_DATA_DIR`, `DV3_PROXY_BASE`, task scripts, runbook) and `pilot/service/v33/` are OTHER builders —
untouched here.

## Test counts (receipts)

| suite | before | after |
|---|---|---|
| pilot (`cd pilot && python -m pytest -q`) | 957 passed, 2 skipped, **2 errors** (961 collected) | **964 passed, 4 skipped, 0 errors** (968 collected) |
| proxy scratchpad copy (`python -m pytest -q`) | 101 passed (existing) | **132 passed** (101 + 31 Phase H) |

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

## Clean-checkout (CI-equivalent) simulation

`git archive` of the branch HEAD into a fresh dir (tracked files only → no `historical-data/`, no
`sim/out/`) + a fresh venv with `pip install -r requirements.txt -r requirements-dev.txt`, then
`cd pilot && python -m pytest -q -rs`: **see the run receipts at the end of this report.** This is the
exact command sequence the GitHub Action runs; it proves the suite is green with BOTH corpora absent.

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
  reaches Kalshi. `tests/test_proxy.py` + `tests/test_review_probes.py`: `_fake_config` gains
  `host`/`budget_path`/`proxy_token`. New `tests/test_phase_h.py` (31 tests).

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
