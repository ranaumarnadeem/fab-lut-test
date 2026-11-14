"""fabtest route / faultsim / report (fabtest P5).

Work dir layout (fabtest/project.py):
  route/configs.json        routed test configs (fab_router backbone recipe), checkpointed
                            after every config; route --extend adds configs to it
  faultsim/sample.json      the fault sample (seed, ids)
  faultsim/chunk_<i>.csv    per sampled fault: status per config (D detected, P possibly,
                            T / U not); a chunk that exists is not run again
  report.txt                coverage, all configs and per tile type, 95% interval
"""
from __future__ import annotations

import csv
import json
import math
import os
import random
import re
import sys
import time
from collections import Counter
from pathlib import Path

os.environ.setdefault("FASTSIM_OSC", "local")
ROOT = Path(__file__).resolve().parent.parent
for _d in ("configs", "sim", "sim/fastsim", "rtl"):
    if str(ROOT / _d) not in sys.path:
        sys.path.insert(0, str(ROOT / _d))


def model(spec):
    from fabtest.model import GenericFabric
    return GenericFabric(spec)


def route(spec, work: Path, n_configs: int, chains: int = 2, extend: bool = False) -> Path:
    import fab_router as fr
    out = work / "route" / "configs.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    ff = model(spec)
    fr.CHAINS = chains
    prior, plog = [], []
    if extend and out.exists():
        d = json.loads(out.read_text())
        prior, plog = d["configs"], d["log"]
        assert d["chains"] == chains, "route --extend with another --chains"
    t0 = time.time()

    def save(cs, lg, total, left):
        out.write_text(json.dumps({"configs": prior + cs, "log": plog + lg, "chains": chains,
                                   "items": {"total": total, "shown": total - left}}, indent=0))
    configs, open_work, total, log, _ = fr.route_all(ff, n_configs, do_polish=False, prior=prior or None,
                                                     seed=lambda cfg, b: fr.backbone(cfg, b),
                                                     idx0=len(prior), checkpoint=save)
    done = total - sum(len(v) for v in open_work.values())
    save(configs, log, total, total - done)
    print(f"fabtest route: {len(prior) + len(configs)} configs, {done} of {total} items "
          f"({100 * done / total:.2f}%), {time.time() - t0:.0f} s -> {out}")
    return out


def _stims(spec, work: Path, cfg_path: Path | None):
    import run_fullfabric as rf
    ff = model(spec)
    return rf.build_stims(ff, cfg_path or work / "route" / "configs.json", quiet=True)


def faultsim(spec, work: Path, sample: int = 2000, seed: int = 1, chunk: int = 2000, jobs: int = 4,
             cfg_path: Path | None = None) -> Path:
    """Sampled fault simulation of every config that passes the fault-free check."""
    import fastsim
    fdir = work / "faultsim"
    fdir.mkdir(parents=True, exist_ok=True)
    stims = _stims(spec, work, cfg_path)
    nl = fastsim.Netlist(spec.name if hasattr(spec, "name") else "fabtest", work / "synth" / "netlist.json")
    keep = []
    for st in stims:
        try:
            fastsim.Engine(nl, [st], W=1, limit_factor=200).run_batch([])
            keep.append(st)
        except RuntimeError as e:
            print(f"  left out {st.name}: {e}", flush=True)
    print(f"fabtest faultsim: {len(keep)} of {len(stims)} configs pass the fault-free check", flush=True)
    (fdir / "configs.json").write_text(json.dumps({"kept": [s.name for s in keep],
                                                   "left_out": [s.name for s in stims if s not in keep]}))
    faults = list(csv.DictReader((work / "synth" / "faults.csv").open()))
    n = min(sample, len(faults)) if sample else len(faults)
    smp = sorted(random.Random(seed).sample(range(len(faults)), n)) if n < len(faults) else list(range(n))
    meta = {"seed": seed, "n": n, "of": len(faults), "chunk": chunk}
    old = fdir / "sample.json"
    if old.exists() and json.loads(old.read_text()) != meta:
        for p in fdir.glob("chunk_*.csv"):
            p.unlink()
    old.write_text(json.dumps(meta))
    names = [s.name for s in keep]
    for i in range(0, n, chunk):
        path = fdir / f"chunk_{i // chunk:04d}.csv"
        if path.exists():
            continue
        fl = [faults[j] for j in smp[i:i + chunk]]
        t0 = time.time()
        cfgs, stat = fastsim.run_faults("fabtest", keep, fl, jobs, limit_factor=200, nl=nl, quiet=True)
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["id", "path", "polarity", *names])
            for f, st in zip(fl, stat):
                w.writerow([f["id"], f["path"], f["polarity"], *[st[c] for c in names]])
        tmp.rename(path)
        print(f"  chunk {i // chunk}: {len(fl)} faults in {time.time() - t0:.0f} s", flush=True)
    return fdir


def part_of(spec, path: str) -> str:
    """Tile type (or macro module, or top-level wiring) a fault path belongs to."""
    sup = "|".join(map(re.escape, spec.supertiles)) or "(?!)"
    m = re.match(rf"Tile_X\d+Y\d+_(?:(?:{sup})\.Tile_X\d+Y\d+_)?(\w+?)\.", path)
    if m and m.group(1) in spec.types:
        return m.group(1)
    for mi in spec.d["top"]["macros"]:
        if path.startswith(mi["inst"] + "."):
            return mi["module"]
    return "top-level wiring"


def report(spec, work: Path) -> str:
    fdir = work / "faultsim"
    rows = [r for p in sorted(fdir.glob("chunk_*.csv")) for r in csv.DictReader(p.open())]
    if not rows:
        raise SystemExit("fabtest report: no fault simulation results (run faultsim first)")
    cfgs = [c for c in rows[0] if c not in ("id", "path", "polarity")]
    meta = json.loads((fdir / "sample.json").read_text())
    tot, det = Counter(), Counter()
    per_cfg = Counter()
    for r in rows:
        k = part_of(spec, r["path"])
        d = any(r[c] == "D" for c in cfgs)
        for c in cfgs:
            per_cfg[c] += r[c] == "D"
        for kk in (k, "all"):
            tot[kk] += 1
            det[kk] += d
    L = [f"fabtest report: {len(rows)} of {meta['of']} faults sampled (seed {meta['seed']}), {len(cfgs)} configs"]
    for k in sorted(tot, key=lambda k: (k != "all", -tot[k])):
        p = det[k] / tot[k]
        ci = 1.96 * math.sqrt(p * (1 - p) / tot[k])
        L.append(f"  {k:20s} {tot[k]:8d} sampled: {100 * p:6.2f}% +- {100 * ci:.2f}")
    left = json.loads((fdir / "configs.json").read_text()).get("left_out", []) if (fdir / "configs.json").exists() else []
    if left:
        L.append(f"  configs left out (fault-free check): {', '.join(left)}")
    text = "\n".join(L) + "\n"
    (work / "report.txt").write_text(text)
    return text
