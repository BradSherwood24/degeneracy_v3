# Shakedown Day 1 — first contact findings (2026-08-21)

The pilot's first day watching live water. Six scheduled runs, mode `shakedown`
(no orders possible), Task `DegeneracyV3Pilot` firing at :40 UTC hourly against the
supervised signing proxy. Everything below cost $0.00.

## The run table

| close (UTC) | outcome | what it taught |
|---|---|---|
| 16:00 | stand-down | **Venue fact 1**: 15M markets list days ahead as `initialized`, flip `active` exactly at open (:45). A status-filtered discovery at :40 sees nothing, forever. Fixed: status-agnostic selection by close-time. |
| 17:00 | stand-down | **Venue fact 2**: the 15M `floor_strike` does not exist until open — it materializes at :45 with the `active` flip, set from BTC spot (non-round, e.g. 77315.17). No anchor at :40 → no pairing, no G, no C. Fixed: the two-phase wake (Phase A anchor-free at :40; Phase B polls the strike at :45, then pairs → G → σ̂ → quintile → dial). |
| 18:00 | stand-down | **Ops fact**: the proxy had died silently (it was spawned from a Claude session shell and got reaped). The run retried per policy, gave up, stood down clean, exit 0 — the fail-closed law demonstrated against real infrastructure failure. Fixed: proxy now a Task Scheduler task (`DegeneracyProxy`) with restart-on-failure. |
| 19:00 | **full window** | G $24.01, σ̂ 191.9, quintile reproduced live, 702,672 journal records — and the **first would-fire** (below). |
| 20:00 | **full window** | G $39.28, 636k records, zero signals — correct restraint, which is most of this policy. |
| 21:00 | **full window** | **Venue fact 3**: a **$100-step hourly generation existed at Friday 21:00 UTC** — an hour the entire 69-day corpus showed only as $250 ($500 Fridays). The A2 ladder alarm fired exactly as designed: strangle disabled, sub-$1 kept running, clean window. Either the venue changed or dual-generation listings are now standard at that hour. The map was wrong; the guard worked. |

Also proven live along the way: σ̂'s trailing anchors ARE available from `/markets`
(the top pre-live risk — the strangle path exists), leg-role reversal handled (the
21:00Z window's anchor sat below the hourly strike), and the overlap guard, budget
persistence, and two-phase timing all behaved.

## The first would-fire (19:00Z)

At t−61s the machine saw the pair for **C = $0.9993** (NO on the 15M at 0.8¢ + YES
on the hourly at 99.0¢, actual fee formula) — sub-$1 flip, the late-window floor
dust that is 83% of this policy's expected volume. Settlement: BRTI finished above
the corridor; the hourly YES paid $1.00. **Floor case: +0.07¢ per pair, verified
against actual venue settlement results.** The pin (BRTI inside the $24.01 band)
would have paid +$1.0007. The arithmetic held end-to-end: discovery, anchor,
census pairing, σ̂, quintile, fees, trigger, settlement — one unbroken chain
agreeing with reality in a single window.

**The shakedown gate's formal minimum (≥2 runs, ≥1 would-fire) is met.**

## The first parity report — 3/3, and the honest delta

Mid-day fetch + `tape_sim` over the day (partial, through 21:00Z) + the five-bin
harness over the three full windows (`pilot/build/parity_0821.*`):

- **F15 neutrality: PASS. Zero fire/no-fire flips.** Both quiet windows were quiet
  in both worlds; the 19:00Z window fired in both worlds, same source.
- Inside the matched fire, the **book-vs-prints delta** showed itself plainly:

| | sim (prints) | live (asks) |
|---|---|---|
| entry | t−849s | t−61s |
| C | $0.9921 | $0.9993 |
| floor payout | +0.79¢ | +0.07¢ |

The sim's print-based first entry joined a cross that happened *inside the spread*,
fourteen minutes before the live book's asks ever went sub-$1. Fire/no-fire agrees —
the decision layer is honest — but the sim's *price* was 0.72¢ better than anything
a taker could have had. n=1; if the pattern holds across coming windows, the honest
armed-pilot expectation shifts toward **smaller floors, same pin lottery**, and the
falsifier's paired protocol will pin the size of that haircut with real fills.
This is not a surprise — it is the named structural delta (commission, "known
confounds") getting its first measurement.

## Standing state

- Ledger `pilot/ledger/pilot_ledger.jsonl`; journals per window; scheduler log
  `pilot/logs/scheduler.out`. 374 pilot + 111 proxy tests green.
- The schedule runs unattended (survives session compaction/closure — both services
  are Task Scheduler tasks). Proxy needs a manual start after any machine reboot
  until the logon trigger gets an elevated-shell registration.
- Partial 2026-08-21 day-files deleted from the corpus (fetcher skips existing
  days); refetch the full day before any census use. `sim/out/day0821` kept with a
  partial-day note.
- Owed by Brad before the freeze: F8 stop-action confirmation (hold complete floor
  pairs / flatten unprotected), the V2 retirement bar (proposed 4¢/side), then the
  falsifier `[pin]`s + `STATUS: FROZEN` on his verbatim word, `ALLOW_ORDERS`, and
  `mode.txt → armed`.

## Scorecard

Three stand-downs, three lessons, all fixed same-day with regression tests. Three
full windows, one would-fire, one correctly-fired alarm on a venue change the
corpus never saw. First parity report: perfect decision agreement, first honest
measurement of the print-vs-ask haircut. The tape said what the market was; the
shakedown is finding out what the market *is* — at a burn rate of zero. 🐀⚓
