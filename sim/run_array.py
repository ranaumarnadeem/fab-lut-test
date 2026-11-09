#!/usr/bin/env python3
"""Fault simulation of an R x C LUT4AB array, border-only observation (Phase 10).

  python3 sim/run_array.py faultsim R C [--suite uniform] [--jobs N]

uniform: config c = router config RTc (configs/sm_router.json) in every tile,
array-level loops broken (configs/fabric.Array.break_loops). Netlist
synth/netlist_array_<R>x<C>.json (rtl/gen_array.py, synth/synth_array.ys.in),
faults faults/array_<R>x<C>.csv. Fast kernel (sim/fastsim); the fault-free
lane must equal the array model. Writes reports/array_<R>x<C>_<suite>_matrix.csv.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "configs"), str(ROOT / "sim"), str(ROOT / "sim" / "fastsim")]
import fabric  # noqa: E402
import fastsim  # noqa: E402
import sm_configs as sc  # noqa: E402

REPORTS = ROOT / "reports"
ROUTER = json.loads((ROOT / "configs" / "sm_router.json").read_text())["configs"]


def uniform(arr: fabric.Array):
    stims, logs = [], {}
    for c, sel in enumerate(ROUTER):
        sels = {t: dict(sel) for t in arr.tiles}
        logs[c] = arr.break_loops(sels)
        stims.append(arr.stim(f"U{c:02d}", sels, sc.frame_order(c)))
    return stims, logs


def routed(arr: fabric.Array):
    """configs/fab_router_<R>x<C>.json (configs/fab_router.py)."""
    data = json.loads((ROOT / "configs" / f"fab_router_{arr.R}x{arr.C}.json").read_text())
    stims, logs = [], {}
    for c, cfg in enumerate(data["configs"]):
        sels = {t: {o: k for o, k in cfg[arr.tn(t)].items()} for t in arr.tiles}
        if arr.cycles(sels):
            raise SystemExit(f"router config {c}: loop")
        logs[c] = []
        stims.append(arr.stim(f"A{c:02d}", sels, sc.frame_order(c % sc.F)))
    return stims, logs


def clocked(arr: fabric.Array):
    """8 clocked SR / EN configs (configs/fab_router.clocked_configs), 16 code + 48 random vectors."""
    import random
    import fab_router
    stims = []
    for c, (sels, plan) in enumerate(fab_router.clocked_configs(arr)):
        rnd = random.Random(2000 + c)
        vecs = list(arr.vecs) + [rnd.getrandbits(len(arr.border_in)) for _ in range(48)]
        stims.append(fabric.stim_clocked(arr, f"K{c:02d}", sels, sc.frame_order(c), plan, vecs))
        print(f"  K{c:02d}: {int(stims[-1].sample.sum())} samples", flush=True)
    return stims, {}


def cmd_faultsim(a) -> None:
    arr = fabric.Array(a.R, a.C)
    t0 = time.time()
    stims, logs = {"uniform": uniform, "router": routed, "clocked": clocked}[a.suite](arr)
    print(f"{arr.name}: {len(arr.tiles)} tiles, {len(arr.border_in)} border inputs (w = {arr.W}), "
          f"{len(arr.border_out)} border outputs; {len(stims)} configs built in {time.time() - t0:.1f} s; "
          f"loop moves per config {[len(v) for v in logs.values()]}", flush=True)
    faults = list(csv.DictReader((ROOT / "faults" / f"{arr.name}.csv").open()))
    cfgs, stat = fastsim.run_faults(arr.name, stims, faults, a.jobs)
    path = REPORTS / f"{arr.name}_{a.suite}_matrix.csv"
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "path", "polarity", "type", *cfgs, "any"])
        for f, st in zip(faults, stat):
            v = set(st.values())
            anyd = "D" if "D" in v else "P" if "P" in v else "T" if "T" in v else "U"
            w.writerow([f["id"], f["path"], f["polarity"], f["type"], *[st[c] for c in cfgs], anyd])
    print(f"   -> {path.relative_to(ROOT)}")


def tile_map():
    """(tile path, polarity) -> (tile fault id, region class)."""
    reg = {r["id"]: r["class"] for r in csv.DictReader((ROOT / "faults" / "tile_generic_regions.csv").open())}
    return {(f["path"], f["polarity"]): (f["id"], reg[f["id"]])
            for f in csv.DictReader((ROOT / "faults" / "tile_generic.csv").open())}


def cmd_analyze(a) -> None:
    import re
    from collections import Counter, defaultdict
    arr = fabric.Array(a.R, a.C)
    tm = tile_map()
    rt = {r["id"]: r for r in csv.DictReader((REPORTS / "tile_generic_rt_matrix.csv").open())}
    mats = [list(csv.DictReader((REPORTS / f"{arr.name}_{su}_matrix.csv").open())) for su in a.suite]
    rows = [dict(r, any="D" if any(m[j]["any"] == "D" for m in mats) else r["any"]) for j, r in enumerate(mats[0])]
    ck = {r["id"]: r for r in csv.DictReader((REPORTS / "tile_generic_ck_matrix.csv").open())}
    tot, det, tile_det = Counter(), Counter(), Counter()
    unmapped = Counter()
    for r in rows:
        m = re.match(r"X(\d+)Y(\d+)\.(.*)", r["path"])
        key = (m.group(3), r["polarity"]) if m else None
        if key not in tm:
            cls, tid = "inter-tile / config chain", None
            unmapped[r["path"].split(".")[0]] += 1
        else:
            tid, cls = tm[key]
        x, y = (int(m.group(1)), int(m.group(2))) if m else (-1, -1)
        pos = ("corner" if x in (0, arr.C - 1) and y in (0, arr.R - 1) else
               "edge" if x in (0, arr.C - 1) or y in (0, arr.R - 1) else "interior") if m else "-"
        for k in (cls, (cls, pos), "all"):
            tot[k] += 1
            det[k] += r["any"] == "D"
            if tid is not None:
                tile_det[k] += rt[tid]["any"] == "D" or ("clocked" in a.suite and ck[tid]["any"] == "D")
    print(f"{arr.name} {'+'.join(a.suite)}: strict D per population (tile level, same suites, on the same faults)")
    for k in sorted(tot, key=str):
        print(f"  {str(k):55s} {tot[k]:6d}  {det[k]:6d} = {100 * det[k] / tot[k]:6.2f}%   "
              f"tile {100 * tile_det[k] / tot[k]:6.2f}%")


def cmd_report(a) -> None:
    """reports/array_summary.md: coverage per array size, suite combination,
    population and tile position, against the tile level on the same faults."""
    import re
    from collections import Counter
    tm = tile_map()
    rt = {r["id"]: r for r in csv.DictReader((REPORTS / "tile_generic_rt_matrix.csv").open())}
    ck = {r["id"]: r for r in csv.DictReader((REPORTS / "tile_generic_ck_matrix.csv").open())}
    out = ["# LUT4AB arrays: fabric-level test (Phase 10)", "",
           "R x C arrays of the tile netlist, FABulous wiring (rtl/gen_array.py), stimulus only at the "
           "border inputs, observation only at the border outputs, config through FrameData rows and "
           "FrameStrobe columns. Strict D (3-valued, all storage X at power-up), fast kernel; the "
           "fault-free lane equals the array model in every config. \"tile\": the same faults' tile-level "
           "result (tile router RT00..15, and the tile clocked configs where the suite has clocked).", "",
           "Suites: uniform = tile router config RTc in every tile (array loops broken); router = "
           "configs/fab_router.py on the array; clocked = 8 registered-LUT SR / EN configs, flops routed "
           "to the border.", ""]
    for R, C in [(2, 2), (3, 3)]:
        arr = fabric.Array(R, C)
        avail = [su for su in ("uniform", "router", "clocked")
                 if (REPORTS / f"{arr.name}_{su}_matrix.csv").exists()]
        if not avail:
            continue
        mats = {su: list(csv.DictReader((REPORTS / f"{arr.name}_{su}_matrix.csv").open())) for su in avail}
        ncfg = {su: len([k for k in mats[su][0] if k not in ("id", "path", "polarity", "type", "any")])
                for su in avail}
        combos = [c for c in (["uniform"], ["router"], ["uniform", "clocked"], ["router", "clocked"])
                  if all(su in avail for su in c)]
        out += [f"## {arr.name}: {len(arr.tiles)} tiles, {len(arr.border_in)} border inputs, "
                f"{len(arr.border_out)} border outputs", "",
                "Configs: " + ", ".join(f"{su} {ncfg[su]}" for su in avail) + ".", "",
                "| population | position | faults | " + " | ".join(
                    f"{' + '.join(c)} | tile" for c in combos) + " |",
                "|---|---|---|" + "---|---|" * len(combos)]
        base = mats[avail[0]]
        info = []
        for r in base:
            m = re.match(r"X(\d+)Y(\d+)\.(.*)", r["path"])
            key = (m.group(3), r["polarity"]) if m else None
            tid, cls = tm.get(key, (None, "inter-tile / config chain"))
            if m:
                x, y = int(m.group(1)), int(m.group(2))
                pos = ("corner" if x in (0, C - 1) and y in (0, R - 1) else
                       "edge" if x in (0, C - 1) or y in (0, R - 1) else "centre")
            else:
                pos = "-"
            info.append((cls, pos, tid))
        groups = sorted({(c, p) for c, p, _ in info}) + [(c, "all") for c in sorted({c for c, _, _ in info})] + \
            [("all", "all")]
        for cls, pos in groups:
            idx = [j for j, (c, p, _) in enumerate(info)
                   if (cls == "all" or c == cls) and (pos == "all" or p == pos)]
            cells = []
            for combo in combos:
                d = sum(any(mats[su][j]["any"] == "D" for su in combo) for j in idx)
                td = sum(info[j][2] is not None and (rt[info[j][2]]["any"] == "D" or
                         ("clocked" in combo and ck[info[j][2]]["any"] == "D")) for j in idx)
                cells += [f"{100 * d / len(idx):.2f}%", f"{100 * td / len(idx):.2f}%"]
            lab = "**" + cls + "**" if pos == "all" else cls
            out.append(f"| {lab} | {pos} | {len(idx)} | " + " | ".join(cells) + " |")
        out.append("")
    (REPORTS / "array_summary.md").write_text("\n".join(out) + "\n")
    print("reports/array_summary.md")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("faultsim")
    p.add_argument("R", type=int)
    p.add_argument("C", type=int)
    p.add_argument("--suite", choices=["uniform", "router", "clocked"], default="uniform")
    p.add_argument("--jobs", type=int, default=os.cpu_count())
    p = sub.add_parser("analyze")
    p.add_argument("R", type=int)
    p.add_argument("C", type=int)
    p.add_argument("--suite", nargs="+", default=["router", "clocked"])
    sub.add_parser("report")
    a = ap.parse_args()
    {"faultsim": cmd_faultsim, "analyze": cmd_analyze, "report": cmd_report}[a.cmd](a)


if __name__ == "__main__":
    main()
