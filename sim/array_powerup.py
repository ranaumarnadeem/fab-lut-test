#!/usr/bin/env python3
"""Power-up check (cone-split, fast kernel) for an array or fabric block (Phase 11).

  python3 sim/array_powerup.py R C [--fabric] [--suites ...] [--max-group 12]

All configs of the given suites form one test (a power-up value holds for the
whole test). X-only faults: P in some config, D in none, over all suites;
the FrameStrobe stems are left out as in the tile runs. Decision as
sim/fastsim/powerup.py. Writes reports/<name>_powerup.json and prints the
exhaustive coverage (strict D, or detected for every power-up state).
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "configs"), str(ROOT / "sim"), str(ROOT / "sim" / "fastsim")]
import fabric  # noqa: E402
import fastsim  # noqa: E402
import powerup  # noqa: E402
import run_array  # noqa: E402

REPORTS = ROOT / "reports"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("R", type=int)
    ap.add_argument("C", type=int)
    ap.add_argument("--fabric", action="store_true")
    ap.add_argument("--suites", nargs="+", default=["router_nopolish", "merged_nopolish", "clocked", "lut"])
    ap.add_argument("--max-group", type=int, default=12)
    a = ap.parse_args()
    arr = fabric.Array(a.R, a.C, fabric=a.fabric)
    t0 = time.time()
    stims, mats = [], []
    for su in a.suites:
        p = REPORTS / f"{arr.name}_{su}_matrix.csv"
        if not p.exists():
            continue
        base, _, tag = su.partition("_")
        run_array.TAG = "_" + tag if tag else ""
        st, _ = run_array.SUITES[base](arr)
        stims += st
        mats.append(list(csv.DictReader(p.open())))
    cfgs = [st.name for st in stims]
    rows = [{} for _ in mats[0]]
    for m in mats:
        for j, r in enumerate(m):
            rows[j].update({c: r[c] for c in r if c in cfgs})
    faults = list(csv.DictReader((ROOT / "faults" / f"{arr.name}.csv").open()))
    stem = re.compile(r"FrameStrobe_C\w*\[\d+\]$")
    xo = {}
    for j, r in enumerate(rows):
        st = [r[c] for c in cfgs]
        if "D" not in st and "P" in st and not stem.match(faults[j]["path"]):
            xo[j] = [c for c in cfgs if r[c] == "P"]
    det = sum(1 for r in rows if "D" in r.values())
    print(f"{arr.name}: {len(cfgs)} configs, {len(faults)} faults, strict D {det}, X-only {len(xo)} "
          f"(stimuli {time.time() - t0:.0f} s)", flush=True)
    nl = fastsim.Netlist(arr.name)
    pu = powerup.PowerUp(nl, stims, ROOT / "synth" / f"netlist_{arr.name}.json")
    fl = [faults[j] for j in xo]
    v = pu.decide(fl, {k: xo[j] for k, j in enumerate(xo)}, a.max_group)
    from collections import Counter
    cnt = Counter(r["verdict"] for r in v.values())
    exh = det + cnt["detected"]
    print(f"{arr.name}: power-up check {dict(cnt)}; exhaustive {exh} of {len(faults)} = "
          f"{100 * exh / len(faults):.3f}% (strict {100 * det / len(faults):.3f}%), {time.time() - t0:.0f} s")
    (REPORTS / f"{arr.name}_powerup.json").write_text(json.dumps(
        [{"id": fl[k]["id"], "path": fl[k]["path"], "polarity": fl[k]["polarity"], **r} for k, r in v.items()],
        indent=0))


if __name__ == "__main__":
    main()
