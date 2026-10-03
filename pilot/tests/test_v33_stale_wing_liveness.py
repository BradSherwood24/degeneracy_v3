"""V3.3 re-arm gate D (rewritten, 2026-10-03): STALE-WING LIVENESS + tracked cancel-all before re-place.

The 2026-10-03 02:00Z naked-fill incident: a deep wing strike book (T84699.99) was simply QUIET for 2.9 s
while the strike connection streamed normally; the V3.2-inherited 1.0 s per-strike age read it "stale",
the 1.5 s hold expired, the core cancelled all 11 rests UNTRACKED, the feed read fresh 230 ms later and
``_place_all`` re-placed 11 over 11 unconfirmed cancels.

The fix under test:
  1. connection-level liveness (``strike_feed_ts`` from ANY strike fold) -> ``stale_or_missing_wing`` only
     on a MISSING / SUSPECT wing book, a DEAD strike feed (``strike_feed_dead_s``), or a wing book older
     than the LOOSE ``wing_book_max_age_s`` (the same bound the take gate uses);
  2. the wing TAKE gate prices off a quiet-but-live book;
  3. every resumable cancel-all is TRACKED and the re-place waits for every confirm.

Golden fixtures (tests/fixtures/v33/stale_wing_*.json) were extracted from the real journals with
``pilot/build/v33_stale_wing_measure.py:extract_fixture`` (thinned 100 ms per stream; a thinned gap is never
shorter than the true gap). No network, no journal read, no sealed/holdout data.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from decimal import Decimal

from service.v32.core import _wing_prices as v32_wing_prices
from service.v33 import (
    ActionKind,
    BookUpdate,
    ClockTick,
    Fill,
    OrderAck,
    OrderCancelled,
    V33State,
    decide_v33,
    load_v33_params,
)
from service.v33.core import _strike_feed_alive, _wing_prices_for_bucket
from service.v33.params import V33_STRIKE_FEED_DEAD_S_DEFAULT, V33_WING_BOOK_MAX_AGE_S_DEFAULT
from tests.test_v33_core import (
    B_SD,
    STK_SD,
    STK_SU,
    STK_SU2,
    T,
    _bring_up_ladder,
    _feed,
    _params,
    _sd,
    _state,
    _top,
)

_FX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "v33")


def _kinds(acts, kind):
    return [a for a in acts if a.kind == kind]


def _holds(acts):
    return [a for a in acts if a.kind == ActionKind.STAND_DOWN and a.reason == "stale_or_missing_wing_hold"]


def _sd_cancels(acts):
    return [a for a in acts if a.kind == ActionKind.STAND_DOWN and a.reason == "stale_or_missing_wing_cancel"]


# ===========================================================================
# shipped defaults (measured) and the loader
# ===========================================================================
def test_shipped_values_are_the_measured_defaults_and_pinned():
    from service.v33.params import FROZEN_V33_PARAMS_SHA256, PREVIOUS_V33_PARAMS_SHA256_L4
    p = load_v33_params()
    assert p.strike_feed_dead_s == V33_STRIKE_FEED_DEAD_S_DEFAULT == 4.0
    assert p.wing_book_max_age_s == V33_WING_BOOK_MAX_AGE_S_DEFAULT == 30.0
    # 2026-10-03 RE-PIN: both keys are written EXPLICITLY in the shipped JSON (registered-specs rule), so
    # the frozen sha enforces them; the L4 sha (keys absent) is kept as history.
    assert p.raw["strike_feed_dead_s"] == 4.0 and p.raw["wing_book_max_age_s"] == 30.0
    assert p.sha256 == FROZEN_V33_PARAMS_SHA256
    assert PREVIOUS_V33_PARAMS_SHA256_L4 == "295590ce6536be72ab17cecea05dcdc2921db98b05df0b8eacc906d75f532def"
    assert p.sha256 != PREVIOUS_V33_PARAMS_SHA256_L4
    assert p.wing_book_max_age_s >= p.freshness_max_age_s


def test_loader_reads_explicit_keys_and_fails_closed(tmp_path):
    import pytest

    from service.v33.params import DEFAULT_V33_PARAMS_PATH, V33ParamsInvalid
    raw = json.load(open(DEFAULT_V33_PARAMS_PATH, encoding="utf-8"))
    q = tmp_path / "p.json"
    q.write_text(json.dumps({**raw, "strike_feed_dead_s": 3.5, "wing_book_max_age_s": 20.0}))
    p = load_v33_params(str(q), expected_sha=None)
    assert p.strike_feed_dead_s == 3.5 and p.wing_book_max_age_s == 20.0
    for bad in ({"strike_feed_dead_s": 0}, {"strike_feed_dead_s": -1}, {"wing_book_max_age_s": 0.5},
                {"wing_book_max_age_s": float("inf")}):
        q.write_text(json.dumps({**raw, **bad}))
        with pytest.raises(V33ParamsInvalid):
            load_v33_params(str(q), expected_sha=None)


# ===========================================================================
# 1. hold -> cancel-all -> fresh 200 ms later -> NO place until the 11th confirm
# ===========================================================================
def test_hold_expiry_cancel_all_waits_for_every_confirm_before_replace():
    p = _params(tol=Decimal("0.01"), deb_ms=0)          # shipped stand_down_hold_ms 1500, dead 4.0 s
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)
    oids = [o.order_id for o in st.ladder]
    assert len(oids) == 11 and all(oids)
    # strike feed DEAD: no strike frame for > strike_feed_dead_s (bucket frames keep the clock moving).
    t_dead = now + p.strike_feed_dead_s + 0.1
    st, a = _feed(p, st, BookUpdate(B_SD, _top("0.35", "0.36"), t_dead))
    assert _holds(a) and len(st.ladder) == 11 and not _kinds(a, ActionKind.CANCEL_REST)
    # hold expires -> tracked cancel-all.
    t_cancel = t_dead + p.stand_down_hold_ms / 1000.0 + 0.05
    st, a = _feed(p, st, ClockTick(t_cancel))
    assert len(_kinds(a, ActionKind.CANCEL_REST)) == 11 and _sd_cancels(a)
    assert st.ladder == () and st.awaiting_replace and st.outstanding_cancels == 11
    # the feed reads fresh 200 ms later (the 02:00Z sequence) -> NO re-place while cancels are unconfirmed.
    t_fresh = t_cancel + 0.2
    acts = []
    for ev in (BookUpdate(STK_SU, _top("0.36", "0.37"), t_fresh), BookUpdate(STK_SD, _sd("0.76"), t_fresh),
               BookUpdate(B_SD, _top("0.35", "0.36"), t_fresh + 0.01), ClockTick(t_fresh + 0.5)):
        st, x = _feed(p, st, ev)
        acts += x
    assert st.W is not None and st.n_top is not None          # healthy again ...
    assert not _kinds(acts, ActionKind.PLACE_REST)            # ... but nothing re-placed
    # confirms 1..10 -> still nothing placed.
    for i, oid in enumerate(oids[:10]):
        st, x = _feed(p, st, OrderCancelled(oid, t_fresh + 0.6 + i * 0.01, Decimal(0)))
        assert not _kinds(x, ActionKind.PLACE_REST), f"re-placed after only {i + 1} confirms"
        assert st.outstanding_cancels == 10 - i
    # the 11th confirm releases the re-place: eleven fresh rungs.
    st, x = _feed(p, st, OrderCancelled(oids[10], t_fresh + 0.8, Decimal(0)))
    assert len(_kinds(x, ActionKind.PLACE_REST)) == 11
    assert not st.awaiting_replace and st.outstanding_cancels == 0 and len(st.ladder) == 11


# ===========================================================================
# 2. one wing quiet 3 s with the feed alive -> no hold; a fill takes off the quiet book
# ===========================================================================
def test_quiet_wing_with_live_feed_never_holds_and_takes_off_the_quiet_book():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)                 # STK_SD (yes wing) last updated at ``now``
    acts = []
    t = now
    for i in range(1, 31):                                # 3 s: every OTHER strike + the bucket update
        t = now + i * 0.1
        for ev in (BookUpdate(STK_SU, _top("0.36", "0.37"), t), BookUpdate(STK_SU2, _top("0.20", "0.21"), t),
                   BookUpdate(B_SD, _top("0.35", "0.36"), t)):
            st, x = _feed(p, st, ev)
            acts += x
    assert t - st.strike_ts[79600] >= 3.0 - 1e-9          # the yes wing is 3 s quiet
    assert _strike_feed_alive(p, st, t)
    assert not [a for a in acts if a.kind == ActionKind.STAND_DOWN]
    assert not _kinds(acts, ActionKind.CANCEL_REST) and not _kinds(acts, ActionKind.PLACE_REST)
    assert st.hold_since is None and len(st.ladder) == 11 and all(o.live for o in st.ladder)
    # V3.2's 1.0 s gate WOULD have refused this book (the pre-fix behaviour that flapped the hold) ...
    assert v32_wing_prices(st, t, p) is None
    # ... the V3.3 gate prices it.
    assert _wing_prices_for_bucket(st, 79600, 79700, t, p) == (Decimal("0.76"), Decimal("0.64"))
    # a rest fill during the quiet -> TAKE_WINGS priced off the quiet STK_SD book (ask 0.76 + margin).
    top = max(st.ladder, key=lambda o: o.price)
    st, x = _feed(p, st, Fill(top.order_id, top.client_order_id, Decimal(1), top.price, "no", t + 0.01))
    st, y = _feed(p, st, BookUpdate(STK_SU2, _top("0.20", "0.21"), t + 0.2))   # coalesce window closes
    takes = _kinds(x + y, ActionKind.TAKE_WINGS)
    assert len(takes) == 1
    yes_leg = next(l for l in takes[0].legs if l.side == "yes")
    assert yes_leg.ticker == STK_SD and yes_leg.limit == Decimal("0.76") + p.wing_margin
    assert t + 0.2 - st.strike_ts[79600] > 3.0              # taken while the yes wing was still quiet


# ===========================================================================
# 3. strike feed dead -> hold -> tracked cancel-all (silent AND lagging feed)
# ===========================================================================
def test_dead_strike_feed_holds_then_cancels_with_tracked_cancels():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)
    # bucket frames only, every 0.25 s: the strike connection is silent.
    acts = []
    t = now
    while t < now + p.strike_feed_dead_s + p.stand_down_hold_ms / 1000.0 + 0.6:
        t += 0.25
        st, x = _feed(p, st, BookUpdate(B_SD, _top("0.35", "0.36"), t))
        acts += x
    assert len(_holds(acts)) == 1
    assert len(_kinds(acts, ActionKind.CANCEL_REST)) == 11 and len(_sd_cancels(acts)) == 1
    assert st.ladder == () and st.outstanding_cancels == 11 and st.awaiting_replace
    # order: the hold precedes the cancel-all.
    kinds = [(a.kind, a.reason) for a in acts]
    assert kinds.index((ActionKind.STAND_DOWN, "stale_or_missing_wing_hold")) < kinds.index(
        (ActionKind.STAND_DOWN, "stale_or_missing_wing_cancel"))


def test_lagging_strike_feed_is_dead_even_while_frames_arrive():
    """The 2026-10-02 15:00Z shape: strike frames KEEP ARRIVING but carry server ts 30+ s behind the eval
    clock (watchdog data_age 31-46 s). Liveness reads the frame's OWN ts (``book_ts``), so it is dead."""
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)
    acts = []
    for i in range(1, 25):
        t = now + i * 0.25
        st, x = _feed(p, st, BookUpdate(B_SD, _top("0.35", "0.36"), t))
        acts += x
        # strike frames arrive at eval clock t but are stamped 30 s old (book_ts).
        st, x = _feed(p, st, BookUpdate(STK_SU2, _top("0.20", "0.21"), t, book_ts=t - 30.0))
        acts += x
    assert not _strike_feed_alive(p, st, now + 6.0)
    assert _holds(acts) and _sd_cancels(acts) and st.ladder == () and st.outstanding_cancels == 11


