# Phase 3 execution + safety — adversarial review (SEPARATE opus48 REVIEWER)

Reviewed 2026-08-21. Scope: `pilot/service/{executor,reconciler,ledger,stops}.py`,
`pilot/service/orders/{translate,envelope}.py` + their tests; consumed interfaces in
`signal.py`/`policy.py`/`journal.py`/`parity.py`; the signing proxy
`degeneracy-proxy/proxy.py` + tests; V2 originals (`degeneracy_v2/kalshi/order_translate.py`,
`rest.py`). Against `pilot/PLAN.md` (Phase 3, F10/F13, imbalance protocol), `commission.md`,
`falsifier.md` (A1–A4, S1–S5, I1–I4, P-gates), and the Phase-3 build report (10 confessions).

Constraints honored: I did NOT read `degeneracy-proxy/.env` or any `*.pem` (the proxy's own test
suite loads its signer via its sanctioned module import — the pilot-side compat test still uses the
AST-extraction path that never touches key material). No sealed-day (08-02..18) file read by any
code path I wrote or ran. No live network dialed. All order traffic in tests targets a fake
proxy/injected post.

## VERDICT: **APPROVE-WITH-FIXES**

The decision core is faithful to the frozen ceremony (2,300 differential scenarios, zero
divergence). The **order-create endpoint was wrong on BOTH sides** and is now fixed and
re-verified end-to-end. Three defects fixed in-tree; the remaining flags are Phase-4 wiring /
PENDING-BRAD items, none of which is a fire-wrongly path. **Arming still correctly refuses** (the
falsifier is DRAFT; policy sha holds; S5 gates hold). This approval is *for arming once the
Phase-4 harness wires the flagged items and Brad freezes the falsifier* — the code layer is sound.

Final test counts (0 failed): **pilot repo 329 passed** (was 267; +12 reimplementation, +7 Phase-3
review probes, +1 end-to-end compat, +13 concurrently-added `pilot_ledger` outside my scope, minus
renames); **proxy repo 111 passed** (was 89; +22 events-path coverage). All original 89 proxy tests
remain green.

---

## THE ENDPOINT FIX (both sides — the one file-ownership exception, exercised)

**F0 — CRITICAL — FIXED (proxy + envelope).** The live V2 order-create endpoints are
`POST /trade-api/v2/portfolio/events/orders` and `.../events/orders/batched` (verified against
docs.kalshi.com 2026-08-21 AND prod-proven in `degeneracy_v2/kalshi/rest.py`: `create_order` posts
`PORTFOLIO_URL + "/events/orders"`, and its docstring records that the `/events/orders` PATH was
correct since the 2026-06-04 migration — the 2026-07-11 incident was the HOST). The build targeted
the LEGACY `/portfolio/orders[/batched]` on both sides. Through the real API a legacy-path order
would (a) miss the proxy's order-write prefix → route to the market-data host, (b) bypass every
proxy cap, (c) 404 upstream. A live mirage: the fake-proxy tests "passed" against a path the
exchange rejects.

Fixes:
- **`degeneracy-proxy/proxy.py`**: introduced `_ORDER_WRITE_PREFIXES` (events + legacy) and
  `is_order_write_path()`; widened `_ORDER_CREATE_PATHS` to the four create paths (events single/
  batch + legacy single/batch); added `_ORDER_BATCH_PATHS` for endpoint-keyed single-vs-batch;
  updated all three usage sites — the non-create-POST refusal, the batch detection, and the
  orders-host routing — to cover both prefixes. Legacy kept so a stale caller still routes+caps
  rather than slipping through uncapped. (Side benefit: V2 cancels on `/events/orders/{id}` now
  route to the orders host too, which the old single-prefix check would have missed.)
- **`pilot/service/orders/envelope.py`**: `SINGLE_CREATE_PATH` / `BATCH_CREATE_PATH` now target the
  live events paths; docstring rewritten from "confessed divergence" to "resolved on both sides".

