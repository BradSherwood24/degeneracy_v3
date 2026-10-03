"""V3.3 FILL-ATTRIBUTION regression tests — the 2026-09-30 22:00Z first-live-fill incident.

Reproduces, from a fixture cut read-only from the real live journal
(``tests/fixtures/v33/incident_20260930T220000Z_slice.jsonl``; provenance in the fixture header), the
defect chain the Registration entry (2026-09-30 21:50:55Z) registered, and pins each fix D1-D4:

  D1  fill-to-bucket attribution comes from the ORDER's OWN bucket (retained through cancel-all via
      ``cancel_ctx``) or the fill's own market ticker — NEVER the current spot bucket; unnameable bucket
      fails CLOSED (no wings, stand down).
  D2  wing strikes are solved from the FILLED rung's bucket, not ``st.spot_Sd`` (which had moved on).
  D3  fractional counts (``count_fp`` 1.00 / 0.44) are parsed exactly end-to-end; wings size to 1.44.
  D4  a missing wing leg is retried no more often than ``WING_RETRY_MIN_INTERVAL_MS`` (250 ms), one in
      flight — not the incident's 967 per-tick IOC creates.

No network, no disk beyond the shipped policy + the fixture. The SEAL (2026-08-02..18) is never touched;
the fixture is from 2026-09-30, outside the seal.
"""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from service.book import TopOfBook
from service.v33 import (
    ActionKind,
    BookUpdate,
    Fill,
    OrderAck,
    OrderCancelled,
    V33Params,
    V33State,
    decide_v33,
    load_v33_params,
)
from service.v33.core import (
    WING_RETRY_MIN_INTERVAL_MS,
    _book_rung_fill,
    _sd_for_bucket_ticker,
)

# --- the same synthetic board the core goldens use (79600/79700 stand in for the incident's 83600/83700)
CLOSE = "2026-09-04T20:00:00Z"
T = 1_000_000
BK = {"KXBTC-RANGE-B79600": (79600.0, 79699.99), "KXBTC-RANGE-B79700": (79700.0, 79799.99)}
STK_SD = "KXBTCD-26SEP0416-T79599.99"    # strike for bucket floor 79600 (the YES wing on the 79600 fill)
STK_SU = "KXBTCD-26SEP0416-T79699.99"    # strike for bucket floor 79700 (the NO wing on the 79600 fill)
STK_SU2 = "KXBTCD-26SEP0416-T79799.99"   # strike for bucket floor 79800 (the WRONG NO wing = 79700 spot)
B_SD = "KXBTC-RANGE-B79600"
B_SU = "KXBTC-RANGE-B79700"

FIXTURE = Path(__file__).parent / "fixtures" / "v33" / "incident_20260930T220000Z_slice.jsonl"


# ===========================================================================
# harness (mirrors tests/test_v33_core.py)
# ===========================================================================
def _top(bid: str, ask: str, *, suspect: bool = False) -> TopOfBook:
    yb, ya = Decimal(bid), Decimal(ask)
    return TopOfBook(
        yes_bid=yb, yes_bid_size=Decimal(100), yes_ask=ya, yes_ask_size=Decimal(100),
        no_bid=Decimal(1) - ya, no_bid_size=Decimal(100), no_ask=Decimal(1) - yb,
        no_ask_size=Decimal(100), suspect=suspect,
    )


def _sd(ask: str) -> TopOfBook:
    return _top(str(Decimal(ask) - Decimal("0.01")), ask)


def _params(**over) -> V33Params:
    p = replace(load_v33_params(), E_min=Decimal("0.05"))   # pin to the 5..15c anchor for exact prices
    p = replace(p, tol=Decimal("0.01"), deb_ms=0, bucket_switch_deb_ms=0, bucket_switch_hysteresis_usd=0)
    return replace(p, **over) if over else p


def _state(p: V33Params) -> V33State:
    return V33State.new(CLOSE, T, BK, p)


def _feed(p, st, event):
    st, a = decide_v33(p, st, event)
    st.check_invariants(p)
    return st, a


def _feed_all(p, st, events):
    acts = []
    for e in events:
        st, a = _feed(p, st, e)
        acts += a
    return st, acts


def _books(now, sd_ask="0.76", *, b_bid="0.35", su_bid="0.36"):
    return [
        BookUpdate(B_SD, _top(b_bid, str(Decimal(b_bid) + Decimal("0.01"))), now),
        BookUpdate(STK_SU, _top(su_bid, str(Decimal(su_bid) + Decimal("0.01"))), now),
        BookUpdate(STK_SD, _sd(sd_ask), now),
    ]


def _bring_up_ladder(p, st, now, sd_ask="0.76"):
    st, acts = _feed_all(p, st, _books(now, sd_ask))
    places = [a for a in acts if a.kind == ActionKind.PLACE_REST]
    assert places
    for a in places:
        st, _ = _feed(p, st, OrderAck(a.client_order_id, f"OID-{a.client_order_id}", now))
    assert all(o.live for o in st.ladder)
    return st


