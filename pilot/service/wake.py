"""WakeContext — the :XX pre-window REST sweep (leg discovery, ladder-map check, balance gate).

Brad's ruling (PLAN F11/F17): NO hardcoded pairing rule. For the upcoming top-of-hour close we
QUERY the markets endpoint for both series and DISCOVER the pair dynamically. Everything is UTC
(review F16).

Pure logic (leg selection, ladder-map arithmetic) is separated from the REST shell so it is fully
testable without a network — `WakeContext.sweep()` is the thin I/O wrapper; the module-level
functions are pure.

LEG DISCOVERY
  For each series (15-minute = KXBTC15M, hourly = KXBTCD) collect the markets whose `close_time`
  equals the target close, group them into LADDERS by (event_ticker, open_time) — each distinct
  open_time is a distinct GENERATION — and select the ladder with the SMALLEST window duration
  (close - open). This is the "smallest window duration among candidates sharing the close_time"
  rule, and it is exactly what disambiguates the 2026-07-31 21:00 UTC DUAL-GENERATION case (one
  ladder opened a week early with a $500 step, another opened the day before with a $250 step, both
  co-settling at 21:00Z) — we validate the ladder the smallest-window rule pairs, per the
  commission's "validate the ladder of the specific market actually paired". Selection is
  STATUS-AGNOSTIC (live finding 2026-08-21): 15M markets are listed DAYS ahead as "initialized" and
  flip "initialized"->"active" EXACTLY at open_time (the :45 window start), so at the :40 wake the
  co-settling 15M leg ALWAYS exists but is still "initialized" — we select by close_time regardless
  of status and refuse only a leg whose markets are ALL clearly dead. If either leg is missing, or
  its close has already passed, or all its markets are dead (settled/finalized/closed/determined)
  -> STAND DOWN (this also covers the 06-09 UTC missing-hourly days -> no hourly candidate -> stand
  down cleanly).

LADDER MAP (verified over all 69 corpus days; PLAN + commission)
  Expected floor-strike step = $100 for every hour EXCEPT 21:00 UTC, which is $250, or $500 on
  FRIDAYS (weekday of the close_time date, UTC). The observed step is computed from the SELECTED
  hourly ladder's floor_strikes. Any deviation (non-uniform steps, or observed != expected, or too
  few strikes to measure) -> alarm flag + strangle_disabled=True. Sub-$1 is structure-independent
  and may continue (the caller honors strangle_disabled).

BALANCE + AFFORDABILITY
  Balance is fetched via the proxy REST path. An optional `affordability_gate` callable (the Phase 3
  executor wires the real one) is invoked as gate(balance, wake_result) -> bool; Phase 1 leaves it
  None (affordable = None, "not evaluated").
"""

from __future__ import annotations

import datetime as _dt
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from service.proxy_auth import ProxyAuth

logger = logging.getLogger(__name__)

FIFTEEN_SERIES = "KXBTC15M"
HOURLY_SERIES = "KXBTCD"

# Leg SELECTION is STATUS-AGNOSTIC (live finding 2026-08-21). Kalshi 15M markets are listed DAYS
# ahead as status "initialized" and flip "initialized"->"active" EXACTLY at open_time (the :45 window
# start; observed 15:45:07 for a 15:45:00 open); at the :40 wake the co-settling 15M leg ALWAYS
# exists but is still "initialized". An active-only allow-list ({"active","open"}) therefore stood
# EVERY window down forever. We now select by close_time regardless of status and refuse ONLY a leg
# whose markets are ALL clearly dead. DEAD_STATUSES is the fail-closed deny-list: a ladder with only
# dead markets is treated as a missing leg (stand down). Anything else — "initialized"/"active"/
# "open" or an as-yet-unseen live status — is a selectable candidate, so a novel status string never
# silently stands us down again. Freshness gates downstream (signal.decide) forbid any decision until
# real book data flows, so selecting a not-yet-open leg is safe.
DEAD_STATUSES = frozenset({"settled", "finalized", "closed", "determined"})
# The statuses we positively expect a live leg to report (documentation/observability only — selection
# uses the DEAD deny-list above). Retained under the old name as a back-compat alias.
LIVE_STATUSES = frozenset({"initialized", "active", "open"})
ACTIVE_STATUSES = LIVE_STATUSES