# ===========================================================================
# 4. suspect wing book -> stale (unchanged)
# ===========================================================================
def test_suspect_wing_book_is_stale_with_a_live_feed():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)
    st, a = _feed(p, st, BookUpdate(STK_SD, _top("0.75", "0.76", suspect=True), now + 0.1))
    assert _strike_feed_alive(p, st, now + 0.1)
    assert st.W is None and _holds(a) and len(st.ladder) == 11
    assert _wing_prices_for_bucket(st, 79600, 79700, now + 0.1, p) is None   # never price a suspect book


def test_missing_wing_book_is_stale():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, acts = _feed(p, st, BookUpdate(B_SD, _top("0.35", "0.36"), now))
    st, a2 = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now))    # Sd strike never seen
    assert st.W is None and not _kinds(acts + a2, ActionKind.PLACE_REST)
    assert [a for a in acts + a2 if a.kind == ActionKind.STAND_DOWN and a.reason == "stale_or_missing_wing"]


def test_wing_older_than_loose_bound_is_stale_even_with_a_live_feed():
    """Belt-and-braces: a wing book older than ``wing_book_max_age_s`` is stale for BOTH the rest and the
    take (the ladder never rests a rung whose fill the take gate would refuse)."""
    p = _params(tol=Decimal("0.01"), deb_ms=0, wing_book_max_age_s=4.0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)
    acts = []
    for i in range(1, 46):                                  # 4.5 s, STK_SD quiet, the rest alive
        t = now + i * 0.1
        for ev in (BookUpdate(STK_SU, _top("0.36", "0.37"), t), BookUpdate(B_SD, _top("0.35", "0.36"), t)):
            st, x = _feed(p, st, ev)
            acts += x
    assert _strike_feed_alive(p, st, t)
    assert _holds(acts) and _wing_prices_for_bucket(st, 79600, 79700, t, p) is None


