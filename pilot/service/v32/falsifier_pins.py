"""falsifier_pins.py — the pre-registered [pin] thresholds of ``ceremony/v32_falsifier.md``.

Every verdict/retirement/promotion threshold the V3.2 falsifier commits to is a NAMED constant here so
(a) ``service.v32.report`` computes the scoreboard verdict from the SAME numbers the document pins, and
(b) a unit test (``test_v32_falsifier_pins.py``) asserts the doc text and these constants agree — a
drift in either fails the suite. This module is pure data: no orders, no network, no key/holdout read.

House law (registered-specs rule): the doc does not merely describe the thresholds — these constants
ENFORCE them. Change a number here only when Brad amends the falsifier (append-only Registration), and
re-run the doc/code agreement test.

Lock is expressed in CENTS (a $0.093 lock is +9.3c). Rates are per UTC evaluation day. Gaps are the
shadow-minus-live lock difference in cents.
"""

from __future__ import annotations

from decimal import Decimal

# --- Verdict at n >= MIN_N completed sets (the primary kill gate) -------------------------------
V32_FALSIFIER_MIN_N = 30                                # [pin] live completed sets before a verdict
V32_FALSIFIER_MIN_MEAN_LOCK_CENTS = Decimal("4.0")     # [pin] mean realized lock >= +4.0c
V32_FALSIFIER_MIN_PCT_POSITIVE = Decimal("80")         # [pin] % of sets with positive realized lock
V32_FALSIFIER_MIN_FILL_RATE_PER_DAY = Decimal("2.0")   # [pin] live fills (sets) per UTC day
# MEASUREMENT CLARIFICATION 3 (2026-09-19 ~22:40Z, Brad's verbatim go): the >= 2.0 sets/day gate above
# measured the TAPE REGIME (June-July had ~3x September's pump traffic), not our execution. It is kept
# DEFINED (add-only law) but the VERDICT no longer gates on it; the report still prints sets/day as
# INFO. The gate is now the CAPTURE RATIO = (live completed sets) / (ideal-shadow E=0.10 fills inside
# the quoting window), both counted over ARMED windows carrying a spot bucket -- how much of the pump
# availability the shadow proves was there did we actually capture. Pin proposed by Claude at n=6;
# Brad confirms the 0.50 number at merge (Registration 3).
V32_CAPTURE_RATIO_MIN = Decimal("0.50")                # [pin] live completed sets / ideal-shadow fills
V32_FALSIFIER_MAX_EXEC_GAP_CENTS = Decimal("3.0")      # [pin] mean (shadow E=0.10 lock - live lock)
V32_FALSIFIER_MAX_ONE_LEGGED = 2                       # [pin] one-legged sets tolerated of the first 30

# The shadow E the execution gap is measured against (must be the live edge E; the shadow ladder in
# policy/v32_params.json is {0.08, 0.10, 0.12} and the live edge is 0.10).
V32_FALSIFIER_SHADOW_GAP_E = "0.10"                    # [pin] shadow E the exec gap compares live to

# --- Retirement R1-R4 (pre-committed) ----------------------------------------------------------
# R1 = the verdict gate above (any threshold missed at n >= MIN_N -> KILL; no re-spec on the same
#      evaluation window).
V32_R2_CONSECUTIVE_NEG_DAYS = 3          # [pin] R2: 3 consecutive UTC days of negative realized -> KILL
V32_R3_LEGGED_LATCHES_PER_WEEK = 2       # [pin] R3: 2 S1_LEGGED day-latches within a 7-day window -> KILL
V32_R4_EXEC_GAP_CENTS = Decimal("5.0")   # [pin] R4: execution gap > 5.0c ...
V32_R4_MIN_N = 15                        # [pin] ... already at n >= 15 -> KILL (early execution kill)

# --- Promotion to 2 contracts (Brad's word required; these are the necessary conditions) ---------
V32_PROMOTION_MIN_N = 60                  # [pin] alive at n >= 60 completed sets, AND
V32_PROMOTION_MIN_DEPTH_LOTS = 10        # [pin] thinner-wing depth >= 10 lots at every completion

# Power note (documented, not a threshold): SE of the mean lock at sd ~5c and n=30 is ~0.9c, so the
# +4.0c bar sits ~4.4 SE above 0 — R1 catches a broken premise, not a marginal edge.
V32_FALSIFIER_LOCK_SD_CENTS = Decimal("5.0")
V32_FALSIFIER_POWER_SE_AT_30_CENTS = Decimal("0.9")
