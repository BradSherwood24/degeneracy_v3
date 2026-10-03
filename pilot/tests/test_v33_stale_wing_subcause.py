"""V3.3 stale-wing SUB-CAUSE labeling (2026-10-03): the diagnostic label on the
``stale_or_missing_wing`` stand-down. LABEL ONLY -- no behaviour change; the reason strings, dedup,
falsifier and economics are untouched. Covered here:

  * the pure classifier ``wing_unavailable_cause`` -- one minimal state per label, in the gate's own
    order of checks, plus "W present -> classifier not consulted";
  * the hold / cancel / resume JOURNAL payloads carry ``sub_cause`` (driven through the real V33Driver);
  * the additive ledger count ``stand_down_sub_causes`` appears and sums to the hold count;
  * tonight's motivating case as a golden: a wing strike with YES bids only (``yes_ask`` None) holds
    with ``sub_cause == wing_no_ask_sd``, cancels after ``stand_down_hold_ms``, and never re-places
    (the #120 no-re-place harness).

FAKES ONLY -- no network, no proxy, no journal read, no sealed/holdout data.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from service.book import TopOfBook
from service.run_v32 import FrozenExecutor
from service.v33 import (
    ActionKind,
    BookUpdate,
    OrderCancelled,
    V33State,
    load_v33_params,
)
from service.v33.core import _strike_feed_alive, wing_unavailable_cause
from service.v33.ledger import build_v33_ledger_row
import service.run_v33 as RUN
from tests.test_v33_core import (
    B_SD,
    BK,
    CLOSE,
    STK_SD,
    STK_SU,
    T,
    _bring_up_ladder,
    _feed,
    _params,
    _sd,
    _state,
    _top,
)

_ONE = Decimal(1)
SD, SU = 79600, 79700   # the strike floors of STK_SD / STK_SU under BK


# ===========================================================================
# minimal-state TopOfBook builders for the None / invalid-price cases
# ===========================================================================
def _yes_bids_only(bid: str = "0.50") -> TopOfBook:
    """A strike book with YES bids but no YES offers at any price (its last NO bid was lifted) ->
    ``yes_ask`` is None. This is tonight's 23:00Z shape on the YES-leg wing strike."""
    b = Decimal(bid)
    return TopOfBook(yes_bid=b, yes_bid_size=Decimal(100), yes_ask=None, yes_ask_size=None,
                     no_bid=None, no_bid_size=None, no_ask=_ONE - b, no_ask_size=Decimal(100),
                     suspect=False)


def _no_ask_none(bid: str = "0.30") -> TopOfBook:
    """A Su strike book with no YES bids -> ``no_ask`` (= 1 - yes_bid of the NO offer) is None, while its
    own yes_ask is present (so the Sd checks pass and the classifier reaches the Su ask-present check)."""
    b = Decimal(bid)
    return TopOfBook(yes_bid=b, yes_bid_size=Decimal(100), yes_ask=b + Decimal("0.01"),
                     yes_ask_size=Decimal(100), no_bid=_ONE - (b + Decimal("0.01")),
                     no_bid_size=Decimal(100), no_ask=None, no_ask_size=None, suspect=False)


def _invalid_yes_ask() -> TopOfBook:
    """yes_ask at exactly 1.00 -- OUTSIDE the rest gate's open interval (0, 1), so ``_compute_W`` refuses
    it. Present (not None), so the classifier reaches the price-validity check."""
    return TopOfBook(yes_bid=Decimal("0.99"), yes_bid_size=Decimal(100), yes_ask=_ONE,
                     yes_ask_size=Decimal(100), no_bid=Decimal(0), no_bid_size=Decimal(100),
                     no_ask=Decimal("0.01"), no_ask_size=Decimal(100), suspect=False)