# ===========================================================================
# gate D audit: the other resumable no-quote cancel paths are tracked too
# ===========================================================================
def test_stale_bucket_cancel_is_tracked_and_resume_waits_for_confirms():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)
    oids = [o.order_id for o in st.ladder]
    t = now + p.bucket_freshness_max_age_s + 1.0
    st, a = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), t))
    st, b = _feed(p, st, BookUpdate(STK_SD, _sd("0.76"), t))
    assert [x for x in a + b if x.kind == ActionKind.STAND_DOWN and x.reason == "stale_bucket"]
    assert st.ladder == () and st.outstanding_cancels == 11 and st.awaiting_replace
    st, c = _feed(p, st, BookUpdate(B_SD, _top("0.35", "0.36"), t + 0.1))     # healthy again
    assert not _kinds(c, ActionKind.PLACE_REST)
    for oid in oids[:-1]:
        st, c = _feed(p, st, OrderCancelled(oid, t + 0.2, Decimal(0)))
        assert not _kinds(c, ActionKind.PLACE_REST)
    st, c = _feed(p, st, OrderCancelled(oids[-1], t + 0.3, Decimal(0)))
    assert len(_kinds(c, ActionKind.PLACE_REST)) == 11


def test_n_below_min_cancel_is_tracked():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)
    p2 = replace(p, n_min=Decimal("0.55"))                   # n_top 0.50 < n_min -> stand down
    st, a = _feed(p2, st, ClockTick(now + 0.1))
    assert [x for x in a if x.kind == ActionKind.STAND_DOWN and x.reason == "n_below_min"]
    assert st.ladder == () and st.outstanding_cancels == 11 and st.awaiting_replace
    st, a = _feed(p, st, ClockTick(now + 0.2))              # n_min back -> healthy, but 11 unconfirmed
    assert not _kinds(a, ActionKind.PLACE_REST)


