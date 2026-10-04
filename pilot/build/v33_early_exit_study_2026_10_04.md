# Early-exit study: can a held set be sold above $2 before settlement?

*2026-10-04, after the 14:00Z window (11 sets, +$1.58 realized). Brad, watching the account value during that
window, saw it jump +$2, +$3, more than +$4 and asked whether selling early would have squeezed out ~$3.*

**Verdict: NO. The executable unwind value of a held set sits ~6.5c BELOW $2 for essentially the whole hold.
It pokes above $2 for 0.9% of held time, above +2c net for 0.04% of held time (longest run 1.7 s, median
run 20 ms), and above +5c net NEVER (time-weighted, 27 windows, 62 lots, 3.6 held hours). The +$2..+$4 jumps
on the screen were last-trade MARKS on deep strikes, not bids: the last-trade mark peaked +36c/set in the
14:00Z window (x11 = +$3.96) while the best executable unwind at that moment was -0.8c/set.**

## Method

Script: `pilot/build/v33_early_exit_study.py` (reads a window journal, rebuilds the three books from the WS
tape with `service.book.BookMirror`, no proxy). For every armed window that completed at least one set
(5 V3.3 + 23 V3.2 windows, 2026-09-15 .. 2026-10-04), from the first `rest_fill` to close, at every book change
on the three markets we hold:

- `top`  = bucket NO best bid + Sd YES best bid + Su NO best bid (what a taker unwind of one set fetches)
- `net`  = top - 2 - three taker fees (0.07 p (1-p) per leg, ceil to $0.0001; maker fee is 0 on crypto but an
  unwind is a taker)
- `vwap` = same walking depth to unwind ALL lots held at that instant (None if depth short)
- `mark` = last-trade price of each leg summed - 2 (what a UI mark looks like)

Reported per window: max net, time-weighted fraction of held time with net > 0 / > 2c / > 5c, longest
contiguous run above 0 and above 2c, max vwap for all lots, median net, max last-trade mark premium.

Fees and spreads are the only costs modelled. NOT modelled, and both make it worse: (a) Kalshi has no atomic
multi-leg order, so a three-leg IOC unwind can fill one or two legs and leave a DIRECTIONAL position -- the
same one-legged failure mode that the frozen pin kills us for; (b) decision + round trip (~100-250 ms observed
on our wing fills) against runs that are mostly 10-50 ms long.

## Results

Pooled, 27 windows (the 09-30 22:00Z window excluded, see below):

| | |
|---|---|
| windows / lots / held time | 27 / 62 / 3.6 h |
| time-weighted fraction of held time with net > 0 | 0.91% |
| ... net > +2c | 0.04% |
| ... net > +5c | 0.00% |
| median of per-window median net | -6.5c (range -8.8c .. -3.5c) |
| per-window max net: median / p75 / max | +6.5c / +10.7c / +25.1c |
| longest run above 0: median / max | 1.0 s / 10.4 s |
| longest run above +2c: median / max | 0.02 s / 1.71 s |
| last-trade mark premium, per-window max: median / max | +19c / +62c |
| windows where the mark peaked > +10c | 23 of 27 |

Per window (net in cents per set; `>0` and `>2c` are time-weighted fractions of held time; run = longest
contiguous run above that level):