# Ladder-map constants (dollars). 21:00 UTC is the sole special hour.
STEP_DEFAULT = Decimal(100)
STEP_2100 = Decimal(250)
STEP_2100_FRIDAY = Decimal(500)
SPECIAL_HOUR = 21
FRIDAY = 4  # datetime.weekday(): Monday=0 .. Sunday=6

MARKETS_PATH = "/markets"
BALANCE_PATH = "/portfolio/balance"
_MAX_PAGES = 50  # pagination safety cap


def coerce_exchange_index(v: Any) -> int | None:
    """Fail-closed coercion of a market record's ``exchange_index`` field to int | None.

    Exchange sharding (Kalshi changelog 2026-08-24: "Crypto… provisioned on dedicated exchange
    instances"): KXBTC15M / KXBTCD markets now carry ``exchange_index`` (0, 2, …). An order that
    omits it did NOT auto-route on 2026-08-27 (both IOC legs -> ``market_not_found`` on shard 0),
    so routing MUST be explicit. A record whose ``exchange_index`` is absent, None, or non-integral
    yields None -> the dispatch layer REFUSES rather than sending an unrouted order (fail closed)."""
    if v is None:
        return None
    try:
        # Reject bools (bool is an int subclass) and non-integral floats; accept "2"/2/2.0.
        if isinstance(v, bool):
            return None
        iv = int(v)
    except (TypeError, ValueError):
        return None
    if isinstance(v, float) and float(iv) != v:
        return None
    return iv


class WakeError(Exception):
    """Unrecoverable error assembling the wake context (a stand-down is NOT an error — see below)."""


def parse_utc(iso: str) -> _dt.datetime:
    """ISO-8601 (trailing 'Z' or offset; naive assumed UTC) -> aware UTC datetime."""
    s = iso.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = _dt.datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.astimezone(_dt.timezone.utc)


def close_epoch(iso: str) -> int:
    return int(parse_utc(iso).timestamp())


def window_seconds(open_time: str, close_time: str) -> int | None:
    try:
        return close_epoch(close_time) - close_epoch(open_time)
    except (ValueError, TypeError):
        return None


def expected_step(close_iso: str) -> Decimal:
    """Expected floor-strike step (dollars) for a market closing at `close_iso` (UTC ladder map)."""
    dt = parse_utc(close_iso)
    if dt.hour == SPECIAL_HOUR:
        return STEP_2100_FRIDAY if dt.weekday() == FRIDAY else STEP_2100
    return STEP_DEFAULT


def observed_step(floor_strikes: list) -> tuple[Decimal | None, bool]:
    """Return (step, uniform) from a ladder's floor_strikes.

    `step` is the smallest consecutive gap between sorted unique strikes (Decimal, exact via
    str-coercion); `uniform` is True iff every consecutive gap is identical. (None, False) when
    fewer than two distinct strikes exist (cannot measure a step -> fail closed at the call site).
    """
    strikes = sorted({Decimal(str(s)) for s in floor_strikes if s is not None})
    if len(strikes) < 2:
        return None, False
    diffs = [strikes[i + 1] - strikes[i] for i in range(len(strikes) - 1)]
    uniform = all(d == diffs[0] for d in diffs)
    return min(diffs), uniform