def test_dry_pending_ladder_cancel_does_not_wedge_replace():
    """DRY (shakedown): rungs never ack (no order_id), the cancels produce no OrderCancelled. Only LIVE
    (acked) rungs are counted, so a dry ladder's tracked cancel-all leaves outstanding 0 and the resume
    re-places at once (pre-fix dry behaviour) instead of waiting forever."""
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = V33State.new("2026-09-04T20:00:00Z", T, {"KXBTC-RANGE-B79600": (79600.0, 79699.99),
                                                   "KXBTC-RANGE-B79700": (79700.0, 79799.99)}, p,
                      shakedown=True)
    now = T - 600
    for ev in (BookUpdate(B_SD, _top("0.35", "0.36"), now), BookUpdate(STK_SU, _top("0.36", "0.37"), now),
               BookUpdate(STK_SD, _sd("0.76"), now)):
        st, _ = _feed(p, st, ev)
    assert len(st.ladder) == 11 and all(o.pending for o in st.ladder)
    t = now + p.strike_feed_dead_s + 0.1
    st, _ = _feed(p, st, BookUpdate(B_SD, _top("0.35", "0.36"), t))
    st, a = _feed(p, st, ClockTick(t + p.stand_down_hold_ms / 1000.0 + 0.05))
    assert len(_kinds(a, ActionKind.WOULD_CANCEL_REST)) == 11 and st.outstanding_cancels == 0
    st, a = _feed(p, st, BookUpdate(STK_SD, _sd("0.76"), t + 2.0))
    st, b = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), t + 2.0))
    assert len(_kinds(a + b, ActionKind.WOULD_PLACE_REST)) == 11