def _order_at(st, price: str):
    return next(o for o in st.ladder if o.price == Decimal(price))


# ===========================================================================
# D3 — count_fp is parsed EXACTLY (the incident's 0.44 -> full-lot truncation)
# ===========================================================================
def _fixture_fill_frames() -> dict[str, dict]:
    """The two real ``fill`` WS frames from the incident, keyed by client_order_id."""
    out = {}
    for line in FIXTURE.read_text().splitlines():
        rec = json.loads(line)
        if rec.get("kind") == "kalshi_ws" and rec["obj"].get("type") == "fill":
            msg = rec["obj"]["msg"]
            out[msg["client_order_id"]] = msg
    return out


def test_fixture_is_the_real_incident_on_b83650():
    frames = _fixture_fill_frames()
    assert set(frames) == {"v33-2026-09-30T22:00:00Z-312", "v33-2026-09-30T22:00:00Z-313"}
    for msg in frames.values():
        assert msg["market_ticker"] == "KXBTC-26SEP3018-B83650"     # the rungs' OWN bucket (Sd 83600)
        assert msg["purchased_side"] == "no"
    # the true fractional sizes: 1.00 + 0.44 = 1.44 NO.
    assert frames["v33-2026-09-30T22:00:00Z-312"]["count_fp"] == "1.00"
    assert frames["v33-2026-09-30T22:00:00Z-313"]["count_fp"] == "0.44"


def test_count_fp_parsed_fractional_not_truncated_to_a_full_lot():
    """D3: the driver now parses ``count_fp`` as Decimal. The pre-fix
    ``int(pf.get('count') or 0) or rec.count`` truncated 0.44 -> 0 and fell back to the placed lot (1),
    so a 0.44 fill became a whole lot. Pin both the fix and the bug it replaced."""
    from service.run_v32 import _fill_event
    from service.run_v33 import _count_dec

    frames = _fixture_fill_frames()
    f313 = frames["v33-2026-09-30T22:00:00Z-313"]               # count_fp 0.44

    # the fix: raw count_fp -> exact Decimal.
    assert _count_dec(f313["count_fp"]) == Decimal("0.44")
    assert _count_dec(frames["v33-2026-09-30T22:00:00Z-312"]["count_fp"]) == Decimal("1.00")

    # The BUG it replaced: the shared ``_fill_event`` (``run_v32.parse_fill``) used to int-truncate
    # ``count_fp`` to 0 for a sub-1 fill, and the old ``int(pf['count'] or 0) or rec.count`` then fell
    # back to the placed lot (1). The 2026-10-01 V3.2 fractional MECHANICS fixed the SHARED parser too, so
    # ``_fill_event`` now returns the EXACT fraction; run_v33's ``on_fill`` reads ``payload['count_fp']``
    # directly, so its behaviour is unchanged, but the shared parser no longer manufactures the mirage.
    pf = _fill_event(f313)
    assert pf["count"] == Decimal("0.44")
    # the old int-truncation + placed-lot fallback is gone at the source:
    old_buggy = int(pf.get("count") or 0) or 1
    assert old_buggy == 1 and pf["count"] != old_buggy          # exact fraction, not the mirage


# ===========================================================================
# D1 — a late fill surfaced by the cancel confirm attributes to the rung's OWN bucket
# ===========================================================================
def _to_new_bucket(p, st, now):
    """Move spot to 79700 so the ladder (on 79600) is cancelled-all and the bucket commits to 79700."""
    st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now))
    st, _ = _feed(p, st, BookUpdate(STK_SU2, _top("0.20", "0.21"), now))
    st, acts = _feed(p, st, BookUpdate(B_SU, _top("0.55", "0.57"), now))
    return st, acts


