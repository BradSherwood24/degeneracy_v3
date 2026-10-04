"""Early-exit study: once a set (bucket NO + YES@Sd + NO@Su) is held, what would unwinding it as a taker fetch?

Per journal window that held sets: rebuild the three books from the WS tape, and from the first rest fill onward
track, at every book change on those three markets:
  top   = noBid(bucket) + yesBid(Sd) + noBid(Su)            executable top-of-book unwind per set
  net   = top - 2 - 3 taker fees (0.07*p*(1-p) per leg, ceil to 1e-4)
  vwap  = same, but walking depth to unwind ALL lots held at that instant (None if depth short)
  mark  = last-trade prices summed (what a UI mark looks like) - 2
Outputs a one-line summary + optional per-second series CSV.
"""
from __future__ import annotations

import csv
import gzip
import json
import math
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # <repo>/pilot
from service.book import BookMirror  # noqa: E402

D = Decimal


def fee(p: Decimal, n: int = 1) -> Decimal:
    raw = D("0.07") * p * (1 - p) * n
    return (raw * 10000).to_integral_value(rounding="ROUND_CEILING") / 10000


def walk(book: dict[Decimal, Decimal], lots: int) -> Decimal | None:
    """VWAP proceeds per contract selling `lots` into bids (descending), None if depth short."""
    need = D(lots)
    got = D(0)
    for price in sorted(book, reverse=True):
        take = min(book[price], need)
        got += take * price
        need -= take
        if need <= 0:
            return got / lots
    return None


def run(path: str, series_out: str | None = None, pos_mode: bool = True):
    close_ts = None
    bucket = sd = su = None
    lots = 0
    fills: list[tuple[float, int]] = []
    books: dict[str, BookMirror] = {}
    last: dict[str, Decimal | None] = {}
    rows = []
    # first pass: find tickers + fills (cheap: only non-ws kinds)
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            if '"kalshi_ws"' in line[:60]:
                continue
            r = json.loads(line)
            k = r.get("kind")
            o = r.get("obj", {})
            if k == "window_meta":
                close_ts = datetime.fromisoformat(o["close_time"].replace("Z", "+00:00")).timestamp()
            elif k == "rest_fill":
                bucket = o["market"]
                fills.append((r["local_ts"], int(D(str(o["count"])))))
            elif k == "take_wings" and sd is None:
                for leg in o["legs"]:
                    if leg["side"] == "yes":
                        sd = leg["ticker"]
                    else:
                        su = leg["ticker"]
    if bucket is None or sd is None or su is None:
        return {"path": path, "error": f"tickers missing bucket={bucket} sd={sd} su={su}"}
    fills.sort()
    tick = {bucket, sd, su}
    for t in tick:
        books[t] = BookMirror()
        last[t] = None
    fi = 0
    held_from = fills[0][0] if fills else None
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            if '"kalshi_ws"' not in line[:60]:
                continue
            r = json.loads(line)
            o = r["obj"]
            m = o["msg"]
            t = m.get("market_ticker")
            if t not in tick:
                continue
            typ = o.get("type")
            ts = r["local_ts"]
            if typ == "orderbook_snapshot":
                books[t].apply_snapshot(m)
            elif typ == "orderbook_delta":
                books[t].apply_delta(m)
            elif typ == "trade":
                if t == sd:
                    last[t] = D(m["yes_price_dollars"])
                else:
                    last[t] = D(m["no_price_dollars"])
                continue
            else:
                continue
            while fi < len(fills) and fills[fi][0] <= ts:
                lots += fills[fi][1]
                fi += 1
            if pos_mode and lots == 0:
                continue
            if any(b.suspect for b in books.values()):
                continue
            b_no = books[bucket].best_bid("no")
            s_yes = books[sd].best_bid("yes")
            u_no = books[su].best_bid("no")
            if not (b_no and s_yes and u_no):
                continue
            top = b_no.price + s_yes.price + u_no.price
            fees = fee(b_no.price) + fee(s_yes.price) + fee(u_no.price)
            net = top - 2 - fees
            depth = min(b_no.size, s_yes.size, u_no.size)
            vw = None
            if lots > 0:
                a = walk(books[bucket].no_bids, lots)
                b = walk(books[sd].yes_bids, lots)
                c = walk(books[su].no_bids, lots)
                if a is not None and b is not None and c is not None:
                    vw = a + b + c - 2 - (fee(a) + fee(b) + fee(c))
            mk = None
            if all(last[x] is not None for x in tick):
                mk = last[bucket] + last[sd] + last[su] - 2
            rows.append((ts, lots, float(top), float(net), float(depth), None if vw is None else float(vw),
                         None if mk is None else float(mk), float(b_no.price), float(s_yes.price), float(u_no.price)))
    if not rows:
        return {"path": path, "error": "no rows"}
    # summary
    def frac(pred):
        # time-weighted fraction of held time where pred holds
        tot = 0.0
        hit = 0.0
        for (a, b) in zip(rows, rows[1:]):
            dt = b[0] - a[0]
            tot += dt
            if pred(a):
                hit += dt
        return hit / tot if tot else 0.0

    def longest_run(pred):
        best = 0.0
        start = None
        for (a, b) in zip(rows, rows[1:]):
            if pred(a):
                if start is None:
                    start = a[0]
                best = max(best, b[0] - start)
            else:
                start = None
        return best

    nets = [r[3] for r in rows]
    imax = max(range(len(rows)), key=lambda i: nets[i])
    vws = [r[5] for r in rows if r[5] is not None]
    mks = [r[6] for r in rows if r[6] is not None]
    held_s = rows[-1][0] - rows[0][0]
    summ = {
        "path": Path(path).name, "bucket": bucket, "sd": sd, "su": su, "lots_final": lots,
        "held_s": round(held_s, 1), "t_minus_at_first_fill": round(close_ts - rows[0][0], 1) if close_ts else None,
        "max_net_top": round(max(nets), 4), "t_minus_at_max": round(close_ts - rows[imax][0], 1) if close_ts else None,
        "legs_at_max": rows[imax][7:10], "depth_at_max": rows[imax][4],
        "frac_net_gt0": round(frac(lambda r: r[3] > 0), 4), "frac_net_gt2c": round(frac(lambda r: r[3] > 0.02), 4),
        "frac_net_gt5c": round(frac(lambda r: r[3] > 0.05), 4),
        "longest_run_gt0_s": round(longest_run(lambda r: r[3] > 0), 3),
        "longest_run_gt2c_s": round(longest_run(lambda r: r[3] > 0.02), 3),
        "max_net_vwap_all_lots": round(max(vws), 4) if vws else None,
        "frac_vwap_gt0": round(frac(lambda r: r[5] is not None and r[5] > 0), 4),
        "max_mark_last_trade": round(max(mks), 4) if mks else None,
        "median_net_top": round(sorted(nets)[len(nets) // 2], 4),
        "n_rows": len(rows),
    }
    if series_out:
        with open(series_out, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["ts", "t_minus", "lots", "top", "net", "depth", "vwap_net", "mark_last", "bucket_no_bid", "sd_yes_bid", "su_no_bid"])
            last_sec = None
            for r in rows:
                sec = math.floor(r[0])
                if sec != last_sec:
                    w.writerow([datetime.fromtimestamp(r[0], timezone.utc).strftime("%H:%M:%S.%f")[:-3],
                                round(close_ts - r[0], 1) if close_ts else None, *r[1:]])
                    last_sec = sec
    return summ


if __name__ == "__main__":
    out = sys.argv[2] if len(sys.argv) > 2 else None
    print(json.dumps(run(sys.argv[1], out), default=str, indent=1))
