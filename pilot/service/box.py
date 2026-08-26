"""box.py — the PURE decision core for "the wide box" (Brad's late-window deep-ITM box).

House law (same discipline as signal.py): all strategy logic lives here and in pure helpers.
No clock reads, no network, no disk, no globals. Time comes ONLY from event timestamps, so the
same function runs live and in replay bit-identically. Money is Decimal; floats appear only for
timestamps/ages. State is frozen dataclasses transitioned with ``dataclasses.replace``.

WHAT IT REPRODUCES — the scratch "wide box" study (scratchpad/wide_box.py, ``run`` with
TARGET=0.95, MIN15=0.85, start_min=10), mapped onto a LIVE order book. Each structural
live/candle mapping is a NAMED delta reported in the Phase-box1 build report CONFESSIONS.

The strategy, per hourly close T:
  * Universe: the 15-minute market opening at :45 (anchor A = its floor_strike, ticker prefix
    KXBTC15M) and the hourly ladder KXBTCD co-settling at T (strikes K).
  * Read the 15M top-of-book: yes_ask ya, yes_bid yb (book.py's binary identity already gives
    yes_ask = 1 - best NO bid). mid15 = (ya+yb)/2. Both must be present and the spread
    (ya - yb) <= max_spread.
  * If mid15 < 0.5 (BTC below A): buy 15M NO (no_ask = 1 - yb, no_bid = 1 - ya); hourly
    candidates are strikes K < A, hourly leg = YES (ask/bid/mid from the YES side). Pin region
    [K, A).
  * Else (BTC above A): buy 15M YES (ask ya, bid yb); hourly candidates are strikes K > A,
    hourly leg = NO (NO ask = 1 - yes_bid, NO bid = 1 - yes_ask, NO mid = 1 - yes_mid). Pin
    region [A, K).
  * A candidate qualifies only if it has both bid and ask present and spread <= max_spread.
  * Choose the candidate whose hourly-leg MID is nearest ``target_mid`` (0.95); ties -> the
    strike nearer A.
  * Filters: 15M leg ask >= ``min15_ask`` (0.85); hourly leg ask in
    [``hourly_ask_min``, ``hourly_ask_max``] (0.90 .. 0.99). Either fails => no fire.
  * Legs: ``contracts`` each, limit = the observed ask for that leg. Real cost
    C = hourly_ask + fee(hourly_ask) + m15_ask + fee(m15_ask), fee = the audited census fee,
    IMPORTED from service._simlaw (never retyped). Informational C_mid = hourly_mid + m15_mid
    (no fees, mirrors the scratch), implied_pin = C_mid - 1. Payoff is $2 if BTC settles in the
    pin region, else $1 (settlement is NOT part of this pure core).

Windowing / plumbing (decide_box):
  * Entry window: fire only when entry_start_s >= (T - now) >= entry_end_s (T-600 .. T-60). The
    FIRST qualifying instant fires; the window is then DONE (one pair per hour). The chosen
    strike may change from instant to instant before the fire; that is intended.
  * No orders inside the settle cutoff (t_minus < no_orders_after_s_to_settle) -> STAND_DOWN,
    emitted once, like signal.py.
  * Freshness: BOTH chosen legs' tops updated within freshness_max_leg_age_s (1.0s) and NOT
    suspect, else no fire (fail closed) — the same live mapping as signal.py's freshness gate,
    over the two legs the selector picked at this instant.
  * Shakedown flag -> WOULD_FIRE instead of FIRE, like signal.py.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal

from service._simlaw import close_epoch, fee
from service.book import TopOfBook

# Reuse the signal-core event/action vocabulary so replay/journal machinery is shared.
from service.signal import (
    FIRE,
    STAND_DOWN,
    WOULD_FIRE,
    Action,
    BookUpdate,
    ClockTick,
    LegOrder,
)

# --- source identifier (journals / parity) ---
WIDE_BOX = "wide-box"

# --- leg sides (Kalshi YES-perspective outcome we BUY on that leg) ---
BUY_YES = "yes"
BUY_NO = "no"

_ONE = Decimal(1)
_TWO = Decimal(2)
_HALF = Decimal("0.5")
_LIMIT_CEILING = Decimal("0.99")   # a Kalshi binary caps at 0.99 on the buy side


# ---------------------------------------------------------------------------
# Selection results
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BoxSelection:
    """The chosen box at one instant. Money is Decimal; strikes/anchor are Decimal dollars.

    ``C`` is the real fee-inclusive limit cost of the pair (both asks + census fee on each).
    ``C_mid`` and ``implied_pin`` are informational (fee-free mids), mirroring the scratch study.
    """

    hourly_ticker: str
    hourly_side: str            # BUY_YES (below case) or BUY_NO (above case)
    hourly_ask: Decimal         # the OBSERVED ask (the "decided ask" the daily report compares to)
    hourly_bid: Decimal
    hourly_mid: Decimal
    hourly_limit: Decimal       # IOC limit = min(observed ask + limit_margin, 0.99)
    m15_ticker: str
    m15_side: str               # BUY_NO (below case) or BUY_YES (above case)
    m15_ask: Decimal            # the OBSERVED ask
    m15_bid: Decimal
    m15_limit: Decimal          # IOC limit = min(observed ask + limit_margin, 0.99)
    strike_K: Decimal
    anchor_A: Decimal
    C: Decimal
    C_mid: Decimal
    implied_pin: Decimal


@dataclass(frozen=True)
class NoBox:
    """No box qualified at this instant; ``reason`` says why (for the journal). Falsy on purpose
    so callers may write ``if not sel``."""

    reason: str

    def __bool__(self) -> bool:  # pragma: no cover - trivial
        return False


# ---------------------------------------------------------------------------
# Pure selection
# ---------------------------------------------------------------------------
def _mid(ask: Decimal, bid: Decimal) -> Decimal:
    return (ask + bid) / _TWO


def _leg_cost(price: Decimal) -> Decimal:
    """price + audited census fee(price). fee is IMPORTED (never retyped)."""
    return price + fee(price)


def select_box(
    anchor_A: Decimal,
    m15_ticker: str,
    m15_top: TopOfBook,
    ladder: Mapping[str, tuple[Decimal, TopOfBook]],
    params: "BoxParams",
) -> BoxSelection | NoBox:
    """Pure box selection at one instant. Returns a BoxSelection, or a NoBox(reason).

    ``ladder`` maps each hourly ticker -> (strike K, its TopOfBook). All quote arithmetic is
    Decimal. This function does NOT consult book ``suspect`` or freshness — those are the
    decide_box gate applied to the two CHOSEN legs (mirrors signal.py: select the pair, then
    gate it). It mirrors the scratch ``quotes()`` spread rule, side logic, nearest-target
    selection, and the two ask filters.
    """
    ya, yb = m15_top.yes_ask, m15_top.yes_bid
    if ya is None or yb is None:
        return NoBox("15M missing bid/ask")
    if ya - yb > params.max_spread:
        return NoBox("15M spread > max_spread")
    mid15 = _mid(ya, yb)

    if mid15 < _HALF:
        # BTC below the anchor: buy 15M NO, hourly YES on a strike below A.
        m15_side = BUY_NO
        m15_ask, m15_bid = _ONE - yb, _ONE - ya
        below = True
    else:
        # BTC above the anchor: buy 15M YES, hourly NO on a strike above A.
        m15_side = BUY_YES
        m15_ask, m15_bid = ya, yb
        below = False

    if m15_ask < params.min15_ask:
        return NoBox("15M ask < min15_ask")

    # Build qualifying hourly candidates: (ticker, K, h_ask, h_bid, h_mid).
    cands: list[tuple[str, Decimal, Decimal, Decimal, Decimal]] = []
    for tk, (K, top) in ladder.items():
        hya, hyb = top.yes_ask, top.yes_bid
        if hya is None or hyb is None:
            continue
        if hya - hyb > params.max_spread:
            continue
        if below:
            if K >= anchor_A:
                continue
            h_ask, h_bid, h_mid = hya, hyb, _mid(hya, hyb)          # hourly YES side
        else:
            if K <= anchor_A:
                continue
            h_ask, h_bid = _ONE - hyb, _ONE - hya                    # hourly NO side
            h_mid = _ONE - _mid(hya, hyb)
        cands.append((tk, K, h_ask, h_bid, h_mid))

    if not cands:
        return NoBox("no qualifying hourly candidate")

    # Nearest hourly-mid to target; ties -> strike nearer A. Deterministic key.
    def _key(c: tuple[str, Decimal, Decimal, Decimal, Decimal]) -> tuple[Decimal, Decimal]:
        _tk, K, _h_ask, _h_bid, h_mid = c
        return (abs(h_mid - params.target_mid), abs(K - anchor_A))

    chosen_tk, K, h_ask, h_bid, h_mid = min(cands, key=_key)
    if not (params.hourly_ask_min <= h_ask <= params.hourly_ask_max):
        return NoBox("hourly ask outside [hourly_ask_min, hourly_ask_max]")

    # C and the filters are computed from the OBSERVED asks. The IOC limit gets a margin so it
    # still fills if the level walks up a cent or two (Kalshi matches at the resting price, so the
    # margin costs nothing when the level is still there); capped at the 0.99 buy ceiling.
    C = _leg_cost(h_ask) + _leg_cost(m15_ask)
    C_mid = h_mid + _mid(m15_ask, m15_bid)
    implied_pin = C_mid - _ONE
    hourly_limit = min(h_ask + params.limit_margin, _LIMIT_CEILING)
    m15_limit = min(m15_ask + params.limit_margin, _LIMIT_CEILING)
    return BoxSelection(
        hourly_ticker=chosen_tk,
        hourly_side=(BUY_YES if below else BUY_NO),
        hourly_ask=h_ask,
        hourly_bid=h_bid,
        hourly_mid=h_mid,
        hourly_limit=hourly_limit,
        m15_ticker=m15_ticker,
        m15_side=m15_side,
        m15_ask=m15_ask,
        m15_bid=m15_bid,
        m15_limit=m15_limit,
        strike_K=K,
        anchor_A=anchor_A,
        C=C,
        C_mid=C_mid,
        implied_pin=implied_pin,
    )


# ---------------------------------------------------------------------------
# Window state (immutable; decide_box returns a NEW state via dataclasses.replace)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BoxState:
    close_time: str
    T: int                                  # settlement epoch (seconds)
    anchor_A: Decimal
    m15_ticker: str
    strikes: Mapping[str, Decimal]          # hourly ticker -> strike K (the subscribed ladder)
    tops: Mapping[str, TopOfBook] = field(default_factory=dict)
    ts: Mapping[str, float] = field(default_factory=dict)
    shakedown: bool = False
    entered: bool = False
    fired_selection: BoxSelection | None = None
    standdown_emitted: bool = False

    @classmethod
    def new(
        cls,
        close_time: str,
        anchor_A: Decimal,
        m15_ticker: str,
        strikes: Mapping[str, Decimal],
        *,
        shakedown: bool = False,
        T: int | None = None,
    ) -> "BoxState":
        return cls(
            close_time=close_time,
            T=int(close_epoch(close_time)) if T is None else int(T),
            anchor_A=anchor_A,
            m15_ticker=m15_ticker,
            strikes=dict(strikes),
            tops={},
            ts={},
            shakedown=shakedown,
        )


def _leg_fresh(state: BoxState, ticker: str, now: float, params: "BoxParams") -> bool:
    """One leg known, not suspect, and within the freshness bound at ``now`` (fail closed)."""
    top = state.tops.get(ticker)
    last_ts = state.ts.get(ticker)
    if top is None or top.suspect:
        return False
    if last_ts is None:
        return False
    age = now - last_ts
    if age < 0:
        return False
    return age <= params.freshness_max_leg_age_s


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------
def decide_box(
    params: "BoxParams", state: BoxState, event: BookUpdate | ClockTick
) -> tuple[BoxState, list[Action]]:
    """Pure decision. Returns (new_state, actions). See module docstring for the full law."""
    now = event.server_ts
    st = state

    # 1) fold a book update into state (only the 15M leg or a subscribed ladder strike).
    if isinstance(event, BookUpdate):
        if event.market == st.m15_ticker or event.market in st.strikes:
            st = replace(
                st,
                tops={**st.tops, event.market: event.top},
                ts={**st.ts, event.market: now},
            )
        else:
            return st, []

    # 2) window already entered -> done (one pair per hour).
    if st.entered:
        return st, []

    t_minus = st.T - now

    # 3) no orders inside the settle cutoff (emit StandDown once).
    if t_minus < params.no_orders_after_s_to_settle:
        if not st.standdown_emitted:
            st = replace(st, standdown_emitted=True)
            return st, [
                Action(
                    kind=STAND_DOWN,
                    t_minus_s=t_minus,
                    reason=(
                        f"within no-orders-to-settle cutoff "
                        f"(t-{t_minus:.3f}s < {params.no_orders_after_s_to_settle}s)"
                    ),
                )
            ]
        return st, []

    # 4) entry window: fire only when entry_start_s >= t_minus >= entry_end_s. Outside it, seed
    #    book state but never fire (before the window = warmup; after it = past entry).
    if not (params.entry_end_s <= t_minus <= params.entry_start_s):
        return st, []

    # 5) select the box from the CURRENT book (the strike may change instant to instant).
    m15_top = st.tops.get(st.m15_ticker)
    if m15_top is None:
        return st, []
    ladder = {tk: (K, st.tops[tk]) for tk, K in st.strikes.items() if tk in st.tops}
    sel = select_box(st.anchor_A, st.m15_ticker, m15_top, ladder, params)
    if not isinstance(sel, BoxSelection):
        return st, []

    # 6) freshness / not-suspect gate over the two CHOSEN legs. Fail closed.
    if not (
        _leg_fresh(st, sel.m15_ticker, now, params)
        and _leg_fresh(st, sel.hourly_ticker, now, params)
    ):
        return st, []

    # 7) fire (WOULD_FIRE in shakedown), then the window is DONE.
    kind = WOULD_FIRE if st.shakedown else FIRE
    legs = (
        LegOrder(sel.hourly_ticker, sel.hourly_side, params.contracts, sel.hourly_limit),
        LegOrder(sel.m15_ticker, sel.m15_side, params.contracts, sel.m15_limit),
    )
    st = replace(st, entered=True, fired_selection=sel)
    return st, [
        Action(
            kind=kind,
            source=WIDE_BOX,
            legs=legs,
            count=params.contracts,
            C=sel.C,
            ev=None,
            t_minus_s=t_minus,
        )
    ]


# ---------------------------------------------------------------------------
# Policy loader (sha-pinned, fail-closed) — mirrors service.policy.load_policy.
# ---------------------------------------------------------------------------
# (imports kept local to the loader section so the pure core above has no I/O deps)
import hashlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
from typing import Any  # noqa: E402

_POLICY_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "policy")
DEFAULT_BOX_POLICY_PATH = os.path.join(_POLICY_DIR, "box_params.json")

# Canonical sha of the frozen box roster shipped in policy/box_params.json. A plain
# load_box_policy() self-verifies the shipped file against this and refuses any drift.
# Re-pinned in phase box-2 when ``pair_cost_max`` (the S1_box booked-cost ceiling) was added to
# the roster; the box roster is not yet ceremonially frozen (a box falsifier comes later).
FROZEN_BOX_POLICY_SHA256 = "480d46347c6d5e5b136d34df1555516cf1b3d3899b41611a2f0dafb786305eb3"


class BoxPolicyShaMismatch(Exception):
    """Raised when the loaded box policy's canonical sha != the expected sha (fail-closed)."""