def test_late_fill_via_cancel_confirm_books_on_own_bucket_not_spot():
    """D1 (the incident, exactly): rungs rest on 79600; spot moves to 79700 and the bucket-change
    cancel-all tears the ladder down; the two fills surface only via the cancel confirm. They MUST book
    on 79600 (the rungs' own bucket), with the true fractional counts 1.00 / 0.44 — never the moved-on
    spot bucket 79700."""
    p = _params()
    st = _bring_up_ladder(p, _state(p), T - 600)
    assert st.rest_bucket_Sd == 79600
    o312 = _order_at(st, "0.47")     # -312 analogue (filled 1.00)
    o313 = _order_at(st, "0.46")     # -313 analogue (filled 0.44)

    st, acts = _to_new_bucket(p, st, T - 599)
    assert st.awaiting_replace and st.ladder == () and st.spot_Sd == 79700
    assert len(st.cancel_ctx) == 11                         # every torn-down rung retained its bucket
    # every retained record keeps the rung's OWN bucket_Sd (79600), not the new spot (79700).
    assert all(ctx[4] == 79600 for ctx in st.cancel_ctx.values())

    # the fills surface via the cancel confirm (filled_count_before_cancel), fractional.
    st, _ = _feed(p, st, OrderCancelled(o312.order_id, T - 598, Decimal("1.00")))
    st, _ = _feed(p, st, OrderCancelled(o313.order_id, T - 598, Decimal("0.44")))

    fills = {f.coid: f for f in st.rest_fills}
    assert fills[o312.client_order_id].bucket_Sd == 79600
    assert fills[o312.client_order_id].bucket_ticker == B_SD
    assert fills[o312.client_order_id].bucket_Su == 79700
    assert fills[o312.client_order_id].count == Decimal("1.00")
    assert fills[o313.client_order_id].bucket_Sd == 79600      # NOT 79700 (the pre-fix spot fallback)
    assert fills[o313.client_order_id].count == Decimal("0.44")
    assert not st.bucket_unknown                              # the bucket WAS nameable -> no fail-close


# ===========================================================================
# D2 — the wings are solved on the FILL's bucket, not the current spot
# ===========================================================================
def test_wings_solved_on_fill_bucket_strikes_not_spot_strikes():
    """D2: after the two fills book on 79600 while spot is 79700, the coalesced wing take must buy the
    79600-bucket strikes (YES@Sd=79600 = STK_SD, NO@Su=79700 = STK_SU), sized to 1.44 — NOT the
    79700-bucket strikes (STK_SU / STK_SU2) the incident mis-hedged."""
    p = _params()
    st = _bring_up_ladder(p, _state(p), T - 600)
    o312, o313 = _order_at(st, "0.47"), _order_at(st, "0.46")
    st, _ = _to_new_bucket(p, st, T - 599)
    st, _ = _feed(p, st, OrderCancelled(o312.order_id, T - 598, Decimal("1.00")))
    st, _ = _feed(p, st, OrderCancelled(o313.order_id, T - 598.0, Decimal("0.44")))

    # keep the 79600-bucket strike books fresh and advance past the coalesce window to take the wings.
    # gate D (2026-10-03): the take fires on the FIRST post-coalesce tick, priced off the quiet-but-live
    # STK_SD book (feed alive, age < wing_book_max_age_s), so collect both ticks' actions.
    st, a0 = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), T - 597))
    st, acts = _feed(p, st, BookUpdate(STK_SD, _sd("0.76"), T - 597))
    acts = a0 + acts
    takes = [a for a in acts if a.kind == ActionKind.TAKE_WINGS]
    assert len(takes) == 1
    legs = takes[0].legs
    yes_leg = next(l for l in legs if l.side == "yes")
    no_leg = next(l for l in legs if l.side == "no")
    assert yes_leg.ticker == STK_SD                 # 79600 strike (the fill's Sd), not 79700
    assert no_leg.ticker == STK_SU                  # 79700 strike (the fill's Su), not 79800
    assert no_leg.ticker != STK_SU2                 # the incident's wrong NO wing
    assert takes[0].count == Decimal("1.44")
    assert st.wing_batches[0].bucket_Sd == 79600
    assert st.wing_legs[0].count == Decimal("1.44")


# ===========================================================================
# D1 — fail-closed: a fill whose bucket cannot be named takes NO wings and stands down
# ===========================================================================
def test_book_rung_fill_fails_closed_on_unnameable_bucket():
    p = _params()
    st = _bring_up_ladder(p, _state(p), T - 600)
    # a fill for a coid NOT in the ladder, with no retained bucket and no resolvable ticker.
    st2, acts = _book_rung_fill(
        p, st, "ghost-coid", Decimal("0.46"), Decimal("0.44"), 4, Decimal("0.09"), T - 590,
        retained_Sd=None, fill_ticker=None,
    )
    assert st2.bucket_unknown is True
    assert st2.stood_down is True
    assert not [a for a in acts if a.kind == ActionKind.TAKE_WINGS]
    assert any(a.kind == ActionKind.STAND_DOWN and a.reason == "fill_bucket_unknown" for a in acts)
    # the fill IS booked (so the held NO leg is accounted) but on a None bucket, un-hedged.
    rf = st2.rest_fills[-1]
    assert rf.bucket_Sd is None and rf.bucket_ticker is None and rf.count == Decimal("0.44")


