"""shakedown.py — wire decide() onto the Phase-1 recorder pipeline in WouldFire-only mode.

The shakedown rung runs with ALLOW_ORDERS off and NO Executor (Phase 3 hasn't built one). This
module COMPOSES with service.record_window (it does not modify it): ``ShakedownRecorder`` subclasses
the Phase-1 ``WindowRecorder`` and, after each book frame is folded into BookMirror exactly as the
passive spine does, drives the PURE ``decide()`` in shakedown mode and journals every WouldFire /
StandDown. The recorder's journal-before-dispatch tap, watchdogs, flush, and golden-replay capture
are inherited unchanged.

``SignalDriver`` is the small pure-ish adapter (state in, actions out) that can also be unit-tested
on its own without any WS/recorder. The window meta (high/low leg tickers, quintile, strangle
gate, fair value) comes from quintile.py at wake; here it is passed in as a seeded WindowState.

Phase-4 wiring note (confessed): the sigma-hat anchor tape needs the 8 trailing 15M markets, which
WakeContext does not fetch (it queries only the co-settling close). The service harness must fetch
those (a /markets query over T-7200..T) and hand quintile.compute_window_stats the tape before
building the WindowState. ``build_shakedown_state`` takes the already-computed QuintileResult.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from service._simlaw import EVCurve
from service.book import TopOfBook
from service.journal import Journal
from service.policy import PolicyParams
from service.quintile import QuintileResult
from service.record_window import WindowRecorder
from service.signal import (
    FIRE,
    STAND_DOWN,
    WOULD_FIRE,
    Action,
    BookUpdate,
    WindowState,
    decide,
)
from service.wake import WakeResult
from service.ws_client import _parse_server_ts

_ACTION_KIND_TO_JOURNAL = {
    WOULD_FIRE: "would_fire",
    FIRE: "fire",
    STAND_DOWN: "stand_down",
}


def build_shakedown_state(
    result: QuintileResult, ev_curve: EVCurve, *, strangle_disabled: bool, shakedown: bool = True
) -> WindowState:
    """Seed a WindowState for signal.decide() from a QuintileResult + the census EV curve."""
    return WindowState.new(
        close_time=result.close_time,
        high_ticker=result.high_ticker,
        low_ticker=result.low_ticker,
        quintile=result.quintile,
        fair_strangle_q=ev_curve.fair_for("strangle", result.quintile),
        strangle_disabled=strangle_disabled,
        shakedown=shakedown,
        G=result.G,
        sigma_hat=result.sigma_hat,
        T=result.T,
    )


def _action_payload(a: Action) -> dict:
    """A JSON-serializable dict for the journal (Decimals go through the journal's Decimal
    default -> string; here we keep them as Decimal so nothing is lossily floated)."""
    return {
        "kind": a.kind,
        "source": a.source,
        "count": a.count,
        "C": a.C,
        "ev": a.ev,
        "t_minus_s": a.t_minus_s,
        "reason": a.reason,
        "legs": [
            {"ticker": lg.ticker, "side": lg.side, "count": lg.count, "limit_price": lg.limit_price}
            for lg in a.legs
        ],
    }


class SignalDriver:
    """Holds the live WindowState and drives decide() on each book update. Pure logic; the only
    side effects are journaling actions and invoking an optional callback."""

    def __init__(
        self,
        params: PolicyParams,
        state: WindowState,
        journal: Journal,
        clock: Callable[[], float] = time.time,
        on_action: Callable[[Action], None] | None = None,
    ) -> None:
        self.params = params
        self.state = state
        self.journal = journal
        self.clock = clock
        self._on_action = on_action
        self.actions: list[Action] = []

    def on_book_update(self, market: str, top: TopOfBook, server_ts: float) -> list[Action]:
        """Fold one leg's top-of-book into decide(); journal + collect any actions."""
        self.state, actions = decide(self.params, self.state, BookUpdate(market, top, server_ts))
        for a in actions:
            self.actions.append(a)
            self.journal.append(
                _ACTION_KIND_TO_JOURNAL.get(a.kind, "signal_action"),
                _action_payload(a),
                self.clock(),
            )
            if self._on_action is not None:
                self._on_action(a)
        return actions

    @property
    def entered(self) -> bool:
        return self.state.entered

    @property
    def fired_source(self) -> str | None:
        return self.state.fired_source


class ShakedownRecorder(WindowRecorder):
    """WindowRecorder + a SignalDriver on the two paired legs. WouldFire-only (no orders exist).

    Only the high/low legs drive decide(); the rest of the subscribed hourly ladder is journaled by
    the inherited tap and folded into BookMirror (for replay/depth) but does not move the decision,
    exactly as signal.decide ignores non-paired markets.
    """

    def __init__(
        self,
        wake_result: WakeResult,
        journal: Journal,
        params: PolicyParams,
        state: WindowState,
        clock: Callable[[], float] = time.time,
        capture_tops: bool = True,
        on_action: Callable[[Action], None] | None = None,
    ) -> None:
        # Fail-closed guarantee (REVIEW phase2 F2): the shakedown rung is WouldFire-ONLY. decide()
        # emits FIRE (not WOULD_FIRE) whenever the state's shakedown flag is False, so a caller that
        # seeds a non-shakedown state here would have this runner journal a FIRE action even though
        # there is no Executor. Refuse it loudly rather than trust the convention.
        if not state.shakedown:
            raise ValueError(
                "ShakedownRecorder requires a shakedown WindowState (WouldFire-only); "
                "got shakedown=False — refusing to run the shakedown rung on a live-fire state"
            )
        super().__init__(wake_result, journal, clock=clock, capture_tops=capture_tops)
        self.driver = SignalDriver(params, state, journal, clock=clock, on_action=on_action)

    def _drive(self, market: str, payload: dict) -> None:
        if market not in (self.driver.state.high_ticker, self.driver.state.low_ticker):
            return
        book = self.books.get(market)
        if book is None:
            return
        server_ts = _parse_server_ts(payload)
        if server_ts is None:
            server_ts = self.clock()
        self.driver.on_book_update(market, book.top_of_book(), server_ts)

    def _on_snapshot(self, market: str, payload: dict) -> None:
        super()._on_snapshot(market, payload)
        self._drive(market, payload)

    def _on_delta(self, market: str, payload: dict) -> None:
        super()._on_delta(market, payload)
        self._drive(market, payload)