```
close        rost lots held   maxnet  @T-   legs(bucketNO,SdYES,SuNO) depth  >0     run    >2c    run    median  markmax
10-03T00:00  v33   1   684s   +7.4c   104   (0.30, 0.81, 0.99)          3   1.3%   1.1s   0.0%   0.0s   -4.4c   +19c
10-03T01:00  v33   3   414s   +0.9c   249   (0.09, 0.95, 0.98)          6   0.8%   3.0s   0.0%   0.0s   -3.5c    +8c
10-04T06:00  v33   8   529s   +8.5c   159   (0.52, 0.99, 0.61)          3   1.0%   1.0s   0.0%   0.0s   -6.6c   +27c
10-04T14:00  v33  11   751s   +1.3c   287   (0.06, 0.98, 0.98)         11   0.8%   4.4s   0.0%   0.0s   -5.6c   +36c
09-15T00:00  v32   1   549s   +1.7c    98   (0.92, 0.12, 0.99)          3   0.0%   0.0s   0.0%   0.0s   -6.5c   +12c
09-15T17:00  v32   1   296s   +3.6c    51   (0.92, 0.14, 0.99)         30   0.6%   0.5s   0.0%   0.0s   -7.7c    +9c
09-16T05:00  v32   1   290s   +7.3c   109   (0.19, 0.98, 0.92)         88   0.0%   0.0s   0.0%   0.0s   -6.6c   +13c
09-17T22:00  v32   1   317s  +13.5c   270   (0.52, 0.97, 0.68)         10   1.0%   1.9s   0.0%   0.0s   -5.9c   +26c
09-18T00:00  v32   1   377s  +17.6c    10   (0.22, 0.99, 0.98)         14   0.3%   0.4s   0.0%   0.0s   -7.6c   +62c
09-18T04:00  v32   1   525s   +8.5c    32   (0.17, 0.94, 0.99)         12   1.3%   3.1s   0.0%   0.1s   -8.5c   +14c
09-19T22:00  v32   1   539s  +15.8c    78   (0.72, 0.99, 0.48)          8   0.4%   0.6s   0.0%   0.0s   -6.9c   +51c
09-20T04:00  v32   2   300s   +0.7c    91   (0.21, 0.97, 0.85)         16   0.8%   1.5s   0.0%   0.0s   -6.9c    +9c
09-20T08:00  v32   2   460s  +11.2c    44   (0.80, 0.35, 0.99)         10   1.7%   3.5s   0.8%   1.7s   -7.6c   +19c
09-20T09:00  v32   2   332s   +2.8c   114   (0.21, 0.87, 0.97)          1   3.2%   3.1s   0.0%   0.0s   -4.8c    +9c
09-20T12:00  v32   2   446s   +1.7c   273   (0.37, 0.96, 0.72)          1   0.5%   2.2s   0.0%   0.0s   -7.4c   +17c
09-21T06:00  v32   2   300s   +6.5c   117   (0.47, 0.66, 0.97)         50   1.5%   1.5s   0.0%   0.1s   -7.5c   +21c
09-26T10:00  v32   2   290s   +7.6c   154   (0.47, 0.65, 0.99)          6   0.4%   0.7s   0.0%   0.0s   -7.5c   +42c
09-26T17:00  v32   2   642s  +25.1c    50   (0.48, 0.81, 0.99)         80   1.6%   2.2s   0.0%   0.0s   -5.6c   +26c
09-26T19:00  v32   2   520s   +4.5c    50   (0.15, 0.99, 0.92)         11   2.8%  10.4s   0.0%   0.0s   -7.6c   +12c
09-26T20:00  v32   2   685s   +2.8c    46   (0.22, 0.84, 0.99)         18   0.2%   0.9s   0.0%   0.0s   -6.5c   +17c
09-27T02:00  v32   2   793s  +11.9c   174   (0.74, 0.99, 0.42)          1   1.0%   1.0s   0.0%   0.1s   -4.9c   +21c
09-27T08:00  v32   2   563s  +10.7c   125   (0.86, 0.99, 0.28)         25   1.3%   1.0s   0.1%   0.3s   -4.5c   +20c
09-27T18:00  v32   2   734s   -1.5c   280   (0.97, 0.99, 0.03)         25   0.0%   0.0s   0.0%   0.0s   -6.4c   +12c
09-27T19:00  v32   2   418s   +6.5c    91   (0.56, 0.99, 0.55)         12   0.4%   0.3s   0.1%   0.1s   -3.9c   +28c
09-27T22:00  v32   2   556s   +1.5c   340   (0.25, 0.98, 0.81)          1   0.9%   0.9s   0.0%   0.0s   -4.3c   +33c
09-29T04:00  v32   2   405s   +3.7c    83   (0.23, 0.84, 0.99)         12   0.5%   1.1s   0.0%   0.0s   -8.8c   +12c
09-29T23:00  v32   2   258s   +3.7c    98   (0.24, 0.99, 0.83)        130   0.4%   0.2s   0.0%   0.0s   -4.5c   +11c
```

