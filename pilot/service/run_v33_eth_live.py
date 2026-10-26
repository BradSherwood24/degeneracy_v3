"""run_v33_eth_live.py — the ETH LIVE (armable) V3.3 window process.

Brad (2026-10-26): "ETH live at 1 lot, sharing the Render worker with BTC." Unlike ``run_v33_eth`` (the
laptop DRY-by-construction logger, which NEVER arms), this entry point arms the ETH ladder through the
EXACT SAME armed path as BTC — ``service.run_v33.main`` parameterised by ``ETH_LIVE_SPEC``. There is NO
forked or copied armed logic: the convergence core, the ``V33LiveExecutor`` / async twin, the reconcile,
the ledger, the stops and the finalize are all the shared ``run_v33`` spine. The spec supplies only the
series-specific knobs (coid prefix ``v33e-``, sweep/position scope ``KXETH``, the KXETHD strike parser,
no 15M leg, and the ETH roster's own journals/logs/ledger/mode-file/day-guard/falsifier).

Arming still fails CLOSED until Brad acts, exactly like BTC:
  * the ETH mode file (``ops/v33_eth_mode.txt``) must read ``armed`` (absent/unknown -> dry); AND
  * the ETH falsifier (``ceremony/v33_eth_falsifier.md``) must carry ``STATUS: FROZEN`` — it ships as a
    DRAFT, so S5 degrades every ETH window to dry until Brad freezes it (an agent can never arm ETH by
    editing code); AND
  * S5 caps, reconcile-first (KXETH* clean), the ETH day-guard, and the ACCOUNT-WIDE S4 (shared balance;
    an S4 latch on BTC's guard stops ETH too) must all pass.

House law unchanged: money is Decimal, time from server timestamps, every network edge injected, the
SEALED/holdout dates never read, all Kalshi access via the proxy base, no key material touched.
"""

from __future__ import annotations

from service.run_v33 import main as _run_v33_main
from service.v33.series import ETH_LIVE_SPEC


def main(argv: list[str] | None = None) -> int:
    """Run one ETH V3.3 window through the shared ``run_v33`` spine with the ETH live spec."""
    return _run_v33_main(argv, spec=ETH_LIVE_SPEC)


if __name__ == "__main__":
    raise SystemExit(main())