@dataclass(frozen=True)
class BoxParams:
    """The frozen wide-box roster. Money is Decimal; times are plain numbers (seconds)."""

    roster_name: str
    target_mid: Decimal
    hourly_ask_min: Decimal
    hourly_ask_max: Decimal
    min15_ask: Decimal
    max_spread: Decimal
    limit_margin: Decimal
    entry_start_s: int
    entry_end_s: int
    freshness_max_leg_age_s: float
    no_orders_after_s_to_settle: int
    contracts: int
    # S1_box booked-cost ceiling (both legs, fees in). A filled box pair whose cost exceeds this is a
    # guaranteed loss against the $2 pinned ceiling -> S1_box trips (halts the day). Pinned in the roster.
    pair_cost_max: Decimal
    sha256: str
    raw: dict[str, Any] = field(repr=False, default_factory=dict)


def canonical_sha256(obj: dict[str, Any]) -> str:
    """sha256 of the canonical JSON encoding (sorted keys, tight separators, utf-8)."""
    canon = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canon).hexdigest()


def load_box_policy(
    path: str = DEFAULT_BOX_POLICY_PATH,
    expected_sha: str | None = FROZEN_BOX_POLICY_SHA256,
) -> BoxParams:
    """Load + freeze the wide-box roster.

    ``expected_sha`` defaults to the pinned FROZEN_BOX_POLICY_SHA256, so a plain
    load_box_policy() self-verifies the shipped file and refuses any drift. Pass
    ``expected_sha=None`` to load without the check (only the tooling that INTENDS to re-pin a
    new roster does this); a mismatch raises BoxPolicyShaMismatch (fail-closed / S5 discipline).
    """
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    sha = canonical_sha256(raw)
    if expected_sha is not None and sha != expected_sha:
        raise BoxPolicyShaMismatch(
            f"box policy sha mismatch for {path}: got {sha}, expected {expected_sha} "
            f"(refusing to load a box policy whose canonical sha does not match)"
        )
    return BoxParams(
        roster_name=str(raw.get("roster_name", "")),
        target_mid=Decimal(str(raw["target_mid"])),
        hourly_ask_min=Decimal(str(raw["hourly_ask_min"])),
        hourly_ask_max=Decimal(str(raw["hourly_ask_max"])),
        min15_ask=Decimal(str(raw["min15_ask"])),
        max_spread=Decimal(str(raw["max_spread"])),
        limit_margin=Decimal(str(raw["limit_margin"])),
        entry_start_s=int(raw["entry_start_s"]),
        entry_end_s=int(raw["entry_end_s"]),
        freshness_max_leg_age_s=float(raw["freshness_max_leg_age_s"]),
        no_orders_after_s_to_settle=int(raw["no_orders_after_s_to_settle"]),
        contracts=int(raw["contracts"]),
        pair_cost_max=Decimal(str(raw["pair_cost_max"])),
        sha256=sha,
        raw=raw,
    )
