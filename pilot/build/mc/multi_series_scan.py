"""multi_series_scan.py -- inventory + volume scan of Kalshi HOURLY strike+range PAIRS.

Brad's ask (2026-09-22 ~02:40Z, verbatim): "could you research all other markets that run both the
hourly strike and hourly bucket markets? ... The structure of the derivatives is what our strategy
makes money on, not the underlying ... scope out just how to go about saving all that data ... use the
Kalshi API to research what markets we can even tap into, and how much data we're even talking about
... Im worried because I dont think these markets have the volume to support our strategy. We need to
mark what volume of contracts are being traded. The pump fader doesnt work if no one's trading."

WHAT THIS DOES (read-only, GET-only, via the local signing proxy; re-runnable from cache):
  Stage A  classify -- every hourly series (frequency == "hourly" in series_all.json): pull one recent
           settled event's nested markets -> strike_type (between=RANGE bucket / greater(_or_equal)/less
           =STRIKE), markets/event, bucket width, strike spacing, floor/cap ticker grammar, alive?
  Stage B  pair -- for each underlying, is there BOTH an hourly RANGE and an hourly STRIKE series
           settling at the SAME close time? Verify the pump-fader geometry: strikes exactly at the
           bucket floor and cap so YES@floor + NO@cap + NO@bucket pays $2 everywhere.
  Stage C  volume -- for each alive PAIR (+ KXBTC/KXBTCD baseline): settled markets last ~14 days ->
           contracts/hour, hours-of-day distribution, share of events with ANY volume, spot-bucket vs
           rest. Sampled hours: 1-min candlesticks for the spot bucket + its two wings (T-15..T-5 and
           T-5..T-0 volume; best bid/ask at T-10/T-5; wing two-sided at T-5). Spot-bucket trades for
           ~10 hours (print sizes, taker side, sweeps >= 20 lots in T-15..T-5). Everything expressed
           as a fraction of KXBTC.

House law honored here: GET only, <=3 req/s with sleep(0.4), NO bulk traffic in minute-of-hour
[38,59] (a live window runs then). Never dials Kalshi directly; never reads .env/.pem. Cache dir keeps
re-runs from re-fetching. NOT a trading path.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
import urllib.error
from collections import Counter, defaultdict
from datetime import datetime, timezone

PROXY = "http://127.0.0.1:8642/trade-api/v2"
HERE = os.path.dirname(os.path.abspath(__file__))
# Cache in scratchpad (wiped between sessions is fine -- this is a re-fetch cache, not a deliverable).
import tempfile

# Re-fetch cache (overridable). Defaults to a stable temp dir so a re-run in any session reuses it.
CACHE = os.environ.get("MS_CACHE", os.path.join(tempfile.gettempdir(), "dv3_multiseries_cache"))
# The full series list. If the env/default file is absent, main() fetches /series?limit=1000 into cache.
SERIES_ALL = os.environ.get("MS_SERIES_ALL",
                            os.path.join(tempfile.gettempdir(), "dv3_series_all.json"))
os.makedirs(CACHE, exist_ok=True)

_GET_COUNT = 0
_SLEEP = 0.4


def _in_quiet_band() -> bool:
    # House law: no bulk traffic in [38,59]. Also hold [0,1] so bulk starts only AFTER :02
    # (the live window settles by :00; :00-:02 is post-settlement cleanup).
    m = datetime.now(timezone.utc).minute
    return m >= 38 or m < 2


def _wait_out_quiet():
    while _in_quiet_band():
        now = datetime.now(timezone.utc)
        print(f"[pacing] quiet band (min={now.minute}); sleeping 30s until [02,37]", flush=True)
        time.sleep(30)


def _slug(path: str) -> str:
    s = path.replace(PROXY, "").strip("/")
    for ch in "/?&=:.":
        s = s.replace(ch, "_")
    return s[:180]


def get(path: str, cache_key: str | None = None, allow_404: bool = True):
    """GET {PROXY}{path} through the local proxy, cached to disk. Returns parsed JSON or None."""
    global _GET_COUNT
    key = cache_key or _slug(path)
    fp = os.path.join(CACHE, key + ".json")
    if os.path.exists(fp):
        try:
            with open(fp, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    _wait_out_quiet()
    url = PROXY + path
    req = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if allow_404 and e.code in (404, 400):
            data = {"__http_error__": e.code}
        else:
            print(f"[get] HTTP {e.code} on {path}", flush=True)
            data = {"__http_error__": e.code}
    except Exception as e:
        print(f"[get] ERR {type(e).__name__} on {path}: {e}", flush=True)
        return None
    _GET_COUNT += 1
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    time.sleep(_SLEEP)
    return data


# ---------------------------------------------------------------------------
# Series classification helpers
# ---------------------------------------------------------------------------
def load_hourly_series():
    if os.path.exists(SERIES_ALL):
        with open(SERIES_ALL, "r", encoding="utf-8") as f:
            allser = json.load(f)["series"]
    else:
        # No local snapshot: fetch the full series list once (the /series page returns all series).
        d = get("/series?limit=1000", cache_key="series_all") or {}
        allser = d.get("series") or []
        try:
            with open(SERIES_ALL, "w", encoding="utf-8") as f:
                json.dump({"series": allser}, f)
        except OSError:
            pass
    hourly = [s for s in allser if s.get("frequency") == "hourly"]
    return allser, hourly


def strike_class(strike_type: str | None) -> str:
    if strike_type == "between":
        return "RANGE"
    if strike_type in ("greater", "greater_or_equal", "less", "less_or_equal"):
        return "STRIKE"
    return f"OTHER({strike_type})"


def first_settled_event(series: str):
    """(event_ticker, raw events payload) for the most-recent settled event, or (None, payload)."""
    for status in ("settled", "closed"):
        ev = get(f"/events?series_ticker={series}&status={status}&limit=8",
                 cache_key=f"events_{series}_{status}")
        evs = (ev or {}).get("events") or []
        if evs:
            return evs[0].get("event_ticker"), ev
    return None, None


def event_nested(event_ticker: str):
    d = get(f"/events/{event_ticker}?with_nested_markets=true", cache_key=f"evn_{event_ticker}")
    return (d or {}).get("event") or (d or {})


def is_alive(series: str) -> bool:
    ev = get(f"/events?series_ticker={series}&status=open&limit=2", cache_key=f"events_{series}_open")
    return bool((ev or {}).get("events"))


def _sub(m: dict) -> str:
    return str(m.get("yes_sub_title") or m.get("subtitle") or "")


def _num(s: str):
    try:
        return float(s.replace("$", "").replace(",", "").strip())
    except Exception:
        return None


def _fp(v, default=0.0):
    """Kalshi fixed-point field ('volume_fp', 'open_interest_fp', ...): decimal string/number -> float.
    Contracts come back as e.g. '41415.02' (a fixed-point count); we read them as float and, for
    cross-series comparison and print sizes, round to the nearest contract at the call site."""
    if v is None:
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def _mvol(m: dict) -> float:
    """Total settled volume of a market, in contracts (reads volume_fp; falls back to volume)."""
    v = m.get("volume_fp")
    if v is None:
        v = m.get("volume")
    return _fp(v)


def bounds_of(m: dict):
    """(floor, cap) for a bucket market. Prefers floor_strike/cap_strike; else parses the subtitle
    "$X to Y" (DOGE-style custom buckets have NULL fields). Returns (None,None) if not a bucket."""
    fs, cs = m.get("floor_strike"), m.get("cap_strike")
    if fs is not None and cs is not None:
        return float(fs), float(cs)
    sub = _sub(m)
    if " to " in sub:
        a, b = sub.split(" to ", 1)
        return _num(a), _num(b)
    return None, None


def strike_level_of(m: dict):
    """The T-strike level of a directional market: floor_strike field, else parse ticker `-T<num>`."""
    fs = m.get("floor_strike")
    if fs is not None:
        return round(float(fs), 4)
    tk = m.get("ticker") or ""
    if "-T" in tk:
        return _num(tk.split("-T")[-1])
    return None


def strike_threshold_of(m: dict):
    """The round economic threshold of a directional market from its subtitle: "$2,550 or above"
    -> 2550.0, "$0.135 or above" -> 0.135. Tick-independent (unlike floor_strike, which carries a
    per-series epsilon). Falls back to floor_strike+guess only if no subtitle number."""
    sub = _sub(m)
    for kw in (" or above", " or below"):
        if kw in sub:
            return _num(sub.split(kw)[0])
    fs = m.get("floor_strike")
    return round(float(fs), 6) if fs is not None else None


def market_kind(m: dict) -> str:
    """'bucket' | 'strike' | 'other', grammar-agnostic (handles strike_type=custom via ticker/subtitle)."""
    st = m.get("strike_type")
    tk = m.get("ticker") or ""
    sub = _sub(m)
    if st == "between" or ("-B" in tk and " to " in sub):
        return "bucket"
    if st in ("greater", "greater_or_equal", "less", "less_or_equal"):
        return "strike"
    if "-T" in tk and ("or above" in sub or "or below" in sub):
        return "strike"
    return "other"


def classify_series(series: str) -> dict:
    et, _ = first_settled_event(series)
    out = {"series": series, "event": et, "alive": is_alive(series)}
    if not et:
        out["class"] = "NO_SETTLED_EVENT"
        return out
    evt = event_nested(et)
    mkts = evt.get("markets") or []
    types = Counter(m.get("strike_type") for m in mkts)
    out["n_markets"] = len(mkts)
    out["strike_types"] = dict(types)
    # dominant class via grammar-agnostic market_kind (bucket->RANGE, strike->STRIKE)
    kinds = Counter(market_kind(m) for m in mkts)
    out["market_kinds"] = dict(kinds)
    top = kinds.most_common(1)[0][0] if kinds else "other"
    out["class"] = {"bucket": "RANGE", "strike": "STRIKE"}.get(top, f"OTHER({top})")
    # bucket width = modal gap between consecutive bucket floors (tick-independent; cap-floor+tick
    # would bake in a per-series tick). Equals strike spacing when strikes sit at every bucket edge.
    bnds = [bounds_of(m) for m in mkts if market_kind(m) == "bucket"]
    floors = sorted({f for f, c in bnds if f is not None})
    fgaps = Counter()
    for a, b in zip(floors, floors[1:]):
        fgaps[round(b - a, 6)] += 1
    out["bucket_width_mode"] = fgaps.most_common(1)[0][0] if fgaps else None
    # also record raw cap-floor span (for the geometry note)
    spans = Counter()
    for f, c in bnds:
        if f is not None and c is not None:
            spans[round(float(c) - float(f), 6)] += 1
    out["bucket_span_mode"] = spans.most_common(1)[0][0] if spans else None
    # strike spacing = modal gap between consecutive strike levels
    slevels = sorted({strike_level_of(m) for m in mkts
                      if market_kind(m) == "strike" and strike_level_of(m) is not None})
    gaps = Counter()
    for a, b in zip(slevels, slevels[1:]):
        gaps[round(b - a, 4)] += 1
    out["strike_spacing_mode"] = gaps.most_common(1)[0][0] if gaps else None
    out["fields_null"] = bool(bnds) and all(m.get("floor_strike") is None
                                            for m in mkts if market_kind(m) == "bucket")
    out["floor_min"], out["floor_max"] = (floors[0], floors[-1]) if floors else (None, None)
    # close cadence -> close_time hour (single event only; cadence refined in volume stage)
    ct = None
    for m in mkts:
        ct = m.get("close_time") or ct
    out["sample_close_time"] = ct
    # ticker grammar sample
    out["sample_tickers"] = [m.get("ticker") for m in mkts[:3]]
    out["settlement_sources"] = None
    return out


# ---------------------------------------------------------------------------
# Volume stage
# ---------------------------------------------------------------------------
_KEEP_FIELDS = ("ticker", "event_ticker", "close_time", "result", "status",
                "floor_strike", "cap_strike", "volume_fp", "volume",
                "open_interest_fp", "yes_sub_title", "subtitle", "strike_type")


def _trim(m: dict) -> dict:
    """Keep only the fields the scan needs (drops price_ranges/rules_* etc.) -- the RAM-short box
    OOMs holding whole market objects for 14 series at once."""
    return {k: m.get(k) for k in _KEEP_FIELDS}


def settled_markets(series: str, max_pages: int = 45, days: int = 14) -> list[dict]:
    """status=settled markets within the last `days` (min_close_ts-bounded), paginated, cached/page.
    Returns TRIMMED market dicts (memory-light)."""
    min_ts = int(time.time() - days * 86400)
    out, cursor, page = [], "", 0
    while page < max_pages:
        q = (f"/markets?series_ticker={series}&status=settled&limit=1000"
             f"&min_close_ts={min_ts}")
        if cursor:
            q += f"&cursor={cursor}"
        d = get(q, cache_key=f"mkts_{series}_settled_p{page}")
        if not d or "__http_error__" in d:
            break
        ms = d.get("markets") or []
        out.extend(_trim(m) for m in ms)
        cursor = d.get("cursor") or ""
        page += 1
        if not cursor or not ms:
            break
    return out


def _close_epoch(m: dict):
    ct = m.get("close_time")
    if not ct:
        return None
    try:
        return datetime.fromisoformat(ct.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def volume_summary(series: str, days: int = 14) -> dict:
    ms = settled_markets(series)
    now = time.time()
    cutoff = now - days * 86400
    ms = [m for m in ms if (_close_epoch(m) or 0) >= cutoff]
    by_event = defaultdict(list)
    for m in ms:
        by_event[m.get("event_ticker")].append(m)
    ev_vols, ev_hours, ev_any, spot_shares = [], Counter(), 0, []
    for et, mk in by_event.items():
        v = sum(_mvol(x) for x in mk)
        spot_v = sum(_mvol(x) for x in mk if x.get("result") == "yes")
        ev_vols.append(v)
        if v > 0:
            ev_any += 1
            spot_shares.append(spot_v / v)
        ce = _close_epoch(mk[0])
        if ce:
            ev_hours[datetime.fromtimestamp(ce, timezone.utc).hour] += int(round(v))
    ev_vols.sort()
    spot_shares.sort()
    n = len(ev_vols)
    closes = [_close_epoch(m) for m in ms if _close_epoch(m)]
    cov_lo = datetime.fromtimestamp(min(closes), timezone.utc).isoformat() if closes else None
    cov_hi = datetime.fromtimestamp(max(closes), timezone.utc).isoformat() if closes else None

    def pct(p):
        if not n:
            return 0
        return ev_vols[min(n - 1, int(p * n))]

    return {
        "series": series,
        "n_events": n,
        "n_markets": len(ms),
        "coverage_from": cov_lo,
        "coverage_to": cov_hi,
        "coverage_hours": n,
        "contracts_per_hour_mean": (sum(ev_vols) / n) if n else 0,
        "contracts_per_hour_median": pct(0.5),
        "contracts_per_hour_p90": pct(0.9),
        "share_events_with_volume": (ev_any / n) if n else 0,
        "spot_bucket_vol_share_median": (spot_shares[len(spot_shares) // 2] if spot_shares else None),
        "total_contracts": sum(ev_vols),
        "vol_by_hour": dict(ev_hours),
        "_markets": ms,  # kept for spot-bucket selection; stripped before JSON dump
    }


def candles(series: str, ticker: str, start_ts: int, end_ts: int):
    d = get(
        f"/series/{series}/markets/{ticker}/candlesticks?start_ts={start_ts}&end_ts={end_ts}"
        f"&period_interval=1",
        cache_key=f"cndl_{ticker}_{start_ts}",
    )
    if not d or "__http_error__" in d:
        return []
    return d.get("candlesticks") or []


def trades(ticker: str, limit: int = 1000):
    d = get(f"/markets/trades?ticker={ticker}&limit={limit}", cache_key=f"trd_{ticker}")
    if not d or "__http_error__" in d:
        return []
    return d.get("trades") or []


def _cval(c, path, default=None):
    """Pull a possibly-nested candle field, e.g. ('yes_bid','close')."""
    cur = c
    for k in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return cur if cur is not None else default


def sample_hours_volume(series_range: str, series_strike: str, vol: dict, strike_markets: list,
                        n_hours: int, n_trade_hours: int, bucket_width: float | None) -> dict:
    """For up-to n_hours settled hours: candlesticks for the spot bucket (result==yes range market)
    + its two wing strikes. Measures T-15..T-5 and T-5..T-0 volume, bid/ask at T-10/T-5, wing
    two-sided at T-5. Spot-bucket trades for n_trade_hours (print sizes, sweeps>=20 in T-15..T-5).

    Grammar-agnostic: spot-bucket bounds via bounds_of; wings DISCOVERED from the strike series'
    settled markets by matching strike level == floor-0.01 (lo, YES leg) and == cap (hi, NO leg),
    keyed per close_time, so no per-series ticker string-building (which breaks on decimals)."""
    ms = vol.get("_markets") or []
    width = float(bucket_width) if bucket_width else None
    # strike THRESHOLD (the "$X or above" number from the subtitle) -> ticker, per close_time.
    # Threshold is tick-independent: floor_strike carries a per-series epsilon, the subtitle carries
    # the round boundary. lo wing threshold == floor; hi wing threshold == floor+width (== next floor).
    strike_by_close: dict[str, dict[float, str]] = defaultdict(dict)
    for sm in strike_markets:
        th = strike_threshold_of(sm)
        ct = sm.get("close_time")
        if th is not None and ct and sm.get("ticker"):
            strike_by_close[ct][round(float(th), 6)] = sm["ticker"]
    # spot buckets = settled range markets with result 'yes' and resolvable bounds
    spots = []
    for m in ms:
        if m.get("result") != "yes":
            continue
        f, c = bounds_of(m)
        if f is None or c is None:
            continue
        spots.append((m, f, c))
    spots.sort(key=lambda t: _close_epoch(t[0]) or 0, reverse=True)
    picked = spots[:n_hours]
    res = {"n_sampled": 0, "rows": [], "trade_rows": []}

    def _match(levels: dict, target: float):
        if not levels or target is None:
            return None
        best = min(levels, key=lambda x: abs(x - target))
        tol = max(abs(target) * 1e-4, 1e-6)
        return levels[best] if abs(best - target) <= tol else None

    for idx, (m, floor, cap) in enumerate(picked):
        ce = _close_epoch(m)
        btk = m["ticker"]
        ct = m.get("close_time")
        levels = strike_by_close.get(ct, {})
        # lo wing (YES leg, "close >= floor"): threshold == floor
        # hi wing (NO leg, "close < floor+width"): threshold == floor + width (== cap + tick)
        lo_tk = _match(levels, floor)
        hi_tk = _match(levels, (floor + width) if width else cap)
        w = {"close": ct, "bucket": btk, "floor": floor, "cap": cap,
             "lo_wing": lo_tk, "hi_wing": hi_tk,
             "wings_found": bool(lo_tk and hi_tk)}
        # candle windows
        t_open = int(ce - 15 * 60)
        t5 = int(ce - 5 * 60)
        t10 = int(ce - 10 * 60)
        bc = candles(series_range, btk, int(ce - 16 * 60), int(ce))
        w["bucket_vol_T15_T5"] = _vol_between(bc, t_open, t5)
        w["bucket_vol_T5_T0"] = _vol_between(bc, t5, int(ce))
        w["bucket_ba_T10"] = _ba_at(bc, t10)
        w["bucket_ba_T5"] = _ba_at(bc, t5)
        for wing, wtk in (("lo", lo_tk), ("hi", hi_tk)):
            if not wtk:
                continue
            wc = candles(series_strike, wtk, int(ce - 16 * 60), int(ce))
            w[f"{wing}_vol_T15_T5"] = _vol_between(wc, t_open, t5)
            ba5 = _ba_at(wc, t5)
            w[f"{wing}_ba_T5"] = ba5
            w[f"{wing}_two_sided_T5"] = bool(ba5 and ba5[0] and ba5[1])
        res["rows"].append(w)
        res["n_sampled"] += 1
        # trades for the first n_trade_hours spot buckets
        if idx < n_trade_hours:
            tr = trades(btk)
            sizes = [_tcnt(t) for t in tr]
            sweeps = 0            # any-side prints >=20 in T-15..T-5
            yes_sweeps = 0        # YES-taker prints >=20 (the pump that pushes NO down to our n)
            taker = Counter()
            for t in tr:
                ts = t.get("created_time") or t.get("ts")
                te = None
                if isinstance(ts, str):
                    try:
                        te = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                    except Exception:
                        te = None
                elif isinstance(ts, (int, float)):
                    te = ts if ts < 1e12 else ts / 1000.0
                cnt = _tcnt(t)
                side = t.get("taker_side")
                taker[side] += 1
                if te is not None and t_open <= te <= t5 and cnt >= 20:
                    sweeps += 1
                    if side == "yes":
                        yes_sweeps += 1
            res["trade_rows"].append({
                "bucket": btk, "n_trades": len(tr),
                "median_print": (sorted(sizes)[len(sizes) // 2] if sizes else 0),
                "max_print": (max(sizes) if sizes else 0),
                "sweeps_ge20_T15_T5": sweeps,
                "yes_sweeps_ge20_T15_T5": yes_sweeps,
                "taker_side_counts": dict(taker),
            })
    return res


def _tcnt(t) -> int:
    """Contract count of a trade (count, else count_fp)."""
    c = t.get("count")
    if c is None:
        c = t.get("count_fp")
    return int(round(_fp(c)))


def _vol_between(cs, a, b):
    tot = 0
    for c in cs:
        te = c.get("end_period_ts") or c.get("ts") or c.get("end_ts")
        if te is None:
            continue
        if a <= te <= b:
            v = c.get("volume")
            if v is None:
                v = c.get("volume_fp")
            if v is None:
                v = _cval(c, ("volume", "close"), 0)
            tot += int(round(_fp(v)))
    return tot


def _ba_at(cs, t):
    """(bid, ask) in dollars, closest candle at or before t, from yes_bid/yes_ask close_dollars."""
    best = None
    for c in cs:
        te = c.get("end_period_ts") or c.get("ts") or c.get("end_ts")
        if te is None or te > t:
            continue
        if best is None or te > best[0]:
            bid = _cval(c, ("yes_bid", "close_dollars")) or _cval(c, ("yes_bid", "close"))
            ask = _cval(c, ("yes_ask", "close_dollars")) or _cval(c, ("yes_ask", "close"))
            best = (te, bid, ask)
    return (best[1], best[2]) if best else (None, None)


# ---------------------------------------------------------------------------
# Candidate pair roster (by underlying) -- from series_all.json hourly inventory
# ---------------------------------------------------------------------------
CANDIDATES = {
    # underlying: (range_series_candidates, strike_series_candidates)
    "BTC": (["KXBTC"], ["KXBTCD"]),
    "ETH": (["KXETH"], ["KXETHD"]),
    "SOL": (["KXSOL", "KXSOLE"], ["KXSOLD"]),
    "DOGE": (["KXDOGE"], ["KXDOGED"]),
    "XRP": (["KXXRP", "KXRIPPLE"], ["KXXRPD"]),
    "BNB": (["KXBNB"], ["KXBNBD"]),
    "NEAR": (["KXNEAR"], ["KXNEARD", "KXNEARH"]),
    "TON": (["KXTON"], ["KXTOND", "KXTONH"]),
    "ZEC": (["KXZEC"], ["KXZECD", "KXZECH"]),
    "HYPE": (["KXHYPE"], ["KXHYPED"]),
    "SP500": (["KXINXI", "INXI"], ["KXINXU"]),
    "NASDAQ": (["NASDAQ100I"], ["KXNASDAQ100U"]),
    "DJI": (["KXDJI"], ["KXDJI"]),
    "NKY": (["KXNKY"], ["KXNKY"]),
    "KOSPI": (["KXKR200"], ["KXKR200"]),
    "GOLD": (["KXGOLDH"], ["KXGOLDH"]),
    "SILVER": (["KXSILVERH"], ["KXSILVERH"]),
    "WTI": (["KXWTIH", "WTIH"], ["KXWTIH", "WTIH"]),
    "PALLADIUM": (["KXPALLADIUMH"], ["KXPALLADIUMH"]),
}


def main():
    stage = sys.argv[1] if len(sys.argv) > 1 else "all"
    allser, hourly = load_hourly_series()
    hourly_tickers = sorted(s["ticker"] for s in hourly)
    meta = {s["ticker"]: s for s in hourly}
    result = {"generated": datetime.now(timezone.utc).isoformat(),
              "hourly_series_count": len(hourly),
              "hourly_tickers": hourly_tickers}

    # -- Stage A: classify every candidate series (and any hourly series we can cheaply reach) --
    to_classify = sorted({t for pair in CANDIDATES.values() for grp in pair for t in grp})
    cls = {}
    for s in to_classify:
        if s not in meta:
            cls[s] = {"series": s, "class": "NOT_IN_SERIES_ALL"}
            continue
        c = classify_series(s)
        src = meta[s].get("settlement_sources")
        c["settlement_sources"] = ([x.get("name") for x in src] if isinstance(src, list) else src)
        c["exchange_index"] = meta[s].get("exchange_index")
        c["title"] = meta[s].get("title")
        cls[s] = c
        print(f"[A] {s:14} class={c.get('class'):8} alive={c.get('alive')} "
              f"n_mkts={c.get('n_markets')} width={c.get('bucket_width_mode')} "
              f"spacing={c.get('strike_spacing_mode')} ct={c.get('sample_close_time')}", flush=True)
    result["classification"] = cls

    # -- Stage B: pair up --
    pairs = {}
    for und, (rng_c, stk_c) in CANDIDATES.items():
        rng = [s for s in rng_c if cls.get(s, {}).get("class") == "RANGE"]
        stk = [s for s in stk_c if cls.get(s, {}).get("class") == "STRIKE"]
        rng_alive = [s for s in rng if cls.get(s, {}).get("alive")]
        stk_alive = [s for s in stk if cls.get(s, {}).get("alive")]
        rsel = (rng_alive or rng or [None])[0]
        ssel = (stk_alive or stk or [None])[0]
        both_alive = bool(rng_alive and stk_alive)
        # close-time match
        same_close = None
        if rsel and ssel:
            same_close = cls[rsel].get("sample_close_time") == cls[ssel].get("sample_close_time") \
                or (cls[rsel].get("sample_close_time") is not None
                    and cls[ssel].get("sample_close_time") is not None
                    and cls[rsel]["sample_close_time"][-9:] == cls[ssel]["sample_close_time"][-9:])
        pairs[und] = {
            "underlying": und, "range_series": rsel, "strike_series": ssel,
            "range_alive": bool(rng_alive), "strike_alive": bool(stk_alive),
            "both_alive": both_alive,
            "range_candidates_that_are_range": rng, "strike_candidates_that_are_strike": stk,
            "bucket_width": cls.get(rsel, {}).get("bucket_width_mode") if rsel else None,
            "strike_spacing": cls.get(ssel, {}).get("strike_spacing_mode") if ssel else None,
            "same_close_hint": same_close,
            "exch": cls.get(rsel, {}).get("exchange_index") if rsel else None,
            "settlement_range": cls.get(rsel, {}).get("settlement_sources") if rsel else None,
            "settlement_strike": cls.get(ssel, {}).get("settlement_sources") if ssel else None,
        }
        print(f"[B] {und:10} range={rsel} strike={ssel} both_alive={both_alive} "
              f"width={pairs[und]['bucket_width']} spacing={pairs[und]['strike_spacing']}", flush=True)
    result["pairs"] = pairs

    if stage == "classify":
        _dump(result)
        return

    # -- Stages C + C2: volume + sampled candles/trades, ONE PAIR AT A TIME (free markets between
    # pairs; the RAM-short box OOMs if all 14 series' markets are held at once) --
    import gc
    vols, samples = {}, {}
    alive_pairs = [und for und, p in pairs.items() if p["both_alive"] or und == "BTC"]
    for und in alive_pairs:
        p = pairs[und]
        rser, sser = p["range_series"], p["strike_series"]
        if not rser or not sser:
            continue
        vr = volume_summary(rser)
        vs = volume_summary(sser)
        for ser, v in ((rser, vr), (sser, vs)):
            print(f"[C] {ser:14} hours={v['n_events']} ({str(v['coverage_from'])[:10]}..{str(v['coverage_to'])[:10]}) "
                  f"mean/h={v['contracts_per_hour_mean']:.0f} med/h={v['contracts_per_hour_median']:.0f} "
                  f"p90={v['contracts_per_hour_p90']:.0f} anyvol={v['share_events_with_volume']:.0%} "
                  f"spot_share={v['spot_bucket_vol_share_median']} total={v['total_contracts']:.0f}",
                  flush=True)
        n_hours = 24 if und == "BTC" else 12
        n_trade = 10 if und == "BTC" else 6
        s = sample_hours_volume(rser, sser, vr, vs.get("_markets") or [], n_hours, n_trade,
                                p["bucket_width"])
        rows = s["rows"]
        if rows:
            def med(xs):
                xs = sorted([x for x in xs if x is not None])
                return xs[len(xs) // 2] if xs else 0
            s["rollup"] = {
                "n": len(rows),
                "wings_found_share": sum(1 for r in rows if r.get("wings_found")) / len(rows),
                "bucket_vol_T15_T5_med": med([r.get("bucket_vol_T15_T5") for r in rows]),
                "bucket_vol_T5_T0_med": med([r.get("bucket_vol_T5_T0") for r in rows]),
                "lo_two_sided_T5_share": sum(1 for r in rows if r.get("lo_two_sided_T5")) / len(rows),
                "hi_two_sided_T5_share": sum(1 for r in rows if r.get("hi_two_sided_T5")) / len(rows),
            }
            tr = s["trade_rows"]
            if tr:
                s["rollup"]["median_print_med"] = med([r["median_print"] for r in tr])
                s["rollup"]["max_print_max"] = max([r["max_print"] for r in tr])
                s["rollup"]["sweeps_ge20_med"] = med([r["sweeps_ge20_T15_T5"] for r in tr])
                s["rollup"]["yes_sweeps_ge20_med"] = med([r.get("yes_sweeps_ge20_T15_T5", 0) for r in tr])
            print(f"[C2] {und:10} wings_found={s['rollup']['wings_found_share']:.0%} "
                  f"bkt_T15T5_med={s['rollup']['bucket_vol_T15_T5_med']} "
                  f"lo2s={s['rollup']['lo_two_sided_T5_share']:.0%} hi2s={s['rollup']['hi_two_sided_T5_share']:.0%} "
                  f"sweeps_med={s['rollup'].get('sweeps_ge20_med')}", flush=True)
        # keep only lightweight summaries; drop raw markets + per-row detail before next pair
        vr.pop("_markets", None)
        vs.pop("_markets", None)
        s.pop("rows", None)
        vols[rser] = vr
        vols[sser] = vs
        samples[und] = s
        gc.collect()

    result["samples"] = samples
    result["volume"] = vols
    result["get_count"] = _GET_COUNT
    _dump(result)
    print(f"\nTOTAL GETs this run: {_GET_COUNT}", flush=True)


def _dump(result):
    outp = os.path.join(HERE, "multi_series_scan.json")
    with open(outp, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=1, default=str)
    print(f"[dump] wrote {outp}", flush=True)


if __name__ == "__main__":
    main()