@dataclass(frozen=True)
class Leg:
    """One discovered ladder (a generation): the co-settling markets of a series."""

    series: str
    event_ticker: str
    open_time: str
    close_time: str
    window_seconds: int
    market_tickers: tuple[str, ...]
    floor_strikes: tuple[float, ...]
    markets: tuple[dict, ...] = field(repr=False, default=())

    @property
    def primary_ticker(self) -> str:
        """The lowest-ticker market — a stable representative (the 15m leg has exactly one)."""
        return self.market_tickers[0]


@dataclass(frozen=True)
class LadderCheck:
    expected_step: Decimal
    observed_step: Decimal | None
    uniform: bool
    ok: bool
    strangle_disabled: bool
    alarm: bool
    reason: str


@dataclass(frozen=True)
class StandDown:
    """A clean, non-error refusal to run this window (missing leg, closed, inactive)."""

    close_time: str
    reason: str


@dataclass(frozen=True)
class WakeResult:
    close_time: str
    fifteen_leg: Leg
    hourly_leg: Leg
    ladder: LadderCheck
    balance: Any = None
    affordable: bool | None = None
    # F1 (dual-generation): ALL live hourly generations co-settling at the close, each as a Leg.
    # ``hourly_leg`` remains the SELECTED (smallest-window) generation; ``hourly_ladders`` is the
    # full pool run_window pairs against at phase B (census h1_by_ct semantics). Empty on a
    # back-compat construction -> callers fall back to the single selected generation.
    hourly_ladders: tuple[Leg, ...] = ()

    @property
    def exchange_index_by_ticker(self) -> dict[str, int | None]:
        """{ticker: exchange_index} for EVERY market discovered this wake — the 15M leg PLUS the full
        hourly ladder pool (all live generations, or the selected generation on a back-compat
        construction). A market whose record lacks ``exchange_index`` maps to None (fail closed).

        The dispatch layer (``run_window`` box + corridor paths, ``stops`` flatten) reads THIS map to
        stamp ``IntentLeg.exchange_index``; the Executor REFUSES to dispatch a leg whose exchange_index
        is None (the 2026-08-27 ``market_not_found`` incident: omission did not auto-route). Both legs
        of a box/strangle — the hourly ladder market chosen at phase B and the 15M anchor — are
        present here, so every dispatched leg can be routed explicitly."""
        out: dict[str, int | None] = {}
        for m in tuple(self.fifteen_leg.markets) + self.hourly_pool_markets:
            tk = m.get("ticker")
            if tk is None:
                continue
            out[str(tk)] = coerce_exchange_index(m.get("exchange_index"))
        return out

    @property
    def hourly_pool_markets(self) -> tuple[dict, ...]:
        """ALL co-settling hourly strikes across generations (mirrors census ``h1_by_ct`` pooling)
        for the phase-B nearest-threshold pairing + its equidistant tie-exclusion. Falls back to the
        selected generation's markets when the pooled ladders were not retained (single-generation /
        back-compat construction)."""
        if self.hourly_ladders:
            return tuple(m for leg in self.hourly_ladders for m in leg.markets)
        return tuple(self.hourly_leg.markets)

    def subscribe_tickers(self, hourly_limit: int | None = None) -> list[str]:
        """Tickers to subscribe for this window: the 15m market + the hourly ladder.

        `hourly_limit` (if set) caps the hourly ladder to its first N tickers (ticker-sorted) —
        ops lever for the passive spine, since a full hourly ladder can be ~188 strikes. Phase 2+
        will instead select the near-the-money strikes once an anchor exists.
        """
        hourly = list(self.hourly_leg.market_tickers)
        if hourly_limit is not None:
            hourly = hourly[:hourly_limit]
        return list(self.fifteen_leg.market_tickers) + hourly


