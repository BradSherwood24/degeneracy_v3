"""Probe (read-only, scratchpad): what does the V3.2 core do with a 1-of-2 partial rest fill at contracts=2?"""
import sys; sys.path.insert(0, r"C:\Users\Brads\Python_stuff\degeneracy_v3\pilot"); sys.path.insert(0, r"C:\Users\Brads\Python_stuff\degeneracy_v3\pilot\tests")
from decimal import Decimal
import test_v32_core as tc
from service.v32 import Fill, ActionKind
p = tc._params(contracts=2)
st = tc._state(p)
now = tc.T - 600
st, coid = tc._bring_up_live_rest(p, st, now)
print("rest placed count:", st.rest_live.count, "price", st.rest_live.price)
oid = st.rest_live.order_id
st, acts = tc._feed(p, st, Fill(oid, coid, Decimal(1), st.rest_live.price, "no", now + 0.1))
wings = [a for a in acts if a.kind == ActionKind.TAKE_WINGS]
print("after 1-of-2 fill: rest_fill.count =", st.rest_fill.count, "| wings actions:", len(wings), "| wing leg counts:", [l.count for l in st.wing_legs],
      "| state still tracks the rest? rest_live =", st.rest_live, "rest_pending =", st.rest_pending)
# second lot fills 30 s later
st2, acts2 = tc._feed(p, st, Fill(oid, coid, Decimal(1), Decimal("0.45"), "no", now + 30))
print("second-lot fill: rest_fill.count =", st2.rest_fill.count, "| new wing actions:", len([a for a in acts2 if a.kind == ActionKind.TAKE_WINGS]), "| any action:", [a.kind for a in acts2])
# quote end: does the core emit a cancel for the leftover lot?
st3, acts3 = tc._feed(p, st, tc.BookUpdate(tc.STK_SD, tc._top("0.60", "0.61"), tc.T - 299))
print("at T-4:59 actions:", [a.kind for a in acts3])
