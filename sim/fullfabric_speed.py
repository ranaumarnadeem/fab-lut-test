#!/usr/bin/env python3
"""Fast-kernel feasibility on the full demo fabric (Phase 12).

  python3 sim/fullfabric_speed.py [--faults 2000] [--jobs 4]

synth/netlist_fullfabric.json (synth/build_fullfabric.sh: ext/demo eFPGA.v,
14 x 10 tiles, 84 LUT4AB). One config: router config RT00
(configs/sm_router.json) in every LUT4AB tile, every other tile all zero;
written through the real frame interface (FrameData row y = bits
[32 y + 31 : 32 y], FrameStrobe column x = bits [20 x + 19 : 20 x], eFPGA.v),
column by column, frame by frame (data / strobe / release); then 64 random
vectors on the 28 W_IO pads. Observed: W_IO pad outputs and Config_access
bits. No model here: the fault-free lane is the reference (no expected-value
check). Times the netlist load, the fault-free run and a random sample of
faults, and extrapolates to the whole fault list.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "configs"), str(ROOT / "sim"), str(ROOT / "sim" / "fastsim")]
import fastsim  # noqa: E402
import sm_configs as sc  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--faults", type=int, default=2000)
    ap.add_argument("--jobs", type=int, default=4)
    a = ap.parse_args()
    t0 = time.time()
    nl = fastsim.Netlist("fullfabric")
    t_load = time.time() - t0
    js = json.loads((ROOT / "synth" / "netlist_fullfabric.json").read_text())
    ports = js["modules"]["eFPGA"]["ports"]
    pads_in = sorted(p for p in ports if re.fullmatch(r"Tile_X0Y\d+_[AB]_O_top", p))
    outs = sorted(p for p in ports if ports[p]["direction"] == "output" and p.startswith("Tile_X0Y"))
    lut_cols = [x for x in range(10) if f"Tile_X{x}Y1_A_O_top" not in ports and x not in (0, 3, 6, 9)]
    rt00 = json.loads((ROOT / "configs" / "sm_router.json").read_text())["configs"][0]
    words = sc.frame_words(sc.config_word(rt00))
    in_names = [f"FrameData[{k}]" for k in range(512)] + [f"FrameStrobe[{k}]" for k in range(200)] + \
               pads_in + ["UserCLK"]
    rows, sample = [], []

    def row(fd: dict[int, int], strobe, inp: int):
        r = np.zeros(len(in_names), dtype=np.uint8)
        for y, w in fd.items():
            for k in range(32):
                r[32 * y + k] = (w >> k) & 1
        if strobe is not None:
            x, f = strobe
            r[512 + 20 * x + f] = 1
        for j in range(len(pads_in)):
            r[712 + j] = (inp >> j) & 1
        return r
    fd = {}
    for x in range(10):
        for f in range(20):
            fd = {y: (words[f] if x in lut_cols else 0) for y in range(1, 15)}
            rows += [row(fd, None, 0), row(fd, (x, f), 0), row(fd, None, 0)]
            sample += [0, 0, 0]
    rnd = random.Random(5)
    for _ in range(64):
        rows.append(row(fd, None, rnd.getrandbits(len(pads_in))))
        sample.append(1)
    st = fastsim.Stim("RT00_all", in_names, np.array(rows), np.array(sample, dtype=np.uint8), outs, None)
    faults = list(csv.DictReader((ROOT / "faults" / "fullfabric.csv").open()))
    smp = random.Random(1).sample(faults, a.faults)
    t1 = time.time()
    cfgs, stat = fastsim.run_faults("fullfabric", [st], smp, a.jobs, nl=nl, do_collapse=False, quiet=True)
    dt = time.time() - t1
    from collections import Counter
    cnt = Counter(s[cfgs[0]] for s in stat)
    per = dt / len(smp)
    print(f"fullfabric: {nl.n_cells} cells, {len(faults)} faults, load {t_load:.0f} s; "
          f"{len(rows)} steps per config ({len(pads_in)} pads in, {len(outs)} outputs observed)")
    print(f"  {len(smp)} random faults, 1 config: {dt:.1f} s on {a.jobs} threads ({1e3 * per:.1f} ms per fault); "
          f"{dict(cnt)}")
    print(f"  extrapolated: all {len(faults)} faults, one config: {per * len(faults) / 60:.0f} min "
          f"(before fault collapsing)")


if __name__ == "__main__":
    main()