def _group_ladders(markets: list[dict], close_time_iso: str) -> list[list[dict]]:
    """Group the markets co-settling at `close_time_iso` into ladders by (event_ticker, open_time).

    Only markets whose close_time matches the target (epoch-equal, so 'Z' vs '+00:00' spelling does
    not matter) participate. Returns one list of markets per generation.
    """
    target = close_epoch(close_time_iso)
    groups: dict[tuple[str, str], list[dict]] = {}
    for m in markets:
        ct = m.get("close_time")
        if ct is None:
            continue
        try:
            if close_epoch(ct) != target:
                continue
        except (ValueError, TypeError):
            continue
        key = (m.get("event_ticker", ""), m.get("open_time", ""))
        groups.setdefault(key, []).append(m)
    return list(groups.values())


def _select_smallest_window(ladders: list[list[dict]], close_time_iso: str) -> list[dict] | None:
    """Pick the ladder with the smallest window (close - open). Deterministic tie-break: smallest
    window, then MOST strikes (denser ladder), then lowest event_ticker."""
    scored: list[tuple[int, int, str, list[dict]]] = []
    for lad in ladders:
        if not lad:
            continue
        open_time = lad[0].get("open_time", "")
        win = window_seconds(open_time, close_time_iso)
        if win is None:
            continue
        ev = lad[0].get("event_ticker", "")
        scored.append((win, -len(lad), ev, lad))
    if not scored:
        return None
    scored.sort(key=lambda t: (t[0], t[1], t[2]))
    return scored[0][3]


def _make_leg(series: str, ladder: list[dict], close_time_iso: str) -> Leg:
    open_time = ladder[0].get("open_time", "")
    tickers = tuple(sorted(str(m["ticker"]) for m in ladder if m.get("ticker")))
    strikes = tuple(m["floor_strike"] for m in ladder if m.get("floor_strike") is not None)
    win = window_seconds(open_time, close_time_iso) or 0
    return Leg(
        series=series,
        event_ticker=str(ladder[0].get("event_ticker", "")),
        open_time=str(open_time),
        close_time=close_time_iso,
        window_seconds=win,
        market_tickers=tickers,
        floor_strikes=strikes,
        markets=tuple(ladder),
    )


def _leg_is_live(ladder: list[dict], now_epoch: float, dead_statuses) -> bool:
    """Fail-closed liveness for STATUS-AGNOSTIC leg selection (live finding 2026-08-21): the close
    must be in the FUTURE, AND at least one market must NOT be in a clearly-dead status. A ladder
    whose markets are ALL dead (settled/finalized/closed/determined) is treated as a missing leg
    (stand down). An "initialized" 15M leg at the :40 wake — the exact live scenario — is LIVE here:
    it is not dead and its close is in the future. (Per-strike status semantics for ladders — wings
    can settle early — are handled the same way: requiring >=1 non-dead market avoids standing down
    on a normal ladder while still refusing an all-dead one.)"""
    if not ladder:
        return False
    try:
        if close_epoch(ladder[0]["close_time"]) <= now_epoch:
            return False
    except (KeyError, ValueError, TypeError):
        return False
    return any(m.get("status") not in dead_statuses for m in ladder)


def discover_legs(
    markets_15m: list[dict],
    markets_hourly: list[dict],
    close_time_iso: str,
    now_epoch: float,
    dead_statuses=DEAD_STATUSES,
) -> tuple[Leg, Leg] | StandDown:
    """Pure leg discovery. Returns (fifteen_leg, hourly_leg) or a StandDown with a reason."""
    for series, markets, label in (
        (FIFTEEN_SERIES, markets_15m, "15-minute"),
        (HOURLY_SERIES, markets_hourly, "hourly"),
    ):
        ladders = _group_ladders(markets, close_time_iso)
        if not ladders:
            return StandDown(close_time_iso, f"no {label} leg co-settling at {close_time_iso}")

    fifteen_ladders = _group_ladders(markets_15m, close_time_iso)
    hourly_ladders = _group_ladders(markets_hourly, close_time_iso)
    fifteen_sel = _select_smallest_window(fifteen_ladders, close_time_iso)
    hourly_sel = _select_smallest_window(hourly_ladders, close_time_iso)
    if fifteen_sel is None:
        return StandDown(close_time_iso, "15-minute candidates have unparseable windows")
    if hourly_sel is None:
        return StandDown(close_time_iso, "hourly candidates have unparseable windows")
    if not _leg_is_live(fifteen_sel, now_epoch, dead_statuses):
        return StandDown(close_time_iso, "15-minute leg not live (closed or all markets dead)")
    if not _leg_is_live(hourly_sel, now_epoch, dead_statuses):
        return StandDown(close_time_iso, "hourly leg not live (closed or all markets dead)")
    return _make_leg(FIFTEEN_SERIES, fifteen_sel, close_time_iso), _make_leg(
        HOURLY_SERIES, hourly_sel, close_time_iso
    )


