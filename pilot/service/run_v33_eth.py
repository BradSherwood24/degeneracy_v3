"""run_v33_eth.py — the DRY-ONLY ETH side-by-side of the V3.3 rolling-ladder pump-fader.

Brad (2026-10-16/17): "add to the queue of work copying this strategy to the ETH markets ... setting up
something dry to record if there's any money there" / "Get dry ETH logging on laptop". This runs the SAME
series-agnostic V3.3 core (``decide_v33`` / ``V33Driver``) against the ETH hourly RANGE (``KXETH``) +
STRIKE (``KXETHD``) markets, in DRY, journaling + ledgering exactly like a BTC dry window (``dry_sim``
fills, per-window ledger rows, a comparable report), so we can MEASURE whether ETH has edge before any
code investment beyond this adapter. It writes to its OWN journals/ledger/logs/mode file (``journals_v33_eth``,
``ledger/v33_eth_ledger.jsonl``, ``logs_v33_eth``, ``ops/v33_eth_mode.txt``) and NEVER touches BTC's files.

DRY BY CONSTRUCTION — enforced three ways:
  (a) the ENTRY POINT refuses ``armed`` regardless of the mode file / --mode flag. ``eth_effective_mode``
      maps every requested mode to ``dry`` and NEVER returns ``armed``; a request for armed is journaled
      ``armed_refused_dry_only`` and the window still runs DRY.
  (b) NO write-capable executor is ever constructed. The ONLY executor this module builds is the reused dry
      ``FrozenExecutor`` (``build_eth_executor``); it imports no ``ProxyWriter`` / ``V33LiveExecutor`` and
      never calls ``build_executor_v33``. There is no code path from here to an order write.
  (c) operational belt (noted, not relied on): the laptop proxy is read-only (``orders_enabled: false``,
      and its allowlist is KXBTC-only, so an ETH order could not be placed even if one were attempted).

House law throughout: money is Decimal; time comes only from server timestamps; every network edge is
injected (tests use fakes — this is NEVER dialed against the live proxy from an automated context); the
SEALED/holdout dates are never read; fail closed. The ETH roster has no falsifier and no day-guard because
it never arms. Reuses run_v33's process spine (``V33Driver`` / ``run_v33_window`` / ``_finalize``) and
run_v32's discovery helpers (parameterized by series) UNCHANGED for the BTC tree.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time
from typing import Any

from service.proxy_auth import ProxyAuth
from service.record_range import StreamJournal, discover_range_markets, journal_filename
from service.record_window import (
    GRACE_SECONDS,
    arm_hard_stop,
    next_top_of_hour_iso,
)
from service.wake import close_epoch
from service.ws_client import KalshiWebSocketClient

from service.v33 import V33ParamsInvalid, V33ParamsShaMismatch, V33State
from service.v33.eth import (
    ETH_RANGE_SERIES,
    ETH_ROSTER,
    ETH_STRIKE_SERIES,
    load_v33_eth_params,
    parse_eth_strike_ticker,
)
from service.v33.reconcile import reconcile_live
from service.paths import (
    default_proxy_base,
    journal_dir_v33_eth,
    ledger_path_v33_eth,
    log_dir_v33_eth,
    mode_path_v33_eth,
)

# Reuse run_v33's process spine (driver / window run-loop / finalize / stand-down) and run_v32's discovery
# helpers UNCHANGED. run_v33_eth NEVER calls build_executor_v33 or constructs a ProxyWriter.
import service.run_v33 as RUN
import service.run_v32 as R

logger = logging.getLogger(__name__)


# ===========================================================================
# Mode resolution — DRY BY CONSTRUCTION (enforcement (a))
# ===========================================================================
def eth_requested_mode(cli_mode: str | None, mode_txt_path: str) -> str:
    """The RAW requested mode (CLI --mode wins; else ops/v33_eth_mode.txt). Lower-cased, stripped; '' if
    absent. This is only used to DETECT an armed request so it can be refused + journaled — it is NEVER the
    effective mode."""
    raw = cli_mode if cli_mode else RUN.read_v33_mode_file(mode_txt_path)
    return (raw or "").strip().lower()


def eth_effective_mode(requested: str | None) -> str:
    """The EFFECTIVE mode for the ETH roster: always ``dry``. ``armed`` is NEVER returned regardless of the
    requested mode (enforcement (a)) — the ETH roster exists only to measure, never to trade."""
    return "dry"


def build_eth_executor(bucket_map: dict[str, tuple[float, float]]) -> Any:
    """The ONLY executor the ETH roster builds: the reused DRY ``FrozenExecutor`` (enforcement (b)). There
    is no ``mode`` parameter and no branch to a write-capable executor — by construction the ETH roster
    cannot send an order."""
    return R.FrozenExecutor(bucket_map)


def _eth_stand_down(summary_path: str, ledger_path: str, close_iso: str, reason: str,
                    params_sha: str | None, clock) -> int:
    """Stand the ETH dry window down (one-line summary + a labelled ETH ledger row)."""
    return RUN._stand_down(
        summary_path, ledger_path, close_iso, reason,
        resolved_mode="dry", effective_mode="dry", degrade=None, params_sha=params_sha, clock=clock,
        series=ETH_RANGE_SERIES, roster=ETH_ROSTER)


# ===========================================================================
# main
# ===========================================================================
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="V3.3 ETH DRY side-by-side window process (KXETH/KXETHD). DRY BY CONSTRUCTION — "
                    "never arms, builds no write-capable executor. Records dry_sim fills + a labelled "
                    "ETH ledger row for the measurement report.")
    parser.add_argument("--close", default=None, help="Target close ISO (UTC). Default: next :00.")
    parser.add_argument("--mode", default=None,
                        help="Requested mode (else read ops/v33_eth_mode.txt). ANY value runs DRY; "
                             "'armed' is refused and journaled (the ETH roster never arms).")
    parser.add_argument("--journal-dir", default=journal_dir_v33_eth())
    parser.add_argument("--log-dir", default=log_dir_v33_eth())
    parser.add_argument("--ledger", default=ledger_path_v33_eth())
    parser.add_argument("--mode-file", default=mode_path_v33_eth())
    parser.add_argument("--proxy-base", default=None, help="Override the proxy base URL.")
    parser.add_argument("--flush-every", type=int, default=200)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    clock = time.time
    proxy_base_url = args.proxy_base or default_proxy_base()
    proxy = ProxyAuth(base_url=proxy_base_url)
    close_iso = args.close or next_top_of_hour_iso(clock())
    summary_path = os.path.join(args.journal_dir, "summary.jsonl")

    # HARD STOP armed at the earliest point the deadline is known (mirrors run_v33), BEFORE the first proxy
    # call, so a synchronous discovery hang is covered. Cancelled on the normal exit path below.
    deadline = close_epoch(close_iso) + GRACE_SECONDS
    hard_stop = arm_hard_stop(deadline, close_iso)

    # DRY ENFORCEMENT (a): detect (to refuse) an armed request; the effective mode is ALWAYS dry.
    requested = eth_requested_mode(args.mode, args.mode_file)
    effective_mode = eth_effective_mode(requested)
    armed_refused = requested == "armed"
    assert effective_mode != "armed", "ETH roster must never run armed (dry by construction)"

    try:
        params = load_v33_eth_params()
    except (V33ParamsShaMismatch, V33ParamsInvalid, KeyError, OSError, ValueError) as e:
        hard_stop.cancel()
        return _eth_stand_down(summary_path, args.ledger, close_iso, f"params_load_failed: {e}",
                               params_sha=None, clock=clock)

    # --- discovery (ETH series; parameterized helpers, BTC-default elsewhere) ---
    strike_disc = R.discover_strike_ladder(proxy, close_iso, clock(), series=ETH_STRIKE_SERIES,
                                           parse_floor=parse_eth_strike_ticker)
    range_disc = discover_range_markets(proxy, close_iso, clock(), series=ETH_RANGE_SERIES)
    bucket_map = R.build_bucket_map(range_disc)
    bucket_map, dropped_width = R.filter_buckets_to_width(bucket_map, params.bucket_width)

    if not bucket_map:
        hard_stop.cancel()
        return _eth_stand_down(summary_path, args.ledger, close_iso,
                               f"no {ETH_RANGE_SERIES} range buckets of width {params.bucket_width} "
                               f"co-settling at {close_iso}", params_sha=params.sha256, clock=clock)
    if not strike_disc.tickers:
        hard_stop.cancel()
        return _eth_stand_down(summary_path, args.ledger, close_iso,
                               f"no {ETH_STRIKE_SERIES} strike ladder co-settling at {close_iso}",
                               params_sha=params.sha256, clock=clock)
    obs_width = R.observed_bucket_width(bucket_map)
    if obs_width is not None and obs_width != params.bucket_width:
        hard_stop.cancel()
        return _eth_stand_down(summary_path, args.ledger, close_iso,
                               f"bucket width {obs_width} != params.bucket_width {params.bucket_width} "
                               f"(ETH $5 hourly buckets only; the wide 21Z daily buckets stand down)",
                               params_sha=params.sha256, clock=clock)

    logger.info("[V33-ETH] %s: %d strikes, %d buckets, effective=dry (requested=%r)", close_iso,
                len(strike_disc.tickers), len(bucket_map), requested or "")

    journal_path = os.path.join(args.journal_dir, journal_filename(close_iso))
    journal = StreamJournal(journal_path, flush_every=args.flush_every)
    journal.open()
    journal.append("window_meta",
                   {"roster": ETH_ROSTER, "series": ETH_RANGE_SERIES, "close_time": close_iso,
                    "resolved_mode": "dry", "effective_mode": "dry", "dry_enforced": True,
                    "armed_refused": armed_refused, "requested_mode": requested,
                    "params_sha": params.sha256, "strike_series": ETH_STRIKE_SERIES,
                    "range_series": ETH_RANGE_SERIES, "strike_count": len(strike_disc.tickers),
                    "bucket_count": len(bucket_map), "bucket_width": params.bucket_width,
                    "rungs": params.rungs, "m15_series": None, "m15_tickers": []}, clock())
    if armed_refused:
        journal.append("armed_refused_dry_only", {"requested_mode": requested}, clock())
        logger.warning("[V33-ETH] 'armed' requested but REFUSED — the ETH roster is dry by construction.")

    # --- build the dry window (enforcement (b): FrozenExecutor only; include_private False always) ---
    cts = close_epoch(close_iso)
    # The strike-floor map lets the reused V3.2 ``_fold_book`` / ``_stamp_strike_feed`` classify the KXETHD
    # strikes by lookup (the BTC ``parse_strike_ticker`` prefix would reject them). The ``.99`` sub-tick
    # convention is identical, so the floors also match ``parse_eth_strike_ticker`` -- the map is the
    # series-agnostic channel the core was designed to receive (MULTI_SERIES_DATA_PLAN §4).
    state = V33State.new(close_iso, cts, bucket_map, params, shakedown=True,  # DRY -> WOULD_* twins
                         strike_floor_map=dict(strike_disc.floor_by_ticker))
    executor = build_eth_executor(bucket_map)
    driver = RUN.V33Driver(params, state, journal, executor, dry_sim=True, clock=clock)
    shared = R.V32Recorder(journal, driver, clock=clock, m15_tickers=frozenset())

    strike_ws = KalshiWebSocketClient(proxy_auth=proxy, tickers=list(strike_disc.tickers),
                                      callbacks=shared.callbacks(False), include_private=False,
                                      record=shared.tap, clock=clock, channels=R.STRIKE_CHANNELS)
    bucket_sub_tickers = sorted(set(bucket_map))
    bucket_ws = KalshiWebSocketClient(proxy_auth=proxy, tickers=bucket_sub_tickers,
                                      callbacks=shared.callbacks(False), include_private=False,
                                      record=shared.tap, clock=clock, channels=R.BUCKET_CHANNELS)
    strike_conn = R._ConnRecorder(shared, strike_ws, "strikes", list(strike_disc.tickers))
    bucket_conn = R._ConnRecorder(shared, bucket_ws, "buckets", bucket_sub_tickers)

    gate = R.connect_gate_epoch(cts, params)
    lag_sampler = R.LagSampler({"strikes": strike_ws, "buckets": bucket_ws})
    try:
        try:
            asyncio.run(RUN.run_v33_window(shared, strike_conn, bucket_conn, driver, clock, deadline,
                                           gate, order_poll=False, lag_sampler=lag_sampler))
        except KeyboardInterrupt:
            logger.warning("[V33-ETH] Ctrl+C — flushing streamed journal.")
        finally:
            # DRY: no venue fills (nothing was sent). reconcile_live(driver, None) reconciles the dry_sim
            # core against the (empty) executor-truth set so the ETH ledger row carries the SAME shape as a
            # BTC dry row — the report reads them identically.
            recon = None
            try:
                recon = reconcile_live(driver, None)
                for ub in recon.unbooked_fills:
                    journal.append("unbooked_fill", ub, clock())
            except Exception as e:  # noqa: BLE001 — reconcile accounting never blocks the close/row write
                journal.append("alarm", {"alarm": "reconcile_failed", "error": str(e)}, clock())
                recon = None
            summary = RUN._finalize(
                journal=journal, shared=shared, driver=driver, close_iso=close_iso,
                resolved_mode="dry", effective_mode="dry", degrade=None, params=params,
                strike_disc=strike_disc, bucket_map=bucket_map, journal_path=journal_path,
                summary_path=summary_path, ledger_path=args.ledger,
                strike_lag=lag_sampler.mean("strikes"), bucket_lag=lag_sampler.mean("buckets"),
                lag_stats=lag_sampler.summaries(), clock=clock, m15_tickers=[], armed=False,
                recon=recon, reconcile_alarms=0, series=ETH_RANGE_SERIES, roster=ETH_ROSTER)
            logger.info("[V33-ETH] window done: %s", summary)
    finally:
        hard_stop.cancel()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
