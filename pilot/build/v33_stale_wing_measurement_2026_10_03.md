# V3.3 gate F -- stale-wing measurement (2026-10-03)

Tool: `pilot/build/v33_stale_wing_measure.py` (stdlib only), run over every V3.3 journal closing
2026-09-30 .. 2026-10-03 (85 windows; 10-03 19:00Z is the last). Command:

```
python build/v33_stale_wing_measure.py --journals <journals_v33> --start 2026-09-30 --end 2026-10-03     --feed-dead-s 4.0 --wing-max-age-s 30.0
```

## What each column is

- `hold/resume/cancel`: journaled `stand_down_hold` / `stand_down_resume` / `stand_down_cancel` records.
- `wd_strk`: `watchdog_stale` alarms on the strikes connection. `resub`: strike-snapshot re-subscribe
  bursts inside the quote window. WS close/reconnect is NOT journaled as its own kind; these two are the
  only reconnect evidence in the journals.
- `feed gap`: gap between consecutive KXBTCD `orderbook_delta` server timestamps, T-900..T-300.
- `feed age`: `now - strike_feed_ts` at every core evaluation, where `now` is the driver's monotone eval
  clock (max server ts over driven frames, plus a ClockTick every 0.5 s of wall time). This is the value
  `strike_feed_dead_s` is compared against. Unlike the gap, it catches a LAGGING strike connection:
  frames still arrive, but their own timestamps run behind the bucket connection.
- `wing gap`: inter-delta gap on the ladder bucket's two wing strikes. The yes-leg is T(Sd-0.01) and
  the no-leg is T(Su-0.01), taken from the `place_rest` / `would_place_rest` ticker. `wing age`: the
  wing book's age sampled every 0.5 s, so it is uniform in time.
- `old-replay`: the hold state machine replayed under the pre-fix predicate (a wing strike's own delta
  is more than 1.0 s old). `to-SD` stops at the journaled terminal stand-down. This replay reproduces
  the journaled counts, for example 02:00Z 5/1, 22:00Z 39/0, 23:00Z 36/0, 03:00Z 12/0 and 10-02 15:00Z
  3/3. `full` runs the whole quote window as if nothing had stood down.
- `new-replay`: the new predicate, which fires when a wing book is missing, when the strike feed age
  exceeds 4.0 s, or when a wing book is older than 30.0 s. `suspect` is not journaled, so it cannot be
  replayed.

## Table (feed-dead 4.0 s, wing bound 30.0 s)