def live_hourly_ladders(
    markets_hourly: list[dict],
    close_time_iso: str,
    now_epoch: float,
    dead_statuses=DEAD_STATUSES,
) -> tuple[Leg, ...]:
    """All LIVE hourly generations co-settling at the target close, each as a Leg (F1).

    Census (``sim/census.py`` ``h1_by_ct``) pools ALL 1H markets sharing the close_time across
    generations for the nearest-threshold pairing (and its equidistant tie-exclusion). The pilot
    must pair against that same pool — not one selected generation — so this returns every live
    generation, letting run_window pool their strikes at phase B and run the ladder-map check
    against the generation of the CHOSEN market. Dead-only or unparseable-window generations are
    dropped (fail-closed, matching ``_leg_is_live`` / ``_select_smallest_window``); the selected
    ``hourly_leg`` is always among the returned ladders."""
    out: list[Leg] = []
    for lad in _group_ladders(markets_hourly, close_time_iso):
        if not lad:
            continue
        if window_seconds(lad[0].get("open_time", ""), close_time_iso) is None:
            continue
        if not _leg_is_live(lad, now_epoch, dead_statuses):
            continue
        out.append(_make_leg(HOURLY_SERIES, lad, close_time_iso))
    return tuple(out)


def ladder_check(hourly_leg: Leg) -> LadderCheck:
    """Validate the SELECTED hourly ladder's floor-strike step against the UTC ladder map."""
    exp = expected_step(hourly_leg.close_time)
    obs, uniform = observed_step(list(hourly_leg.floor_strikes))
    if obs is None:
        return LadderCheck(
            expected_step=exp,
            observed_step=None,
            uniform=False,
            ok=False,
            strangle_disabled=True,
            alarm=True,
            reason="cannot measure step (fewer than two distinct strikes) — fail closed",
        )
    if not uniform:
        return LadderCheck(
            expected_step=exp,
            observed_step=obs,
            uniform=False,
            ok=False,
            strangle_disabled=True,
            alarm=True,
            reason=f"non-uniform ladder steps (min gap {obs})",
        )
    if obs != exp:
        return LadderCheck(
            expected_step=exp,
            observed_step=obs,
            uniform=True,
            ok=False,
            strangle_disabled=True,
            alarm=True,
            reason=f"step {obs} != expected {exp}",
        )
    return LadderCheck(
        expected_step=exp,
        observed_step=obs,
        uniform=True,
        ok=True,
        strangle_disabled=False,
        alarm=False,
        reason="ok",
    )


AffordabilityGate = Callable[[Any, WakeResult], bool]