End-to-end re-verified: `test_orders_proxy_compat.py::test_envelope_paths_are_recognized_creates_by_the_proxy`
proves the EXACT paths the envelope emits are recognized as creates by the REAL proxy source
(AST-extracted, no key load); the proxy repo's new handler tests
(`test_events_single_create_forwards_to_orders_host_and_consumes_budget`,
`test_events_batch_create_forwards_and_consumes_entry_count`,
`test_events_create_cap_violation_blocks_before_upstream`,
`test_events_orders_key_bypass_blocked_end_to_end`, `test_events_amend_post_is_blocked_uncapped_write`)
prove events-path creates route to `external-api.kalshi.com`, get capped, consume budget per entry,
and that non-create events POSTs are refused. Proxy test coverage added: `is_order_create` params
8→17, new `is_order_write_path` param set (9), 5 handler events tests → **+22, all 111 green**.

---

## FINDINGS (severity · disposition)

### FIXED

**F1 — MEDIUM — FIXED — `FEE_IS_TOTAL=True` was the UNSAFE (less-halting) default for S1.**
`ledger._fee_of` added `average_fee_paid` once. If Kalshi's field is a per-contract *average* (the
name says "average"), that UNDER-counts fees for any `fill_count >= 2` (rung-2 size) → `realized_min`
too high → S1 LESS likely to halt — the exact direction the commission forbids ("under-counting is
the unsafe direction"). The confession's own rationale contradicted its chosen default. **Fixed:
`FEE_IS_TOTAL = False`** (multiply by `fill_count`): exact if the field is per-contract, over-count
(safe, more-halting) if it is total. Inert at `fill_count == 1` (all current 1-pair tests unchanged;
regression `test_fee_is_total_default_is_fail_closed_multiply` pins the 2-contract behavior). Live
value resolves it on the first fill; if TOTAL is confirmed, flip back.

**F2 — MEDIUM — FIXED (authorized parity edit) — bin-5 empty-deltas false "sim tells the truth".**
Phase-2 handoff F3: `parity.assign_bin` scored `BIN_BOTH_MATCH` when both fired + filled but NO
fills leg was comparable to the paired tickers (`deltas == {}`) — a certified "match" backed by zero
comparisons. **Fixed** with a minimal guard: `if not deltas` → new `LABEL_UNCOMPARABLE`
(neutrality preserved; a data-fault marker, never bin-5). The builder's `ledger.to_window_fills`
keys legs to the actual tickers so this fires only on a genuine schema/pairing fault (belt +
suspenders). The Phase-2 FLAGGED marker test was updated to assert the FIXED behavior
(`test_F3_empty_deltas_no_longer_scored_as_match`, which also proves a genuine pair still certifies
bin-5).

### FLAGGED (Phase-4 wiring / PENDING-BRAD — not fixed here)

**F3 — MEDIUM/HIGH — S4 daily-loss AND A4 guard-trips reset EVERY WINDOW.** Both counters live on
a per-process `StopState`, and the architecture is process-per-window. So the falsifier's
"daily realized-loss cap $5.00 (S4)" and "≥5 guard trips in one UTC day (A4)" are enforced
**per window, not per UTC day** — the UNSAFE direction (each hour forgives prior losses/trips). The
builder's `test_s4_trips_at_daily_cap` passes only because it accumulates within one `StopState`,
masking the gap. Phase 4 MUST persist a UTC-day tally (mirror the proxy's `OrderBudget`, which does
key by UTC date). Markers added: `test_MARKER_s4_daily_loss_resets_with_a_fresh_state_per_window`,
`test_MARKER_a4_guard_trips_reset_with_a_fresh_state_per_window`. (Deeper: realized P&L only lands at
settlement, often after the originating window's process exits — this ties into DEFERRED F4.)

**F4 — MEDIUM — stop-authorized flatten bypasses the no-orders-to-settle cutoff.**
`build_flatten_intent` leaves `t_minus_s=None` and `StopController.trip` calls `execute(intent,
stop_authorized=True)` without a `t_minus_s`, so the executor's `t_minus_s < 1s` refusal is skipped:
a stop-flatten can POST inside the 1s settle cutoff (I3 says positions ride to settlement past 1s).
Whether a *stop* flatten should be exempt from I3 is exactly PENDING-BRAD F8. Phase 4 should pass an
event-derived `t_minus_s` (so the cutoff at least applies) or Brad must rule the exemption explicit.
Marker: `test_MARKER_stop_flatten_dispatches_inside_settle_cutoff`.

**F5 — MEDIUM — S3 positions poll can race the order path → spurious S3.** The Reconciler's `tick`
takes no executor mutex; a poll landing between the exchange accepting a fill and the executor
recording the response would see a position the ledger lacks → S3 (safe-but-costly false halt).
Mitigation the harness MUST apply: gate the S3 diff on `not state.inflight_cids()` (skip/defer while
any intent is in flight — single order authority makes that window small and well-defined).

**F6 — LOW/MEDIUM — reconciler `SellDown`/`RetryBuy` proposals carry no `reduce_only`.** Confession
5 claims "reduce_only=True on all sell-down / flatten intents", but only `build_flatten_intent`
(stops) sets it; the Reconciler→Intent conversion is Phase-4 and unbuilt. The rebalance-sell path's
reduce_only guarantee therefore rests on the harness. Bounded (`sell_count = over_net − min ≤ held`,
so it cannot oversell mathematically), hence LOW-MEDIUM — but the harness MUST set `reduce_only=True`
on rebalance sells, and the proposals should carry the field so the guarantee is structural.

**F7 — LOW/MEDIUM — no interlock binds `executor.armed` to the latched `StopState`.** A trip calls
`set_armed(False)`, but nothing prevents a later `set_armed(True)` (arming flow) from re-arming a
stopped executor within a window — only convention does. Recommend the StopController be the sole arm
authority and refuse to re-arm once any stop has latched.

**F8 — LOW — client rate-budget can refuse a stop-authorized flatten.** The token budget check
(F10) runs for `stop_authorized` flattens too; an exhausted window budget would refuse a risk-
reducing flatten. Flattens should bypass the CLIENT token budget (as they bypass the arm), leaving
the proxy's daily budget as the real cap.

**F9 — LOW — `arming_check` verifies caps PRESENCE, not value-agreement.** S5 confirms the proxy
`/health` caps block exists + `orders_enabled`, but not that `max_contracts_per_order`/prefixes/
budget MATCH the executor's caps (defense-in-depth intent, PLAN item 6). A looser proxy would still
arm. LOW because the executor's own caps bind independently; recommend a value cross-check anyway.

**F10 — LOW/informational — A1 slippage is near-silent as built.** `check_slippage_alarms` compares
`avg_fill` to the intent's own IOC limit (the observed ask). For a marketable IOC a buy fills ≤ limit
/ a sell ≥ limit, so this is a price-*improvement* magnitude that structurally can rarely exceed 2¢;
adverse slippage shows up instead as non-fill (bin-3). The meaningful "slippage" (falsifier A1 →
bin-4, live-vs-sim) is computed only in the paired report. The builder documents this; ruled
ACCEPTABLE-with-flag (it is a keep-running alarm, not a stop) — consider feeding the sim-expected
price if a real-time bin-4 alarm is wanted.

**F11 — LOW — S1 pessimistic on imbalanced states.** `realized_min` charges the FULL two-leg cash
out (incl. overhang) against `matched_pairs * $1`, so a transient imbalance can read negative and
false-halt. This is the SAFE (more-halting) direction the commission demands, so it is not a defect;
but the harness should evaluate S1 on balanced/settled pairs to avoid spurious mid-rebalance halts.

**F12 — informational — crash-mid-window loses the WHOLE in-memory journal.** Per F13 the journal is
buffered in memory and flushed only at window close, so a true crash loses every record (not just one
response). `rebuild_from_journal` is offline diagnosis of a FLUSHED journal; the only LIVE crash
safety is reconcile-first's positions poll (inherited position → flatten + stop), which is correct
fail-closed. Consistent with DEFERRED F4 (in-window supervisor). The build report frames `rebuild`
as crash recovery — clarify it is flushed-journal diagnosis + the poll is the live guard.

**F13 — informational — missing PLAN-exit fault case ADDED.** "crash mid-rebalance" was not
distinctly tested (only crash-mid-order). Added `test_crash_mid_rebalance_rebuild_flags_sell_inflight`
and `test_crash_mid_rebalance_via_executor_journal` — a journaled rebalance-sell intent whose
response is lost is flagged in-flight, position pre-sell preserved.

**F14 — informational (OUT OF SCOPE) — concurrently-added `service/pilot_ledger.py`.** A
promotion-gate module (`pilot_ledger.py` mtime 02:08, `test_pilot_ledger.py` 02:18) was added by a
concurrent agent DURING this review. One of its tests (`calendar_days`) failed transiently while the
file was mid-edit, then went green on re-run (13/13). It imports NO module I changed; my Phase-3
changes are causally unrelated. Reported for the record; not mine to fix.

---

## Fire-wrongly / order-path audit (highest stakes) — all safe

- **Mutex**: every dispatch (including stop flattens) goes through `Executor.execute` under one
  RLock; the concurrency test asserts max POST concurrency == 1. No other code path POSTs.
- **Armed gate**: unarmed dispatch is possible ONLY for a `stop_authorized` FLATTEN
  (`stop_authorized_non_flatten` refuses anything else); strategy orders after a stop are refused
  `not_armed`.
- **POST never retried**: `grep` confirms one `self._post` call; transport error / 429 / 5xx / non-2xx
  → synthetic no-fill, NEVER re-POSTed. V2's `rest.py` law (POST not retried) is honored; GET/DELETE
  retry lives only on the read path.
- **Ceiling with actual fees**: `cost_so_far (Decimal, actual fills+fees) + projected (deficit ×
  (ask+census fee)) <= ceiling_per_pair × overfilled_net`, `<=` inclusive at the boundary; over the
  ceiling → sell-down, NEVER a buy above it. Over-counting fees (post-F1) biases toward sell-down =
  safe.
- **Sell-down rounds DOWN**: `int(over_net) − int(min(nets))`; integer contract counts make `int()`
  exact; can never round up, can never oversell (`≤ held`).
- **reduce_only** is set on stop flattens (verified through translate passthrough → envelope →
  `reduce_only: true`). See F6 for the rebalance-sell gap.
- **Stop latch**: `apply_stop` latches; `trip` freezes then flattens; strategy is frozen while the
  flatten path runs (mutex + armed=False). See F7 (no structural re-arm interlock) and F4 (flatten
  cutoff).

## Confession rulings (all 10)

1. **Proxy create-paths, not V2 events-paths** — **REJECTED**, the live path is events/orders;
   **FIXED on both sides** (F0). The registered spec (the live API, prod-proven in V2) rules over the
   proxy's stale prefix; I fixed the proxy to the spec rather than building the pilot to the wrong
   proxy.
2. **Price precision override** — ACCEPTABLE. `wire_price` preserves deci-cent (4dp) and is
   byte-identical to `translate` on whole cents (tested); direction still comes from the verbatim
   port.
3. **`average_fee_paid` as TOTAL** — **PARTIALLY REJECTED**: the default was the unsafe direction;
   **FIXED to per-contract multiply** (F1). Rationale corrected.
4. **S1 worst-case $1/pair floor** — ACCEPTABLE (correct min payout for the flip pair). Note F11:
   pessimistic on imbalanced states = safe.
5. **reduce_only on sell-down/flatten** — ACCEPTABLE for flatten (enforced+tested); FLAGGED for
   rebalance-sell (F6).
6. **single-flight (transient) vs entry-dedup (persistent)** — ACCEPTABLE; the mutex already
   guarantees no overlap, both refusal reasons distinct and tested.
7. **StopController flatten via `stop_authorized`** — ACCEPTABLE mechanism, but see F4 (cutoff
   bypass) and F7 (re-arm interlock); the flag itself is PENDING-BRAD F8.
8. **positions-poll sign/shape** — ACCEPTABLE as a live-verification item; see F5 (poll race).
9. **Journal `local_ts` injectable clock** — ACCEPTABLE; trading gates read event-derived `t_minus_s`
   only (verified — no wall-clock leak into a decision).
10. **ledger lazy `parity` import** — ACCEPTABLE (read-only composition).

Plus: the **AST-extraction proxy-compat approach** is SOUND and necessary (a plain `import proxy` in
the pilot process would load `.env`/PEM — forbidden); it uses the real proxy source so parser drift
breaks it. Fragile to refactors that add new inter-symbol deps, but the proxy repo's own handler
tests are the authoritative end-to-end check. ACCEPTABLE.

---

## (B) Independent reimplementation of the decision core

**Methodology.** From `commission.md` + `falsifier.md` + PLAN's frozen-policy description ALONE (not
from `signal.py`/`reconciler.py`) I wrote `pilot/tests/reference_impl_review.py`: a parallel
`RefEntryEngine` (entry decision) and `ref_propose` (imbalance protocol). Only the DECISION RULES are
re-derived; the frozen primitives the commission says to import (`fee`, the census EV curve for
`fair_strangle_q`, `WINDOW_S`) are imported by both sides so the ONLY thing that can differ is a
comparison operator / gate / bound / rounding — exactly the surface under test. Production
(`signal.decide` and `reconciler.propose_rebalance`) and the reference are driven with the SAME
seeded event/state streams and asserted decision-identical (proposal type + which + ticker + side +
count + limit price; entry kind + source + C + ev + t_minus + legs).

**Scenario count: 2,300 randomized-but-seeded** (1,100 entry streams, seed 20260821; 1,200 imbalance
states, seed 994001) PLUS explicit boundary tests. A `pytest.ini` was added so the
commission-mandated filename (`reference_impl_review.py`, which does not match pytest's default
`test_*.py`) is collected in the normal suite.

**Boundaries verified decision-identical (both sides):**
- sub-$1 flip **C == $1.00 → NO fire** (STRICT `<`); C one deci-cent below → fires.
- Q1-strangle **EV == 5¢ → fires** (inclusive `>=`); EV == 4.9¢ → no fire.
- freshness **age == 1.0s → fresh/fires**; 1.001s → stale/no fire.
- entry **t_minus == 1s → fires**; 0.999s → StandDown.
- imbalance **t_minus == 3s → retry-buy**, 2.999s → sell-down only; **== 1s → sell-down**, 0.999s →
  ride to settlement.
- ceiling **cost+projected == ceiling_total → buys** (`<=`); a hair over → sell-down.
- **retry count == 5 → buy blocked** → sell-down.
- **sell-down rounds DOWN** (3:1 → sell 2 to 1); **GiveUp → S2**.

**Divergences found: NONE.** Across all 2,300 scenarios and every boundary, production matches a
plain reading of the frozen text. (Four initial failures were in MY test scaffolding — a non-grid-
aligned census `FAIR`, and seed events firing before the boundary tick; in each `prod == ref` held.
Fixed by using a synthetic `fair` for the EV operator, seeding at the boundary instant for timing,
and a computed exact ceiling. No production change resulted.)

## Files
- Fixed (proxy, file-ownership exception): `C:\Users\Brads\Python_stuff\degeneracy-proxy\proxy.py`
- Fixed: `C:\Users\Brads\Python_stuff\degeneracy_v3\pilot\service\orders\envelope.py`
- Fixed: `C:\Users\Brads\Python_stuff\degeneracy_v3\pilot\service\ledger.py` (`FEE_IS_TOTAL`)
- Fixed (authorized parity edit): `C:\Users\Brads\Python_stuff\degeneracy_v3\pilot\service\parity.py`
  (`LABEL_UNCOMPARABLE` bin-5 guard)
- Added: `C:\Users\Brads\Python_stuff\degeneracy_v3\pilot\tests\reference_impl_review.py` (12 tests,
  2,300 scenarios)
- Added: `C:\Users\Brads\Python_stuff\degeneracy_v3\pilot\tests\test_review_probes3.py` (7 probes/markers)
- Added: `C:\Users\Brads\Python_stuff\degeneracy_v3\pilot\pytest.ini` (collect the mandated file)
- Updated tests: proxy `tests/test_proxy.py` + `tests/test_review_probes.py` (events-path coverage);
  pilot `tests/test_orders_{envelope,executor,proxy_compat}.py`, `tests/test_stops.py`,
  `tests/test_review_probes2.py` (events paths + F3 fixed-behavior).
- Reviewed unchanged: `service/{executor,reconciler,stops}.py`, `service/orders/translate.py`
  (verbatim V2 port confirmed identical body).