| window | hold | resume | cancel | wd_strk | resub | feed gap max | feed gap p99.9 | feed age max | feed age p99.9 | wing gap max | wing gap p99 | wing age p99/max | old-replay to-SD hold/cancel | old-replay full | new-replay full hold/cancel |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 20260930T000000Z | 1 | 1 | 0 | 0 | 0 | 0.55 | 0.12 | 0.50 | 0.19 | 1.05 | 0.15 | 0.55/1.00 | 1/0 | 1/0 | 0/0 |
| 20260930T010000Z | 0 | 0 | 0 | 0 | 0 | 0.29 | 0.08 | 0.37 | 0.29 | 0.99 | 0.12 | 0.44/0.83 | 0/0 | 0/0 | 0/0 |
| 20260930T020000Z | 0 | 0 | 0 | 0 | 0 | 0.24 | 0.08 | 0.24 | 0.15 | 0.98 | 0.11 | 0.38/0.89 | 0/0 | 0/0 | 0/0 |
| 20260930T030000Z | 2 | 2 | 0 | 0 | 0 | 0.31 | 0.09 | 0.31 | 0.26 | 1.58 | 0.13 | 0.46/1.23 | 2/0 | 2/0 | 0/0 |
| 20260930T040000Z | 2 | 2 | 0 | 0 | 0 | 0.42 | 0.10 | 0.41 | 0.18 | 1.36 | 0.15 | 0.49/1.18 | 2/0 | 2/0 | 0/0 |
| 20260930T050000Z | 5 | 5 | 0 | 0 | 0 | 0.40 | 0.12 | 0.39 | 0.19 | 1.81 | 0.20 | 0.64/1.58 | 5/0 | 5/0 | 0/0 |
| 20260930T060000Z | 14 | 14 | 0 | 0 | 0 | 0.31 | 0.13 | 0.30 | 0.20 | 1.78 | 0.19 | 0.72/1.72 | 13/0 | 13/0 | 0/0 |
| 20260930T070000Z | 4 | 4 | 0 | 0 | 0 | 0.35 | 0.12 | 0.35 | 0.13 | 1.54 | 0.18 | 0.47/1.18 | 4/0 | 4/0 | 0/0 |
| 20260930T080000Z | 11 | 10 | 1 | 0 | 0 | 0.40 | 0.13 | 0.37 | 0.17 | 2.59 | 0.22 | 0.63/2.41 | 11/1 | 11/1 | 0/0 |
| 20260930T090000Z | 9 | 9 | 0 | 0 | 0 | 0.35 | 0.13 | 0.34 | 0.22 | 1.39 | 0.16 | 0.68/1.12 | 8/0 | 8/0 | 0/0 |
| 20260930T100000Z | 5 | 5 | 0 | 0 | 0 | 0.39 | 0.13 | 0.37 | 0.19 | 1.54 | 0.19 | 0.54/1.15 | 5/0 | 5/0 | 0/0 |
| 20260930T110000Z | 20 | 20 | 0 | 0 | 0 | 0.38 | 0.15 | 0.37 | 0.15 | 2.10 | 0.22 | 0.93/1.79 | 20/0 | 20/0 | 0/0 |
| 20260930T120000Z | 0 | 0 | 0 | 0 | 0 | 0.30 | 0.10 | 0.29 | 0.18 | 0.97 | 0.13 | 0.43/0.85 | 0/0 | 0/0 | 0/0 |
| 20260930T130000Z | 42 | 22 | 19 | 4 | 4 | 38.09 | 0.03 | 31.08 | 23.80 | 38.12 | 0.07 | 31.08/43.15 | 48/19 | 96/36 | 46/39 |
| 20260930T140000Z | 84 | 64 | 19 | 2 | 4 | 51.39 | 0.03 | 31.56 | 5.24 | 51.66 | 0.09 | 51.91/57.91 | 75/18 | 149/34 | 39/34 |
| 20260930T150000Z | 76 | 47 | 29 | 1 | 2 | 41.98 | 0.03 | 41.96 | 17.26 | 42.10 | 0.07 | 14.24/30.96 | 123/34 | 123/34 | 74/49 |
| 20260930T160000Z | 65 | 30 | 35 | 0 | 0 | 0.21 | 0.05 | 12.76 | 12.10 | 0.97 | 0.09 | 9.35/12.76 | 122/42 | 122/42 | 69/57 |
| 20260930T170000Z | 50 | 47 | 3 | 0 | 0 | 0.19 | 0.06 | 8.45 | 4.03 | 0.88 | 0.10 | 7.03/8.45 | 96/9 | 96/9 | 8/7 |
| 20260930T180000Z | 37 | 35 | 2 | 0 | 0 | 0.25 | 0.07 | 9.78 | 2.42 | 0.96 | 0.10 | 7.79/9.81 | 75/8 | 75/8 | 4/4 |
| 20260930T190000Z | 45 | 30 | 15 | 0 | 2 | 24.61 | 0.08 | 17.59 | 6.06 | 24.71 | 0.12 | 9.31/10.08 | 71/21 | 130/37 | 32/29 |
| 20260930T200000Z | 37 | 25 | 11 | 0 | 0 | 0.36 | 0.08 | 9.69 | 3.46 | 1.57 | 0.12 | 9.20/9.84 | 101/19 | 102/20 | 19/19 |
| 20260930T220000Z | 63 | 51 | 12 | 1 | 1 | 119.15 | 0.21 | 74.95 | 72.13 | 119.45 | 0.40 | 9.24/119.22 | 92/21 | 128/27 | 24/22 |
| 20260930T230000Z | 9 | 9 | 0 | 0 | 0 | 0.42 | 0.15 | 0.37 | 0.15 | 1.79 | 0.21 | 0.63/1.76 | 9/0 | 9/0 | 0/0 |
| 20261001T000000Z | 3 | 3 | 0 | 0 | 0 | 0.38 | 0.11 | 0.30 | 0.20 | 1.75 | 0.16 | 0.49/1.33 | 3/0 | 3/0 | 0/0 |
| 20261001T010000Z | 2 | 2 | 0 | 0 | 0 | 0.34 | 0.14 | 0.34 | 0.17 | 1.28 | 0.20 | 0.56/1.05 | 3/0 | 3/0 | 0/0 |
| 20261001T020000Z | 19 | 19 | 0 | 0 | 0 | 0.33 | 0.14 | 0.32 | 0.15 | 1.86 | 0.21 | 0.79/1.61 | 20/0 | 20/0 | 0/0 |
| 20261001T030000Z | 12 | 12 | 0 | 0 | 0 | 0.34 | 0.15 | 0.33 | 0.14 | 1.83 | 0.24 | 0.66/1.65 | 12/0 | 12/0 | 0/0 |
| 20261001T040000Z | 1 | 1 | 0 | 0 | 0 | 0.29 | 0.10 | 0.27 | 0.13 | 1.28 | 0.16 | 0.46/1.06 | 1/0 | 1/0 | 0/0 |
| 20261001T050000Z | 0 | 0 | 0 | 0 | 0 | 0.27 | 0.08 | 0.48 | 0.18 | 0.89 | 0.11 | 0.33/0.79 | 0/0 | 0/0 | 0/0 |
| 20261001T100000Z | 3 | 3 | 0 | 0 | 0 | 0.52 | 0.14 | 0.53 | 0.17 | 2.52 | 0.18 | 0.59/2.21 | 3/0 | 3/0 | 0/0 |
| 20261001T110000Z | 3 | 3 | 0 | 0 | 0 | 0.44 | 0.11 | 0.36 | 0.14 | 1.19 | 0.15 | 0.47/1.06 | 3/0 | 3/0 | 0/0 |
| 20261001T120000Z | 5 | 5 | 0 | 0 | 0 | 0.37 | 0.11 | 0.30 | 0.13 | 1.19 | 0.15 | 0.45/0.95 | 5/0 | 5/0 | 0/0 |
| 20261001T130000Z | 6 | 6 | 0 | 0 | 0 | 0.39 | 0.11 | 0.33 | 0.20 | 1.25 | 0.18 | 0.55/1.03 | 6/0 | 6/0 | 0/0 |
| 20261001T140000Z | 41 | 18 | 23 | 16 | 10 | 53.36 | 0.02 | 44.75 | 40.44 | 53.64 | 0.05 | 35.26/44.10 | 0/0 | 44/24 | 41/18 |
| 20261001T150000Z | 45 | 27 | 18 | 9 | 7 | 53.65 | 0.02 | 53.58 | 48.00 | 53.78 | 0.05 | 37.79/51.95 | 46/18 | 46/18 | 19/10 |
| 20261001T160000Z | 10 | 10 | 0 | 0 | 0 | 0.11 | 0.03 | 1.40 | 1.08 | 0.66 | 0.05 | 0.66/1.11 | 10/0 | 10/0 | 0/0 |
| 20261001T170000Z | 0 | 0 | 0 | 0 | 0 | 0.20 | 0.05 | 0.48 | 0.42 | 0.70 | 0.08 | 0.26/0.53 | 0/0 | 0/0 | 0/0 |
| 20261001T180000Z | 0 | 0 | 0 | 0 | 0 | 0.22 | 0.06 | 0.36 | 0.29 | 0.96 | 0.12 | 0.46/0.77 | 0/0 | 0/0 | 0/0 |
| 20261001T190000Z | 3 | 3 | 0 | 0 | 0 | 0.28 | 0.07 | 0.99 | 0.61 | 1.35 | 0.13 | 0.44/1.02 | 3/0 | 3/0 | 0/0 |
| 20261001T200000Z | 0 | 0 | 0 | 0 | 0 | 0.32 | 0.10 | 0.24 | 0.14 | 0.93 | 0.14 | 0.43/0.73 | 0/0 | 0/0 | 0/0 |
| 20261001T220000Z | 18 | 18 | 0 | 0 | 0 | 0.43 | 0.15 | 0.42 | 0.19 | 1.73 | 0.23 | 0.80/1.50 | 19/0 | 19/0 | 0/0 |
| 20261001T230000Z | 2 | 2 | 0 | 0 | 0 | 0.33 | 0.11 | 0.27 | 0.19 | 1.23 | 0.21 | 0.53/0.85 | 1/0 | 1/0 | 0/0 |
| 20261002T000000Z | 3 | 3 | 0 | 0 | 0 | 0.24 | 0.10 | 0.52 | 0.37 | 1.39 | 0.16 | 0.54/1.37 | 4/0 | 4/0 | 0/0 |
| 20261002T010000Z | 1 | 1 | 0 | 0 | 0 | 0.29 | 0.08 | 0.27 | 0.22 | 1.39 | 0.14 | 0.43/0.99 | 1/0 | 1/0 | 0/0 |
| 20261002T020000Z | 0 | 0 | 0 | 0 | 0 | 0.18 | 0.07 | 0.26 | 0.18 | 4.34 | 0.23 | 2.05/3.99 | 47/9 | 47/9 | 0/0 |
| 20261002T030000Z | 0 | 0 | 0 | 0 | 0 | 0.27 | 0.05 | 0.64 | 0.33 | 0.88 | 0.11 | 0.37/0.78 | 0/0 | 0/0 | 0/0 |
| 20261002T040000Z | 0 | 0 | 0 | 0 | 0 | 0.23 | 0.07 | 2.96 | 2.44 | 0.86 | 0.13 | 0.51/2.84 | 2/2 | 2/2 | 0/0 |
| 20261002T050000Z | 0 | 0 | 0 | 0 | 0 | 0.16 | 0.04 | 0.82 | 0.61 | 0.97 | 0.08 | 0.26/0.65 | 0/0 | 0/0 | 0/0 |
| 20261002T060000Z | 0 | 0 | 0 | 0 | 0 | 0.24 | 0.07 | 0.35 | 0.25 | 0.96 | 0.13 | 0.42/0.92 | 0/0 | 0/0 | 0/0 |
| 20261002T070000Z | 1 | 1 | 0 | 0 | 0 | 0.28 | 0.09 | 0.25 | 0.15 | 1.16 | 0.15 | 0.45/0.97 | 1/0 | 1/0 | 0/0 |
| 20261002T080000Z | 0 | 0 | 0 | 0 | 0 | 0.21 | 0.07 | 0.42 | 0.26 | 0.72 | 0.10 | 0.35/0.67 | 0/0 | 0/0 | 0/0 |
| 20261002T090000Z | 5 | 5 | 0 | 0 | 0 | 0.30 | 0.09 | 0.32 | 0.18 | 1.40 | 0.17 | 0.51/1.11 | 5/0 | 5/0 | 0/0 |
| 20261002T100000Z | 2 | 2 | 0 | 0 | 0 | 0.27 | 0.08 | 0.50 | 0.44 | 1.77 | 0.12 | 0.40/1.47 | 2/0 | 2/0 | 0/0 |
| 20261002T110000Z | 2 | 2 | 0 | 0 | 0 | 0.39 | 0.10 | 0.51 | 0.41 | 1.63 | 0.16 | 0.48/1.59 | 2/0 | 2/0 | 0/0 |
| 20261002T120000Z | 6 | 6 | 0 | 0 | 0 | 0.25 | 0.08 | 2.38 | 1.72 | 1.86 | 0.12 | 0.70/2.15 | 6/0 | 6/0 | 0/0 |
| 20261002T130000Z | 0 | 0 | 0 | 0 | 0 | 0.16 | 0.03 | 0.72 | 0.55 | 0.74 | 0.07 | 0.35/0.73 | 0/0 | 0/0 | 0/0 |
| 20261002T140000Z | 64 | 64 | 0 | 0 | 0 | 0.13 | 0.03 | 2.31 | 1.98 | 0.68 | 0.05 | 1.26/1.97 | 64/0 | 64/0 | 0/0 |
| 20261002T150000Z | 3 | 0 | 3 | 10 | 7 | 46.19 | 0.03 | 46.25 | 44.25 | 46.28 | 0.09 | 40.19/46.25 | 3/3 | 68/27 | 20/11 |
| 20261002T160000Z | 1 | 1 | 0 | 0 | 0 | 0.14 | 0.04 | 1.05 | 0.78 | 0.56 | 0.07 | 0.33/0.77 | 1/0 | 1/0 | 0/0 |
| 20261002T170000Z | 0 | 0 | 0 | 0 | 0 | 0.15 | 0.05 | 0.53 | 0.43 | 0.60 | 0.08 | 0.25/0.47 | 0/0 | 0/0 | 0/0 |
| 20261002T180000Z | 1 | 1 | 0 | 0 | 0 | 0.30 | 0.09 | 0.46 | 0.37 | 1.11 | 0.15 | 0.42/0.88 | 1/0 | 1/0 | 0/0 |
| 20261002T190000Z | 0 | 0 | 0 | 0 | 0 | 0.20 | 0.05 | 0.40 | 0.23 | 0.66 | 0.10 | 0.28/0.56 | 0/0 | 0/0 | 0/0 |
| 20261002T200000Z | 0 | 0 | 0 | 0 | 0 | 0.29 | 0.10 | 0.35 | 0.25 | 0.95 | 0.15 | 0.32/0.80 | 0/0 | 0/0 | 0/0 |
| 20261002T220000Z | 39 | 39 | 0 | 0 | 0 | 0.58 | 0.21 | 0.58 | 0.39 | 2.30 | 0.38 | 1.01/2.01 | 39/0 | 39/0 | 0/0 |
| 20261002T230000Z | 36 | 36 | 0 | 0 | 0 | 0.45 | 0.22 | 0.45 | 0.22 | 2.41 | 0.38 | 1.00/1.95 | 36/0 | 36/0 | 0/0 |
| 20261003T000000Z | 21 | 21 | 0 | 0 | 0 | 0.90 | 0.30 | 0.75 | 0.30 | 2.00 | 0.51 | 1.14/1.96 | 21/0 | 51/0 | 0/0 |
| 20261003T010000Z | 16 | 16 | 0 | 0 | 0 | 0.38 | 0.21 | 0.37 | 0.23 | 2.74 | 0.37 | 0.85/2.54 | 2/1 | 21/1 | 0/0 |
| 20261003T020000Z | 5 | 4 | 1 | 0 | 0 | 0.59 | 0.28 | 0.55 | 0.29 | 4.11 | 0.48 | 1.44/3.97 | 5/1 | 47/5 | 0/0 |
| 20261003T030000Z | 12 | 12 | 0 | 0 | 0 | 0.51 | 0.23 | 0.48 | 0.22 | 1.69 | 0.43 | 0.90/1.55 | 12/0 | 28/0 | 0/0 |
| 20261003T040000Z | 14 | 13 | 1 | 0 | 0 | 0.81 | 0.43 | 0.75 | 0.47 | 3.32 | 0.87 | 1.74/3.19 | 119/6 | 119/6 | 0/0 |
| 20261003T050000Z | 42 | 41 | 1 | 0 | 0 | 0.58 | 0.24 | 0.57 | 0.31 | 2.89 | 0.42 | 1.50/2.73 | 63/1 | 63/1 | 0/0 |
| 20261003T060000Z | 67 | 59 | 8 | 0 | 0 | 0.72 | 0.34 | 0.71 | 0.48 | 7.12 | 0.70 | 2.52/6.86 | 50/5 | 123/17 | 0/0 |
| 20261003T070000Z | 44 | 42 | 2 | 0 | 0 | 0.66 | 0.27 | 0.56 | 0.26 | 2.91 | 0.42 | 1.29/2.51 | 44/2 | 44/2 | 0/0 |
| 20261003T080000Z | 71 | 61 | 10 | 0 | 0 | 0.43 | 0.22 | 0.37 | 0.21 | 5.68 | 0.32 | 2.05/5.30 | 58/9 | 63/10 | 0/0 |
| 20261003T090000Z | 81 | 70 | 10 | 0 | 0 | 0.89 | 0.38 | 0.63 | 0.48 | 7.15 | 0.69 | 2.70/6.94 | 76/8 | 151/31 | 0/0 |
| 20261003T100000Z | 99 | 72 | 27 | 0 | 0 | 0.79 | 0.37 | 0.66 | 0.33 | 11.13 | 0.75 | 5.54/11.08 | 182/62 | 182/62 | 0/0 |
| 20261003T110000Z | 140 | 110 | 30 | 0 | 0 | 0.60 | 0.31 | 0.59 | 0.32 | 13.61 | 0.56 | 5.77/13.27 | 137/30 | 170/48 | 0/0 |
| 20261003T120000Z | 20 | 17 | 3 | 0 | 0 | 0.97 | 0.44 | 0.88 | 0.46 | 13.80 | 0.76 | 8.84/13.72 | 0/0 | 117/42 | 0/0 |
| 20261003T130000Z | 74 | 70 | 4 | 0 | 0 | 0.47 | 0.25 | 0.40 | 0.27 | 3.32 | 0.45 | 1.60/3.11 | 69/4 | 76/4 | 0/0 |
| 20261003T140000Z | 45 | 42 | 3 | 0 | 0 | 0.52 | 0.28 | 0.45 | 0.27 | 3.00 | 0.47 | 1.33/2.72 | 37/1 | 51/2 | 0/0 |
| 20261003T150000Z | 27 | 26 | 1 | 0 | 0 | 0.59 | 0.25 | 0.55 | 0.25 | 3.14 | 0.37 | 1.30/2.92 | 27/1 | 51/3 | 0/0 |
| 20261003T160000Z | 4 | 4 | 0 | 0 | 0 | 0.61 | 0.27 | 0.67 | 0.61 | 2.44 | 0.43 | 1.06/2.06 | 2/0 | 32/0 | 0/0 |
| 20261003T170000Z | 11 | 10 | 0 | 0 | 0 | 0.69 | 0.36 | 0.57 | 0.34 | 4.70 | 0.63 | 1.93/4.38 | 1/0 | 81/5 | 0/0 |
| 20261003T180000Z | 7 | 7 | 0 | 0 | 0 | 0.52 | 0.19 | 0.52 | 0.22 | 1.81 | 0.27 | 0.72/1.63 | 13/0 | 13/0 | 0/0 |
| 20261003T190000Z | 29 | 26 | 3 | 0 | 0 | 0.61 | 0.30 | 0.57 | 0.28 | 7.16 | 0.54 | 3.75/7.02 | 9/0 | 98/22 | 0/0 |

