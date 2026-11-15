#!/usr/bin/env python3
"""A config set on the demo against a stored run: the same sampled faults, fault by fault.

  scripts/compare_with_stored.py <run dir> [--stored results/demo_fullbram_96cfg_50k]
         [--sample 50000] [--chunk 2000] [--jobs 4] [--only 0,1] [--limit N]

<run dir> holds configs.json (fabtest route). The faults are the stored run's: its netlist,
its fault list and its sample (random.Random(1), as tests R1), so a fault is the same
object in both matrices. Every kept config simulates every sampled fault (full matrix), in
chunks written to <run>/coverage/chunk_<i>.csv: an existing chunk is not run again (a
restart resumes). At the end:
  coverage/matrix.csv   id, path, polarity, status per config, any
  coverage/cover.json   greedy set cover: configs in order, cumulative detected, the
                        configs needed for 95% / 99% of the final union
  compare.txt           per part: stored union, this set, both together (95% intervals),
                        and the faults only one side detects
--only / --limit: a dry run on some configs / the first N sampled faults.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import re
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fabtest import project, spec as specmod  # noqa: E402
from fabtest.model.generic import GenericFabric  # noqa: E402
from fabtest.sim import stimuli  # noqa: E402
from fabtest.sim.fastsim import fastsim  # noqa: E402

DEMO = Path("/home/user/ext/demo")


def part(path: str) -> str:
    if path.startswith("Inst_BlockRAM"):
        return "BlockRAM"
    m = re.match(r"Tile_X\d+Y\d+_(?:DSP\.Tile_X\d+Y\d+_)?(\w+?)\.", path)
    return m.group(1) if m else "top-level wiring"


def pct(d: int, n: int) -> str:
    p = d / n if n else 0.0
    return f"{100 * p:6.2f}% +- {196 * math.sqrt(p * (1 - p) / n) if n else 0:.2f}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--stored", default=str(ROOT / "results" / "demo_fullbram_96cfg_50k"))
    ap.add_argument("--sample", type=int, default=50000)
    ap.add_argument("--chunk", type=int, default=2000)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--only", help="config indices (dry run)")
    ap.add_argument("--limit", type=int, help="the first N sampled faults (dry run)")
    a = ap.parse_args()
    run, stored = Path(a.run), Path(a.stored)
    cov = run / "coverage"
    cov.mkdir(parents=True, exist_ok=True)
    cfg = run / "configs.json"
    n_cfg = len(json.loads(cfg.read_text())["configs"])
    only = [int(v) for v in a.only.split(",")] if a.only else None

    faults = list(csv.DictReader((stored / "netlist" / "faults.csv").open()))
    smp = sorted(random.Random(1).sample(range(len(faults)), a.sample))
    if a.limit:
        smp = smp[:a.limit]
    fl = [faults[j] for j in smp]
    del faults
    print(f"compare: {len(fl)} faults of the stored sample, {n_cfg} configs", flush=True)

    ff = GenericFabric(specmod.load(project.init(DEMO)))
    t0 = time.time()
    stims = stimuli.build_stims(ff, cfg, only=only, quiet=True)
    print(f"  stimuli: {len(stims)} configs in {time.time() - t0:.0f} s", flush=True)
    nl = fastsim.Netlist("stored", stored / "netlist" / "netlist.json")
    res = fastsim.fault_free(nl, stims, a.jobs)
    keep = [s for s in stims if res[s.name] is None]
    left = {n: e for n, e in res.items() if e}
    print(f"  fault-free: {len(keep)} of {len(stims)} configs" + (f", left out {left}" if left else ""), flush=True)
    names = [s.name for s in keep]
    (cov / "configs.json").write_text(json.dumps({"kept": names, "left_out": left}, indent=1))

    for i in range(0, len(fl), a.chunk):
        path = cov / f"chunk_{i // a.chunk:04d}.csv"
        if path.exists():
            continue
        part_fl = fl[i:i + a.chunk]
        t1 = time.time()
        _, stat = fastsim.run_faults("stored", keep, part_fl, a.jobs, limit_factor=200, nl=nl, quiet=True)
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["id", "path", "polarity", *names])
            for f, s in zip(part_fl, stat):
                w.writerow([f["id"], f["path"], f["polarity"], *[s[c] for c in names]])
        tmp.rename(path)
        print(f"  chunk {i // a.chunk}: {len(part_fl)} faults in {time.time() - t1:.0f} s", flush=True)

    # ---------------------------------------------------------------- results
    rows = [r for p in sorted(cov.glob("chunk_*.csv")) for r in csv.DictReader(p.open())]
    assert [r["id"] for r in rows] == [f["id"] for f in fl], "chunks do not match the sample"
    with (cov / "matrix.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "path", "polarity", *names, "any"])
        for r in rows:
            v = {r[c] for c in names}
            w.writerow([r["id"], r["path"], r["polarity"], *[r[c] for c in names],
                        "D" if "D" in v else "P" if "P" in v else "T" if "T" in v else "U"])
    det = {c: {k for k, r in enumerate(rows) if r[c] == "D"} for c in names}
    done, curve = set(), []
    left_c = list(names)
    while left_c:
        c = max(left_c, key=lambda c: (len(det[c] - done), -names.index(c)))
        new = det[c] - done
        if not new:
            break
        done |= new
        left_c.remove(c)
        curve.append([c, len(new), len(done)])
    final = len(done)
    marks = {m: next(k + 1 for k, e in enumerate(curve) if e[2] >= final * m / 100) for m in (95, 99)} if curve else {}
    (cov / "cover.json").write_text(json.dumps({"faults": len(rows), "detected": final, "configs": len(names),
                                                "curve": curve, "configs_for_pct_of_final": marks}, indent=0))

    stored_m = list(csv.DictReader((stored / "coverage" / "union96_matrix.csv").open()))
    sm = {r["id"]: r["any"] == "D" for r in stored_m}
    tot, d_old, d_new, d_both = Counter(), Counter(), Counter(), Counter()
    only_old, only_new = Counter(), Counter()
    for k, r in enumerate(rows):
        o, n = sm[r["id"]], k in done
        for p in (part(r["path"]), "all"):
            tot[p] += 1
            d_old[p] += o
            d_new[p] += n
            d_both[p] += o or n
            only_old[p] += o and not n
            only_new[p] += n and not o
    L = [f"{run.name} ({len(names)} configs) against {stored.name} (stored union), {len(rows)} faults of the stored sample",
         f"  {'part':18s} {'faults':>7s}  {'stored':>16s}  {'this set':>16s}  {'both':>16s}  only stored / only this"]
    for p in sorted(tot, key=lambda p: (p != "all", -tot[p])):
        L.append(f"  {p:18s} {tot[p]:7d}  {pct(d_old[p], tot[p])}  {pct(d_new[p], tot[p])}  {pct(d_both[p], tot[p])}"
                 f"  {only_old[p]} / {only_new[p]}")
    if marks:
        L.append(f"  set cover: {marks[95]} configs for 95% of this set's union, {marks[99]} for 99%")
    text = "\n".join(L) + "\n"
    (run / "compare.txt").write_text(text)
    print(text, end="")


if __name__ == "__main__":
    main()
