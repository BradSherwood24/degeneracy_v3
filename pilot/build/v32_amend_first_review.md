# Review — PR #59 amend-first replace with cancel+create fallback

**Reviewer:** Opus 4.8 (Fable's delegated reviewer)
**Date:** 2026-09-15
**PR:** #59 `feat/amend-first-replace` -> `main` (https://github.com/BradSherwood24/degeneracy_v3/pull/59)
**Reviewed at:** `origin/feat/amend-first-replace` @ `43c5f59` (diff `origin/main...HEAD`, base @ `98ffdae`)
**Worktree:** `C:\Users\Brads\Python_stuff\dv3_wt_review` (review tree; NOT live, NOT v11/amend/fix)

## Verdict: APPROVE WITH NITS

No blocking money-safety defect. Every replace path preserves "never two live rests," including the
dangerous amend-timeout case. Merging is behavior-neutral on the live pilot (amends 403 at the proxy today
and fall back to the proven cancel+create) until Brad applies the proxy change. Frozen falsifier integrity
is intact. The nits below are non-blocking; nit (2) is a latent invariant to keep an eye on if `contracts`
is ever raised above 1.

---

## 1. Money safety — never two live rests (priority 1): PASS

Traced every path in `service.v32.core._requote` / `_apply_amended` / `_apply_cancelled` and
`service.v32.executor._amend_rest` / `_amend_fallback`:

- **amend 2xx (no cross):** `_amend_rest` (executor.py L764+) parses the response, keeps the SAME
  `order_id`, moves the RestBook entry to the new coid (old coid retained `status="amended"` for F-1
  late-fill attribution), and returns `OrderAmended`. `core._apply_amended` (core.py L592) updates
  `rest_live.price`/coid IN PLACE, clears `amend_in_flight`. Exactly ONE order. ✓
- **amend non-2xx (404/500):** `_amend_fallback` (executor.py L826) runs the EXISTING sharded DELETE
  (PR #50 backoff/status-truth) on the persisting `order_id`; its `OrderCancelled` clears the core's rest
  (`_apply_cancelled` now also clears `amend_in_flight`, core.py L534-537), so the next tick re-places via
  `_pre_place_invariant` (the pre-PLACE venue-truth GET). One order throughout. ✓ (tested)
- **amend TIMEOUT / transport exception (the dangerous case):** `ProxyWriter.rest_post` catches ALL
  exceptions and returns a non-ok `WriteResponse` — it NEVER raises (proxy_writer.py L84-97, docstring
  "Any transport error is returned as a non-ok WriteResponse, not raised"). So a timeout is just
  `resp.ok == False` -> `_amend_fallback`. The fallback cancels by the persisting `order_id`, which is
  valid whether or not the venue applied the amend:
  - if the venue APPLIED the amend, the one order now rests under `coid_new` at the new price but the SAME
    `order_id`; the DELETE-by-order_id removes it. One order. ✓
  - if the DELETE 404s (coid/state moved on), `_cancel_nonok` (executor.py L603) GETs order-status BY
    ORDER_ID (shard-free); a TERMINAL status resolves via status-truth and books any race fill through
    `_finish_cancel`; a still-`resting` read backs off and retries the sharded DELETE. `OrderCancelled` is
    returned ONLY after resolution, and the core cannot PLACE until it receives that `OrderCancelled`
    (`amend_in_flight` holds `_requote`, core.py L935-938). So status-truth always resolves before any
    create. ✓
- **`amend_in_flight` gating:** set in `_emit_amend` (core.py L649); holds all requotes/places
  (`_requote` early-return L935-938) and the bucket-change branch (guarded by `and not st.amend_in_flight`,
  L913); cleared by `OrderAmended` (`_apply_amended`) OR the fallback's `OrderCancelled`
  (`_apply_cancelled`, L537). No path leaves it stuck (rest_post never raises; a mismatched OrderAmended
  still returns `amend_in_flight=False`, core.py L620). ✓
- **no-order_id defense:** if `rest_live.order_id is None` the core falls to sequential cancel+create
  (core.py L959-963) and the executor's `_amend_rest` routes a missing oid straight to the fallback
  (L731-738). ✓

## 2. Fills during the amend (priority 2): PASS (one latent assumption — see nit 2)

- **Normalization convention matches create.** `_amend_rest` parses with
  `parse_single_response(resp.body, side=BUY_NO)` — the IDENTICAL units choke point the create path uses
  (executor.py L393). `normalize_fill_to_side` converts a NO order's YES-space venue price to NO-space
  (`price_no = 1 - price_yes`, envelope.py L171-193). So `average_fill_price` is booked in NO-space,
  directly comparable to the leg limit — correct side/space. ✓
- **Fee** is the venue's `average_fee_paid` (real taker fee), booked into the `fills` money-math (L792-796).
- **De-dup vs WS/poll echo.** The amend fill adds `oid` to `booked_rest_oids` before appending
  (executor.py L792-793); `run_v32._record_fill` and `on_poll_fill` dedup by the SAME `order_id`
  (run_v32.py L924-928, L937), and the `order_id` persists across the amend, so a WS/poll echo of the same
  trade is booked exactly once. ✓
- **count 1 -> full fill, no remainder.** `contracts` is pinned to 1 (PLAN_V32.md L127), so a cross fills
  1 and leaves nothing resting; `_apply_amended` zeroes `rest_live` and takes wings once (one-set latch via
  the `rest_fill is None` guard). ✓

## 3. Replace accounting (priority 3): PASS

- Amend counts as a replace on CONFIRM in `_apply_amended` (core.py L626-634): `replace_count += 1`,
  appends to `replace_times`, sets `last_replace_ts`. The ledger `replaces` field reads
  `state.replace_count` (ledger.py L201, run_v32.py L1496) and the A_REPLACE alarm reads `replace_times`
  (core.py L872), so amends feed both. The fallback (no `OrderAmended`) counts once at the create's
  `_emit_place` instead — exactly once per replace either way. ✓
- Bucket change (different ticker) still CANCELS, never amends (core.py L913 branch + `_cancel_action`); an
  amend can't change the ticker. Quote-end (`t_to_close < quote_end_s` = at/after T-5) and the replace-rate
  alarm both `_cancel_live_if_any`, never amend (core.py L871-905). The amend branch runs only after all
  no-quote guards pass. ✓ tol/deb gate unchanged (core.py L947-951).

## 4. Modes (priority 4): PASS

Dry/shakedown emit `WOULD_AMEND_REST` (twin map complete, actions.py L44); `FrozenExecutor` refuses a real
`AMEND_REST` (added to `_REAL_KINDS`, run_v32.py L504) and synth-amends the `WOULD_` twin so the dry state
machine cycles (run_v32.py L550-576). `LiveExecutor` raises on a `WOULD_AMEND_REST` mis-wire (executor.py
L349). `run_v32._journal_action` journals `amend_rest`/`would_amend_rest` and `_capture_quote(a)` (mirrors
PLACE); executor journals `amend_rest`/`amend_confirmed`/`amend_failed`/`amend_fill`. ✓

## 5. Frozen falsifier integrity (priority 5): PASS

`ceremony/v32_falsifier.md` diff is exactly: (a) the in-place edit of the "What is being judged" line —
`(cancel -> confirm -> create, never two live rests)` becomes
`(amend-first, cancel -> confirm -> create as the fallback; never two live rests)` — the mechanics
sentence only, as recorded in the Registration entry; and (b) one ADDED Registration bullet
(MECHANICS CLARIFICATION, 2026-09-15 ~18:10Z) quoting Brad VERBATIM
("Yea, I agree. Use the cancel and recreate flow as a backup if our post to ammend the order fails. Go
ahead and build that"). STATUS FROZEN, the params sha
`0ac6979...80dc`, `n >= 30`, and every `[pin]` are untouched. `test_v32_falsifier_pins.py` adds one
assertion (MECHANICS CLARIFICATION + "amend" present) and leaves the existing pin assertions unchanged and
green. ✓

## 6. Proxy reality check (priority 6): CONFIRMED

Read `degeneracy-proxy/proxy.py` directly (READ ONLY; no .env/.pem touched):

- `_ORDER_WRITE_PREFIXES` = `/trade-api/v2/portfolio/events/orders` (+ legacy) (L70-73). The amend path
  `/trade-api/v2/portfolio/events/orders/{id}/amend` is UNDER that prefix.
- `is_order_create` returns True only for the exact paths in `_ORDER_CREATE_PATHS` (L77-82, L168-178);
  `/amend` is NOT among them.
- The refusal block (L462-478): any POST under `_ORDER_WRITE_PREFIXES` that is `not is_order_create` ->
  `403 {"cap":"order_write_not_create"}`, BEFORE any upstream contact or budget touch.

So **every amend 403s locally today** and `_amend_rest` falls back to cancel+create. The builder's claim is
CONFIRMED: **merging is behavior-neutral vs the current live pilot** until Brad applies the proxy change and
restarts. The failed amend is a ~1 ms localhost round-trip; it never reaches Kalshi and never consumes
budget.

**`ops/proxy_amend_cap.md` evaluation:** correct and minimal. The `is_order_amend` helper (POST + path
ends `/amend` + under `_ORDER_WRITE_PREFIXES`), the exemption from the blanket refusal, and reuse of
`check_order_caps(entries, max_contracts_per_order, ticker_prefixes)` on the single amend object are the
right, minimal change; `exchange_index` and routing already work via `is_order_write_path`. The budget
recommendation (COUNT amends against `DAILY_ORDER_BUDGET`) is the correct default — it preserves the S5
self-degrade-to-dry bound, and the doc correctly flags that NOT counting needs a separate amend-rate guard.
One advisory for when Brad applies it: confirm `parse_order_entries(body, is_batch=False)` tolerates the
amend body's extra keys (`side`, `client_order_id`, `updated_client_order_id`) and the `"1.00"` count — it
should, since it extracts specific keys, but the doc's suggested proxy unit tests (over-cap -> 403,
non-whitelisted ticker -> 403, well-formed 1-contract KXBTC -> forwarded) should be run before trusting the
path live. The doc is explicitly a PROPOSAL ("Do NOT apply from this repo") — house law respected.

## 7. Golden harness change (priority 7): FAITHFUL

`test_v32_golden.py` adds an `AMEND_REST -> OrderAmended` branch that confirms the amend after ONE RTT
(`_LAT_MS`), fill 0, with the new price applied in place — a single-lag replace with no cancel gap, which
is exactly the real amend mechanic and matches the single-lag `_ref_lagging` reference. No assertion was
relaxed; the +10.36c reference equality still holds (test green). Because the fixture's rests carry an
`order_id` (from `OrderAck`), the core emits `AMEND_REST` (not `CANCEL_REST`) for the replace, so the new
branch is genuinely exercised. Faithful model, not a weakened test. ✓

## 8. Tests (priority 8)

`python -m pytest pilot/tests -q` in the review worktree: **877 passed, 2 skipped, 2 errors**. The 2 errors
are `test_quintile.py` FileNotFoundError on `historical-data/15-minute/...` — the KNOWN data-absence in
this worktree, unrelated to PR #59. `test_v32_amend.py` collects **12** tests (10 `def` + the
`test_amend_failure_falls_back_to_cancel_create` parametrize x3), all green; the failure parametrize
INCLUDES the transport-timeout case (`WriteResponse(None, {}, False, "post_exception:Timeout")`). The build
report's "12" is honest (collected count). `test_v32_golden.py` / `test_v32_falsifier_pins.py` /
`test_v32_core.py` / `test_run_v32.py` modifications all pass.

**Conflict hot spots vs PR #56 (`fix/phantom-resting`, not yet merged):** PR #56 edits
`executor._pre_place_invariant` / `_finish_cancel` / counters; PR #59 ADDS `_amend_rest` / `_amend_body` /
`_amend_fallback` / amend counters and calls (does not redefine) `_pre_place_invariant`, `_finish_cancel`,
`_resolve_cancel_success`, `_cancel_nonok`. Overlap is the `LiveExecutor.__init__` counter block and
`on_action` dispatch (both add lines in the same regions) and the shared reliance on `_finish_cancel`
semantics. Whichever merges second should re-run the full suite and hand-verify that `_finish_cancel` /
`_pre_place_invariant` still behave as PR #59's fallback assumes (status-truth resolve + single live rest).
No logical conflict expected, only textual.

## 9. Nits / observations (non-blocking)

1. **Timeout+applied+crossed price attribution.** In the rare double-fault where the amend times out but
   HAD crossed at the new price, the fallback books the race fill at the OLD resting price (`rec.price` in
   `_finish_cancel`; the core books `rest_live.price`, also old, in `_apply_cancelled`), not the amended
   price. Position is still bounded (wings taken); discrepancy <= `tol` (0.02); `exec_price_mismatch` is
   the live check. Worth one line in the build report's Unknowns.
2. **Latent partial-fill assumption (watch if `contracts` > 1).** `_apply_amended` and the executor ignore
   `remaining_count` on a crossed amend. Safe ONLY because `contracts` is pinned to 1 (a cross = full fill,
   remainder 0). If `contracts` is ever raised, a partial-cross amend would zero `rest_live` while a
   remainder rests on the venue (phantom, unhedged). Add a `remaining_count > 0` guard/assert before that
   day. The executor already journals `remaining` but neither path acts on it.
3. **Redundant match clauses.** `_apply_amended`'s `matched` has two `client_order_id` comparisons; the 3rd
   is subsumed by the 2nd, and both are dead at confirm time (the core still holds the OLD coid, the event
   carries the NEW one). Matching correctly succeeds via `order_id`. Harmless; could be trimmed to
   `order_id`-only for clarity.
4. **FrozenExecutor synth-amend RestRecord** omits `exchange_index`/`expiration_epoch` (the live executor's
   `new_rec` sets them). Dry path only, no money impact; cosmetic.
5. **No direct unit test** for the specific double-fault (amend timeout + fallback DELETE 404 ->
   status-truth resolve). The underlying `_cancel_nonok` status-truth path is tested elsewhere and reused
   verbatim by `_amend_fallback`, so coverage is adequate, but a targeted test would harden the exact
   sequence.
6. **Co-Authored-By** — the builder used a different Co-Authored-By line than this reviewer's. Cosmetic,
   noted per instructions.

## Blocking items

None.