def _cstate(p, *, feed_ts, sd_top=None, su_top=None, sd_ts=None, su_ts=None):
    """A minimal state carrying only what the classifier reads: the spot pair, the strike-feed ts, and the
    two wing strike books / their ages."""
    tops: dict[int, TopOfBook] = {}
    ts: dict[int, float] = {}
    if sd_top is not None:
        tops[SD] = sd_top
        ts[SD] = sd_ts if sd_ts is not None else feed_ts
    if su_top is not None:
        tops[SU] = su_top
        ts[SU] = su_ts if su_ts is not None else feed_ts
    return replace(_state(p), spot_Sd=SD, spot_Su=SU, strike_feed_ts=feed_ts,
                   strike_tops=tops, strike_ts=ts)


# ===========================================================================
# 1. the pure classifier -- one minimal state per label, in gate order
# ===========================================================================
def test_cause_feed_dead():
    p = _params()
    now = p.strike_feed_dead_s + 1.0          # last strike frame at ts 0 -> feed age > dead bound
    st = _cstate(p, feed_ts=0.0, sd_top=_sd("0.76"), su_top=_top("0.36", "0.37"))
    assert not _strike_feed_alive(p, st, now)
    assert wing_unavailable_cause(p, st, now) == "feed_dead"


def test_cause_wing_missing_sd():
    p = _params()
    st = _cstate(p, feed_ts=1000.0, su_top=_top("0.36", "0.37"))   # Sd never seen
    assert wing_unavailable_cause(p, st, 1000.0) == "wing_missing_sd"


def test_cause_wing_missing_su():
    p = _params()
    st = _cstate(p, feed_ts=1000.0, sd_top=_sd("0.76"))            # Su never seen
    assert wing_unavailable_cause(p, st, 1000.0) == "wing_missing_su"


def test_cause_wing_suspect_su():
    p = _params()
    su = _top("0.36", "0.37", suspect=True)
    st = _cstate(p, feed_ts=1000.0, sd_top=_sd("0.76"), su_top=su)
    assert wing_unavailable_cause(p, st, 1000.0) == "wing_suspect_su"


def test_cause_wing_too_old_sd():
    p = _params(wing_book_max_age_s=4.0)
    now = 1000.0
    # feed alive (Su just ticked) but the Sd book is 5 s old > the loose bound.
    st = _cstate(p, feed_ts=now, sd_top=_sd("0.76"), su_top=_top("0.36", "0.37"),
                 sd_ts=now - 5.0, su_ts=now)
    assert _strike_feed_alive(p, st, now)
    assert wing_unavailable_cause(p, st, now) == "wing_too_old_sd"


def test_cause_wing_no_ask_sd():
    p = _params()
    st = _cstate(p, feed_ts=1000.0, sd_top=_yes_bids_only("0.50"), su_top=_top("0.36", "0.37"))
    assert wing_unavailable_cause(p, st, 1000.0) == "wing_no_ask_sd"


def test_cause_wing_no_ask_su():
    p = _params()
    st = _cstate(p, feed_ts=1000.0, sd_top=_sd("0.76"), su_top=_no_ask_none("0.30"))
    assert wing_unavailable_cause(p, st, 1000.0) == "wing_no_ask_su"


def test_cause_wing_price_invalid_sd():
    p = _params()
    st = _cstate(p, feed_ts=1000.0, sd_top=_invalid_yes_ask(), su_top=_top("0.36", "0.37"))
    assert wing_unavailable_cause(p, st, 1000.0) == "wing_price_invalid_sd"


def test_w_present_classifier_not_consulted():
    """When the W gate produces a value, ``wing_sub_cause`` is never set (the classifier is consulted
    ONLY on a None gate)."""
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)
    assert st.W is not None and st.wing_sub_cause is None


# ===========================================================================
# 2. the hold / cancel / resume JOURNAL payloads carry sub_cause (real driver)
# ===========================================================================
class _J:
    def __init__(self) -> None:
        self.recs: list[tuple[str, dict]] = []

    def append(self, k, o, t) -> None:
        self.recs.append((k, dict(o)))

    def kinds(self):
        return [k for k, _ in self.recs]