## Summary

```
windows: 85  healthy (no strike watchdog, no re-subscribe): 77
healthy strike feed AGE (eval clock): n=24003771 p99=1.91 p99.9=9.20 p99.99=11.62 max=12.76
healthy strike inter-frame GAP: n=19480445 p99.9=0.13 max=0.97
wing strike inter-delta GAP (all windows): n=10767145 p50=0.00 p99=0.16 p99.9=0.54 max=119.45
healthy wing book AGE (time-uniform, 0.5 s ticks): n=184690 p99=3.50 p99.9=8.34 p99.99=11.15 max=13.72
journaled holds total=1747 cancels=294; old-replay holds=3140 cancels=589; new-replay holds=395 cancels=299
```

The same run also swept `strike_feed_dead_s` (wing bound 15 s; the hold machine is path-dependent,
so the counts do not fall monotonically):

```
sweep strike_feed_dead_s=1.0: holds=1166 cancels=307 windows_with_holds=17
sweep strike_feed_dead_s=1.5: holds=869 cancels=305 windows_with_holds=15
sweep strike_feed_dead_s=2.0: holds=642 cancels=297 windows_with_holds=15
sweep strike_feed_dead_s=2.5: holds=411 cancels=298 windows_with_holds=13
sweep strike_feed_dead_s=3.0: holds=469 cancels=329 windows_with_holds=12
sweep strike_feed_dead_s=4.0: holds=397 cancels=299 windows_with_holds=12
sweep strike_feed_dead_s=5.0: holds=359 cancels=286 windows_with_holds=12
sweep strike_feed_dead_s=10.0: holds=83 cancels=46 windows_with_holds=9
```

