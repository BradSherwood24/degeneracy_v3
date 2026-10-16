"""V3.3 PARTIAL HEDGE (2026-10-10 20:00Z incident): take the quotable wing at the fill instead of
blocking the whole batch while the other leg's ask is missing.

The incident (reconstructed from ``pilot/journals_v33/20261010T200000Z.jsonl.gz``, read-only):
  * 11 rungs filled 19:54:54.648 .. 19:54:54.710 (prices 0.49 .. 0.59) in bucket B83050;
  * the UPPER wing (NO on T83099.99, ~0.99) had NO ask -> ``_wing_prices`` returned None ->
    ``_wing_step`` did ``continue`` and sent NEITHER leg;
  * the batch waited 37.41 s (take fired 19:55:32.117 at YES 0.82 / NO 0.99) while the LOWER wing
    (YES on T82999.99, the real risk, ~0.60 at the fill) climbed to 0.82. Loss $3.84; ~$2.2 was the wait.

Brad's rule: "Can both orders fill -> no -> place what orders we can", then treat the deferred leg like
an empty wing (the existing per-leg RETRY path takes it once its own book is priceable). The both-legs
law and V3.2 are untouched; the change is a V3.3-local per-leg take.

No network, no journal read at test time (the incident book states are reconstructed inline from the
journal's recorded facts). 2026-08-20..29 (holdout) and the 2026-08-02..18 seal are never touched.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from service._simlaw import fee
from service.book import TopOfBook
from service.v33 import ActionKind, BookUpdate, ClockTick, Fill, V33State, decide_v33
from service.v33.core import (
    BUY_NO,
    BUY_YES,
    _strike_feed_alive,
    _take_batch_partial,
    _wing_leg_ask,
)
from service.v33.reconcile import _hedged_lots_from_state
from tests.test_v33_core import (
    STK_SD,
    STK_SU,
    T,
    _bring_up_ladder,
    _feed,
    _params,
    _sd,
    _state,
    _sweep_fill,
    _top,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _no_ask_top(bid: str = "0.36") -> TopOfBook:
    """A strike book with a bid but NO ask on EITHER side (no yes_ask, no no_ask) -- the 2026-10-10
    upper-wing condition (``wing_no_ask_su``: nobody offering)."""
    return replace(_top(bid, str(Decimal(bid) + Decimal("0.01"))),
                   yes_ask=None, yes_ask_size=None, no_ask=None, no_ask_size=None)


def _fill_ladder(p, st, now, n=11):
    """Bring up the ladder and sweep-fill ``n`` rungs into one coalesced batch."""
    st, _ = _bring_up_ladder(p, st, now)
    for i, o in enumerate(sorted(st.ladder, key=lambda o: o.price, reverse=True)[:n]):
        st, _ = _sweep_fill(p, st, o, now + 0.5 + i * 0.001)
    return st


def _close_with(p, st, now, *, sd=None, su=None):
    """Set the Sd / Su strike books WITHIN the coalesce window (no take yet), then advance a ClockTick
    PAST the window (now + 0.75) so the batch flushes and the wings take against EXACTLY those book states
    -- the only way to pin both legs' state at the take, since the first post-window feed would otherwise
    take against whatever books happen to be current then."""
    tw = now + 0.55
    if sd is not None:
        st, _ = _feed(p, st, BookUpdate(STK_SD, sd, tw))
    if su is not None:
        st, _ = _feed(p, st, BookUpdate(STK_SU, su, tw + 0.01))
    st, acts = _feed(p, st, ClockTick(now + 0.75))
    return st, acts


def _legs(st, idx):
    return [l for l in st.wing_legs if l.batch == idx]


# ===========================================================================
# (a) one leg priceable, the other missing -> ONE take for the priceable leg
# ===========================================================================
def test_lower_wing_taken_when_upper_wing_has_no_ask():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st = _fill_ladder(p, st, now)
    # close the coalesce window: upper wing (STK_SU, the NO leg) has NO ask; lower wing (STK_SD) quotable.
    st, acts = _close_with(p, st, now, sd=_sd("0.60"), su=_no_ask_top())
    b = st.wing_batches[0]
    assert b.taken and not b.completed and not b.one_legged
    legs = _legs(st, b.index)
    assert len(legs) == 2
    by_side = {l.side: l for l in legs}
    # YES leg SENT (pending, real coid, priced at 0.60 + wing_margin); NO leg DEFERRED (unfilled, NO coid).
    assert by_side[BUY_YES].status == "pending" and by_side[BUY_YES].client_order_id
    assert by_side[BUY_YES].limit == min(Decimal("0.60") + p.wing_margin, Decimal("0.99"))
    assert by_side[BUY_NO].status == "unfilled" and by_side[BUY_NO].client_order_id == ""


def test_exactly_one_partial_take_across_the_close():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st = _fill_ladder(p, st, now)
    st, acts = _close_with(p, st, now, sd=_sd("0.60"), su=_no_ask_top())
    takes = [a for a in acts if a.kind == ActionKind.TAKE_WINGS]
    assert len(takes) == 1
    tk = takes[0]
    # the ACTION carries BOTH legs (YES priced, NO deferred/limit=None) and the full count, so the driver
    # journals it as the batch's ``take_wings`` (len(legs)==2) -> journal-rebuild hedge coverage credited.
    assert tk.count == 11 and len(tk.legs) == 2
    yes_leg = next(l for l in tk.legs if l.side == BUY_YES)
    no_leg = next(l for l in tk.legs if l.side == BUY_NO)
    assert yes_leg.limit == min(Decimal("0.60") + p.wing_margin, Decimal("0.99"))
    assert no_leg.limit is None            # deferred leg: unpriced, not sent this instant


# ===========================================================================
# (b) the deferred leg's book becomes priceable -> ONE RETRY_WING via the existing path,
#     lock_floor respected, batch completes and is credited (no 10-09 regression)
# ===========================================================================
def test_deferred_leg_taken_by_existing_retry_path_then_completes():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st = _fill_ladder(p, st, now)
    st, _ = _close_with(p, st, now, sd=_sd("0.60"), su=_no_ask_top())
    idx = st.wing_batches[0].index
    # fill the YES leg that was sent at the partial take.
    yes_leg = next(l for l in _legs(st, idx) if l.side == BUY_YES)
    st, _ = _feed(p, st, Fill(None, yes_leg.client_order_id, Decimal(yes_leg.count), yes_leg.limit,
                              BUY_YES, now + 0.8))
    # the upper wing's ask reappears -> the SAME _retry_batch path takes the deferred NO leg (one retry).
    t2 = now + 1.0
    acts = []
    st, a = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), t2)); acts += a
    st, a = _feed(p, st, BookUpdate(STK_SD, _sd("0.60"), t2)); acts += a
    retries = [a for a in acts if a.kind == ActionKind.RETRY_WING]
    assert len(retries) == 1 and len(retries[0].legs) == 1 and retries[0].legs[0].side == BUY_NO
    no_leg = next(l for l in _legs(st, idx) if l.side == BUY_NO)
    assert no_leg.status == "pending" and no_leg.client_order_id  # a real coid minted AT retry, not before
    # fill the retried NO leg -> batch completes, credited, NOT one-legged (the 10-09 path works on HEAD).
    st, _ = _feed(p, st, Fill(None, no_leg.client_order_id, Decimal(no_leg.count), no_leg.limit,
                              BUY_NO, t2 + 0.05))
    b = st.wing_batches[0]
    assert b.completed and not b.one_legged and not st.one_legged
    assert st.sets_done == 11 and _hedged_lots_from_state(st) == 11


def test_deferred_retry_respects_lock_floor():
    # an absurd lock_floor blocks the deferred-leg retry through the SAME gate the normal retry uses --
    # the partial path does NOT bypass lock_floor.
    p = _params(tol=Decimal("0.01"), deb_ms=0, lock_floor=Decimal("5.00"))
    st = _state(p)
    now = T - 600
    st = _fill_ladder(p, st, now)
    st, _ = _close_with(p, st, now, sd=_sd("0.60"), su=_no_ask_top())
    idx = st.wing_batches[0].index
    yes_leg = next(l for l in _legs(st, idx) if l.side == BUY_YES)
    st, _ = _feed(p, st, Fill(None, yes_leg.client_order_id, Decimal(yes_leg.count), yes_leg.limit,
                              BUY_YES, now + 0.8))
    t2 = now + 1.0
    acts = []
    st, a = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), t2)); acts += a
    st, a = _feed(p, st, BookUpdate(STK_SD, _sd("0.60"), t2)); acts += a
    assert not [a for a in acts if a.kind == ActionKind.RETRY_WING]   # floor not cleared -> no retry
    assert next(l for l in _legs(st, idx) if l.side == BUY_NO).status == "unfilled"


# ===========================================================================
# (c) both legs priceable -> byte-identical to today (a normal two-leg take, no partial path)
# ===========================================================================
def test_both_priceable_is_unchanged_two_leg_take():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st = _fill_ladder(p, st, now)
    t = now + 0.7
    acts = []
    st, a = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), t)); acts += a
    st, a = _feed(p, st, BookUpdate(STK_SD, _sd("0.76"), t)); acts += a
    takes = [a for a in acts if a.kind == ActionKind.TAKE_WINGS]
    assert len(takes) == 1 and takes[0].count == 11
    # both legs priced (no None limit), both pending with real coids -> no partial/deferred leg exists.
    tk = takes[0]
    assert all(l.limit is not None for l in tk.legs) and len(tk.legs) == 2
    legs = _legs(st, st.wing_batches[0].index)
    assert all(l.status == "pending" and l.client_order_id for l in legs)
    # the take's lock is computed (the normal path), not None.
    assert takes[0].lock is not None


# ===========================================================================
# (d) neither leg priceable, or the strike feed is dead -> waits exactly as before
# ===========================================================================
def test_neither_leg_priceable_waits():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st = _fill_ladder(p, st, now)
    st, acts = _close_with(p, st, now, sd=_no_ask_top("0.59"), su=_no_ask_top())   # BOTH legs no ask
    assert not [a for a in acts if a.kind in (ActionKind.TAKE_WINGS, ActionKind.RETRY_WING)]
    b = st.wing_batches[0]
    assert not b.taken and not b.one_legged and not _legs(st, b.index)   # nothing sent, batch waits


def test_feed_dead_waits_even_with_a_quotable_book():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st = _fill_ladder(p, st, now)
    # a ClockTick far past strike_feed_dead_s (4.0 s) with no fresh strike fold -> the feed is DEAD; even
    # though STK_SD's book still carries an ask, the partial path must NOT send (same gate as the take law).
    t = now + 0.5 + p.strike_feed_dead_s + 1.0
    st, acts = _feed(p, st, ClockTick(t))
    assert not _strike_feed_alive(p, st, t)
    assert not [a for a in acts if a.kind in (ActionKind.TAKE_WINGS, ActionKind.RETRY_WING)]
    assert not st.wing_batches[0].taken


# ===========================================================================
# (e) settle cutoff with the deferred leg never priceable -> one_legged, as today
# ===========================================================================
def test_cutoff_with_deferred_leg_never_priceable_is_one_legged():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st = _fill_ladder(p, st, now)
    st, _ = _close_with(p, st, now, sd=_sd("0.60"), su=_no_ask_top())   # YES sent, NO deferred/unfilled
    idx = st.wing_batches[0].index
    assert st.wing_batches[0].taken
    # the deferred NO leg never gets a book; the window reaches the settle cutoff.
    t_cut = st.close_epoch - p.no_orders_after_s_to_settle + 0.001
    st, acts = _feed(p, st, ClockTick(t_cut))
    b = st.wing_batches[0]
    assert b.one_legged and not b.completed          # a bounded, unhedged set -> drives S1_LEGGED
    assert st.one_legged


# ===========================================================================
# (f) print-through batches are untouched by the partial path
# ===========================================================================
def test_print_through_batch_not_partial_taken():
    # build a print-through batch via the print-through flow, then starve a wing and ClockTick: the
    # partial path must NOT touch it (PT batches hit ``if b.print_through: continue`` before the branch).
    from tests.test_v33_print_through import (
        _bring_up as _pt_bring_up,
        _params as _pt_params,
        _state as _pt_state,
    )
    from service.v33 import Trade

    p = _pt_params(print_through_ticks=1)
    st = _pt_state(p)
    now = T - 600
    st = _pt_bring_up(p, st, now)
    # a print one tick below the top offer fires the pre-emptive (print-through) take.
    st, acts = _feed(p, st, Trade("KXBTC-RANGE-B79600", Decimal("0.49"), "yes", Decimal(5), now + 0.5))
    pt_batches = [b for b in st.wing_batches if b.print_through]
    assert pt_batches, "expected a print-through batch from the trigger"
    pt = pt_batches[0]
    taken_before = pt.taken
    legs_before = {(l.ticker, l.side, l.status, l.client_order_id) for l in _legs(st, pt.index)}
    # starve a wing and tick: the partial branch is for non-print-through batches only.
    t = now + 0.7
    st, a1 = _feed(p, st, BookUpdate(STK_SU, _no_ask_top(), t))
    st, a2 = _feed(p, st, ClockTick(t + 0.001))
    pt2 = next(b for b in st.wing_batches if b.index == pt.index)
    # no deferred/empty-coid leg was injected into the PT batch by the partial path.
    legs_after = {(l.ticker, l.side, l.status, l.client_order_id) for l in _legs(st, pt.index)}
    assert legs_after == legs_before
    assert pt2.print_through and pt2.taken == taken_before


# ===========================================================================
# per-leg gate helper: identical per-strike conditions to the V3.2 law
# ===========================================================================
def test_wing_leg_ask_mirrors_v32_conditions():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st = _fill_ladder(p, st, now)
    t = now + 0.7
    st, _ = _feed(p, st, BookUpdate(STK_SD, _sd("0.60"), t))
    # Sd=79600 strike, YES leg: present, fresh, ask 0.60 in (0,1] -> returns 0.60.
    assert _wing_leg_ask(st, 79600, BUY_YES, t, p) == Decimal("0.60")
    # a strike never seen -> None; a suspect book -> None; no ask -> None.
    assert _wing_leg_ask(st, 99999, BUY_YES, t, p) is None
    st, _ = _feed(p, st, BookUpdate(STK_SU, _no_ask_top(), t))
    assert _wing_leg_ask(st, 79700, BUY_NO, t, p) is None               # no_ask absent
    st, _ = _feed(p, st, BookUpdate(STK_SU, replace(_top("0.36", "0.37"), suspect=True), t))
    assert _wing_leg_ask(st, 79700, BUY_NO, t, p) is None               # suspect


# ===========================================================================
# the 10-10 incident fixture: the lower wing's take goes out AT the fill, not 37 s later
# ===========================================================================
def test_incident_lower_wing_take_at_fill_not_37s_later():
    """Reconstructs the 2026-10-10 20:00Z book state at the fill (real fill ladder 0.49..0.59; upper wing
    NO@Su with no ask; lower wing YES@Sd quotable ~0.60) and asserts the fix sends the LOWER wing AT the
    fill moment instead of waiting 37 s for the 0.99 upper ask (by which point the real run paid 0.82).

    A FOCUSED fixture (not a full journal-driven replay): driving the whole v33 core from the 12 MB /
    702k-frame journal is not cheap, and the incident is fully characterised by these two book states.
    The lower-wing ask 0.60 is the incident's stated fill-time value; the assertion is about TIMING (take
    at the fill vs 37 s later) and the deferred-leg mechanics, which do not depend on the exact cent."""
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    # 11 rungs fill in ~60 ms (the real incident filled 19:54:54.648 .. .710).
    st = _fill_ladder(p, st, now, n=11)
    assert st.rungs_filled == 11
    # the incident book state at the fill: NO@Su has no ask, YES@Sd quotable ~0.60. Close the window.
    st, acts = _close_with(p, st, now, sd=_sd("0.60"), su=_no_ask_top("0.98"))
    takes = [a for a in acts if a.kind == ActionKind.TAKE_WINGS]
    assert len(takes) == 1                   # the lower wing is TAKEN at the fill -- not blocked.
    yes_leg = next(l for l in takes[0].legs if l.side == BUY_YES)
    # priced at the fill-time ask (~0.60 + margin), NOT the 0.82 the real run paid 37 s later.
    assert yes_leg.limit == min(Decimal("0.60") + p.wing_margin, Decimal("0.99"))
    assert yes_leg.limit < Decimal("0.82")
    # the upper wing is deferred (unfilled, no coid) -- "treated like an empty wing".
    idx = st.wing_batches[0].index
    no_leg = next(l for l in _legs(st, idx) if l.side == BUY_NO)
    assert no_leg.status == "unfilled" and no_leg.client_order_id == ""
    assert st.wing_batches[0].taken


def test_fixture_not_holdout_or_seal():
    # the fixture is synthetic (CLOSE = 2026-09-04); it references no sealed (08-02..18) or holdout
    # (08-20..29) date and reads no journal at test time.
    from tests.test_v33_core import CLOSE
    assert CLOSE == "2026-09-04T20:00:00Z"
