# V3.3 fractional + async-writer integration build report

Branch `fix/v33-fill-attribution` (PR #106). Merge of `origin/main` (PR #105 async order writer,
`da9859c`) into the fractional fill-attribution branch (`8e759de`), followed by the port of the
fractional fixes into the off-loop async executor twin. FAKES/TESTS ONLY — no network, no proxy, no
key, no PEM, no holdout, no mode/params/falsifier-STATUS change.

- Merge commit: `e0f8a7e` (`Merge: 8e759de da9859c`, a true merge commit — NOT a rebase; both
  histories stay reviewable).
- Pre-merge main live-tree baseline: 1409 passed / 1 skipped. Post-merge here: **1426 passed / 5
  skipped**. After the async port + new tests: **1432 passed / 5 skipped** (`cd pilot; python -m
  pytest -q`).

---

## 1. Conflict hunks and how each was resolved

Four files conflicted; `service/v33/executor.py` auto-merged. Every resolution is append-only
(ceremony) or additive (both sides' fields kept) — no side was dropped.

### 1a. `pilot/ceremony/v33_falsifier.md` (Registration, append-only)
- **HEAD (#106):** the `2026-09-30 ... CORRECTION to the 21:50:55Z entry` line (per-coid count_fp
  pairing reversed: -312=1.00, -313=0.44).
- **origin/main (#105):** the `2026-09-30 -- MECHANICS CLARIFICATION (async order writer)` entry.
- **Resolution:** kept BOTH, ordered per the task: the incident entry (already above the conflict,
  untouched) → MECHANICS CLARIFICATION (origin/main) → CORRECTION (HEAD). Nothing above
  `## Registration`, no `[pin]`, no STATUS line, no params sha touched. `git diff` on the file shows
  only the reordered append.

### 1b. `pilot/service/run_v33.py` (one hunk, ledger-row kwargs)
- **HEAD:** `netted_sets=m.get("netted_sets")` (D5 venue-netted wing pairs).
- **origin/main:** `writer_stats=_v33_writer_stats(driver)` (off-loop writer stats).
- **Resolution:** kept BOTH kwargs on the `make_v33_ledger_row(...)` call. Both are accepted params
  on the row builder (see 1d). The rest of `run_v33.py` auto-merged: `_pump` branches to
  `_pump_async` when async is enabled (`self._async`), and #106's fill handlers (`on_fill`,
  `on_poll_fill`, `_simulate_ladder_fills`, `_count_out`/`_count_dec`, `V33Fill` import) survived
  outside the conflict. **Composition verified:** `_pump_async` filters order-bearing actions by
  `_ORDER_ACTION_KINDS`, which does NOT include `WING_NETTED`, so D5's "informational, no venue
  order" invariant holds on the async path exactly as on the sync path; V33Fill events route through
  the same `_pump` → `_pump_async` → `decide_v33` (V33Fill is not an order action, handled on-loop).

### 1c. `pilot/service/v33/ledger.py` (one hunk, make-row params)
- **HEAD:** `netted_sets: list[Any] | None = None`.
- **origin/main:** `writer_stats: dict[str, Any] | None = None`.
- **Resolution:** kept BOTH params on the row builder signature.

### 1d. `pilot/service/v33/report.py` (one hunk, window fields)
- **HEAD:** `bucket_mismatch` (F3), `netted_sets`, `netted_realised` (D5).
- **origin/main:** `writer` (`r.get("writer_stats") or {}`).
- **Resolution:** kept BOTH sets of per-window fields.

### 1e. `pilot/service/v33/executor.py` (AUTO-MERGED, no conflict — verified)
Both sides present and non-overlapping:
- #105: `WriteTokenBucket.acquire_async` (L202).
- #106: `_fractional_counts = True` (L252), `_count_body_str` (L353), `_dc` (L348), the Decimal
  wing-send chunking in `_take_wings`/`_take_bucket_no`/`_unwind_wings`, `poll_orders_for_bucket`
  returning `dict[str, Decimal]`.

---

## 2. Async-twin fractional port (`service/v33/async_executor.py`)

`V33AsyncExecutor(V33LiveExecutor)` INHERITS the fractional helpers (`_dc`, `_count_body_str`,
`_finish_cancel`, `_resolve_cancel_from_status`, `_fractional_counts=True`, `_wing_filled`,
`_wing_notional`, `_last_confirm_status_fp`). The port touches only the methods the async executor
OVERRIDES. #105 wrote those overrides as twins of the pre-#106 (int) sync methods, so they carried
the int truncation the incident exposed.

### F1 — wing SEND (Decimal end-to-end, mirrors sync)
- **`_take_wings_send`** (the chunking body of `_take_wings_async`): `remaining = self._dc(lg.count)
  - self._wing_filled.get(key, Decimal(0))`; `cnt = min(Decimal(self.wing_cap), remaining - c*cap)`;
  added `_sum_chunks` + `assert _sum_chunks == remaining`; `agg_count` defaultdict → `Decimal(0)`;
  `fc = self._dc(nr.fill_count)`; `got = agg_count.get(key, Decimal(0))`; `total >=
  self._dc(lg.count)`; emit `count=total` (was `Decimal(total)`), partial `count=Decimal(0)`. The
  wire count rides the inherited `_wing_chunk_entry` → `_count_body_str` (whole lots still serialise
  "2.00"-identical; a 1.44 fill now serialises "1.44").
- **`_take_bucket_no_async`** (print-through COMPLETE, dormant): `want = self._dc(...)`; chunk-sum
  assert; `fc = self._dc(...)`; `got = Decimal(0)`; journal/alarm counts `str(...)`; return
  `count=got`.
- **`_unwind_wings_async`** (print-through UNWIND, dormant): `lg_count = self._dc(lg.count)`;
  `want = Decimal(0)`; chunk-sum assert; `sold += self._dc(r.fill_count)`; `str(...)` journal/alarm.
- **place / amend — NOT converted (deliberate, twin-faithful).** The sync base `_place_rest` /
  `_rest_body` and `_amend_rest` / `_amend_body` STILL use `int(action.count)` /
  `int(parsed.fill_count)` post-#106 (rungs are whole lots; placements/amends never fractional). The
  async `_place_rest_async` reuses the inherited `_rest_body` and matches `count=int(action.count)`;
  `_amend_rest_async` matches `int(action.count)` / `int(parsed.fill_count)`. Converting these would
  DIVERGE the twin from its sync original. **Reviewer note:** if the amend/parse fill should ever be
  fractional, that is a sync-side fix that must land in the base `_amend_rest` first; the async twin
  will mirror it.

### F2 — fill DISCOVERY (Decimal, mirrors sync)
- **`_confirm_cancel_filled_async`**: added `self._last_confirm_status_fp = Decimal(0)` at entry and
  `self._last_confirm_status_fp = st.filled_count_fp` inside `if st.available` (mirrors base
  `_confirm_cancel_filled`).
- **`_resolve_cancel_success_async`**: added the `filled_fp` block (`fp_delete = max(0, Decimal(rec.count)
  - rb)`; `filled_fp = max(fp_delete, self._last_confirm_status_fp)`) and passes
  `filled_fp=filled_fp` to the inherited `_finish_cancel`. A 0.44 cancel-confirm fill now delivers
  `OrderCancelled(filled_count_before_cancel=Decimal("0.44"))`.
- **`poll_orders_for_bucket_async`**: return type `dict[str, Decimal]`; `out[str(oid)] =
  Decimal(str(fc))` (was `int(Decimal(str(fc)))` which truncated 0.44 → 0); fallbacks `Decimal(0)`.
- The non-2xx cancel path (`_cancel_nonok_async`) resolves via the INHERITED
  `_resolve_cancel_from_status`, which already carries `filled_fp = st.filled_count_fp` under
  `_fractional_counts` — no override needed.

### Remaining `int(...)` in `async_executor.py` — all legitimate (audited post-port)
`int(exch)` (shard index, L284/L938); `int(rec.count) - int(rb)` (the INT `filled` path in
`_resolve_cancel_success_async`, kept exactly as base — the fractional path is the separate `filled_fp`
block); `int(action.count)` place/amend RestRecord + `int(parsed.fill_count)` amend fill (whole-lot,
match sync).

### Wing retry-storm belt composes with the core 250 ms floor (verified)
Two orthogonal gates: the core `WING_RETRY_MIN_INTERVAL_MS = 250` (D4, core.py — time-based per leg)
decides WHEN a RETRY_WING is emitted; the transport belt (`_take_wings_async`, `_wing_inflight_legs`,
`wing_retries_dropped`) drops a RETRY_WING only while that leg's IOC is in flight and RELEASES in the
`finally` when the take returns. A leg is never un-retried to the cutoff: once the in-flight take
completes (belt cleared), the core's next emission (≥250 ms later) re-claims it. Neither gate was
touched by the merge (belt in #105 async_executor, floor in #106 core.py — core.py was not a conflict
file).

---

## 3. Sync/async twin diff (post-port)

Automated normalized diff (strip `async`/`await`/`_async`, map `_apost`/`_adelete`/`_aget` →
`writer.rest_post/delete/get`, `acquire_async`→`acquire`, `asyncio.sleep`→`self.sleep`) of each
ported method against its sync original. Residual differences, by method:

| async method | sync twin | residual diff after normalization |
|---|---|---|
| `_take_wings_send` | `V33LiveExecutor._take_wings` | docstring; wrapper split (`pending` computed in `_take_wings_async` belt); `chunk_coid = ...` vs `chunk_coid, = (...,)`; `defaultdict` import style; comment "(F1 port)". **No count/Decimal line differs.** |
| `poll_orders_for_bucket_async` | `V33LiveExecutor.poll_orders_for_bucket` | docstring; logger tag. **No count line differs.** |
| `_take_bucket_no_async` | `V33LiveExecutor._take_bucket_no` | docstring; a wrapped-line reflow; inline import placement. **No count line differs.** |
| `_unwind_wings_async` | `V33LiveExecutor._unwind_wings` | docstring; comment "(F1 port)". **No count line differs.** |
| `_resolve_cancel_success_async` | `LiveExecutor._resolve_cancel_success` | docstring. The `filled_fp` block is byte-identical after normalization. |
| `_confirm_cancel_filled_async` | `LiveExecutor._confirm_cancel_filled` | docstring. The `_last_confirm_status_fp` lines are byte-identical. |

Conclusion: only mechanical substitutions, docstrings/comments, logger labels, and the async-only
wrapper split (`_take_wings` → `_take_wings_async` [retry belt] + `_take_wings_send` [chunking])
differ. The decision/count logic is identical to the sync source of truth.

---

## 4. Tests

- New file **`pilot/tests/test_v33_async_fractional.py`** (6 tests, async twins of the sync
  fractional tests; FracAsyncProxy keeps the fractional wire count, no network):
  1. async wing wire count is "1.44" not int-truncated "1.00";
  2. a 1.44 fill is fully hedged 1.44 per wing through the async path;
  3. multi-chunk [2.00, 1.44] for a 3.44 fill (last chunk fractional, Σ = 3.44);
  4. cancel-confirm (2xx DELETE reduced_by 0.56 + status-truth GET fp 0.44) delivers
     `OrderCancelled(filled_count_before_cancel=Decimal("0.44"))` through the async executor;
  5. `poll_orders_for_bucket_async` returns `{oid: Decimal("0.44")}`, not int 0;
  6. `_take_bucket_no_async` chunks a 3.44 want to [2.00, 1.44] and books `count=Decimal("3.44")`.
  These are genuine guards: pre-port they fail (int(1.44)→"1.00"; cancel delivers Decimal(0); poll
  returns 0).
- Both branches' suites pass unchanged: `test_v33_async_writer.py`, `test_v33_fill_attribution.py`,
  `test_v33_fill_discovery_fractional.py`, `test_v33_wing_fractional_exec.py`,
  `test_v33_wing_netting.py` (45 together), plus the v32 suites.
- Full suite: **1432 passed / 5 skipped**.

---

## 5. Reviewer checklist (scrutinise)

1. **Falsifier append order & scope** — confirm nothing above `## Registration` moved; both the
   async MECHANICS CLARIFICATION and the count_fp CORRECTION are present; no `[pin]`/STATUS/params-sha
   change.
2. **place/amend stay int by design** — confirm the async `_place_rest_async`/`_amend_rest_async`
   match the sync (int) rather than being "fixed" to Decimal; this is twin-fidelity, not a miss. If
   the amend fill SHOULD be fractional, that is a sync-side base change first.
3. **cancel-confirm fractional max** — `filled_fp = max(fp_delete, _last_confirm_status_fp)`:
   confirm `_last_confirm_status_fp` is always set before read (set at the top of
   `_confirm_cancel_filled_async`, which `_resolve_cancel_success_async` calls before computing
   `filled_fp`; also initialised in base `__init__`).
4. **chunk-sum asserts** — the three `assert Σ chunks == want/remaining/leg` are on Decimal
   arithmetic; confirm no quantize/rounding can trip them for realistic fractional fills (0.44, 1.44,
   3.44 covered; the wire body quantizes to 2dp via `_count_body_str`).
5. **belt × 250 ms floor** — confirm the belt releases in `finally` and the core floor is independent,
   so no leg is starved to the cutoff.
6. **`_pump_async` / WING_NETTED** — confirm `WING_NETTED` is excluded from `_ORDER_ACTION_KINDS` so
   D5 informational actions never hit the venue on the async path.
7. **async writer still DORMANT** — `--async-writer` / `DV3_V33_ASYNC_WRITER` default OFF; the
   synchronous `V33LiveExecutor` remains the live path. The pinned params sha is untouched.