## Choosing the defaults

**`strike_feed_dead_s = 4.0`.** The windows fall into two groups:

- **Live feed (73 windows).** The strike inter-frame gap stays small: p99.9 0.13 s, and the largest
  single gap is 0.97 s. The eval-clock feed age reaches at most 2.96 s (10-02 04:00Z), 2.38 s
  (10-02 12:00Z) and 2.31 s (10-02 14:00Z). Those three are short strike-connection lags of about
  2-3 s with no gap. Every other live window stays under 1.4 s.
- **Real stall or lag (12 windows).** These are 09-30 13:00-22:00Z, 10-01 14:00/15:00Z and
  10-02 15:00Z. In all of them the feed age reaches 8-75 s: either the watchdog fired and the stream
  re-subscribed, or the strike connection ran 8-12 s behind with no watchdog alarm (09-30
  16:00-20:00Z).

The spec rule was "above the p99.9 healthy inter-frame gap". On the gap alone that allows anything
from about 1 s up. But the bound is compared against the eval-clock AGE, which includes lag, so it
also has to sit above the 2.96 s maximum seen in the live-feed windows. 3.0 s would clear that
maximum by only 0.04 s. 4.0 s clears it by 35% and still sits under every stall or lag window's
maximum: the smallest stall-window maximum is 8.45 s and their p99.9 values are 2.4-72 s. At 4.0 s,
none of the 73 live-feed windows holds, and all 12 stall/lag windows still hold and cancel. The
cancel total matches the journaled number: 299 against 294. The sweep shows 2.0 s would add holds at
10-02 04:00, 12:00 and 14:00Z, and none of those holds would have reached a cancel.