def _drv(p):
    # FrozenExecutor only handles WOULD_* twins, so the driver runs in shakedown/dry_sim (as the golden
    # replay does). The stand-down hold/cancel/resume journaling is mode-independent (STAND_DOWN has no
    # WOULD_* twin), so the sub_cause payloads are exercised exactly as armed.
    st = V33State.new(CLOSE, T, BK, p, shakedown=True)
    return RUN.V33Driver(p, st, _J(), FrozenExecutor(BK), dry_sim=True, clock=lambda: 0.0)


def _bring_up_driver(drv, now):
    # strikes BEFORE the bucket: with no spot bucket yet the stand-down is ``no_spot_bucket`` (not a
    # stale-wing episode), so the sub_cause accumulator starts clean -- the only stale episodes are the
    # ones each test drives.
    drv.on_book_update(STK_SU, _top("0.36", "0.37"), now)
    drv.on_book_update(STK_SD, _sd("0.76"), now)
    drv.on_book_update(B_SD, _top("0.35", "0.36"), now)
    assert len(drv.state.ladder) == 11 and all(o.live for o in drv.state.ladder)
    assert not dict(drv._stand_down_sub_causes)


def _payloads(drv, kind):
    return [o for k, o in drv.journal.recs if k == kind]


def test_journal_hold_and_cancel_carry_sub_cause():
    p = _params(tol=Decimal("0.01"), deb_ms=0)      # dead 4.0 s, hold 1500 ms
    drv = _drv(p)
    now = T - 600
    _bring_up_driver(drv, now)
    # strikes go silent -> feed dies; bucket frames keep the clock moving and run _converge.
    t = now
    while t < now + p.strike_feed_dead_s + p.stand_down_hold_ms / 1000.0 + 1.0:
        t += 0.5
        drv.on_book_update(B_SD, _top("0.35", "0.36"), t)
    holds = _payloads(drv, "stand_down_hold")
    cancels = _payloads(drv, "stand_down_cancel")
    assert len(holds) == 1 and holds[0]["sub_cause"] == "feed_dead"
    assert holds[0]["reason"] == "stale_or_missing_wing"      # reason string UNCHANGED
    assert len(cancels) == 1 and cancels[0]["sub_cause"] == "feed_dead"
    assert cancels[0]["reason"] == "stale_or_missing_wing"


def test_journal_resume_carries_the_held_sub_cause():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    drv = _drv(p)
    now = T - 600
    _bring_up_driver(drv, now)
    drv.on_book_update(B_SD, _top("0.35", "0.36"), now + p.strike_feed_dead_s + 0.5)   # dead -> HOLD
    assert len(_payloads(drv, "stand_down_hold")) == 1
    # fresh strikes BEFORE the hold elapses -> RESUME carrying the held label.
    t = now + p.strike_feed_dead_s + 1.0
    drv.on_book_update(STK_SU, _top("0.36", "0.37"), t)
    drv.on_book_update(STK_SD, _sd("0.76"), t)
    resumes = _payloads(drv, "stand_down_resume")
    assert len(resumes) == 1 and resumes[0]["sub_cause"] == "feed_dead"
    assert resumes[0]["reason"] == "stale_or_missing_wing"
    assert not _payloads(drv, "stand_down_cancel")            # resumed before the hold expired


def test_eval_record_gains_wing_sub_cause_only_when_w_none():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    drv = _drv(p)
    now = T - 600
    _bring_up_driver(drv, now)
    evals_healthy = [o for k, o in drv.journal.recs if k == "v33_eval"]
    assert evals_healthy and all("wing_sub_cause" not in o for o in evals_healthy if o.get("W") is not None)
    # drive the feed dead, then read an eval emitted while W is None.
    t = now
    while t < now + p.strike_feed_dead_s + 1.5:
        t += 0.5
        drv.on_book_update(B_SD, _top("0.35", "0.36"), t)
    w_none_evals = [o for k, o in drv.journal.recs if k == "v33_eval" and o.get("W") is None]
    assert w_none_evals and w_none_evals[-1]["wing_sub_cause"] == "feed_dead"


