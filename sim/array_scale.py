#!/usr/bin/env python3
"""Scaling of the array test with array size (Phase 10c).

  python3 sim/array_scale.py [--sizes 2x2 3x3 4x4 5x5] [--fabric 4x3]

Per array: strict coverage of the union of the suites (no-polish router,
merged, clocked, LUT), by population, against the tile level on the same
faults (tile router, clocked, LUT configs); configs kept by the set cover
(sim/array_cover.py, reports/array_<R>x<C>_cover.json). Fabric blocks
(W_IO west, N / S term, east open): the suites that exist. Writes
reports/array_scaling.md.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "configs"), str(ROOT / "sim"), str(ROOT / "sim" / "fastsim")]
import run_array  # noqa: E402

REPORTS = ROOT / "reports"
SUITES = ["router_nopolish", "merged_nopolish", "clocked", "lut"]


def row_for(name: str, tm, tile_d) -> dict | None:
    mats = [list(csv.DictReader(p.open())) for su in SUITES
            if (p := REPORTS / f"{name}_{su}_matrix.csv").exists()]
    if not mats:
        return None
    tot, det, tdet = {}, {}, {}
    for j, r in enumerate(mats[0]):
        m = re.match(r"(X\d+Y\d+|IOY\d+)\.(.*)", r["path"])
        if m and m.group(1).startswith("IOY"):
            cls, tid = "W_IO", None
        else:
            tid, cls = tm.get((m.group(2), r["polarity"]), (None, "inter-tile / config chain")) if m \
                else (None, "inter-tile / config chain")
        d = any(mm[j]["any"] == "D" for mm in mats)
        for k in (cls, "all"):
            tot[k] = tot.get(k, 0) + 1
            det[k] = det.get(k, 0) + d
            tdet[k] = tdet.get(k, 0) + (tid is not None and tid in tile_d)
    ncfg = sum(len([c for c in mm[0] if c not in ("id", "path", "polarity", "type", "any")]) for mm in mats)
    cov = REPORTS / f"{name}_cover.json"
    cj = json.loads(cov.read_text()) if cov.exists() else None
    return {"tot": tot, "det": det, "tdet": tdet, "ncfg": ncfg, "cover": cj}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", nargs="+", default=["2x2", "3x3", "4x4", "5x5"])
    ap.add_argument("--fabric", nargs="*", default=["4x3"])
    a = ap.parse_args()
    tm = run_array.tile_map()
    tile_d = set()
    for f in ("tile_generic_rt_matrix.csv", "tile_generic_ck_matrix.csv", "tile_generic_matrix.csv"):
        tile_d |= {r["id"] for r in csv.DictReader((REPORTS / f).open()) if r["any"] == "D"}
    out = ["# LUT4AB arrays: scaling (Phase 10c)", "",
           "Strict D (3-valued, all storage X at power-up), fast kernel. Suites: no-polish array router "
           "(configs/fab_router.py --no-polish, fill decorrelation), merged (router configs with the "
           "clocked SR / EN tests folded in), clocked, LUT. \"tile\": the same faults' tile-level result "
           "(tile router + clocked + LUT configs). Configs: all simulated, and kept by greedy set cover "
           "(sim/array_cover.py) for the full detected set and for 99.9% of it.", "",
           "| array | tiles | faults | switch matrix | tile | LUT target | tile | all | tile | configs "
           "simulated | set cover | 99.9% |", "|---|---|---|---|---|---|---|---|---|---|---|---|"]

    def pc(r, k, which):
        return f"{100 * r[which].get(k, 0) / r['tot'][k]:.2f}%" if r["tot"].get(k) else "-"
    rows = [(f"array_{s}", s) for s in a.sizes] + [(f"fabric_{s}", f"fabric {s}") for s in a.fabric]
    for name, lab in rows:
        r = row_for(name, tm, tile_d)
        if r is None:
            continue
        R, C = map(int, lab.split()[-1].split("x"))
        cj = r["cover"]
        full = len(cj["chosen"]) if cj else "-"
        p999 = next((i + 1 for i, (_, _, cv) in enumerate(cj["curve"]) if cv >= 0.999 * cj["detected"]), "-") \
            if cj else "-"
        out.append(f"| {lab} | {R * C} | {r['tot']['all']} | {pc(r, 'sm', 'det')} | {pc(r, 'sm', 'tdet')} | "
                   f"{pc(r, 'target', 'det')} | {pc(r, 'target', 'tdet')} | {pc(r, 'all', 'det')} | "
                   f"{pc(r, 'all', 'tdet')} | {r['ncfg']} | {full} | {p999} |")
        if "W_IO" in r["tot"]:
            out.append(f"| {lab}: W_IO tiles | {R} | {r['tot']['W_IO']} | | | | | {pc(r, 'W_IO', 'det')} | | | | |")
    (REPORTS / "array_scaling.md").write_text("\n".join(out) + "\n")
    print("\n".join(out))


if __name__ == "__main__":
    main()
