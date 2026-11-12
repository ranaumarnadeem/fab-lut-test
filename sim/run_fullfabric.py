#!/usr/bin/env python3
"""Full demo fabric: backbone configs, registered chains, sampled fault simulation (Phase 12).

  python3 sim/run_fullfabric.py route --configs N [--chains 2] [--extend]
  python3 sim/run_fullfabric.py faultsim [--sample 10000] [--jobs 4]
  python3 sim/run_fullfabric.py powerup [--sample 400] [--max-u 10]

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
CKPT = ROOT / "build" / "fab_router_fullfabric_ckpt.json"   # after every config (resume: --extend)
REPORTS = ROOT / "reports"


def cmd_route(a) -> None:
    ff = fullfabric.FullFabric()
    fr.CHAINS = a.chains
    t0 = time.time()
    prior, plog = [], []
    if a.extend:
        d = json.loads((CKPT if a.resume else CFG).read_text())
        prior, plog = d["configs"], d["log"]
        assert d["chains"] == a.chains, "--extend with another --chains"

    def save(cs, lg, total, left, path=CKPT):
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({"configs": prior + cs, "log": plog + lg, "chains": a.chains,
                                    "items": {"total": total, "shown": total - left}}, indent=0))
    configs, open_work, total, log, _ = fr.route_all(ff, a.configs, do_polish=False, prior=prior or None,
                                                     seed=lambda cfg, b: fr.backbone(cfg, b),
                                                     idx0=len(prior), checkpoint=save)
    done = total - sum(len(v) for v in open_work.values())
    save(configs, log, total, total - done, CFG)
    configs = prior + configs
    print(f"fullfabric: {len(configs)} configs, {done} of {total} items ({100 * done / total:.2f}%), "
          f"{time.time() - t0:.0f} s -> {CFG.relative_to(ROOT)}")


def build_stims(ff) -> list:
    data = json.loads(CFG.read_text())
    inv = {ff.tn(t): t for t in ff.tiles}
    stims = []
    for c, cfg in enumerate(data["configs"]):
        sels = {inv[n]: s for n, s in cfg.items()}
        for t in ff.tiles:
            sels.setdefault(t, {})
        tt = sc.cell.XNOR_TT if c % 2 else sc.cell.XOR_TT
        luts = {t: {x: (tt, 0, 1, 0) for x in sc.LUTS} for t in ff.lut_tiles}
        for t in ff.lut_tiles:   # constant LUTs (RegFile W_en / addresses): combinational
            for x, val in sels[t].get("__lut", {}).items():
                luts[t][x] = (0xFFFF if val else 0, 0, 0, 0)
        stims.append(fabric.stim_clocked(ff, f"FF{c:02d}", sels, sc.frame_order(c % sc.F), None,
                                         list(ff.vecs), luts, partial=True))
        print(f"  FF{c:02d}: {int(stims[-1].sample.sum())} samples", flush=True)
    return stims


def cmd_faultsim(a) -> None:
    ff = fullfabric.FullFabric()
    stims = build_stims(ff)
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


def cmd_powerup(a) -> None:
    """X-only faults of the sample matrix (P in some config, D in none): a seeded sample of
    them through the power-up check (sim/fastsim/powerup.py); faults whose U is larger
    than --max-u are not decided (RegFile memory never written: U in the thousands)."""
    import gc
    import powerup
    from collections import Counter
    nl = fastsim.Netlist("fullfabric")   # first: the JSON parse is the memory peak
    gc.collect()
    ff = fullfabric.FullFabric()
    rows = list(csv.DictReader((REPORTS / "fullfabric_chain_sample_matrix.csv").open()))
    cfgs = [c for c in rows[0] if c.startswith("FF")]
    xo = [r for r in rows if r["any"] == "P"]
    n, det = len(rows), sum(r["any"] == "D" for r in rows)
    pick = sorted(random.Random(2).sample(range(len(xo)), min(a.sample, len(xo))))
    want = {xo[j]["id"] for j in pick}
    faults = {f["id"]: f for f in csv.DictReader((ROOT / "faults" / "fullfabric.csv").open()) if f["id"] in want}
    fl = [faults[xo[j]["id"]] for j in pick]
    p_cfgs = {k: [c for c in cfgs if xo[j][c] == "P"] for k, j in enumerate(pick)}
    del rows[:], faults
    stims = {st.name: st for st in build_stims(ff)}
    stims = [stims[c] for c in cfgs]
    del ff
    gc.collect()
    t0 = time.time()
    pu = powerup.PowerUp(nl, stims, ROOT / "synth" / "netlist_fullfabric.json", limit_factor=200)
    v = pu.decide(fl, p_cfgs, jobs=a.jobs, max_u=a.max_u)
    cnt = Counter(r["verdict"] for r in v.values())
    p = cnt["detected"] / len(fl)
    ci = 1.96 * math.sqrt(p * (1 - p) / len(fl))
    ks = sorted(r["k"] for r in v.values())
    print(f"fullfabric power-up: {len(fl)} of {len(xo)} X-only sampled faults, {dict(cnt)}, "
          f"|U| median {ks[len(ks) // 2]}, {time.time() - t0:.0f} s")
    print(f"  detected for every power-up state: {100 * p:.1f}% +- {100 * ci:.1f} of the X-only faults;"
          f" estimated exhaustive coverage {100 * (det + p * len(xo)) / n:.2f}% (strict {100 * det / n:.2f}%)")
    (REPORTS / "fullfabric_powerup.json").write_text(json.dumps(
        [{"id": fl[k]["id"], "path": fl[k]["path"], "polarity": fl[k]["polarity"], **r} for k, r in v.items()],
        indent=0))


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("route")
    p.add_argument("--configs", type=int, default=12)
    p.add_argument("--chains", type=int, default=2)
    p.add_argument("--extend", action="store_true", help="add --configs configs to the routed ones")
    p.add_argument("--resume", action="store_true", help="with --extend: start from the checkpoint")
    p = sub.add_parser("faultsim")
    p.add_argument("--sample", type=int, default=10000)
    p.add_argument("--jobs", type=int, default=4)
    p = sub.add_parser("powerup")
    p.add_argument("--sample", type=int, default=400)
    p.add_argument("--max-u", type=int, default=10)
    p.add_argument("--jobs", type=int, default=4)
    a = ap.parse_args()
    {"route": cmd_route, "faultsim": cmd_faultsim, "powerup": cmd_powerup}[a.cmd](a)


if __name__ == "__main__":
    main()