# ===========================================================================
# 3. the additive ledger count appears and sums to the hold count
# ===========================================================================
def test_ledger_stand_down_sub_causes_sums_to_hold_count():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    drv = _drv(p)
    now = T - 600
    _bring_up_driver(drv, now)
    t = now
    while t < now + p.strike_feed_dead_s + p.stand_down_hold_ms / 1000.0 + 1.0:
        t += 0.5
        drv.on_book_update(B_SD, _top("0.35", "0.36"), t)
    hold_count = len(_payloads(drv, "stand_down_hold"))
    assert hold_count == 1
    subc = dict(drv._stand_down_sub_causes)
    assert subc == {"feed_dead": 1}
    assert sum(subc.values()) == hold_count
    row = build_v33_ledger_row(
        close_time=CLOSE, resolved_mode="dry", effective_mode="dry", degrade=None, params=p,
        state=drv.state, driver_counts=dict(drv.counts), executor_counts={}, ws_counts={},
        strike_count=2, bucket_count=2, journal_path=None, record_count=0, stand_down_reason=None,
        now=0.0, stand_down_sub_causes=subc,
    )
    assert row["stand_down_sub_causes"] == {"feed_dead": 1}


def test_ledger_stand_down_sub_causes_empty_by_default():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    row = build_v33_ledger_row(
        close_time=CLOSE, resolved_mode="dry", effective_mode="dry", degrade=None, params=p,
        state=None, driver_counts={}, executor_counts={}, ws_counts={}, strike_count=0, bucket_count=0,
        journal_path=None, record_count=0, stand_down_reason=None, now=0.0,
    )
    assert row["stand_down_sub_causes"] == {}


# ===========================================================================
# 4. tonight's case as a golden -- YES bids only on the Sd wing (#120 no-re-place harness)
# ===========================================================================
def _kinds(acts, kind):
    return [a for a in acts if a.kind == kind]


def test_golden_yes_bids_only_sd_holds_wing_no_ask_sd_then_cancels_no_replace():
    p = _params(tol=Decimal("0.01"), deb_ms=0)
    st = _state(p)
    now = T - 600
    st, _ = _bring_up_ladder(p, st, now)
    oids = [o.order_id for o in st.ladder]
    assert len(oids) == 11
    # the YES-leg wing strike loses its last NO bid -> yes_ask None, the strike FEED stays alive.
    t = now + 0.1
    st, a = _feed(p, st, BookUpdate(STK_SD, _yes_bids_only("0.50"), t))
    assert _strike_feed_alive(p, st, t)
    assert st.W is None and st.wing_sub_cause == "wing_no_ask_sd" and st.hold_sub_cause == "wing_no_ask_sd"
    assert [x for x in a if x.reason == "stale_or_missing_wing_hold"] and len(st.ladder) == 11
    assert not _kinds(a, ActionKind.CANCEL_REST)
    # the hold elapses -> tracked cancel-all; the book still offers no ask.
    t2 = t + p.stand_down_hold_ms / 1000.0 + 0.05
    st, a = _feed(p, st, BookUpdate(STK_SD, _yes_bids_only("0.50"), t2))
    assert [x for x in a if x.reason == "stale_or_missing_wing_cancel"]
    assert len(_kinds(a, ActionKind.CANCEL_REST)) == 11
    assert st.ladder == () and st.awaiting_replace and st.outstanding_cancels == 11
    assert st.wing_sub_cause == "wing_no_ask_sd"
    # confirm every cancel -> the gate is still None (yes_ask None), so NOTHING re-places.
    for i, oid in enumerate(oids):
        st, x = _feed(p, st, OrderCancelled(oid, t2 + 0.1 + i * 0.01, Decimal(0)))
        assert not _kinds(x, ActionKind.PLACE_REST), f"re-placed after confirm {i + 1}"
    assert st.ladder == () and st.outstanding_cancels == 0
