# Multi-series expansion — hourly strike+range pairs across Kalshi: inventory, volume, data plan

Status: **RESEARCH / SCOPE.** Nothing here is built, armed, or a lever pulled. Read-only proxy GETs
only; no orders, no seal reads. Generated 2026-09-22 by `pilot/build/mc/multi_series_scan.py`
(+ `.out`, `.json`). Total unique proxy GETs this session: **933** (budget 2,000).

## Brad's ask (verbatim, 2026-09-22 ~02:40Z)

> "could you research all other markets that run both the hourly strike and hourly bucket markets?
> Doesnt need to be crypto currency, commodities, indexes, whatever. The structure of the
> derivatives is what our strategy makes money on, not the underlying ... can you scope out just how
> to go about saving all that data, and what data we should save? Feel free to use the Kalshi API to
> research what markets we can even tap into, and how much data we're even talking about. Just scope
> out what markets and what changes are needed to hook into them. More important, maybe, im worried
> because I dont think these markets have the volume to support our strategy. We need to mark what
> volume of contracts are being traded. The pump fader doesnt work if no one's trading the market."

## TL;DR (the volume worry, answered first)

**Brad's worry is correct.** Of Kalshi's 71 hourly series, exactly **7 underlyings** run BOTH a live
hourly RANGE (bucket) series and a live hourly STRIKE series that co-settle — and **all 7 are crypto**
(BTC, ETH, SOL, XRP, DOGE, BNB, HYPE; all `exchange_index 2`, settled by CF Benchmarks, 24/7 at the
top of the hour). **No index, commodity, or FX underlying has a live hourly range series** — they run
the strike ladder alone, so our 3-leg set cannot be built there today.

Among the 7 crypto pairs, the pump-fader's fuel is **spot-bucket taker flow in the T-15..T-5 quoting
window** (a YES sweep that pushes the bucket-NO down to our resting `n`). Measured over ~1-2 weeks:

| pair | spot-bucket T-15..T-5 vol (med) | as % of KXBTC | sweeps ≥20 lots / window (med) | verdict |
|---|---|---|---|---|
| **BTC** | 1,567 | 100% | 22 | **live edge (baseline)** |
| **ETH** | 168 | 10.7% | 2 | **marginal** — the only credible #2; needs its own shadow |
| XRP | 45 | 2.9% | 0 | **dead** today (and geometry misaligned) |
| HYPE | 18 | 1.1% | 0 | **dead** today |
| DOGE | 15 | 1.0% | 0 | **dead** today |
| SOL | 4 | 0.3% | 0 | **dead** today |
| BNB | 1 | 0.06% | 0 | **dead** today |