**`wing_book_max_age_s = 30.0`.** The p99 of the inter-delta gap on the wing strikes is 0.16 s, which
says nothing useful because the busy yes-leg dominates it. What matters is the longest quiet stretch
on a LIVE feed. Those reach 13.8 s (10-03 12:00Z, with feed age max 0.88 s), 13.6 s (11:00Z),
11.1 s (10:00Z) and about 7 s (06:00, 09:00, 19:00Z). The time-uniform wing-age p99.99 across
watchdog-free windows is 11.15 s (max 13.72 s). This bound gates the rest AND the take: a take
refused here leaves the fill naked until the next delta on that book, which is the 02:00Z failure
mode. So it must sit well clear of real quiet. 30 s is about 2.2x the largest quiet observed on a
live feed. Feed death is caught separately, by `strike_feed_dead_s`.

## Golden replays (pilot/tests/test_v33_stale_wing_liveness.py)

- **2026-10-03 02:00Z** (fixture T-905..T-600, 5,391 thinned events): under the new predicate the
  ladder rests once, with zero holds, zero cancels and no stand-down after the first place. Over the
  full journal the new-replay count is 0/0 and the old replay is 47/5. Under the old 1.0 s predicate
  the same fixture holds at least 3 times. In that control, if the cancel-all is reverted to untracked
  (a mutation check), the harness fails with "PLACE_REST while the venue still holds 11 of our rests".
  That failure is the incident itself. With the gate-D tracked cancel, the control passes.
- **2026-10-02 15:00Z** (the real strike-feed stall/lag; fixture T-905..T-840): the new core still
  holds and cancels (stand_down_hold plus stand_down_cancel, 11 or more CANCEL_REST). Over the full
  journal the new-replay count is 20/11.

## Caveats

- The `suspect` flag (seq-gap or malformed delta) is not journaled, so suspect-driven holds cannot be
  replayed. The unit tests cover it instead.
- The replay assumes the ladder is live from the first place onward, re-placed on each healthy tick.
  It models no other no-quote reasons (stale bucket, n_below_min). The OLD replay matching the
  journaled counts is what validates this simplification.
