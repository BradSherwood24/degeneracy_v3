"""box_runner.py — wire the pure ``decide_box`` core onto the Phase-1 recorder pipeline.

The box's live/replay wiring, mirroring ``shakedown.SignalDriver`` + ``ShakedownRecorder`` for the
corridor. ``BoxSignalDriver`` is the small strategy adapter (the generalized ``decide`` protocol:
``decide(params, state, event) -> (state, actions)``, here bound to ``decide_box``); it journals the
box's decision actions and drives an optional ``on_action`` hook. ``BoxWindowRecorder`` subclasses
the Phase-1 ``WindowRecorder`` so the journal-before-dispatch tap, watchdogs, flush, and golden-replay
capture are inherited unchanged, and folds each subscribed book frame into BookMirror before driving
``decide_box``.

Journaling (the allowed "fluff", spec item 2): a ``box_eval`` record is emitted NOT on every tick —
only when the current selection (strike/side) changes, when the NoBox reason changes, or as a
heartbeat at most every ``_HEARTBEAT_S`` seconds. FIRE / WOULD_FIRE carry the full BoxSelection. All
decision math stays in the pure core, so the same driver runs live and in replay bit-identically.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from service.book import TopOfBook
from service.box import (
    FIRE,
    STAND_DOWN,
    WOULD_FIRE,
    Action,
    BookUpdate,
    BoxParams,
    BoxSelection,
    BoxState,
    ClockTick,
    NoBox,
    decide_box,
    select_box,
)
from service.record_window import WindowRecorder
from service.wake import WakeResult
from service.ws_client import _parse_server_ts

# Heartbeat: emit a box_eval at least this often even when the selection has not changed.
_HEARTBEAT_S = 10.0

_BOX_ACTION_KIND_TO_JOURNAL = {
    WOULD_FIRE: "box_would_fire",
    FIRE: "box_fire",
    STAND_DOWN: "box_stand_down",
}


def _ask_size_for_side(top: TopOfBook | None, side: str) -> object:
    """The DISPLAYED size at the ask we would cross on ``side`` (buy YES -> yes_ask_size; buy NO ->
    no_ask_size). None when the top or that size is absent — for the slippage/sizing analysis."""
    if top is None:
        return None
    size = top.yes_ask_size if side == "yes" else top.no_ask_size
    return size


def _selection_payload(sel: BoxSelection, hourly_top: TopOfBook | None,
                       m15_top: TopOfBook | None) -> dict:
    """Full BoxSelection dict for a FIRE/WOULD_FIRE or an in-window box_eval (Decimals kept; the
    journal's Decimal default renders them as strings)."""
    return {
        "hourly_ticker": sel.hourly_ticker,
        "hourly_side": sel.hourly_side,
        "hourly_ask": sel.hourly_ask,
        "hourly_bid": sel.hourly_bid,
        "hourly_mid": sel.hourly_mid,
        "hourly_limit": sel.hourly_limit,
        "hourly_ask_size": _ask_size_for_side(hourly_top, sel.hourly_side),
        "m15_ticker": sel.m15_ticker,
        "m15_side": sel.m15_side,
        "m15_ask": sel.m15_ask,
        "m15_bid": sel.m15_bid,
        "m15_limit": sel.m15_limit,
        "m15_ask_size": _ask_size_for_side(m15_top, sel.m15_side),
        "strike_K": sel.strike_K,
        "anchor_A": sel.anchor_A,
        "C": sel.C,
        "C_mid": sel.C_mid,
        "implied_pin": sel.implied_pin,
    }


def _action_payload(a: Action, sel: BoxSelection | None,
                    hourly_top: TopOfBook | None, m15_top: TopOfBook | None) -> dict:
    payload: dict = {
        "kind": a.kind,
        "source": a.source,
        "count": a.count,
        "C": a.C,
        "t_minus_s": a.t_minus_s,
        "reason": a.reason,
        "legs": [
            {"ticker": lg.ticker, "side": lg.side, "count": lg.count, "limit_price": lg.limit_price}
            for lg in a.legs
        ],
    }
    if sel is not None:
        payload["selection"] = _selection_payload(sel, hourly_top, m15_top)
    return payload


class BoxSignalDriver:
    """Holds the live BoxState and drives ``decide_box`` on each event. Pure decision logic; the only
    side effects are journaling (box_eval + the decision actions) and an optional callback."""

    def __init__(
        self,
        params: BoxParams,
        state: BoxState,
        journal,
        clock: Callable[[], float] = time.time,
        on_action: Callable[[Action], None] | None = None,
    ) -> None:
        self.params = params
        self.state = state
        self.journal = journal
        self.clock = clock
        self._on_action = on_action
        self.actions: list[Action] = []
        self._last_eval_key: tuple | None = None
        self._last_eval_ts: float | None = None

    # --- the generalized decide adapter (BookUpdate OR ClockTick) ---
    def _event(self, event: BookUpdate | ClockTick) -> list[Action]:
        self.state, actions = decide_box(self.params, self.state, event)
        self._maybe_journal_eval(event.server_ts)
        for a in actions:
            self.actions.append(a)
            sel = self.state.fired_selection if a.kind in (FIRE, WOULD_FIRE) else None
            hourly_top = self.state.tops.get(sel.hourly_ticker) if sel is not None else None
            m15_top = self.state.tops.get(self.state.m15_ticker) if sel is not None else None
            self.journal.append(
                _BOX_ACTION_KIND_TO_JOURNAL.get(a.kind, "box_action"),
                _action_payload(a, sel, hourly_top, m15_top),
                self.clock(),
            )
            if self._on_action is not None:
                self._on_action(a)
        return actions

    def on_book_update(self, market: str, top: TopOfBook, server_ts: float) -> list[Action]:
        return self._event(BookUpdate(market, top, server_ts))

    def on_clock_tick(self, server_ts: float) -> list[Action]:
        return self._event(ClockTick(server_ts))

    # --- box_eval (throttled observability) ---
    def _current_view(self) -> BoxSelection | NoBox | None:
        """The box's current selection view from state tops (None until the 15M top is known)."""
        st = self.state
        m15_top = st.tops.get(st.m15_ticker)
        if m15_top is None:
            return None
        ladder = {tk: (K, st.tops[tk]) for tk, K in st.strikes.items() if tk in st.tops}
        return select_box(st.anchor_A, st.m15_ticker, m15_top, ladder, self.params)

    def _maybe_journal_eval(self, now: float) -> None:
        view = self._current_view()
        if view is None:
            return
        if isinstance(view, BoxSelection):
            key: tuple = ("sel", str(view.strike_K), view.hourly_side)
        else:
            key = ("nobox", view.reason)
        heartbeat = self._last_eval_ts is None or (now - self._last_eval_ts) >= _HEARTBEAT_S
        if key == self._last_eval_key and not heartbeat:
            return
        self._last_eval_key = key
        self._last_eval_ts = now
        st = self.state
        rec: dict = {"t_minus_s": st.T - now, "shakedown": st.shakedown, "entered": st.entered}
        if isinstance(view, BoxSelection):
            hourly_top = st.tops.get(view.hourly_ticker)
            m15_top = st.tops.get(st.m15_ticker)
            rec["selection"] = _selection_payload(view, hourly_top, m15_top)
        else:
            rec["no_box_reason"] = view.reason
        self.journal.append("box_eval", rec, self.clock())

    @property
    def entered(self) -> bool:
        return self.state.entered

    @property
    def fired_selection(self) -> BoxSelection | None:
        return self.state.fired_selection


class BoxWindowRecorder(WindowRecorder):
    """WindowRecorder + a BoxSignalDriver over the full subscribed set (15M leg + the whole hourly
    ladder). Unlike the corridor's two-leg driver, EVERY subscribed ticker's book update drives
    ``decide_box`` (which internally folds only the 15M leg + a subscribed ladder strike and ignores
    the rest). Used for shakedown/dry (shakedown state -> WOULD_FIRE only) AND armed (state
    shakedown=False -> FIRE routed to ``on_action``)."""

    def __init__(
        self,
        wake_result: WakeResult,
        journal,
        params: BoxParams,
        state: BoxState,
        clock: Callable[[], float] = time.time,
        capture_tops: bool = True,
        on_action: Callable[[Action], None] | None = None,
    ) -> None:
        super().__init__(wake_result, journal, clock=clock, capture_tops=capture_tops)
        self.driver = BoxSignalDriver(params, state, journal, clock=clock, on_action=on_action)

    def _drive(self, market: str, payload: dict) -> None:
        book = self.books.get(market)
        if book is None:
            return
        server_ts = _parse_server_ts(payload)
        if server_ts is None:
            # F5 (same discipline as LiveWindowRecorder): a frame with no server ts folds the book
            # but must NOT drive a decision on the machine clock (fail-closed freshness law).
            self.journal.append(
                "ws_frame_no_server_ts",
                {"market": market,
                 "note": "no server ts -> book folded, decide_box NOT driven (F5 fail-closed)"},
                self.clock(),
            )
            return
        self.driver.on_book_update(market, book.top_of_book(), server_ts)

    def _on_snapshot(self, market: str, payload: dict) -> None:
        super()._on_snapshot(market, payload)
        self._drive(market, payload)

    def _on_delta(self, market: str, payload: dict) -> None:
        super()._on_delta(market, payload)
        self._drive(market, payload)