Only BTC has a dense pump (median ~22 sweeps ≥20 lots per window, ~19 of them YES-side). ETH has a
faint pulse (~2 sweeps/window, ~1/10 BTC's flow). The other five have **zero ≥20-lot sweeps in the
median window** — there is no pump to fade. The wing (strike) ladders are liquid and two-sided
everywhere (92-100% of sampled windows), confirming the old finding that **wing depth is not the wall
— bucket taker flow is.** The port is cheap to build; the trade only exists where the buckets trade.

---

## 1. Pair inventory

### 1a. The 7 alive crypto pairs (both legs live, co-settling)

Every alive pair: `exchange_index 2`, settlement source **CF Benchmarks**, close **top of the hour,
24/7**, both legs the same event date/hour tag. Range ticker `KX<U>-<tag>-B<mid>`; strike ticker
`KX<U>D-<tag>-T<level>`; tag e.g. `26SEP2122` (date + UTC hour). Geometry test = "does a strike sit at
the bucket floor AND at floor+width, so YES@floor + NO@cap + NO@bucket pays exactly $2 everywhere?"

| U | range | strike | bucket width | strike spacing | markets/event | geometry $2 everywhere? | notes |
|---|---|---|---|---|---|---|---|
| BTC | KXBTC | KXBTCD | $100 | $100 | 188 / 188 | **YES** (186/186 buckets) | the live edge |
| ETH | KXETH | KXETHD | $5 | $5 | 300 / 300 | **YES** (298/298) | clean |
| SOL | KXSOLE | KXSOLD | $0.25 | $0.25 | 300 / 300 | **YES** (298/298) | KXSOL (old range) is dead; use **KXSOLE** |
| DOGE | KXDOGE | KXDOGED | $0.005 | $0.005 | 53 / 53 | **YES** (51/51) | ⚠ `floor_strike`/`cap_strike` are **NULL** — bounds only in ticker+subtitle |
| XRP | KXXRP | KXXRPD | $0.02 | $0.02 | 75 / 75 | **NO — misaligned** | strike grid offset ~$0.0001 below bucket floors → sub-tick payoff holes |
| BNB | KXBNB | KXBNBD | $5 | $5 | 75 / 75 | **YES** (73/73) | clean |
| HYPE | KXHYPE | KXHYPED | $0.25 | $0.25 | 300 / 300 | **YES** (298/298) | clean |

Geometry detail (BTC convention, holds for ETH/SOL/BNB/HYPE/DOGE): bucket `[F, F+width)` pairs the
strike whose "≥ threshold" boundary = **F** (the YES@floor leg) and the strike whose boundary =
**F+width** (the NO@cap leg). Both strikes exist at every bucket edge because strike spacing == bucket
width and the grids share an origin.

**XRP is the exception:** its bucket floors sit on {…0.58, 0.60, 0.62…} but its strike thresholds sit
on {…0.59991, 0.61991…} — offset ~$0.00009 below each bucket floor. No strike lands exactly at a
bucket edge, so the 3-leg set has ~$0.0001-wide payoff holes/overlaps (pays $1 or $3 in a ~0.5%-probable
sliver each side). Not a clean $2 lock. Even if XRP flow grew, this must be fixed first.

**DOGE caveat:** its markets carry `strike_type: "custom"` with **`floor_strike`/`cap_strike` = null**;
the bounds live only in the ticker suffix (`-B0.012`) and the subtitle (`$0.01 to 0.0149999`). The
pilot's `discover_range_markets` reads `floor_strike`/`cap_strike` and would **drop every DOGE bucket**
— DOGE needs a subtitle/ticker bounds parser before it can even be discovered (see §4).

### 1b. Strike-only underlyings (no live hourly range → set NOT constructible today)

These run a live hourly STRIKE ladder but have **no alive hourly RANGE counterpart**, so the pump-fader
set cannot be assembled. Listed for completeness / future watch:

| underlying | strike series | exch | cadence | source |
|---|---|---|---|---|
| S&P 500 | KXINXU | 0 | market hours (:00, ends ~20:00Z) | Google Finance |
| Nasdaq-100 | KXNASDAQ100U | 0 | market hours | Google Finance |
| Dow (DJI) | KXDJI | 0 | market hours | Pyth |
| Nikkei (NKY) | KXNKY | 0 | Japan hours (dormant: last event 2026-09-18) | Pyth |
| Gold | KXGOLDH | 2 | commodity hours (:00..01:00Z sampled) | Pyth - Gold |
| Silver | KXSILVERH | 2 | commodity hours | Pyth - Silver |
| WTI crude | KXWTIH | 2 | commodity hours | Pyth - WTI |
| Palladium | KXPALLADIUMH | 2 | commodity hours | Pyth - Palladium |
| FX (USD*, *USD) | KX*USD*H / *AH | 0 | 24/5 | various |
| Temperature (12 cities) | KXTEMP*H, KXHIGHNYD | 0 | daily-ish directional | NWS |

### 1c. Dormant / no-settled-event-in-retention (cannot classify or pair from the API)

`KXNEAR/KXNEARD/KXNEARH`, `KXTON/KXTOND/KXTONH`, `KXZEC/KXZECD/KXZECH`, `KXSOL` (superseded by KXSOLE),
`KXRIPPLE` (superseded by KXXRP), `KXINXI`, `KXKR200`, `WTIH`, `NASDAQ100I` (last event 2025), `INXI`,
plus legacy FX duplicates (`GBPUSD` vs `KXGBPUSDH`, etc.). These returned no settled event inside
Kalshi's ~68-day retention this session, so they are dormant or too new — if any spins up later, re-run
the scan. NEAR/TON/ZEC each have BOTH a range and a directional series *registered*, so they are the
most likely future crypto pairs to watch.

---

## 2. Volume — the binding number

Method: for each alive pair pull `status=settled` markets over the trailing window (min_close_ts
bounded; coverage 157-328 hours per series depending on market count and the 45-page cap), sum contract
`volume_fp` per event. Then sample recent settled hours (BTC 24, others 12), pull 1-min candlesticks
for the **spot bucket** (the range market that settled YES — the one we'd rest our NO on) and its two
wings, and the spot-bucket trade tape (BTC 10 hours, others 6). Volume unit = contracts.

### 2a. Whole-series flow (all buckets / all strikes per hour)

| pair | range contracts/hr (mean / med / p90) | % of BTC range | strike contracts/hr (mean) | hours w/ any volume | spot-bucket share of range vol (med) |
|---|---|---|---|---|---|
| BTC | 38,493 / 34,322 / 59,383 | 100% | 1,686,385 | 100% | 30% |
| ETH | 8,407 / 4,489 / 11,396 | 21.8% | 63,070 | 99% | 32% |
| XRP | 1,126 / 546 / 2,044 | 2.9% | 4,947 | 100% | 46% |
| DOGE | 629 / 123 / 992 | 1.6% | 960 | 98% | 62% |
| SOL | 516 / 335 / 812 | 1.3% | 9,822 | 99% | 28% |
| HYPE | 505 / 354 / 942 | 1.3% | 2,689 | 100% | 46% |
| BNB | 344 / 101 / 247 | 0.9% | 355 | 100% | 57% |

The strike ladder is 3-50× more liquid than the range series on every pair (the directional market is
the popular one) — good news for the wing legs, irrelevant to the constraint. **The range (bucket)
series is what our resting order lives in, and it is thin everywhere but BTC and, distantly, ETH.**

### 2b. The quoting window — where fills actually come from (per sampled hour, median)

| pair | spot-bucket vol T-15..T-5 | as % BTC | spot-bucket vol T-5..T-0 | sweeps ≥20 lots (any side) | YES-side sweeps ≥20 | median print (lots) | max print | wing two-sided @T-5 (lo / hi) |
|---|---|---|---|---|---|---|---|---|
| BTC | 1,567 | 100% | (measured) | 22 | 19 | 13 | 1,672 | 100% / 96% |
| ETH | 168 | 10.7% | — | 2 | 2 | 8 | 1,614 | 92% / 92% |
| XRP | 45 | 2.9% | — | 0 | 0 | 5 | 388 | 100% / 100% |
| HYPE | 18 | 1.1% | — | 0 | 0 | 2 | 71 | 100% / 92% |
| DOGE | 15 | 1.0% | — | 0 | 0 | 1 | 353 | 100% / 100% |
| SOL | 4 | 0.3% | — | 0 | 0 | 2 | 25 | 100% / 92% |
| BNB | 1 | 0.06% | — | 0 | 0 | 2 | 17 | 100% / 100% |

Baseline reference from prior KXBTC studies: bucket taker flow ~4,000 lots/window in busy hours, prints
p50 ~15 lots, fills come from sweeps of 30-250 lots; wing depth +2c median ~2,800 lots. The table above
is consistent with that (BTC median print 13 lots; occasional 1,600+ sweeps).

### 2c. Per-pair verdict (numbers above → words)

- **BTC — supports the pump-fader (it is the live edge).** ~22 sweeps ≥20 lots per window, dense
  two-sided wings. Nothing here changes the live tree.
- **ETH — marginal; the only viable candidate for a second series.** Real trading (22% of BTC's range
  flow, occasional 1,600-lot sweeps), but the pump density is ~1/10 of BTC (~2 vs ~22 sweeps/window).
  Expect far fewer fills/day and long dry spells; would want its OWN frozen falsifier + shadow
  evaluation (weeks of dry data) before any arm. Do NOT assume BTC's fill rate carries over.
- **XRP — dead today AND geometrically broken.** 2.9% of BTC flow, zero median sweeps, and the strike
  grid doesn't sit at bucket edges (§1a). Two reasons not to touch it.
- **SOL, HYPE, DOGE, BNB — dead for the pump-fader today.** Spot-bucket quoting-window flow is
  single/low-double digits with zero ≥20-lot sweeps in the median hour. "No one's trading the market"
  is literally the case — the strategy has nothing to fade. Re-scan periodically; some are new and
  could grow.

---

## 3. Data plan — what to save, and how much

### 3a. What the pilot already records (the anchor for every estimate)

Two passive, order-free recorders, both order-free proxy WS taps writing `{idx, kind, local_ts, obj}`
JSONL (gz), one file per close:

- **Strike-ladder + 15M** (`pilot/service/record_window.py` / the armed `run_v32` tap →
  `pilot/journals/`): the ~188 KXBTCD strikes + the co-settling KXBTC15M market, `orderbook_delta` +
  `trade` (+ `fill`/`market_positions` when armed), last ~20 min of each hour. **Measured:** one 20-min
  window = 609,141 frames, **11.9 MB gz** (~174 MB raw). Of that, KXBTCD = 252,713 frames (1,344
  frames/market), the single hot KXBTC15M market = 355,877 frames. Memory-note figure ~19 MB gz/window,
  ~460 MB/day (busier hours + full hour).
- **Range buckets** (`pilot/service/record_range.py` → `pilot/journals_range/`): all ~180 KXBTC buckets
  (all widths/generations), `orderbook_delta` + `trade`, full hour, write-through `StreamJournal`
  (flat ~35-45 MB RSS). **Measured:** 188 buckets, quiet hour ≈ 230k records, ~75-80 MB raw, **~5 MB
  gz/hour** (~63 frames/s).

**Full BTC pair recording today ≈ 19 (strike+15M) + 5 (range) ≈ 24 MB gz/window ≈ ~580 MB/day.**

### 3b. Recorder scope for a new pair (a `RangeRecorder`+`record_window` clone per pair)

- **Subscribe:** every market of BOTH series for the target close — the full strike ladder
  (`KX<U>D`) and the full bucket ladder (`KX<U>`). The 15M leg is BTC-only (no other underlying has a
  KX*15M series alive), so a new pair records just the two hourly series.
- **Window:** match the live pilot — begin at the :40 wake (T-20) for the armed/shadow tap; the passive
  range recorder can take the **full hour** (T-60) as it does today, memory permitting.
- **Channels:** `orderbook_delta` + `trade` (skip `ticker` to cut volume, as record_range already does).
- **Store:** (i) raw frames exactly as today (byte-compatible with every existing reader), AND (ii) a
  **derived 1-second top-of-book + trade table per market** (ts, best bid/ask/size, last trade) — a
  10-50× smaller research store that answers "was there a two-sided quote / a sweep at T-k" without
  re-parsing the raw book. Build (ii) offline from the raw journal (a new `tools/extract_tob.py`
  generalizing the existing `historical-data/tob/` extractor), so the recorder itself stays a dumb tap.

### 3c. Size estimates (scaled from the measured BTC anchors)

Journal bytes scale with **market_count × per-market message rate**, and per-market rate tracks
liquidity — so the thin pairs are *cheaper* to record than BTC (fewer trades, sparser deltas; cost is
dominated by the initial `orderbook_snapshot` per market and reconnects). Rough per-window gz, per day
(24 windows), per 30 days:

| pair | markets (strike+bucket) | est. gz / window | est. / day | est. / 30 days |
|---|---|---|---|---|
| BTC (measured, incl. 15M) | 188 + 180 | ~24 MB | ~580 MB | ~17 GB |
| ETH | 300 + 300 | ~8-15 MB | ~200-360 MB | ~6-11 GB |
| SOL / HYPE | 300 + 300 | ~5-8 MB | ~120-190 MB | ~4-6 GB |
| XRP / DOGE / BNB | ~75 + ~75 | ~2-4 MB | ~50-100 MB | ~1.5-3 GB |
| **all 6 non-BTC together** | — | ~25-45 MB | ~0.7-1.1 GB | ~20-33 GB |

(These are order-of-magnitude; the honest way to pin them is to record one dry hour per pair and
measure, as was done for BTC.) The derived 1-s TOB+trades store is ~10-50× smaller than the raw, i.e.
the whole 6-pair set of TOB tables is well under 1 GB/month.

### 3d. Disk / retention implications

- **Laptop:** ~7.5 GB free today (per the RAM/box cleanup). Recording all 6 extra pairs at raw fidelity
  (~0.7-1.1 GB/day) fills that in ~7-10 days — **not sustainable** without pruning. The box also OOMs
  under concurrent heavy work (this scan itself was killed twice for memory), so **do not run 7 full
  pair recorders on the laptop.**
- **Render** (see `RENDER_MIGRATION_PLAN.md`): 20 GB persistent disk = ~40 days for BTC alone at
  today's ~460-580 MB/day; the full 7-pair raw set (~1.3-1.7 GB/day incl. BTC) would want a **40-80 GB
  disk** ($10-20/mo at $0.25/GB/mo) plus the nightly pull-and-prune job already sketched in that plan.
- **Recommended posture:** record **raw** only for the pair(s) under active evaluation (BTC live + at
  most one shadow pair, i.e. ETH); record the cheap **derived 1-s TOB+trades** table for any other pair
  we merely want to watch for a volume regime change. Kalshi REST retention (~68 days rolling, candles
  with bid/ask OHLC but no depth) remains the fallback census for pairs we are not taping live.

### 3e. WS connection limits (unconfirmed this session)

docs.kalshi.com was not reachable via WebFetch (SPA / 404). Observed in production: the pilot already
runs the ~188-strike ladder and ~180-bucket ladder on **separate WS connections** per close (a lagging
strike feed must not poison the bucket feed). So one pair ≈ 2 connections; 7 pairs ≈ 14 connections and
~2,500 market subscriptions concurrently. **Verify the per-connection market cap and per-account
concurrent-connection cap with Kalshi before running more than ~2 pairs at once** — this is an open
question (§5), not a solved number.

---

## 4. Code-change scope — hooking the pilot into another pair (estimate only; nothing built)

The pump-fader is BTC-hardcoded in a handful of named places. A clean port introduces a **per-pair
`SeriesAdapter`** (series prefixes + a strike-threshold/bounds parser + bucket width + settlement tag
grammar) and a **per-pair roster** (params JSON + sha + its own frozen falsifier + its own
ledger/report/shadow). The decision core (`decide_v32`) and executor are already series-agnostic once
they receive a bucket map + strike-floor map, so the port is mostly discovery, parsing, config, and
roster plumbing — **not a rewrite.**

**Every BTC-specific assumption found:**

1. **Series constants** — `record_range.RANGE_SERIES = "KXBTC"`, `v32/events.STRIKE_SERIES_PREFIX =
   "KXBTCD"`, `wake/sigma_feed.FIFTEEN_SERIES = "KXBTC15M"`. → adapter fields per pair. (15M is
   BTC-only; a new pair records/uses no 15M leg.)
2. **Ticker parsing** — `v32/events.parse_strike_ticker` hardcodes `STRIKE_SERIES_PREFIX + "-"` and
   `round(float(...) + 0.01)` (the **$0.01-tick `.99` convention**). This breaks on every non-cent-tick
   pair (SOL/XRP/HYPE tick $0.0001, DOGE $0.0000001). → the adapter must parse the strike **threshold
   from the subtitle** ("$X or above") or carry a per-series tick, not assume `+0.01`. (This scan
   already had to do exactly that — see `strike_threshold_of` / `bounds_of` in `multi_series_scan.py`.)
3. **Bucket discovery** — `record_range.discover_range_markets` + `run_v32.build_bucket_map` read
   `floor_strike`/`cap_strike` and DROP any bucket missing them. **DOGE's fields are null** (bounds in
   subtitle/ticker) → needs a subtitle bounds parser or DOGE is un-discoverable.
4. **Bucket width** — `policy/v32_params.json` `bucket_width: 100` (sha-pinned; $250/$500 hours stand
   down via `observed_bucket_width`). Each pair has its own width ($5, $0.25, $0.02, $0.005…) → **its
   own params JSON + sha**, and `observed_bucket_width` must compute from floor-to-floor gaps (a
   `cap-floor+0.01` shortcut bakes in the BTC tick — same bug this scan hit).
5. **Wing (Sd/Su) derivation** — the core keys strikes by an int floor and pairs `Su = Sd +
   bucket_width`. Fine once width is right and the strike floor map uses the correct threshold parse.
6. **Executor prefixes** — `v32/executor._KXBTC_PREFIX = "KXBTC"` (scopes the startup stray-order
   **sweep**; a new prefix or the sweep misses a leaked rest) and `service/executor.ExecutorConfig
   .ticker_prefixes = ("KXBTC15M","KXBTCD")` (client-side cap). → per-pair prefixes.
7. **COID namespace** — `v32/core._mint_coid` → `v32-{close_time}-{seq}` is **not series-tagged**. Two
   pairs' coids would collide in the by-shard sweep. → namespace per pair, e.g. `v32-eth-{close}-{seq}`.
8. **Proxy allowlist** — `.env ORDER_TICKER_PREFIXES` (Brad's lever; currently `KXBTC15M,KXBTCD,KXBTC`;
   `KXBTC` prefix-matches `KXBTCD`). A new pair needs its prefix added (e.g. `KXETH` covers `KXETHD`),
   plus `DAILY_ORDER_BUDGET` head-room (continuous requote ≈ 80-100 replaces/window/pair).
9. **Falsifier is frozen PER roster** — `ceremony/v32_falsifier.md` + `v32/falsifier_pins.py` are the
   BTC roster `DegeneracyV3_2`. A new pair = a **new roster, its own frozen falsifier, its own shadow
   window and capture-ratio gate**, its own ledger/report partition (the box report already partitions
   by roster — same pattern).
10. **Process model** — one `run_v32` process per pair, OR a multi-pair driver spawning N per-pair
    windows. Given the box's RAM ceiling and the "never two armed boxes" rule, **one process per pair,
    staggered, is the safe default**; a shared driver is a later optimization.

**Size estimate:** a `SeriesAdapter` abstraction + threshold/bounds parser + per-pair params/roster/
falsifier + sweep/coid namespacing + tests ≈ **~500-800 lines across ~3-4 PRs** (adapter+parser;
per-pair params+roster+falsifier; executor sweep/coid; recorder generalization), each Opus 4.8
build+review, Brad merges. Behaviour-neutral for the BTC live tree until a new roster is armed. **No
new-pair arming without its own weeks-long shadow first** — and on today's volume that shadow is only
worth running for **ETH**.

---

## 5. Open questions

1. **Is ETH worth a shadow?** ~1/10 BTC's pump density. A dry `run_v32` on KXETH/KXETHD (recording +
   ideal-shadow, no orders) for 2-4 weeks would measure the real fill rate and capture ratio before any
   code investment beyond the adapter. Brad's call.
