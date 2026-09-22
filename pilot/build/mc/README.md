# pilot/build/mc — V3.2 research scripts (2026-09-15/16)

Scratchpad scripts and their console outputs, committed so they survive Temp cleanup. Read-only inputs: live ms journals
(`pilot/journals_v32/*.jsonl.gz`), `historical-data/tob` + range candles (forward 8/30-9/04), read-only proxy GETs for market strikes/results.
Money math is the pinned law shape (exact taker fee, maker fee 0).

| file | question | verdict |
|---|---|---|
| `v32_mc.py` / `v32_mc_html.py` / `v32_mc_results.json` / `v32_mc.out` | Monte Carlo from $52 (PR #49) | see `V32_MC_2026-09-15.md` |
| `v32_capacity_scan.py` / `.out` | spot-bucket taker flow + print sizes (its wing-depth half is superseded) | ~4,000 lots/window; prints p50 15, max 499 |
| `v32_wing_depth_scan.py` / `.out` | wing ask depth within +0/1/2/5c (BookMirror) | +2c median 2,800, p10 1,000 |
| `v32_book_owner_ceiling.py` / `.out` | ladder at every profitable level, unlimited size | $300-700/day ceiling; lower E adds no hours |
| `pf_15m_gap_wing.py` / `.out` | 15M as the OUTSIDE wing ($3 gap): price vs gap value | fair-priced lottery, +15.6c cost vs 13.8% hit |
| `pf_15m_enterable.py` / `.out` | can the 15M-wing set be entered under $2? | 0.4-0.8% of minutes vs 5.1% for hourly wings |
| `pf_15m_sub1.py` / `.out` | sub-$1 15M box on the tape | 6-13% of minutes under $1 = stale-leg artifacts |
| `v32_sub1_ms.py` / `.out` | sub-$1 box on ms books (all legs live) | taker never under $1; maker 1-3c flickers |
| `v32_sub1_grinder.py` / `.out` | maker-bucket grinder with real fills | 9 fills, -6c total, $2 zone 0 of 9 |
| `pf_dry_spells.py` / `.out` | dry spells between pump-fader fills on the 50-day corpus (fill timing, stale model) | E=15: longest 47 h, 1 run >=36 h; E=20: longest 76 h, 7 runs; pumps/week fell ~3x June -> Sept |
| `probe_size2_partial.py` | does the pre-#62 core orphan the second lot on a 1-of-2 fill? | yes (rest_live nulled, later fill ignored) -> PR #62 |
| `multi_series_scan.py` / `.out` / `.json` | which Kalshi hourly underlyings run BOTH a range + strike series, geometry OK, and enough bucket volume for the pump-fader? (Brad 2026-09-22) | 7 crypto pairs only (BTC/ETH/SOL/XRP/DOGE/BNB/HYPE; all CF Benchmarks, 24/7); no index/commodity/FX has a live hourly range. Bucket flow: BTC 100%, ETH 22% (marginal), rest 0.9-2.9% = dead (0 median sweeps). XRP geometry misaligned; DOGE fields null. Wings liquid everywhere. See `pilot/ops/MULTI_SERIES_DATA_PLAN.md` |
