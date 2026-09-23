"""falsifier_pins.py — the pre-registered [pin] thresholds of ``ceremony/v33_falsifier.md`` (the rolling
K-rung ladder, roster ``DegeneracyV3_3``).

Every verdict / kill / promotion threshold the V3.3 falsifier commits to is a NAMED constant here so
(a) ``service.v33.report`` computes the scoreboard + gate table from the SAME numbers the document pins,
and (b) a unit test (``test_v33_falsifier_pins.py``) asserts the doc text and these constants agree — a
drift in either fails the suite. Pure data: no orders, no network, no key/holdout read.

House law (registered-specs rule): the doc does not merely describe the thresholds — these constants
ENFORCE them. Change a number here only when Brad amends the falsifier (append-only Registration), and
re-run the doc/code agreement test.

The DRAFT is NOT frozen (STATUS: DRAFT — NOT FROZEN). Only Brad's verbatim go flips the STATUS line and
freezes it; until then the S5 gate (``service.v33.stops.decide_v33_arming`` -> ``falsifier_is_frozen``)
refuses to arm V3.3.

Lock is expressed in CENTS (a $0.06 lock is +6.0c). ``n`` counts CONTRACTS (rung fills): a full K-rung
sweep contributes K toward ``n``.
"""

from __future__ import annotations

from decimal import Decimal

# The V3.3 policy sha the doc pins (mirrored so the pins test asserts doc <-> params agreement).
from service.v33.params import FROZEN_V33_PARAMS_SHA256  # noqa: F401  (re-exported for the pins test)

# --- Verdict at n >= MIN_N rung-fills (contracts) -------------------------------------------------
V33_FALSIFIER_MIN_N = 30                                # [pin] realised rung-fills (contracts) before a verdict
V33_FALSIFIER_MIN_MEAN_LOCK_CENTS = Decimal("6.0")     # [pin] mean realised true lock >= +6.0c
V33_FALSIFIER_MAX_RUNG_SHORTFALL_CENTS = Decimal("3.0")  # [pin] (solved E - realised lock) per rung
V33_FALSIFIER_MIN_RUNG_FILLS_FOR_SHORTFALL = 3         # [pin] a rung is judged for shortfall at >= 3 fills
V33_FALSIFIER_MIN_PCT_POSITIVE = Decimal("80")         # [pin] % of rung fills with positive realised lock
V33_CAPTURE_RATIO_MIN = Decimal("0.50")                # [pin] capture ratio at the 10c margin (Reg-3 def)
V33_FALSIFIER_CAPTURE_MARGIN_C = 10                    # [pin] the margin (cents) the capture ratio reads
V33_FALSIFIER_MAX_ONE_LEGGED = 2                       # [pin] one-legged contracts tolerated
V33_FALSIFIER_MIN_SINGLE_ORDER_ROLL_RATIO = Decimal("0.90")  # [pin] >= 90% of rolls move exactly one order

# The shadow E the 10c-margin capture ratio reads (the live V3.2-comparable ideal-shadow edge).
V33_FALSIFIER_SHADOW_GAP_E = "0.10"                    # [pin] shadow E the capture ratio compares to

# --- Kill (early) ---------------------------------------------------------------------------------
V33_KILL_MEAN_LOCK_CENTS = Decimal("2.0")              # [pin] mean lock < +2.0c ...
V33_KILL_MIN_N = 15                                    # [pin] ... already at n >= 15 rung-fills -> KILL
# one-legged > MAX_ONE_LEGGED is also an immediate kill (shares V33_FALSIFIER_MAX_ONE_LEGGED).

# --- Promotion (Brad's dated word required; these are the necessary conditions) -------------------
V33_PROMOTION_MIN_N = 30                                # [pin] alive at n >= 30 rung-fills, AND
# a promotion is 2 lots per rung, OR rungs deeper than 15c live, informed by SO-3's measured deep-end
# absorption (PLAN_V33 sec 6 / sec 8). No automatic promotion; Brad's dated go, like V3.2's.

# Power note (documented, not a threshold): every completed ladder set pays $2/contract at settlement, so
# the lock is STRUCTURAL; n answers slippage / edge-case questions per Brad's 2026-09-18 sizing philosophy,
# not a coin flip. The mean-lock bar catches a broken premise (the deep rungs' edge does not survive real
# fills); the per-rung shortfall is the sharp instrument for latency eating a rung's edge.
