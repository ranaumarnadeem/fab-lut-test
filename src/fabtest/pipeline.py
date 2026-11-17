"""fabtest run / route / faultsim / report (fabtest P5).

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
import hashlib
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


def apply_caps(ff, spec) -> None:
    """The route caps of the fabric's settings (fabtest.toml [route]) on the model and router."""
    from fabtest.patterns.config import fab_router as fr
    c = spec.cfg["route"]
    ff.route_max_pop, ff.route_max_reach, ff.loop_max_moves = c["max_pop"], c["max_reach"], c["loop_max_moves"]
    fr.BRAM_MAX_POP, fr.CONST_RADIUS = c["bram_max_pop"], c["const_radius"]


def route(spec, run: Path, n_configs: int, chains: int = 2, extend: bool = False, ff=None) -> Path:
    from fabtest.patterns.config import fab_router as fr
    out = run / "configs.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    ff = ff or model(spec)
    apply_caps(ff, spec)
    fr.CHAINS = chains
    prior, plog, ppass = [], [], []
    if extend and out.exists():
        d = json.loads(out.read_text())
        prior, plog = d["configs"], d["log"]
        ppass = d.get("passes") or [{"configs": len(prior), "chains": d["chains"]}]
    t0 = time.time()

    def save(cs, lg, total, left):   # chains: of the last pass (passes: every pass)
        out.write_text(json.dumps({"configs": prior + cs, "log": plog + lg, "chains": chains,
                                   "passes": ppass + [{"configs": len(cs), "chains": chains}],
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
    """Sampled fault simulation of every config that passes the fault-free check. A chunk
    that exists is not run again; one of other configs, netlist or sample is removed first."""
    from fabtest.sim.fastsim import fastsim
    fdir = run / "faultsim"
    fdir.mkdir(parents=True, exist_ok=True)
    if faultsim_done(work, run, sample, seed, chunk):
        print(f"fabtest faultsim: {fdir} is up to date")
        return fdir
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
    meta = {"seed": seed, "n": n, "of": len(faults), "chunk": chunk,
            "configs": hashlib.sha1((run / "configs.json").read_bytes()).hexdigest(),
            "netlist": _mtime_key(work / "synth" / "netlist.json")}
    old = fdir / "sample.json"
    if old.exists():
        om = json.loads(old.read_text())
        if {k: meta[k] for k in om} != om or not set(meta) - set(om) <= {"configs", "netlist"}:
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


def _mtime_key(p: Path) -> str:
    st = p.stat()
    return f"{st.st_size}:{int(st.st_mtime)}"


def faultsim_done(work: Path, run: Path, sample: int, seed: int, chunk: int) -> bool:
    """Every chunk of this sample is there, for these configs and this netlist."""
    fdir = run / "faultsim"
    f = fdir / "sample.json"
    if not f.exists() or not (fdir / "configs.json").exists():
        return False
    m = json.loads(f.read_text())
    if m.get("configs") != hashlib.sha1((run / "configs.json").read_bytes()).hexdigest() or \
            m.get("netlist") != _mtime_key(work / "synth" / "netlist.json") or \
            (m["seed"], m["chunk"]) != (seed, chunk) or m["n"] != (min(sample, m["of"]) if sample else m["of"]):
        return False
    return all((fdir / f"chunk_{i:04d}.csv").exists() for i in range((m["n"] + chunk - 1) // chunk))


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


def opaque_bels(spec) -> list[str]:
    """BEL and macro modules modelled as X sources: "<module> in <type> xN (why)"."""
    from fabtest import bels
    from fabtest.bels import std
    count = Counter(t for _, t in spec.tiles())
    out = []
    for typ, k in sorted(bels.classify(spec).items()):
        mods = Counter(m for _, m, pl in k["plugins"] if pl == std.OPAQUE.name)
        forced = spec.d.get("opaque", ())
        out += [f"{m} in {typ} x{count[typ] * n} ({'fabtest.toml [bels] opaque' if m in forced else k['why'] or 'no plugin for the module'})"
                for m, n in sorted(mods.items()) if count[typ]]
    for m in spec.d["top"]["macros"]:
        if m["module"] in spec.d.get("opaque", ()) or not std.macro_for(m["module"]):
            out.append(f"macro {m['module']} {m['inst']}")
    return out


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
    every = len(rows) == meta["of"]
    L = [f"fabtest report: all {len(rows)} faults, {len(cfgs)} configs" if every else
         f"fabtest report: {len(rows)} of {meta['of']} faults sampled (seed {meta['seed']}), {len(cfgs)} configs"]
    for k in sorted(tot, key=lambda k: (k != "all", -tot[k])):
        p = det[k] / tot[k]
        if every:   # every fault simulated: exact
            L.append(f"  {k:20s} {tot[k]:8d} faults:  {100 * p:6.2f}%")
            continue
        ci = 1.96 * math.sqrt(p * (1 - p) / tot[k])
        L.append(f"  {k:20s} {tot[k]:8d} sampled: {100 * p:6.2f}% +- {100 * ci:.2f}")
    left = json.loads((fdir / "configs.json").read_text()).get("left_out", []) if (fdir / "configs.json").exists() else []
    if left:
        L.append(f"  configs left out (fault-free check): {', '.join(left)}")
    op = opaque_bels(spec)
    if op:
        L.append("  opaque, outputs X and faults not tested: " + ", ".join(op))
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


def suite_passes(suite: dict, n_configs: int | None) -> list:
    """The suite's passes, cut to n_configs in all (--configs)."""
    if not n_configs:
        return suite["passes"]
    out, left = [], n_configs
    for p in suite["passes"]:
        if left <= 0:
            break
        out.append({**p, "configs": min(p["configs"], left)})
        left -= out[-1]["configs"]
    if left > 0:   # more than the suite has: the last pass routes the rest
        out[-1]["configs"] += left
    return out


def route_suite(spec, run: Path, passes: list) -> Path:
    """Every pass of a suite, each on top of the passes before it; configs.json then gets
    the recipe (passes, route caps, spec), so `fabtest run` can tell it is complete."""
    from fabtest import config
    out = run / "configs.json"
    recipe = {"passes": passes, "route": spec.cfg["route"], "spec": config.sha(spec.d)}
    if out.exists():
        d = json.loads(out.read_text())
        if d.get("recipe") == recipe:
            print(f"fabtest route: {out} is up to date")
            return out
        if d.get("recipe") is not None:
            raise SystemExit(f"fabtest run: {run} holds configs of another recipe (use another --run)")
        print(f"fabtest route: {out} is incomplete, routing again")
        out.unlink()
    ff = model(spec)
    for i, p in enumerate(passes):
        route(spec, run, p["configs"], p["chains"], extend=i > 0, ff=ff)
    d = json.loads(out.read_text())
    d["recipe"] = recipe
    out.write_text(json.dumps(d, indent=0))
    return out


def run_all(proj: Path, suite_name: str = "default", run_name: str | None = None, n_configs: int | None = None,
            sample: int | None = None, jobs: int | None = None, do_export: bool = True,
            work: Path | None = None, do_cfgtest: bool = False) -> Path:
    """fabtest run: init, synth, route (the suite), faultsim, report, export; a step whose
    inputs did not change is skipped, so a second run (or one after a crash) only does
    what is left."""
    from fabtest import config, export, project, spec as specmod, synth
    t0 = time.time()
    su = config.suite(suite_name)
    w = project.init(proj, work)
    s = specmod.load(w)
    fs = s.cfg["faultsim"]
    sample = fs["sample"] if sample is None else sample
    jobs = jobs or fs["jobs"]
    passes = suite_passes(su, n_configs)
    run = run_dir(w, run_name or (su["name"] + (f"-{n_configs}" if n_configs else "")), create=True)
    print(f"fabtest run: suite {su['name']} ({sum(p['configs'] for p in passes)} configs), sample "
          f"{sample or 'all'}, run {run}", flush=True)
    synth.build(s, w, jobs)
    route_suite(s, run, passes)
    faultsim(s, w, run, sample, fs["seed"], fs["chunk"], jobs)
    print(report(s, run), end="")
    if do_export:   # with --cfgtest also the port test
        if export.up_to_date(run, ports=do_cfgtest):
            print(f"fabtest export: {run / 'export'} is up to date")
        else:
            export.export(s, w, run, ports=do_cfgtest)
    if do_cfgtest:
        from fabtest import cfgtest
        if cfgtest.up_to_date(w, run):
            print(f"fabtest cfgtest: {run / 'cfgtest'} is up to date")
        else:
            print(cfgtest.run(s, w, run, None, jobs), end="")
    print(f"fabtest run: done in {time.time() - t0:.0f} s -> {run}")
    return run
