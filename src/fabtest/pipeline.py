"""fabtest route / faultsim / report (fabtest P5).

The fabric's work dir (build/fabtest/<fabric>, fabtest/project.py) holds the intermediates
(project copy, spec, netlist, fault list). Every run gets its own directory,
results/<fabric>/<run>/:
  configs.json              the test configs: routed (route, checkpointed after every
                            config; --extend adds configs) or copied (--configs-from)
  faultsim/sample.json      the fault sample (seed, size)
  faultsim/chunk_<i>.csv    per sampled fault: status per config (D detected, P possibly,
                            T / U not); a chunk that exists is not run again
  faultsim/configs.json     configs that pass the fault-free check, and those left out
  report.txt                coverage, all configs and per tile type, 95% interval
  powerup.json              power-up check of a sample of the X-only faults (powerup)
  export/                   the test program (fabtest/export)
"""
from __future__ import annotations

import csv
import json
import math
import os
import random
import re
import shutil
import time
from collections import Counter
from pathlib import Path

os.environ.setdefault("FASTSIM_OSC", "local")


def run_dir(work: Path, name: str | None, create: bool = False) -> Path:
    """results/<fabric>/<name>; without a name a new one (create: timestamp) or the latest."""
    from fabtest.paths import RESULTS
    base = RESULTS / work.name
    if name:
        d = base / name
    elif create:
        d = base / time.strftime("%Y%m%d-%H%M%S")
    else:
        runs = sorted((p for p in base.glob("*") if (p / "configs.json").exists()), key=lambda p: p.stat().st_mtime)
        if not runs:
            raise SystemExit(f"fabtest: no run in {base} (fabtest route, or --run NAME --configs-from JSON)")
        d = runs[-1]
    d.mkdir(parents=True, exist_ok=True)
    return d


def use_configs(run: Path, configs_from: Path | None) -> Path:
    """The run's configs.json; --configs-from copies a stored config set into the run."""
    dst = run / "configs.json"
    if configs_from is not None:
        if dst.exists() and dst.read_bytes() != Path(configs_from).read_bytes():
            raise SystemExit(f"fabtest: {dst} holds other configs (use a new --run)")
        shutil.copy(configs_from, dst)
    if not dst.exists():
        raise SystemExit(f"fabtest: {dst} missing (fabtest route, or --configs-from JSON)")
    return dst


def model(spec):
    from fabtest.model.generic import GenericFabric
    return GenericFabric(spec)


def route(spec, run: Path, n_configs: int, chains: int = 2, extend: bool = False) -> Path:
    from fabtest.patterns.config import fab_router as fr
    out = run / "configs.json"
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
    configs, open_work, total, log, _ = fr.route_all(ff, n_configs, prior=prior or None,
                                                     seed=lambda cfg, b: fr.backbone(cfg, b),
                                                     idx0=len(prior), checkpoint=save)
    done = total - sum(len(v) for v in open_work.values())
    save(configs, log, total, total - done)
    print(f"fabtest route: {len(prior) + len(configs)} configs, {done} of {total} items "
          f"({100 * done / total:.2f}%), {time.time() - t0:.0f} s -> {out}")
    return out


def _stims(spec, cfg_path: Path):
    from fabtest.sim import stimuli as rf
    ff = model(spec)
    return rf.build_stims(ff, cfg_path, quiet=True)


def faultsim(spec, work: Path, run: Path, sample: int = 2000, seed: int = 1, chunk: int = 2000, jobs: int = 4) -> Path:
    """Sampled fault simulation of every config that passes the fault-free check."""
    from fabtest.sim.fastsim import fastsim
    fdir = run / "faultsim"
    fdir.mkdir(parents=True, exist_ok=True)
    stims = _stims(spec, run / "configs.json")
    nl = fastsim.Netlist("fabtest", work / "synth" / "netlist.json")
    ff_res = fastsim.fault_free(nl, stims, jobs)
    keep = []
    for st in stims:
        err = ff_res[st.name]
        if err is None:
            keep.append(st)
        else:
            print(f"  left out {st.name}: {err}", flush=True)
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


def report(spec, run: Path) -> str:
    fdir = run / "faultsim"
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
    (run / "report.txt").write_text(text)
    return text


def powerup(spec, work: Path, run: Path, sample: int = 400, seed: int = 2, max_u: int = 10,
            jobs: int = 4) -> str:
    """X-only faults of the run (P in some config, D in none): a seeded sample of them through
    the power-up check (sim/fastsim/powerup.py). A fault detected for every power-up state of
    the elements the test never writes counts as detected; faults whose undecided set U is
    larger than max_u are not decided (RegFile memory never written: U in the thousands)."""
    import gc
    from fabtest.sim.fastsim import fastsim, powerup as pu_mod
    fdir = run / "faultsim"
    rows = [r for p in sorted(fdir.glob("chunk_*.csv")) for r in csv.DictReader(p.open())]
    if not rows:
        raise SystemExit("fabtest powerup: no fault simulation results (run faultsim first)")
    cfgs = [c for c in rows[0] if c not in ("id", "path", "polarity")]
    xo = [r for r in rows if "D" not in {r[c] for c in cfgs} and "P" in {r[c] for c in cfgs}]
    n, det = len(rows), sum(any(r[c] == "D" for c in cfgs) for r in rows)
    pick = sorted(random.Random(seed).sample(range(len(xo)), min(sample, len(xo))))
    want = {xo[j]["id"] for j in pick}
    faults = {f["id"]: f for f in csv.DictReader((work / "synth" / "faults.csv").open()) if f["id"] in want}
    fl = [faults[xo[j]["id"]] for j in pick]
    p_cfgs = {k: [c for c in cfgs if xo[j][c] == "P"] for k, j in enumerate(pick)}
    netlist = work / "synth" / "netlist.json"
    nl = fastsim.Netlist("fabtest", netlist)   # first: the JSON parse is the memory peak
    gc.collect()
    stims = {st.name: st for st in _stims(spec, run / "configs.json")}
    stims = [stims[c] for c in cfgs]
    t0 = time.time()
    pu = pu_mod.PowerUp(nl, stims, netlist, limit_factor=200)
    v = pu.decide(fl, p_cfgs, jobs=jobs, max_u=max_u, leave_x=True) if fl else {}
    from collections import Counter as _C
    cnt = _C(r["verdict"] for r in v.values())
    p = cnt["detected"] / len(fl) if fl else 0.0
    ci = 1.96 * math.sqrt(p * (1 - p) / len(fl)) if fl else 0.0
    (run / "powerup.json").write_text(json.dumps(
        {"sample": len(fl), "x_only": len(xo), "faults": n, "detected_strict": det, "verdicts": dict(cnt),
         "faults_decided": [{"id": fl[k]["id"], "path": fl[k]["path"], "polarity": fl[k]["polarity"], **r}
                            for k, r in v.items()]}, indent=0))
    return (f"fabtest powerup: {len(fl)} of {len(xo)} X-only faults sampled, {dict(cnt)}, {time.time() - t0:.0f} s\n"
            f"  detected for every power-up state: {100 * p:.1f}% +- {100 * ci:.1f} of the X-only faults; "
            f"estimated coverage {100 * (det + p * len(xo)) / n:.2f}% (strict {100 * det / n:.2f}%)\n")
