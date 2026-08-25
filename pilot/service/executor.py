"""executor.py — THE ONLY code path that touches order endpoints (single order-authority mutex).

Every order the pilot places goes through ``Executor.execute``. It fails closed by construction and
enforces, BEFORE any POST (all under one mutex so at most one order is in flight at a time):

  1. ARM: refuses unless ``armed`` (config), EXCEPT a stop-authorized FLATTEN (risk reduction is
     permitted while frozen — see below). ALLOW_ORDERS itself is the PROXY's gate; this is the
     independent client-side arm (defense-in-depth).
  2. CAPS: per-leg count <= ``max_contracts`` and ticker starts with an allowed prefix (the proxy
     enforces the SAME caps independently — PLAN "caps live in two places").
  3. NO-ORDERS CUTOFF: refuses when ``t_minus_s < no_orders_after_s_to_settle`` (falsifier I3). The
     executor takes ``t_minus_s`` as an ARGUMENT (from the event-derived clock) and NEVER reads the
     wall clock for a trading decision.
  4. ENTRY DEDUP: at most one ENTRY per window (idempotency).
  5. SINGLE-FLIGHT: at most one order per (window, side, purpose) in flight at once.
  6. RATE BUDGET (F10): client-side token accounting, 10 tokens per order-entry, refuse when the
     per-window budget is exhausted (default 200 tokens/window). On the ORDER path a proxy 429/5xx
     is NOT retried — treated as a no-fill (the imbalance protocol handles it). POST is NEVER retried
     (V2 law: a create that succeeded server-side but lost its response would duplicate on retry).

The Reconciler NEVER posts; it hands intents here. Entry = both legs in ONE batch request.

Journaling (F13-as-modified): the intent is journaled BEFORE the POST and the response(s) BEFORE
returning, so a crash mid-order is reconstructable (ledger.rebuild_from_journal) and reconcile-first
startup can see the in-flight intent.

Nothing here reads .env / *.pem; all traffic targets the local proxy base only.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from service import ledger as ledger_mod
from service.journal import Journal
from service.ledger import (
    PURPOSE_ENTRY,
    PURPOSE_FLATTEN,
    Intent,
)
from service.orders.envelope import (
    BATCH_CREATE_PATH,
    SINGLE_CREATE_PATH,
    OrderResponse,
    build_batch,
    build_entry,
    no_fill_response,
    parse_batch_response,
    parse_single_response,
)

DEFAULT_PROXY_BASE = "http://127.0.0.1:8642"

# PostFn: (path, body) -> response-like with .status_code (int) and .json() (dict). Injected in
# tests (no live network); the default posts to the local proxy.
PostFn = Callable[[str, dict], Any]


@dataclass(frozen=True)
class ExecutorConfig:
    armed: bool = False  # Brad's ALLOW_ORDERS lever is the proxy's; this is the client-side arm.
    max_contracts: int = 2
    ticker_prefixes: tuple[str, ...] = ("KXBTC15M", "KXBTCD")
    no_orders_after_s_to_settle: int = 1
    window_token_budget: int = 200
    tokens_per_entry: int = 10
    proxy_base: str = DEFAULT_PROXY_BASE


@dataclass(frozen=True)
class ExecResult:
    intent: Intent
    responses: tuple[OrderResponse, ...] = ()
    refused: str | None = None
    http_status: int | None = None

    @property
    def dispatched(self) -> bool:
        return self.refused is None

    @property
    def any_fill(self) -> bool:
        return any(r.filled for r in self.responses)


def _default_post(proxy_base: str) -> PostFn:
    import requests  # local import so importing this module never requires the network

    def post(path: str, body: dict) -> Any:
        return requests.post(proxy_base + path, json=body, timeout=10)

    return post


class Executor:
    """The single order dispatcher. Thread-safe: one reentrant mutex serializes ALL dispatch."""

    def __init__(
        self,
        journal: Journal,
        config: ExecutorConfig | None = None,
        post_fn: PostFn | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._journal = journal
        self._cfg = config or ExecutorConfig()
        self._post = post_fn or _default_post(self._cfg.proxy_base)
        self._clock = clock  # journal timestamps ONLY — never a trading-gate input
        self._lock = threading.RLock()
        self._inflight: set[tuple[str, str, str]] = set()
        self._entered_windows: set[str] = set()
        self._tokens_used: dict[str, int] = {}
        # observability
        self.dispatch_count = 0
        self.refusal_count = 0

    @property
    def armed(self) -> bool:
        return self._cfg.armed

    def set_armed(self, value: bool) -> None:
        """Arm/disarm. Only the stops arming-check should arm; a tripped stop disarms."""
        with self._lock:
            self._cfg = _replace_armed(self._cfg, value)

    # --- pure-ish validation helpers (called under the lock) ---
    def _flight_keys(self, intent: Intent) -> set[tuple[str, str, str]]:
        return {(intent.window, leg.side, intent.purpose) for leg in intent.legs}

    def _cap_violation(self, intent: Intent) -> str | None:
        for leg in intent.legs:
            if leg.count <= 0:
                return f"cap:non_positive_count:{leg.ticker}:{leg.count}"
            if leg.count > self._cfg.max_contracts:
                return f"cap:count>{self._cfg.max_contracts}:{leg.ticker}:{leg.count}"
            if not any(leg.ticker.startswith(p) for p in self._cfg.ticker_prefixes):
                return f"cap:ticker_prefix:{leg.ticker}"
        return None

    def execute(
        self,
        intent: Intent,
        t_minus_s: float | None = None,
        *,
        stop_authorized: bool = False,
    ) -> ExecResult:
        """Dispatch ``intent`` (1 leg = single create, 2 legs = batch). Returns an ExecResult.

        ``t_minus_s`` (event-derived seconds to settlement) defaults to ``intent.t_minus_s``. A
        stop-authorized FLATTEN may dispatch while unarmed; nothing else may.
        """
        if t_minus_s is None:
            t_minus_s = intent.t_minus_s

        with self._lock:
            # (1) authorization
            if stop_authorized:
                if intent.purpose != PURPOSE_FLATTEN:
                    return self._refuse(intent, "stop_authorized_non_flatten")
            elif not self._cfg.armed:
                return self._refuse(intent, "not_armed")

            # (2) caps (defense-in-depth; proxy enforces independently)
            cap = self._cap_violation(intent)
            if cap is not None:
                return self._refuse(intent, cap)

            # (3) no-orders-after-settle cutoff (I3) — event-derived time, never wall clock
            if t_minus_s is not None and t_minus_s < self._cfg.no_orders_after_s_to_settle:
                return self._refuse(
                    intent, f"no_orders_after_s_to_settle:t-{t_minus_s}"
                )

            # (4) entry dedup — at most one entry per window
            if intent.purpose == PURPOSE_ENTRY and intent.window in self._entered_windows:
                return self._refuse(intent, "window_already_entered")

            # (5) single-flight per (window, side, purpose)
            keys = self._flight_keys(intent)
            if keys & self._inflight:
                return self._refuse(intent, "single_flight")

            # (6) rate budget (F10)
            tokens = self._cfg.tokens_per_entry * len(intent.legs)
            used = self._tokens_used.get(intent.window, 0)
            if used + tokens > self._cfg.window_token_budget:
                return self._refuse(intent, "rate_budget_exhausted")

            # --- commit to dispatch: reserve budget, mark in-flight ---
            self._tokens_used[intent.window] = used + tokens
            self._inflight |= keys
            self._journal_intent(intent)
            try:
                result = self._dispatch(intent)
            finally:
                self._inflight -= keys
            if intent.purpose == PURPOSE_ENTRY:
                self._entered_windows.add(intent.window)
            self.dispatch_count += 1
            return result

    def _dispatch(self, intent: Intent) -> ExecResult:
        """Build the wire payload, POST once (never retried), parse, journal responses."""
        single = len(intent.legs) == 1
        if single:
            path = SINGLE_CREATE_PATH
            body = build_entry(intent.legs[0])
        else:
            path = BATCH_CREATE_PATH
            body = build_batch([build_entry(leg) for leg in intent.legs])

        try:
            resp = self._post(path, body)
        except Exception as e:  # noqa: BLE001 - POST never retried; any transport error = no-fill
            responses = tuple(
                no_fill_response(leg.client_order_id, f"post_exception:{type(e).__name__}")
                for leg in intent.legs
            )
            self._journal_responses(responses)
            return ExecResult(intent=intent, responses=responses, refused=None, http_status=None)

        status = getattr(resp, "status_code", None)
        # ORDER path: 429 / 5xx are NOT retried — treat as no-fill (imbalance protocol resolves).
        if status is not None and (status == 429 or status >= 500):
            responses = tuple(
                no_fill_response(leg.client_order_id, f"http_{status}") for leg in intent.legs
            )
            self._journal_responses(responses)
            return ExecResult(
                intent=intent, responses=responses, refused=None, http_status=status
            )
        # A proxy cap 403 / 400 (also non-2xx) is a hard refusal-shaped no-fill (never retried).
        if status is not None and (status < 200 or status >= 300):
            responses = tuple(
                no_fill_response(leg.client_order_id, f"http_{status}") for leg in intent.legs
            )
            self._journal_responses(responses)
            return ExecResult(
                intent=intent, responses=responses, refused=None, http_status=status
            )

        body_json = resp.json()
        if single:
            responses = (parse_single_response(body_json),)
        else:
            parsed = parse_batch_response(body_json)
            responses = tuple(self._align_batch(intent, parsed))
        self._journal_responses(responses)
        return ExecResult(intent=intent, responses=responses, refused=None, http_status=status)

    @staticmethod
    def _align_batch(intent: Intent, parsed: list[OrderResponse]) -> list[OrderResponse]:
        """Match batch response slots to the submitted legs by client_order_id; fall back to order
        if the exchange echoes no client_order_id (defensive — the pilot always sends one)."""
        by_cid = {r.client_order_id: r for r in parsed if r.client_order_id}
        out: list[OrderResponse] = []
        leftover = [r for r in parsed if not r.client_order_id]
        for leg in intent.legs:
            matched = by_cid.pop(leg.client_order_id, None)
            if matched is not None:
                out.append(matched)
            elif leftover:
                r = leftover.pop(0)
                # stamp the leg's cid so the ledger can match it
                out.append(_with_cid(r, leg.client_order_id))
            else:
                out.append(no_fill_response(leg.client_order_id, "no_matching_batch_slot"))
        return out

    # --- refusal + journaling ---
    def _refuse(self, intent: Intent, reason: str) -> ExecResult:
        self.refusal_count += 1
        self._journal_intent(intent)
        responses = tuple(
            no_fill_response(leg.client_order_id, f"refused:{reason}") for leg in intent.legs
        )
        self._journal_responses(responses)
        return ExecResult(intent=intent, responses=responses, refused=reason)

    def _journal_intent(self, intent: Intent) -> None:
        self._journal.append("order_intent", ledger_mod.intent_to_record(intent), self._clock())

    def _journal_responses(self, responses: tuple[OrderResponse, ...]) -> None:
        for r in responses:
            self._journal.append("order_response", ledger_mod.response_to_record(r), self._clock())


def _with_cid(resp: OrderResponse, cid: str) -> OrderResponse:
    from dataclasses import replace

    return replace(resp, client_order_id=cid)


def _replace_armed(cfg: ExecutorConfig, armed: bool) -> ExecutorConfig:
    from dataclasses import replace

    return replace(cfg, armed=armed)
