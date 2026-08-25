# Handoff — 2026-08-19 evening (pre-compaction №2: Rung 1.5 complete, backfill in flight)

Read `rung15_findings_2026_08_19.md` FIRST — it is the full record of today's tape work.
Then this file for live state. The morning handoff (`handoff_2026_08_19.md`) covers
Rung 1 and remains accurate for everything it says. Memory is current.

## LIVE RIGHT NOW (things in motion at compaction time)

1. **Trades-tape backfill running detached** (`tools/run_fetch_trades.ps1`, per-day
   interleave oldest-first, resumable). At compaction: **46/69 days done, at 2026-07-26**,
   ~2M trades/day on the 15M side lately. Expect completion late tonight. Writes
   `historical-data\trades_fetch.done`; a persistent Monitor (completion/errors/20-min
   stall) is watching and will notify.
2. **WHEN THE BACKFILL COMPLETES, DO (in order):**
   a. Verify exit + inventory (69 days × both series in `historical-data/*/trades/`).
   b. **Append the sealed-trades manifest to SEAL.md** (promised in its ADDENDUM section):
      sha256 of every trades file for 2026-08-02..18, via `Get-FileHash` (PowerShell) —
      hashes only, NEVER read content. Deny rules already cover `*.jsonl*`.
   c. Spot-check volume reconciliation on 2–3 NEW train days (sum count_fp vs market
      volume — the completeness proof; script pattern in the findings doc conversation,
      trivial to rewrite).
   d. Tell Brad the tape is complete and the full-tape run is ready.
3. **The full-tape run (Brad-sanctioned next step, needs no new code):**
   `sim/tape_sim.py --start 2026-06-13 --end 2026-08-01 --out sim/out/full60 --staleness 60`
   (and a `--staleness 5` pass), detached via the runner pattern
   (`sim/ceremony/_prompts/run_week_tape_r2.ps1` is the template). TRAIN ONLY — end at
   2026-08-01; the sim's loader refuses sealed dates anyway (tested). Then reproduce the
   three preregistered analyses from the findings doc §"What Rung 1.5 concludes":
   strangle ladder, flip ladder, conditional-x sub-$1 (the third needs a new small
   analysis: spot-vs-corridor position at entry from the 15M tape; exploratory scratchpad
   is fine, or commission it if it starts deciding things).

## Rung 1.5 estate

- `sim/tape_sim.py` — the tape sim, post-A15.10. 98 tests green (`python -m pytest
  sim/tests/`). CLI: `--start --end --out --staleness --direction --census
  --compare-no-refute`.
- `sim/loader.py` — gained `load_trades` (epoch-sorted, seal-guarded, gz).
- Ceremony: `sim/ceremony/rung15_commission.md` (A15.1–A15.10 all adjudicated),
  `rung15_review.md` (3 rounds, all verdicts + addenda), `rung15_build_report.md`
  (4 rounds, 25 confessions). Ceremony is CLOSED — reviewer verified the final law
  independently row-for-row.
- Outputs: `sim/out/week60_r2/`, `week5_r2/` (definitive), `week60/`, `week5/`
  (pre-refutation, frozen for the record), `tape_receipt.json` in each.
- Builder/reviewer were persistent opus48 agent sessions — they do NOT survive
  compaction. The ceremony docs carry everything needed to brief fresh ones.

## Fetcher / data estate

- `tools/fetch_history.py` — now has `trades15`/`trades1h` stages writing
  `historical-data/{15-minute,1-hour}/trades/YYYY-MM-DD.jsonl.gz` (gzipped verbatim raws,
  ~33–60MB/day gz vs ~300MB raw; disk had 22GB free, plan ~3GB). Hourly trades: ±$400
  band + final-3600s clamp, same as candles.
- Trades retention ≈65 days rolling, SHORTER than candles: 06-11/06-12 wrote empty tapes,
  06-13 onward real. Keep re-running the fetcher regularly (all stages are resumable).
- SEAL.md: ADDENDUM (trades tape inherits the seal; manifest pending backfill) +
  disclosure №4 (my one sealed-ticker retention probe: count+timestamp only) are
  registered. `.claude/settings.json` deny patterns widened to `*.jsonl*` BEFORE any
  sealed trades file was written.

## Standing house state (unchanged from morning handoff, still true)

- Sealed surface: Aug 2–18 minus the 2 burned hours; falsifier DRAFT; unseal_runner never
  run; any sealed read = new commission + frozen falsifier + Brad's go.
- Brad still needs to revoke the two burned PEMs (07-12 chat PEM + V1 git-history key).
- Proxy: 127.0.0.1:8642, read-only, running detached. Never touch key/.env/PEM.
- Birth-taker live probe: still pending Brad's go, UNCHANGED by today — but note the tape
  showed the sub-$1 flip is NOT a birth-seam phenomenon (birth-seam hard-floor dwell ≈ 0),
  which weakens the flip-at-birth variant while leaving Brad's original hand-fill
  evidence (strangle side, burned hours) untouched.
- Environment: harness kills tracked background tasks — detached Start-Process +
  .done marker + Monitor/Bash-watcher is the law. PowerShell 5.1 quirks per morning
  handoff. Opus 4.8 for builds/reviews (agent type "opus48").

## Where the thesis stands after today (one paragraph)

Rung 1 killed the strangle at candle fidelity; Rung 1.5 killed both directions at tape
fidelity — efficient minus fees, with every apparent edge dying to a named, verified
artifact (leg-lag mirage, event weighting, cross-side refutation, resolved-lottery
conditioning). What remains: two statistically-unresolved ladder bumps (strangle EV−10 ≈
+2.6¢, flip EV−15 ≈ +3.4¢) and the conditional-x question, all preregistered for the
full-tape run on ~50 train days. The live-book door (real-time WS, maker-side, V2's
guards) remains the only unmeasured territory. Nothing is armed; nothing runs on a timer
except the backfill.