# ===========================================================================
# 5. GOLDEN: the real 02:00Z strike-timestamp sequence and the 15:00Z stall
# ===========================================================================
def _replay_fixture(name: str, params, *, confirm_delay_s: float = 0.6):
    """Replay a thinned journal slice through decide_v33 with a model executor: every place acks on the
    spot; every cancel CONFIRMS ``confirm_delay_s`` later (the 02:00Z confirms took ~0.6 s). The eval clock
    is the driver's: max server ts over driven frames, plus a ClockTick every 0.5 s of arrival (local) time
    = last server ts + local elapsed. Tracks the VENUE's resting orders (acked, not yet cancel-confirmed)
    and asserts no PLACE_REST is ever issued while the venue could hold more than K - 1 of ours (the
    02:00Z 11-over-11 race)."""
    fx = json.load(open(os.path.join(_FX, name), encoding="utf-8"))
    close = T
    bucket_tk = fx["bucket"]
    centre = int(float(bucket_tk.rsplit("-B", 1)[1]))
    sd = centre - 50
    su = sd + 100
    bk = {bucket_tk: (float(sd), float(su) - 0.01)}
    tk_y = f"KXBTCD-FX-T{sd - 0.01:.2f}"
    tk_n = f"KXBTCD-FX-T{su - 0.01:.2f}"
    tk_s = f"KXBTCD-FX-T{su + 100 - 0.01:.2f}"
    books = {"y": (tk_y, _top("0.75", "0.76")), "n": (tk_n, _top("0.36", "0.37")),
             "s": (tk_s, _top("0.20", "0.21")), "b": (bucket_tk, _top("0.35", "0.36"))}
    st = V33State.new("2026-10-03T02:00:00Z", close, bk, params)
    all_acts = []
    pending: list = []                                     # (due_local, OrderCancelled)
    venue: set = set()                                     # our order_ids resting at the venue
    last_server = None
    last_local = None
    next_tick = None

    def run(ev):
        nonlocal st
        q = [ev]
        while q:
            e = q.pop(0)
            st, acts = decide_v33(params, st, e)
            st.check_invariants(params)
            all_acts.extend(acts)
            for a in acts:
                if a.kind == ActionKind.PLACE_REST:
                    assert len(venue) < params.rungs, (
                        f"PLACE_REST while the venue still holds {len(venue)} of our rests")
                    oid = f"OID-{a.client_order_id}"
                    venue.add(oid)
                    q.append(OrderAck(a.client_order_id, oid, e.server_ts))
                elif a.kind == ActionKind.CANCEL_REST and a.order_id:
                    pending.append((last_local + confirm_delay_s, a.order_id))

    for local_ms, server_ms, s in fx["events"]:
        local = close + local_ms / 1000.0
        server = close + server_ms / 1000.0
        while next_tick is not None and next_tick <= local:
            run(ClockTick(last_server + (next_tick - last_local)))
            next_tick += 0.5
        due = [x for x in pending if x[0] <= local]
        for x in due:                                      # the venue confirms the cancel
            pending.remove(x)
            venue.discard(x[1])
            run(OrderCancelled(x[1], last_server, Decimal(0)))
        last_server = server if last_server is None else max(last_server, server)
        last_local = local
        if next_tick is None:
            next_tick = local + 0.5
        tk, top = books[s]
        run(BookUpdate(tk, top, last_server, book_ts=server))
    return fx, all_acts


