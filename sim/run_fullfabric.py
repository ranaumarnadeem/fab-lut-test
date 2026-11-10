#!/usr/bin/env python3
"""Full demo fabric: backbone configs, registered chains, sampled fault simulation (Phase 12).

  python3 sim/run_fullfabric.py route --configs N [--chains 2]
  python3 sim/run_fullfabric.py faultsim [--sample 10000] [--jobs 4]

route: configs/fab_router.py on configs/fullfabric.FullFabric (no polish, backbone:
  per row the LUT chains end at the row's W_IO pads), configs/fab_router_fullfabric.json.
faultsim: every config with all LUTs registered (XOR / XNOR alternating, EN = VCC,
  SR = GND), 64 clocked vectors on the 28 pads; a seeded random sample of the
  fault list (faults/fullfabric.csv); strict D; coverage with a 95% interval and
  per tile type. The fault-free lane must equal the model. Writes
  reports/fullfabric_chain_sample_matrix.csv.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import re
import sys
import time
from pathlib import Path

os.environ.setdefault("FASTSIM_OSC", "local")
ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "configs"), str(ROOT / "sim"), str(ROOT / "sim" / "fastsim")]
import fab_router as fr  # noqa: E402
import fabric  # noqa: E402
import fastsim  # noqa: E402
import fullfabric  # noqa: E402
import sm_configs as sc  # noqa: E402

CFG = ROOT / "configs" / "fab_router_fullfabric.json"
REPORTS = ROOT / "reports"


def cmd_route(a) -> None:
    ff = fullfabric.FullFabric()
    fr.CHAINS = a.chains
    t0 = time.time()
    configs, open_work, total, log, _ = fr.route_all(ff, a.configs, do_polish=False,
                                                     seed=lambda cfg, b: fr.backbone(cfg, b))
    done = total - sum(len(v) for v in open_work.values())
    CFG.write_text(json.dumps({"configs": configs, "log": log, "chains": a.chains,
                               "items": {"total": total, "shown": done}}, indent=0))
    print(f"fullfabric: {len(configs)} configs, {done} of {total} items ({100 * done / total:.2f}%), "
          f"{time.time() - t0:.0f} s -> {CFG.relative_to(ROOT)}")


def cmd_faultsim(a) -> None:
    ff = fullfabric.FullFabric()
    data = json.loads(CFG.read_text())
    inv = {ff.tn(t): t for t in ff.tiles}
    stims = []
    for c, cfg in enumerate(data["configs"]):
        sels = {inv[n]: s for n, s in cfg.items()}
        for t in ff.tiles:
            sels.setdefault(t, {})
        tt = sc.cell.XNOR_TT if c % 2 else sc.cell.XOR_TT
        luts = {t: {x: (tt, 0, 1, 0) for x in sc.LUTS} for t in ff.lut_tiles}
        stims.append(fabric.stim_clocked(ff, f"FF{c:02d}", sels, sc.frame_order(c % sc.F), None,
                                         list(ff.vecs), luts, partial=True))
        print(f"  FF{c:02d}: {int(stims[-1].sample.sum())} samples", flush=True)
    # fault-free check per config first: a config whose fault-free lane differs from the model
    # or does not settle is left out (reported)
    nl = fastsim.Netlist("fullfabric")
    keep = []
    for st in stims:
        try:
            fastsim.Engine(nl, [st], W=1, limit_factor=200).run_batch([])
            keep.append(st)
        except RuntimeError as e:
            print(f"  left out: {e}", flush=True)
    print(f"fullfabric: {len(keep)} of {len(stims)} configs pass the fault-free check", flush=True)
    stims = keep
    faults = list(csv.DictReader((ROOT / "faults" / "fullfabric.csv").open()))
    smp = sorted(random.Random(1).sample(range(len(faults)), a.sample))
    fl = [faults[j] for j in smp]
    t0 = time.time()
    # a full fabric needs more events to settle one input change (switch-matrix SCCs): 200 x cells
    cfgs, stat = fastsim.run_faults("fullfabric", stims, fl, a.jobs, limit_factor=200, nl=nl)
    print(f"fullfabric: {len(fl)} sampled faults x {len(cfgs)} configs in {time.time() - t0:.0f} s", flush=True)
    path = REPORTS / "fullfabric_chain_sample_matrix.csv"
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "path", "polarity", *cfgs, "any"])
        for f, st in zip(fl, stat):
            v = set(st.values())
            w.writerow([f["id"], f["path"], f["polarity"], *[st[c] for c in cfgs],
                        "D" if "D" in v else "P" if "P" in v else "T" if "T" in v else "U"])
    from collections import Counter
    tot, det = Counter(), Counter()
    for f, st in zip(fl, stat):
        m = re.match(r"Tile_X\d+Y\d+_(?:DSP\.Tile_X\d+Y\d+_)?(\w+?)\.", f["path"])
        k = m.group(1) if m else "top-level wiring"
        d = "D" in st.values()
        for kk in (k, "all"):
            tot[kk] += 1
            det[kk] += d
    for k in sorted(tot, key=lambda k: -tot[k]):
        p = det[k] / tot[k]
        ci = 1.96 * math.sqrt(p * (1 - p) / tot[k])
        print(f"  {k:20s} {tot[k]:6d} sampled: {100 * p:6.2f}% +- {100 * ci:.2f}")
    print(f"   -> {path.relative_to(ROOT)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("route")
    p.add_argument("--configs", type=int, default=12)
    p.add_argument("--chains", type=int, default=2)
    p = sub.add_parser("faultsim")
    p.add_argument("--sample", type=int, default=10000)
    p.add_argument("--jobs", type=int, default=4)
    a = ap.parse_args()
    {"route": cmd_route, "faultsim": cmd_faultsim}[a.cmd](a)


if __name__ == "__main__":
    main()