class WakeContext:
    """I/O shell: fetch both series' markets + balance via the proxy, run the pure sweep."""

    def __init__(
        self,
        proxy_auth: ProxyAuth,
        clock: Callable[[], float] = time.time,
        affordability_gate: AffordabilityGate | None = None,
        dead_statuses=DEAD_STATUSES,
    ) -> None:
        self._proxy = proxy_auth
        self._clock = clock
        self._gate = affordability_gate
        self._dead_statuses = dead_statuses

    def _fetch_series_markets(self, series_ticker: str, close_time_iso: str) -> list[dict]:
        """Fetch markets for one series co-settling at the target close (paginated), STATUS-AGNOSTIC.

        No `status` filter (live finding 2026-08-21): the co-settling 15M leg is "initialized" at the
        :40 wake, and a `status=open` filter would hide it -> stand down every window forever. Narrows
        with min_close_ts/max_close_ts == the target close epoch so the API returns only the
        co-settling markets; still filters exactly downstream in `_group_ladders` + `_leg_is_live`.
        """
        target = close_epoch(close_time_iso)
        out: list[dict] = []
        cursor: str | None = None
        for _ in range(_MAX_PAGES):
            params: dict[str, Any] = {
                "series_ticker": series_ticker,
                "min_close_ts": target,
                "max_close_ts": target,
                "limit": 1000,
            }
            if cursor:
                params["cursor"] = cursor
            resp = self._proxy.rest_get(MARKETS_PATH, params)
            out.extend(resp.get("markets", []) or [])
            cursor = resp.get("cursor") or None
            if not cursor:
                break
        return out

    def fetch_co_settling_15m(self, close_time_iso: str) -> list[dict]:
        """Public: the co-settling 15M market(s) for the target close (phase-B anchor poll).

        Live finding 2026-08-21: the co-settling 15M market is listed "initialized" with NO
        floor_strike at the :40 wake and the strike materializes only when it flips "active" at
        open_time (observed ~:45:13). run_window polls THIS at leg open until the anchor strike is
        present. Reuses the same status-agnostic, close-ts-narrowed fetch WakeContext uses at wake."""
        return self._fetch_series_markets(FIFTEEN_SERIES, close_time_iso)

    def _fetch_balance(self) -> Any:
        try:
            return self._proxy.rest_get(BALANCE_PATH)
        except Exception as e:  # noqa: BLE001 - balance is advisory in Phase 1; log and continue
            logger.warning("[WAKE] balance fetch failed: %s", e)
            return None

    def sweep(self, close_time_iso: str) -> WakeResult | StandDown:
        """Assemble the wake context for the given top-of-hour close (UTC)."""
        now = self._clock()
        markets_15m = self._fetch_series_markets(FIFTEEN_SERIES, close_time_iso)
        markets_hourly = self._fetch_series_markets(HOURLY_SERIES, close_time_iso)
        discovered = discover_legs(
            markets_15m, markets_hourly, close_time_iso, now, self._dead_statuses
        )
        if isinstance(discovered, StandDown):
            logger.info("[WAKE] stand down %s: %s", close_time_iso, discovered.reason)
            return discovered
        fifteen_leg, hourly_leg = discovered
        ladder = ladder_check(hourly_leg)
        if ladder.alarm:
            logger.warning("[WAKE] ladder-map alarm %s: %s", close_time_iso, ladder.reason)
        # F1: retain ALL live hourly generations (census h1_by_ct pooling) so phase B pairs against
        # the full co-settling strike set, not just the selected smallest-window generation.
        hourly_ladders = live_hourly_ladders(
            markets_hourly, close_time_iso, now, self._dead_statuses
        )
        balance = self._fetch_balance()
        result = WakeResult(
            close_time=close_time_iso,
            fifteen_leg=fifteen_leg,
            hourly_leg=hourly_leg,
            ladder=ladder,
            balance=balance,
            affordable=None,
            hourly_ladders=hourly_ladders,
        )
        affordable: bool | None = None
        if self._gate is not None:
            try:
                affordable = self._gate(balance, result)
            except Exception as e:  # noqa: BLE001 - a gate error fails closed to not-affordable
                logger.warning("[WAKE] affordability gate raised: %s -> not affordable", e)
                affordable = False
        return WakeResult(
            close_time=close_time_iso,
            fifteen_leg=fifteen_leg,
            hourly_leg=hourly_leg,
            ladder=ladder,
            balance=balance,
            affordable=affordable,
            hourly_ladders=hourly_ladders,
        )
