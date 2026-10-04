"""V3.3 stale-wing SUB-CAUSE labeling -- REVIEWER hardening (PR #127 review, 2026-10-03).

These close coverage gaps the builder's ``test_v33_stale_wing_subcause.py`` left open, verified by hand
during review. ALL assert the LABEL-ONLY contract; none change behaviour:

  * classifier gate-ORDER when TWO checks fail at once (the label must be the FIRST in gate order);
  * classifier NEVER raises (None book, None spot pair, exotic asks) -> ``unknown``;
  * the ``wing_price_invalid_su`` label (the builder tested only the ``_sd`` twin);
  * the open-interval (0, 1) label choice is bit-parity with the REST gate ``_v33_compute_W`` at ask==1;
  * the PLAIN stale stand_down path (hold DISABLED): the driver attaches ``wing_sub_cause`` and tallies
    ONE episode per stale transition -- deduped, so a multi-tick stale run counts once, not per tick.

FAKES ONLY -- no network, no proxy, no journal read, no sealed/holdout data.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from service.book import TopOfBook
from service.v33 import ActionKind
from service.v33.core import _v33_compute_W, wing_unavailable_cause
from tests.test_v33_core import B_SD, T, _params, _state, _sd, _top
from tests.test_v33_stale_wing_subcause import (
    SD,
    SU,
    _bring_up_driver,
    _cstate,
    _drv,
    _invalid_yes_ask,
    _no_ask_none,
    _payloads,
    _yes_bids_only,
)

_ONE = Decimal(1)
_ZERO = Decimal(0)


# ---------------------------------------------------------------------------
# classifier gate-ORDER when more than one check fails at once
# ---------------------------------------------------------------------------
def test_two_failures_su_missing_and_sd_no_ask_labels_missing_first():
    """Su MISSING (missing class) AND Sd has no yes_ask (no_ask class). The REST gate hits the missing
    check first, so the label must be ``wing_missing_su``, never ``wing_no_ask_sd``."""
    p = _params()
    st = _cstate(p, feed_ts=1000.0, sd_top=_yes_bids_only("0.50"))   # Su absent, Sd ask-less
    assert st.strike_tops.get(SU) is None and st.strike_tops[SD].yes_ask is None
    assert wing_unavailable_cause(p, st, 1000.0) == "wing_missing_su"


def test_two_failures_feed_dead_and_sd_missing_labels_feed_dead_first():
    """Feed dead (first gate check) AND Sd missing: the label is ``feed_dead``."""
    p = _params()
    now = p.strike_feed_dead_s + 1.0
    st = _cstate(p, feed_ts=0.0, su_top=_top("0.36", "0.37"))        # Sd absent, feed stale
    assert wing_unavailable_cause(p, st, now) == "feed_dead"


def test_two_failures_sd_suspect_beats_su_too_old():
    """Sd suspect (suspect class) AND Su too-old (age class). Gate order puts suspect before age, so the
    label is ``wing_suspect_sd``."""
    p = _params(wing_book_max_age_s=4.0)
    now = 1000.0
    st = _cstate(p, feed_ts=now, sd_top=_sd("0.76"), su_top=_top("0.36", "0.37"),
                 sd_ts=now, su_ts=now - 10.0)
    st = replace(st, strike_tops={**st.strike_tops, SD: replace(st.strike_tops[SD], suspect=True)})
    assert wing_unavailable_cause(p, st, now) == "wing_suspect_sd"


# ---------------------------------------------------------------------------
# the _su price-invalid twin (builder covered only _sd)
# ---------------------------------------------------------------------------
def test_cause_wing_price_invalid_su():
    p = _params()
    su = TopOfBook(yes_bid=Decimal("0.10"), yes_bid_size=Decimal(100), yes_ask=Decimal("0.11"),
                   yes_ask_size=Decimal(100), no_bid=_ZERO, no_bid_size=Decimal(100),
                   no_ask=_ONE, no_ask_size=Decimal(100), suspect=False)   # no_ask at exactly 1.00
    st = _cstate(p, feed_ts=1000.0, sd_top=_sd("0.76"), su_top=su)
    assert wing_unavailable_cause(p, st, 1000.0) == "wing_price_invalid_su"


# ---------------------------------------------------------------------------
# open-interval (0, 1): the label is bit-parity with the REST gate's own verdict
# ---------------------------------------------------------------------------
def test_ask_at_one_label_matches_rest_gate_refusal():
    """An ask at exactly 1.00 fails the REST gate's open interval -> the gate returns None AND the label
    is ``wing_price_invalid_sd``. (If the gate ever used <= 1, this ask would price and the label would be
    a lie -- this pins them together.)"""
    p = _params()
    st = _cstate(p, feed_ts=1000.0, sd_top=_invalid_yes_ask(), su_top=_top("0.36", "0.37"))
    assert _v33_compute_W(st, SD, SU, 1000.0, p) is None
    assert wing_unavailable_cause(p, st, 1000.0) == "wing_price_invalid_sd"


# ---------------------------------------------------------------------------
# the classifier NEVER raises
# ---------------------------------------------------------------------------
def test_classifier_never_raises_on_none_spot_pair():
    p = _params()
    st = replace(_state(p), spot_Sd=None, spot_Su=None)
    assert wing_unavailable_cause(p, st, 1000.0) == "unknown"


def test_classifier_never_raises_on_none_book_tops():
    """A strike key mapped to a None top (should not happen, but a diagnostic label must survive it) ->
    treated as missing, never an exception."""
    p = _params()
    st = replace(_state(p), spot_Sd=SD, spot_Su=SU, strike_feed_ts=1000.0,
                 strike_tops={SD: None, SU: None}, strike_ts={SD: 1000.0, SU: 1000.0})  # type: ignore[dict-item]
    assert wing_unavailable_cause(p, st, 1000.0) == "wing_missing_sd"


def test_classifier_never_raises_on_both_asks_none():
    """Both wing asks None (Sd no yes_ask, Su no no_ask): Sd is checked first -> ``wing_no_ask_sd``, no
    raise."""
    p = _params()
    st = _cstate(p, feed_ts=1000.0, sd_top=_yes_bids_only("0.50"), su_top=_no_ask_none("0.30"))
    assert wing_unavailable_cause(p, st, 1000.0) == "wing_no_ask_sd"


# ---------------------------------------------------------------------------
# the PLAIN stale stand_down path (hold DISABLED): sub_cause + one-episode tally
# ---------------------------------------------------------------------------
def test_plain_stale_standdown_carries_sub_cause_and_counts_one_episode():
    """With ``stand_down_hold_ms == 0`` the hold branch is skipped: a stale wing goes straight to the plain
    ``stand_down``. It must carry the live ``wing_sub_cause`` and tally exactly ONE episode no matter how
    many stale ticks follow (``_standdown`` dedups on the reason transition)."""
    p = _params(tol=Decimal("0.01"), deb_ms=0, stand_down_hold_ms=0)
    drv = _drv(p)
    now = T - 600
    _bring_up_driver(drv, now)
    # kill the strike feed; keep bucket frames flowing so _converge runs for many ticks.
    t = now
    while t < now + p.strike_feed_dead_s + 5.0:
        t += 0.5
        drv.on_book_update(B_SD, _top("0.35", "0.36"), t)
    assert not _payloads(drv, "stand_down_hold")           # hold disabled -> no hold lifecycle
    plain = [o for k, o in drv.journal.recs if k == "stand_down"
             and o.get("reason") == "stale_or_missing_wing"]
    assert len(plain) == 1, f"expected ONE stale episode, got {len(plain)}"   # deduped across ticks
    assert plain[0]["sub_cause"] == "feed_dead"
    assert dict(drv._stand_down_sub_causes) == {"feed_dead": 1}


def test_plain_stale_episode_tally_equals_emitted_standdowns():
    """The accumulator total equals the number of EMITTED stale stand_downs (one per episode), not ticks."""
    p = _params(tol=Decimal("0.01"), deb_ms=0, stand_down_hold_ms=0)
    drv = _drv(p)
    now = T - 600
    _bring_up_driver(drv, now)
    t = now
    while t < now + p.strike_feed_dead_s + 3.0:
        t += 0.5
        drv.on_book_update(B_SD, _top("0.35", "0.36"), t)
    emitted = sum(1 for k, o in drv.journal.recs if k == "stand_down"
                  and o.get("reason") == "stale_or_missing_wing")
    assert sum(dict(drv._stand_down_sub_causes).values()) == emitted