def test_ticker_inversion_is_the_last_resort_before_fail_closed():
    """D1: when neither the live order nor a retained record names the bucket, the fill's OWN market
    ticker (inverted through ``st.bucket_tickers``) attributes it — only a truly unknown ticker fails
    closed."""
    p = _params()
    st = _bring_up_ladder(p, _state(p), T - 600)
    assert _sd_for_bucket_ticker(st, B_SD) == 79600
    assert _sd_for_bucket_ticker(st, "KXBTC-RANGE-B00000") is None

    st2, _ = _book_rung_fill(
        p, st, "ghost-coid", Decimal("0.47"), Decimal("1.00"), 3, Decimal("0.08"), T - 590,
        retained_Sd=None, fill_ticker=B_SD,
    )
    rf = st2.rest_fills[-1]
    assert rf.bucket_Sd == 79600 and rf.bucket_ticker == B_SD and not st2.bucket_unknown


# ===========================================================================
# D4 — the wing retry cadence is bounded (not 967 per-tick IOC creates)
# ===========================================================================
def _take_one_batch_with_a_missing_leg(p, st, now):
    """Fill one rung, take its wings, then report ONE wing leg filled and the other unfilled."""
    o = _order_at(st, "0.47")
    st, _ = _feed(p, st, Fill(o.order_id, o.client_order_id, Decimal("1.00"), o.price, "no", now))
    st, a0 = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), now + 0.2))
    st, acts = _feed(p, st, BookUpdate(STK_SD, _sd("0.76"), now + 0.2))
    assert [a for a in a0 + acts if a.kind == ActionKind.TAKE_WINGS]   # gate D: first tick may take
    yes_leg = next(l for l in st.wing_legs if l.side == "yes")
    no_leg = next(l for l in st.wing_legs if l.side == "no")
    st, _ = _feed(p, st, Fill(None, yes_leg.client_order_id, Decimal("1.00"), yes_leg.limit, "yes",
                              now + 0.25))
    st, _ = _feed(p, st, Fill(None, no_leg.client_order_id, Decimal(0), no_leg.limit, "no", now + 0.25))
    assert any(l.side == "no" and l.status == "unfilled" for l in st.wing_legs)
    return st, now + 0.25


def test_wing_retry_is_gated_to_250ms_per_missing_leg():
    """D4: with a missing NO wing, feeding the IOC no-fill + a fresh tick on every step must NOT re-fire
    a RETRY_WING more often than WING_RETRY_MIN_INTERVAL_MS. The incident fired one on EVERY tick."""
    assert WING_RETRY_MIN_INTERVAL_MS == 250
    p = _params()
    st = _bring_up_ladder(p, _state(p), T - 600)
    st, t0 = _take_one_batch_with_a_missing_leg(p, st, T - 599)

    retry_times = []
    # 30 rapid ticks over ~300 ms, each resetting the missing leg to unfilled first (mimics the venue's
    # fast IOC no-fill echo). Bounded cadence => far fewer than 30 retries.
    for i in range(30):
        t = t0 + 0.3 + i * 0.01           # 10 ms apart
        no_leg = next((l for l in st.wing_legs if l.side == "no" and l.status == "pending"), None)
        if no_leg is not None:
            st, _ = _feed(p, st, Fill(None, no_leg.client_order_id, Decimal(0), no_leg.limit, "no", t))
        st, acts = _feed(p, st, BookUpdate(STK_SD, _sd("0.76"), t))
        st, _ = _feed(p, st, BookUpdate(STK_SU, _top("0.36", "0.37"), t))
        if [a for a in acts if a.kind == ActionKind.RETRY_WING]:
            retry_times.append(t)

    assert retry_times, "the missing leg must retry at least once"
    # every consecutive retry is >= 250 ms apart (the cadence floor), so ~300 ms admits at most 2.
    gaps = [b - a for a, b in zip(retry_times, retry_times[1:])]
    assert all(g >= WING_RETRY_MIN_INTERVAL_MS / 1000.0 - 1e-9 for g in gaps), gaps
    assert len(retry_times) <= 3                          # vs 30 ticks: never one-per-tick
    assert st.wing_batches[0].retries == len(retry_times)


# ===========================================================================
# D3 — a fractional partial leaves a fractional resting remainder (invariant holds)
# ===========================================================================
def test_fractional_partial_leaves_fractional_rest():
    """D3: a rung weight > 1 partially filled by a fractional count keeps the fractional remainder
    resting; the exposure invariant (filled + resting contracts) is conserved."""
    p = _params(rung_lots=[2] * 11)                       # weight-2 rungs
    st = _bring_up_ladder(p, _state(p), T - 600)
    o = _order_at(st, "0.47")
    assert o.count == Decimal("2")
    st, _ = _feed(p, st, Fill(o.order_id, o.client_order_id, Decimal("1.44"), o.price, "no", T - 599))
    rem = next((x for x in st.ladder if x.client_order_id == o.client_order_id), None)
    assert rem is not None and rem.count == Decimal("0.56")   # 2 - 1.44
    assert st.rest_fills[-1].count == Decimal("1.44")
    st.check_invariants(p)                                # 0 < 0.56 <= max weight; exposure conserved