### The 14:00Z window Brad watched

- Held 11 lots from T-812 s (13:46:27Z) to close. Executable net per set by minute: -5.0c, -5.0c, -5.5c,
  -6.6c, -7.4c, -8.7c (13:51), -5.0c, -4.9c, -4.4c, -2.7c, -2.4c, -2.4c, -1.2c (13:58). It converges toward
  $2 from BELOW as the books tighten into settlement, as a $2-par structure should.
- Best executable moment: +0.4c/set net at 13:55:13Z with depth 5 at top of book. The all-11-lots VWAP never
  cleared +1.3c. Time above zero: 0.8% of the hold; longest run 4.4 s at a fraction of a cent.
- The last-trade mark peaked at +36c/set at 13:54:39Z (legs printed 0.06 / 0.98 / 0.96 and the mark summed
  stale prints) while the executable net at that instant was -0.8c. Eleven sets x +36c = +$3.96 on top of
  par, on top of the +$1.80 lock = the "+$4" on the screen. It was never for sale at that price.

### What the peaks are

The spikes to +10..+25c (09-26 17:00Z +25.1c at depth 80; 09-18 00:00Z +17.6c at T-10 s) last tens of
milliseconds: a bid on one leg flashes at a level inconsistent with the other two and is lifted or pulled.
Longest run above +2c in 3.6 hours of holding was 1.7 s (09-20 08:00Z, 2 lots, +11c). This is the same object
the maker-flip backtest found at entry (crossings = 28 ms flickers) and the board-identity study found for the
sub-$1 all-in hedge (persists 0/936 hours): the fee curve (4-5c on a taker unwind) is wider than the
dislocations, and the dislocations do not persist through decision + round trip.

### Excluded: 2026-09-30 22:00Z

The first Test Fire #1 window shows +96.8c/set for 197 s with (0.99, 0.99, 0.99) at depth 176. Bucket NO bid
0.99 (BTC outside the bucket) with Sd YES 0.99 (BTC above Sd) and Su NO 0.99 (BTC below Su) is three bids
that cannot all be true; at T-27 s the bucket book had NO no-side bid at all and its yes bid was 0.98 (BTC
inside the bucket), consistent with the two strikes. That window is also the one with the chunked NO-wing
retry storm (60+ `take_wings` chunks at 0.05) and the open "09-30 incident pair" ruling. A dollar of free
money sitting for three minutes at depth 176 on a venue with other arbitrageurs is not an opportunity; it is
a book that did not reflect the venue. Excluded as a data-quality outlier, not as an inconvenient result.
Worth a separate look under the 09-30 incident, not under this study.

## What this means for the strategy

1. **No early-exit lever.** A taker unwind of a held set is worth ~$1.935 for almost the whole hold and crosses
   $2 only in ms flickers that cannot be reached and would not cover fees if they were. The $2 settlement IS
   the exit; the lock at entry is the whole trade.
2. **Partial unwinds are worse than nothing.** Any exit that fills one or two of the three legs turns the
   locked set back into a bucket bet. Kalshi has no atomic multi-leg order. The frozen pin treats a naked
   contract as the failure mode; an exit rule would manufacture that failure mode on purpose.
3. **The screen number is not a price.** Kalshi's position mark uses last trades on markets that print rarely
   once deep in the money. Our own `rest_fill` + `take_wings` + settlement accounting (and the ledger's
   realized_delta, which matched the venue to 0.03c today) is the number to watch, not the portfolio value.
4. **Where the "more" actually is**: more lots per rung when the sweep is deep (the 13:48:05Z sweep took 9 lots
   in one print; the 23:48Z 10-02 sweep would have filled 20 at our level), i.e. the `rung_lots` lever already
   on the list -- not timing the exit.

## Files

- `pilot/build/v33_early_exit_study.py` -- the measurement (one journal in, summary + optional per-second CSV)
- journals: `pilot/journals_v33/*.jsonl.gz`, `pilot/journals_v32/*.jsonl.gz` (windows listed above)
- ledger rows: `summary.jsonl` in each journals dir (`sets_done > 0` and `resolved_mode == armed`)