2. **WS limits.** Exact per-connection market cap and per-account concurrent-connection cap — not
   confirmed from docs this session. Needed before running >2 pairs concurrently.
3. **Fee schedule per series.** The exact-fee law (`0.07·p·(1-p)·count`, maker 0) was validated on
   crypto (KXBTC). Confirm the same `fee_multiplier`/`fee_type` on KXETH etc. from `/series/{ticker}`
   before trusting the money math (`series_all.json` carries `fee_multiplier`/`fee_type` per series —
   a cheap check, not done here).
4. **Volume regime drift.** June-July had ~3× September's BTC pump traffic; the thin altcoins today
   could wake up (or BTC's own flow keeps thinning). Re-run `multi_series_scan.py` monthly — it is
   cache-cheap and re-runnable.
5. **XRP geometry.** Is the strike/bucket grid offset a permanent Kalshi design or a transient? If it
   ever aligns, XRP re-enters the candidate set (but only if its volume also grows ~30×).
6. **DOGE null fields.** Worth a one-line note to Kalshi? Every other range series populates
   `floor_strike`/`cap_strike`; DOGE alone leaves them null, forcing a subtitle parse.

---

## Files

- Scan: `pilot/build/mc/multi_series_scan.py` (+ `multi_series_scan.out`, `multi_series_scan.json`).
- This plan: `pilot/ops/MULTI_SERIES_DATA_PLAN.md`.
- Anchored on: `pilot/service/record_range.py`, `record_window.py`, `run_v32.py`, `v32/events.py`,
  `v32/executor.py`, `v32/falsifier_pins.py`, `policy/v32_params.json`, `tools/fetch_history.py`,
  `pilot/ops/RENDER_MIGRATION_PLAN.md`, and a measured 20-min journal (`20260905T170000Z`).
