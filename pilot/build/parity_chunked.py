"""Chunked parity runner: one subprocess per window so memory stays bounded.

Splits the 24-window manifest into single-window manifests, runs
service.parity_report on each (fresh process -> journal memory released),
then aggregates bins + neutrality into one combined report.
"""
import json
import subprocess
import sys
from pathlib import Path

BUILD = Path(r"C:\Users\Brads\Python_stuff\degeneracy_v3\pilot\build")
PILOT = Path(r"C:\Users\Brads\Python_stuff\degeneracy_v3\pilot")
manifest = json.loads((BUILD / "parity_manifest_0821_22.json").read_text(encoding="utf-8"))
tape = manifest["tape_points"]

combined = {"windows": 0, "neutrality_failures": [], "bins": {}, "per_window": []}
tmpdir = BUILD / "parity_chunks"
tmpdir.mkdir(exist_ok=True)

for w in manifest["windows"]:
    tag = w["close_time"].replace(":", "").replace("-", "")
    mpath = tmpdir / f"m_{tag}.json"
    opath = tmpdir / f"r_{tag}.json"
    mpath.write_text(json.dumps({"tape_points": tape, "windows": [w]}), encoding="utf-8")
    r = subprocess.run([sys.executable, "-m", "service.parity_report",
                        "--manifest", str(mpath), "--out-json", str(opath),
                        "--out-text", str(tmpdir / f"r_{tag}.txt")],
                       cwd=str(PILOT), capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[{w['close_time']}] FAILED rc={r.returncode}: {r.stderr[-300:]}", flush=True)
        continue
    res = json.loads(opath.read_text(encoding="utf-8"))
    combined["windows"] += 1
    nf = res.get("neutrality_failures") or []
    combined["neutrality_failures"] += nf if isinstance(nf, list) else [{"window": w["close_time"], "value": nf}]
    for k, v in (res.get("bin_counts") or res.get("bins") or {}).items():
        combined["bins"][k] = combined["bins"].get(k, 0) + v
    combined["per_window"].append({"close_time": w["close_time"], **{k: res.get(k) for k in res if k != "windows"}})
    print(f"[{w['close_time']}] ok", flush=True)

combined["passed_F15"] = len(combined["neutrality_failures"]) == 0
(BUILD / "parity_0821_22_combined.json").write_text(json.dumps(combined, indent=1), encoding="utf-8")
print(json.dumps({k: combined[k] for k in ("windows", "neutrality_failures", "passed_F15", "bins")}, indent=1))