def test_golden_02z_quiet_wings_produce_zero_holds_under_the_new_predicate():
    p = replace(load_v33_params(), E_min=Decimal("0.05"))
    fx, acts = _replay_fixture("stale_wing_02z_20261003.json", p)
    assert fx["source"] == "20261003T020000Z" and len(fx["events"]) > 1000
    assert _kinds(acts, ActionKind.PLACE_REST)              # the ladder rested
    assert _holds(acts) == [] and _sd_cancels(acts) == []
    # (one plain stand-down may precede the first place: at window open a wing book has not been SEEN
    # yet = missing, with nothing resting.) After the ladder rests: no stand-down of any kind.
    first_place = next(i for i, a in enumerate(acts) if a.kind == ActionKind.PLACE_REST)
    assert not [a for a in acts[first_place:] if a.kind == ActionKind.STAND_DOWN]
    assert len(_kinds(acts, ActionKind.PLACE_REST)) == 11   # placed ONCE, never cancelled/re-placed
    assert not _kinds(acts, ActionKind.CANCEL_REST)


def test_golden_02z_old_1s_per_strike_predicate_flaps_on_the_same_sequence():
    """Control: the same fixture under the pre-fix per-strike 1.0 s age (wing bound 1.0 s) DOES hold and
    hold-expire -- the fixture really carries the quiet-wing gaps that flapped live. AND, because every
    cancel-all is now tracked, the harness's venue check (no PLACE_REST while 11 of ours may still rest)
    holds even on this flapping sequence. Verified by mutation: with the pre-fix untracked cancel-all this
    replay raises "PLACE_REST while the venue still holds 11 of our rests" -- the 02:00Z incident."""
    p = replace(load_v33_params(), E_min=Decimal("0.05"), wing_book_max_age_s=1.0)
    _fx, acts = _replay_fixture("stale_wing_02z_20261003.json", p)
    assert len(_holds(acts)) >= 3
    assert _sd_cancels(acts) and len(_kinds(acts, ActionKind.CANCEL_REST)) >= 11


def test_golden_15z_real_stall_still_stands_down():
    p = replace(load_v33_params(), E_min=Decimal("0.05"))
    fx, acts = _replay_fixture("stale_wing_stall_15z_20261002.json", p)
    assert fx["source"] == "20261002T150000Z"
    assert _kinds(acts, ActionKind.PLACE_REST)
    assert _holds(acts) and _sd_cancels(acts)               # the dead feed still holds -> cancels
    assert len(_kinds(acts, ActionKind.CANCEL_REST)) >= 11
    # (the replay harness asserted on every PLACE_REST that the venue never held 11 of ours: every
    # re-place after a stand-down waited for its confirms -- no 11-over-11.)


def test_measured_longest_quiet_wing_14s_on_a_live_feed_never_holds():
    """The longest quiet wing gap measured on a LIVE strike feed (2026-10-03 12:00Z: 13.8 s) is inside the
    shipped wing_book_max_age_s, so it neither holds the rests nor blocks a take."""
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)
    acts = []
    t = now
    for i in range(1, 141):                               # 14 s; STK_SD and STK_SU both quiet
        t = now + i * 0.1
        for ev in (BookUpdate(STK_SU2, _top("0.20", "0.21"), t), BookUpdate(B_SD, _top("0.35", "0.36"), t)):
            st, x = _feed(p, st, ev)
            acts += x
    assert not [a for a in acts if a.kind == ActionKind.STAND_DOWN]
    assert len(st.ladder) == 11 and st.hold_since is None
    assert _wing_prices_for_bucket(st, 79600, 79700, t, p) is not None
