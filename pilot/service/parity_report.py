"""parity_report.py — CLI wrapper around parity.run_parity.

Reads a MANIFEST JSON describing the windows to compare, runs the five-bin harness, and writes
both a machine report (JSON) and a human block (text). The manifest lets the harness run without
any live network and against TRAIN-day / synthetic data only (Phase-2 constraint: never a sealed
tape, never a live order).

Manifest schema:
{
  "tape_points": "sim/out/full60/tape_points.csv",   # or a small slice file
  "windows": [
    {
      "close_time": "2026-06-14T02:00:00Z",
      "journal": "pilot/journals/20260614T020000Z.jsonl",
      "high_ticker": "...", "low_ticker": "...",
      "quintile": 0,
      "G": "12.33", "sigma_hat": 49.54,
      "strangle_disabled": false,
      "shakedown": true,
      "fills": "path/to/fills.json"                    # optional; absent => bins 1-2 only
    }, ...
  ]
}

The window meta (high/low tickers, quintile, G, sigma) is what quintile.py computes at wake and
Phase 4 will journal; here it is supplied explicitly so the harness is fully deterministic.
"""

from __future__ import annotations

import argparse
import json
import os
from decimal import Decimal

from service._simlaw import load_ev_curve
from service.journal import load_journal
from service.parity import (
    LegFill,
    ParityWindowInput,
    WindowFills,
    load_sim_window,
    render_text,
    run_parity,
)
from service.policy import DEFAULT_POLICY_PATH, load_policy
from service.signal import WindowState


def _load_fills(path: str) -> WindowFills:
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    legs = tuple(
        LegFill(
            ticker=str(lg["ticker"]),
            side=str(lg["side"]),
            count=int(lg["count"]),
            avg_price=Decimal(str(lg["avg_price"])),
        )
        for lg in raw.get("legs", [])
    )
    return WindowFills(
        filled=bool(raw.get("filled", False)),
        legs=legs,
        imbalance=bool(raw.get("imbalance", False)),
        realized_payoff=(Decimal(str(raw["realized_payoff"]))
                         if raw.get("realized_payoff") is not None else None),
    )


def build_inputs(manifest: dict, ev_curve) -> list[ParityWindowInput]:
    tape_path = manifest["tape_points"]
    inputs: list[ParityWindowInput] = []
    for w in manifest["windows"]:
        ct = w["close_time"]
        journal = load_journal(w["journal"])
        quintile = int(w["quintile"])
        state0 = WindowState.new(
            close_time=ct,
            high_ticker=str(w["high_ticker"]),
            low_ticker=str(w["low_ticker"]),
            quintile=quintile,
            fair_strangle_q=ev_curve.fair_for("strangle", quintile),
            strangle_disabled=bool(w.get("strangle_disabled", False)),
            shakedown=bool(w.get("shakedown", True)),
            G=(Decimal(str(w["G"])) if w.get("G") is not None else None),
            sigma_hat=(float(w["sigma_hat"]) if w.get("sigma_hat") is not None else None),
        )
        sim_rows = load_sim_window(tape_path, ct)
        fills = _load_fills(w["fills"]) if w.get("fills") else None
        inputs.append(ParityWindowInput(ct, journal.records(), state0, sim_rows, fills))
    return inputs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Five-bin parity report (Phase 2).")
    ap.add_argument("--manifest", required=True, help="manifest JSON (see module docstring)")
    ap.add_argument("--policy", default=DEFAULT_POLICY_PATH)
    ap.add_argument("--out-json", default=None)
    ap.add_argument("--out-text", default=None)
    args = ap.parse_args(argv)

    with open(args.manifest, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    params = load_policy(args.policy)
    ev_curve = load_ev_curve()
    inputs = build_inputs(manifest, ev_curve)
    report = run_parity(inputs, params)

    text = render_text(report)
    print(text)
    print(f"policy sha: {params.sha256}")
    if args.out_json:
        os.makedirs(os.path.dirname(os.path.abspath(args.out_json)), exist_ok=True)
        with open(args.out_json, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, sort_keys=True)
        print(f"wrote {args.out_json}")
    if args.out_text:
        os.makedirs(os.path.dirname(os.path.abspath(args.out_text)), exist_ok=True)
        with open(args.out_text, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"wrote {args.out_text}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
