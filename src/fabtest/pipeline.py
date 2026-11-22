"""fabtest run / route / faultsim / report (fabtest P5).

The fabric's work dir (build/fabtest/<fabric>, fabtest/project.py) holds the intermediates
(project copy, spec, netlist, fault list). Every run gets its own directory,
results/<fabric>/<run>/:
  configs.json              the test configs: routed (route, checkpointed after every
                            config; --extend adds configs) or copied (--configs-from)
  work.json                 the work dir the run belongs to
  faultsim/sample.json      the fault sample (seed, size) and what it was simulated from
                            (configs, netlist, kernel, vectors)
  faultsim/chunk_<i>.csv    per sampled fault: status per config (D detected, P possibly,
                            T / U not); a chunk that exists is not run again
  faultsim/configs.json     configs that pass the fault-free check, and those left out
  faultsim/stale/<time>/    a fault simulation set aside: its netlist, configs or kernel
                            changed, or (--force) another sample was asked for
  report.txt                coverage, all configs and per tile type, 95% interval
  powerup.json              power-up check of a sample of the X-only faults (powerup)
  export/                   the test program (fabtest/export)
Nothing a step made is deleted without --force: results that no longer match their
inputs are moved aside, and a run that holds other configs or another sample is refused.
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
from collections import Counter, defaultdict
from pathlib import Path

from fabtest.util import atomic_open, atomic_write_json, atomic_write_text

os.environ.setdefault("FASTSIM_OSC", "local")
LIMIT_FACTOR = 200   # kernel steps per cell before a lane counts as a timeout (T)


def _runs(base: Path) -> list[str]:
    return sorted(p.name for p in base.glob("*") if (p / "configs.json").exists())


def run_dir(work: Path, name: str | None, create: bool = False) -> Path:
    """results/<fabric>/<name>; without a name a new one (create: timestamp) or the latest
    (the run whose configs changed last). A named run that does not exist is an error
    unless create."""
    from fabtest.paths import RESULTS
    base = RESULTS / work.name
    if name:
        d = base / name
        if not create and not d.exists():
            raise SystemExit(f"fabtest: no run {name} in {base} (runs: {', '.join(_runs(base)) or 'none'})")
    elif create:
        d = base / time.strftime("%Y%m%d-%H%M%S")
    else:
        runs = sorted((p for p in base.glob("*") if (p / "configs.json").exists()),
                      key=lambda p: (p / "configs.json").stat().st_mtime)
        if not runs:
            raise SystemExit(f"fabtest: no run in {base} (fabtest route, or --run NAME --configs-from JSON)")
        d = runs[-1]
    d.mkdir(parents=True, exist_ok=True)
    if not (d / "work.json").exists():
        atomic_write_json(d / "work.json", {"work": str(Path(work).resolve())})
    return d


def _rows(fdir: Path) -> list[dict]:
    """Every row of a run's fault simulation chunks (all of them with the same configs)."""
    rows, head = [], None
    for p in sorted(fdir.glob("chunk_*.csv")):
        with p.open() as fh:
            r = csv.DictReader(fh)
            if head is None:
                head = r.fieldnames
            elif r.fieldnames != head:
                raise SystemExit(f"fabtest: {p} has other configs than the run's first chunk: "
                                 "run faultsim again (--force)")
            rows += list(r)
    return rows


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
        atomic_write_text(out, json.dumps({"configs": prior + cs, "log": plog + lg, "chains": chains,
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


def _stims(spec, cfg_path: Path, only: list[int] | None = None):
    from fabtest.sim import stimuli as rf
    ff = model(spec)
    return rf.build_stims(ff, cfg_path, only=only, quiet=True)


def _inputs_meta(work: Path, run: Path, vectors: int) -> dict:
    """What a fault simulation is simulated from (sample.json): the configs, the netlist
    (content hash), the random vectors, the kernel and its mode."""
    from fabtest import synth
    from fabtest.sim.fastsim import fastsim
    return {"configs": hashlib.sha1((run / "configs.json").read_bytes()).hexdigest(),
            "netlist": synth.netlist_key(work), "vectors": vectors, "kernel": fastsim.kernel_key(),
            "osc": os.environ.get("FASTSIM_OSC", "kill"), "limit_factor": LIMIT_FACTOR}


_INPUT_KEYS = {"configs": "other configs", "of": "another fault list", "kernel": "another fault-simulation kernel",
               "osc": "another FASTSIM_OSC mode", "limit_factor": "another step limit"}


def _stale(old: dict, new: dict, work: Path) -> tuple[str, str] | None:
    """Why the chunks of a stored sample.json (old) do not belong to new: ("inputs", why)
    when what they were simulated from changed (they are wrong now), ("sample", why) when
    another sample is asked for (they are right for theirs), None when they belong. A key
    an older fabtest did not write is not compared."""
    from fabtest import synth
    if "netlist" in old and not synth.netlist_ok(work, old["netlist"]):
        return "inputs", "another netlist"
    for k, why in _INPUT_KEYS.items():
        if k in old and k in new and old[k] != new[k]:
            return "inputs", why
    if "vectors" in new and old.get("vectors", 54) != new["vectors"]:   # older runs: 54 random vectors
        return "inputs", "another [vectors] random"
    if any(k in new and old.get(k) != new[k] for k in ("seed", "n", "chunk")):
        return "sample", (f"another sample (seed {old.get('seed')}, {old.get('n')} of {old.get('of')} faults, "
                          f"chunks of {old.get('chunk')})")
    return None


def _retire(fdir: Path, why: str) -> Path | None:
    """Move a fault simulation that no longer belongs to the run (its chunks, sample.json,
    the untestable proof) to faultsim/stale/<time>/ instead of deleting it."""
    files = [p for p in fdir.iterdir() if p.is_file() and (p.name.startswith("chunk_") or p.name in (
        "sample.json", "configs.json", "untestable.csv", "untestable.json"))]
    n = sum(p.name.startswith("chunk_") and p.suffix == ".csv" for p in files)
    if not n:
        return None
    dest = fdir / "stale" / time.strftime("%Y%m%d-%H%M%S")
    k = 1
    while dest.exists():
        dest = dest.with_name(f"{dest.name.split('_')[0]}_{k}")
        k += 1
    dest.mkdir(parents=True)
    for p in files:
        p.rename(dest / p.name)
    print(f"fabtest faultsim: {n} chunks of {why} moved to {dest}", flush=True)
    return dest


def faultsim(spec, work: Path, run: Path, sample: int = 2000, seed: int = 1, chunk: int = 2000, jobs: int = 4,
             force: bool = False) -> Path:
    """Sampled fault simulation of every config that passes the fault-free check. A chunk
    that exists is not run again. Chunks of other configs, another netlist or kernel are
    moved to faultsim/stale/; a run that holds another sample is refused (force: moved
    aside as well)."""
    from fabtest.sim.fastsim import fastsim
    fdir = run / "faultsim"
    fdir.mkdir(parents=True, exist_ok=True)
    if faultsim_done(work, run, sample, seed, chunk):
        print(f"fabtest faultsim: {fdir} is up to date")
        return fdir
    with (work / "synth" / "faults.csv").open() as fh:
        faults = list(csv.DictReader(fh))
    n = min(sample, len(faults)) if sample else len(faults)
    meta = {"seed": seed, "n": n, "of": len(faults), "chunk": chunk,
            **_inputs_meta(work, run, spec.cfg["vectors"]["random"])}
    old = fdir / "sample.json"
    if old.exists():
        why = _stale(json.loads(old.read_text()), meta, work)
        if why and why[0] == "sample" and not force and any(fdir.glob("chunk_*.csv")):
            raise SystemExit(f"fabtest faultsim: {run} holds the fault simulation of {why[1]}; "
                             "use another --run for this one, or --force to move the old one aside")
        if why:
            _retire(fdir, why[1])
    atomic_write_json(old, meta)
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
    if not keep:
        raise SystemExit("fabtest faultsim: no config passes the fault-free check (see above)")
    atomic_write_json(fdir / "configs.json", {"kept": [s.name for s in keep],
                                              "left_out": [s.name for s in stims if s not in keep]})
    smp = sorted(random.Random(seed).sample(range(len(faults)), n)) if n < len(faults) else list(range(n))
    names = [s.name for s in keep]
    for i in range(0, n, chunk):
        path = fdir / f"chunk_{i // chunk:04d}.csv"
        if path.exists():
            continue
        fl = [faults[j] for j in smp[i:i + chunk]]
        t0 = time.time()
        cfgs, stat = fastsim.run_faults("fabtest", keep, fl, jobs, limit_factor=LIMIT_FACTOR, nl=nl, quiet=True)
        with atomic_open(path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["id", "path", "polarity", *names])
            for f, st in zip(fl, stat):
                w.writerow([f["id"], f["path"], f["polarity"], *[st[c] for c in names]])
        print(f"  chunk {i // chunk}: {len(fl)} faults in {time.time() - t0:.0f} s", flush=True)
    return fdir


def _same_netlist(work: Path, fdir: Path, cmd: str) -> None:
    """Fault ids of a run's fault simulation are those of the netlist it was made with:
    refuse another (newer synth: other fault list) before mapping ids to nets."""
    from fabtest import synth
    meta = json.loads((fdir / "sample.json").read_text())
    with (work / "synth" / "faults.csv").open() as fh:
        n_faults = sum(1 for _ in fh) - 1
    if not synth.netlist_ok(work, meta.get("netlist")) or meta.get("of", n_faults) != n_faults:
        raise SystemExit(f"fabtest {cmd}: the fault simulation is of another netlist "
                         "(fault ids differ): run faultsim again")


def faultsim_done(work: Path, run: Path, sample: int, seed: int, chunk: int) -> bool:
    """Every chunk of this sample is there, for these configs, this netlist, this kernel
    and this many random vectors (fabtest.toml [vectors])."""
    fdir = run / "faultsim"
    f = fdir / "sample.json"
    if not f.exists() or not (fdir / "configs.json").exists():
        return False
    m = json.loads(f.read_text())
    from fabtest import config
    want = {"seed": seed, "chunk": chunk, "n": min(sample, m["of"]) if sample else m["of"],
            **_inputs_meta(work, run, config.load(work)["vectors"]["random"])}
    if _stale(m, want, work):
        return False
    return all((fdir / f"chunk_{i:04d}.csv").exists() for i in range((m["n"] + chunk - 1) // chunk))


CLOCK_NET = re.compile(r"(?:^|\.)(?:Tile_X\d+Y\d+_)?UserCLKo?$|^(?:CLK|UserCLK)$")


def part_of(spec, path: str) -> str:
    """Tile type (or macro module, top-level wiring, or the clock network: a tile's UserCLK
    or UserCLKo segment) a fault path belongs to."""
    if CLOCK_NET.search(path):
        return "clock network"
    sup = "|".join(map(re.escape, spec.supertiles)) or "(?!)"
    m = re.match(rf"Tile_X\d+Y\d+_(?:(?:{sup})\.Tile_X\d+Y\d+_)?(\w+?)\.", path)
    if m and m.group(1) in spec.types:
        return m.group(1)
    for mi in spec.d["top"]["macros"]:
        if path.startswith(mi["inst"] + "."):
            return mi["module"]
    return "top-level wiring"


def work_of(run: Path) -> Path | None:
    """The work dir of a run (its work.json; older runs: build/fabtest/<fabric>), if it exists."""
    from fabtest.paths import REPO
    try:
        w = Path(json.loads((run / "work.json").read_text())["work"])
    except (OSError, ValueError, KeyError):
        w = REPO / "build" / "fabtest" / run.parent.name
    return w if (w / "synth" / "netlist.json").exists() else None


def _undetected(rows, cfgs) -> list[dict]:
    return [r for r in rows if not any(r[c] == "D" for c in cfgs)]


def _untestable_ids(work, run: Path, rows, cfgs) -> set[int] | None:
    """Ids proven untestable (run/faultsim/untestable.csv) when it was made for these
    undetected faults and this netlist; None otherwise."""
    f, m = run / "faultsim" / "untestable.csv", run / "faultsim" / "untestable.json"
    if not f.exists() or not m.exists():
        return None
    from fabtest import synth
    meta = json.loads(m.read_text())
    ids = sorted(int(r["id"]) for r in _undetected(rows, cfgs))
    if meta.get("ids") != hashlib.sha1(json.dumps(ids).encode()).hexdigest() or \
            (work and not synth.netlist_ok(work, meta.get("netlist"))):
        return None
    return {int(r["id"]) for r in csv.DictReader(f.open()) if r["sat"] == "U"}


_CK = None


def _check_one(item):
    st = _CK.check(item[1], item[2])
    return item[0], (st, _CK.why if st == "U" else "")


def untestable(spec, work: Path, run: Path, jobs: int = 4) -> str:
    """SAT proof per undetected fault of the run (sim/untestable.py): run/faultsim/
    untestable.csv (id, path, polarity, sat: U untestable, T testable in its tile, C not
    decided; why: dead or redundant), then the report with the coverage of the testable faults."""
    global _CK
    import multiprocessing as mp
    from concurrent.futures import ProcessPoolExecutor
    from fabtest import synth
    from fabtest.sim import untestable as ut
    from fabtest.sim.fastsim import fastsim
    fdir = run / "faultsim"
    rows = _rows(fdir)
    if not rows:
        raise SystemExit("fabtest untestable: no fault simulation results (run faultsim first)")
    _same_netlist(work, fdir, "untestable")
    cfgs = [c for c in rows[0] if c not in ("id", "path", "polarity")]
    und = _undetected(rows, cfgs)
    ids = sorted(int(r["id"]) for r in und)
    key = {"ids": hashlib.sha1(json.dumps(ids).encode()).hexdigest(), "netlist": synth.netlist_key(work)}
    done = json.loads((fdir / "untestable.json").read_text()) if (fdir / "untestable.json").exists() else {}
    if done.get("ids") == key["ids"] and synth.netlist_ok(work, done.get("netlist")) \
            and (fdir / "untestable.csv").exists():
        print(f"fabtest untestable: {fdir / 'untestable.csv'} is up to date")
        return report(spec, run)
    try:
        import pysat  # noqa: F401
    except ImportError:
        raise SystemExit("fabtest untestable: needs python-sat (pip install python-sat)")
    nets = {i: None for i in ids}
    with (work / "synth" / "faults.csv").open() as fh:
        for r in csv.DictReader(fh):
            if int(r["id"]) in nets:
                nets[int(r["id"])] = r["net"]
    t0 = time.time()
    nl = fastsim.Netlist("fabtest", work / "synth" / "netlist.json")
    _CK = ut.Checker(nl)
    items = [(int(r["id"]), nl.net_bit[nets[int(r["id"])]], int(r["polarity"])) for r in und]
    with ProcessPoolExecutor(max_workers=jobs, mp_context=mp.get_context("fork")) as ex:
        st = dict(ex.map(_check_one, items, chunksize=64))
    (fdir / "untestable.json").unlink(missing_ok=True)   # the marker goes first, comes back last
    with atomic_open(fdir / "untestable.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "path", "polarity", "sat", "why"])
        for r in und:
            w.writerow([r["id"], r["path"], r["polarity"], *st[int(r["id"])]])
    atomic_write_json(fdir / "untestable.json", key)
    c = Counter(v[0] for v in st.values())
    why = Counter(v[1] for v in st.values() if v[0] == "U")
    print(f"fabtest untestable: {len(und)} undetected faults: {c['U']} untestable ({why['dead']} dead, "
          f"{why['redundant']} redundant), {c['T']} testable in their tile, {c['C']} not decided "
          f"({time.time() - t0:.0f} s)")
    return report(spec, run)


def cover(rows: list[dict], cfgs: list[str]) -> list[tuple[str, int, int]]:
    """Greedy set cover of the detected faults: configs in order, each the one that adds the
    most faults not yet detected (ties: the earlier config); (config, new, cumulative) until
    no config adds one."""
    det = {c: {k for k, r in enumerate(rows) if r[c] == "D"} for c in cfgs}
    done, out, left = set(), [], list(cfgs)
    while left:
        c = max(left, key=lambda c: (len(det[c] - done), -cfgs.index(c)))
        new = det[c] - done
        if not new:
            break
        done |= new
        left.remove(c)
        out.append((c, len(new), len(done)))
    return out


_GR = None


def _grade_one(c):
    from fabtest.model import fabric
    ff, data = _GR
    sels = {t: dict(data["configs"][c][ff.tn(t)]) for t in ff.tiles}
    return c, [(key, sorted(v, key=str)) for key, v in fabric.grade(ff, sels).items()]


def item_sets(spec, run: Path, jobs: int = 4) -> list:
    """Per config of the run, the router items it shows (fabric.grade, the whole fabric):
    sorted int32 id arrays, cached in run/items.npz (keyed by the configs)."""
    global _GR
    import multiprocessing as mp
    import numpy as np
    cfg = run / "configs.json"
    key = hashlib.sha1(cfg.read_bytes()).hexdigest()
    cache = run / "items.npz"
    if cache.exists():
        z = np.load(cache, allow_pickle=False)
        if str(z["key"]) == key:
            return [z[f"c{i}"] for i in range(int(z["n"]))]
    data = json.loads(cfg.read_text())
    _GR = (model(spec), data)
    ids: dict = {}
    out = [None] * len(data["configs"])
    t0 = time.time()
    with mp.get_context("fork").Pool(jobs) as pool:
        for c, shown in pool.imap_unordered(_grade_one, range(len(data["configs"]))):
            out[c] = np.array(sorted(ids.setdefault((k, it), len(ids)) for k, v in shown for it in v),
                              dtype=np.int32)
    _GR = None
    with atomic_open(cache, "wb") as fh:
        np.savez(fh, key=key, n=len(out), **{f"c{i}": a for i, a in enumerate(out)})
    print(f"  graded {len(out)} configs: {len(ids)} items shown ({time.time() - t0:.0f} s)", flush=True)
    return out


def cover_items(sets: list, names: list[str]) -> list[tuple[str, int, int]]:
    """Greedy set cover over item id arrays (lazy: a config's gain only shrinks)."""
    import heapq
    import numpy as np
    n = 1 + max((int(a.max()) for a in sets if len(a)), default=0)
    done = np.zeros(n, dtype=bool)
    heap = [(-len(a), i) for i, a in enumerate(sets)]
    heapq.heapify(heap)
    out, total = [], 0
    while heap:
        g, i = heapq.heappop(heap)
        new = int((~done[sets[i]]).sum())
        if new == 0:
            continue
        if heap and new < -heap[0][0]:
            heapq.heappush(heap, (-new, i))
            continue
        done[sets[i]] = True
        total += new
        out.append((names[i], new, total))
    return out


def compact(spec, work: Path, run: Path, target_pct: float | None = None, max_configs: int | None = None,
            name: str | None = None, by: str = "faults", jobs: int = 4) -> str:
    """A run with fewer configs (greedy set cover): the fewest configs that detect target_pct %
    of what all of them detect (default 100), or the max_configs best. by="faults": over the
    run's fault simulation (a sample overfits: the configs are chosen on the faults they are
    measured on); by="items": over the router items every config shows in the whole fabric
    (fabric.grade, cached in run/items.npz), the fault simulation only measures. The new run <run>_c<k> holds them in cover order, renumbered
    FF00.., with the fault simulation columns carried over (report and export work on it
    without simulating again); compact.json the whole cover curve. The new run is never the
    run itself or another run (only an earlier compaction of this run is replaced)."""
    fdir = run / "faultsim"
    chunks = sorted(fdir.glob("chunk_*.csv"))
    rows = _rows(fdir)
    if by == "faults" and not rows:
        raise SystemExit("fabtest compact: no fault simulation results (run faultsim first, or --by items)")
    n_cfg = len(json.loads((run / "configs.json").read_text())["configs"])
    if by == "items":   # the router items of the whole fabric: no fault sample to overfit
        kept = [c for c in rows[0] if c not in ("id", "path", "polarity")] if rows else [f"FF{i:02d}" for i in range(n_cfg)]
        sets = item_sets(spec, run, jobs)
        idx = {f"FF{i:02d}": i for i in range(n_cfg)}
        cfgs = kept
        curve = cover_items([sets[idx[c]] for c in kept], kept)
    else:
        cfgs = [c for c in rows[0] if c not in ("id", "path", "polarity")]
        curve = cover(rows, cfgs)
    final = curve[-1][2] if curve else 0
    if max_configs:
        k = min(max_configs, len(curve))
    else:
        goal = final * (100.0 if target_pct is None else target_pct) / 100
        k = next((i + 1 for i, e in enumerate(curve) if e[2] >= goal - 1e-9), len(curve))
    pick = [c for c, _, _ in curve[:k]]
    data = json.loads((run / "configs.json").read_text())
    idx = {f"FF{i:02d}": i for i in range(len(data["configs"]))}
    out = run.parent / (name or f"{run.name}_c{k}")
    if out.resolve() == run.resolve():
        raise SystemExit(f"fabtest compact: --to {out.name} is the run itself: choose another name")
    if out.exists():
        prev = out / "compact.json"
        try:
            ours = json.loads(prev.read_text()).get("from") == str(run)
        except (OSError, ValueError):
            ours = False
        if not ours:
            raise SystemExit(f"fabtest compact: {out} exists and is not a compaction of {run.name}: "
                             "choose another --to")
        shutil.rmtree(out)   # an earlier compaction of this run: made again from the run
    (out / "faultsim").mkdir(parents=True)
    new = {"configs": [data["configs"][idx[c]] for c in pick],
           "log": [{"from": c, **(data.get("log") or [{}] * len(data["configs"]))[idx[c]]} for c in pick],
           "chains": data.get("chains"), "compacted_from": str(run)}
    atomic_write_text(out / "configs.json", json.dumps(new, indent=0))
    if (run / "work.json").exists():
        shutil.copy(run / "work.json", out / "work.json")
    ren = {c: f"FF{i:02d}" for i, c in enumerate(pick)}
    for p in chunks if rows else ():
        with p.open() as fh:
            rs = list(csv.DictReader(fh))
        with atomic_open(out / "faultsim" / p.name, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["id", "path", "polarity", *ren.values()])
            for r in rs:
                w.writerow([r["id"], r["path"], r["polarity"], *[r[c] for c in pick]])
    if rows:
        meta = json.loads((fdir / "sample.json").read_text())
        meta["configs"] = hashlib.sha1((out / "configs.json").read_bytes()).hexdigest()
        atomic_write_json(out / "faultsim" / "sample.json", meta)
        atomic_write_json(out / "faultsim" / "configs.json", {"kept": list(ren.values()), "left_out": []})
    marks = {m: next(i + 1 for i, e in enumerate(curve) if e[2] >= final * m / 100) for m in (90, 95, 99, 100)} if curve else {}
    atomic_write_text(out / "compact.json", json.dumps({"from": str(run), "by": by, "faults": len(rows),
                                                        "detected": final, "configs": len(cfgs), "curve": curve,
                                                        "picked": k, "configs_for_pct": marks}, indent=0))
    unit = "router items" if by == "items" else "faults"
    if by == "items":
        n = final
        L = [f"fabtest compact: {len(cfgs)} configs show {final} router items; {k} kept "
             f"({100 * curve[k - 1][2] / max(final, 1):.2f}% of them) -> {out}" if curve else "fabtest compact: nothing shown"]
        if rows:
            fk = sum(any(r[c] == "D" for c in pick) for r in rows)
            fa = sum(any(r[c] == "D" for c in cfgs) for r in rows)
            L.append(f"  faults of the run's simulation: {100 * fk / len(rows):.2f}% with them, "
                     f"{100 * fa / len(rows):.2f}% with all")
    else:
        n = len(rows)
        L = [f"fabtest compact: {len(cfgs)} configs detect {final} of {n} {unit} ({100 * final / n:.2f}%); "
             f"{k} kept ({100 * curve[k - 1][2] / n:.2f}%) -> {out}" if curve else "fabtest compact: nothing detected"]
    L += [f"  {m:3d}% of it: {c} configs" for m, c in marks.items()]
    text = "\n".join(L) + "\n"
    print(text, end="")
    return text


def pack(spec, work: Path, run: Path, n_configs: int = 8, name: str | None = None, time_limit: float = 300,
         types: list[str] | None = None, jobs: int = 4, solver: str = "greedy") -> Path:
    """n_configs more configs for the faults the run's fault simulation leaves undetected
    (fault-targeted bin packing, patterns.config.pack; types: only faults of these tile types).
    The new run <run>_p<k> holds the run's configs and the new ones; its fault simulation is
    the run's, with columns of the new configs added (only they are simulated). The new run is
    never the run itself or another run (only an earlier packing of this run is replaced)."""
    from fabtest import config
    from fabtest.patterns.config import fab_router as fr
    from fabtest.patterns.config import pack as pk
    from fabtest.sim.fastsim import fastsim
    fdir = run / "faultsim"
    chunks = sorted(fdir.glob("chunk_*.csv"))
    if not chunks:
        raise SystemExit("fabtest pack: no fault simulation results (run faultsim first)")
    _same_netlist(work, fdir, "pack")
    meta = json.loads((fdir / "sample.json").read_text())
    why = _stale(meta, _inputs_meta(work, run, config.load(work)["vectors"]["random"]), work)
    if why and why[0] == "inputs":   # the new configs' columns would be simulated from other inputs
        raise SystemExit(f"fabtest pack: the run's fault simulation is of {why[1]}: run faultsim again")
    out = run.parent / (name or f"{run.name}_p{n_configs}")
    if out.resolve() == run.resolve():
        raise SystemExit(f"fabtest pack: --to {out.name} is the run itself: choose another name")
    if out.exists():
        try:
            ours = json.loads((out / "pack.json").read_text()).get("from") == str(run)
        except (OSError, ValueError):
            ours = False
        if not ours:
            raise SystemExit(f"fabtest pack: {out} exists and is not a packing of {run.name}: choose another --to")
    t0 = time.time()
    data = json.loads((run / "configs.json").read_text())
    rows = _rows(fdir)
    cols = [c for c in rows[0] if c not in ("id", "path", "polarity")]
    with (work / "synth" / "faults.csv").open() as fh:
        faults = {f["id"]: f for f in csv.DictReader(fh)}
    und = [faults[r["id"]] for r in rows if not any(r[c] == "D" for c in cols)
           and (not types or part_of(spec, r["path"]) in types)]
    ff = model(spec)
    apply_caps(ff, spec)
    fr.CHAINS = data.get("chains") or 2
    nl = fastsim.Netlist("fabtest", work / "synth" / "netlist.json")
    print(f"fabtest pack: {len(data['configs'])} configs, {len(und)} of {len(rows)} sampled faults undetected"
          + (f" in {', '.join(types)}" if types else ""), flush=True)
    new, log, info = pk.pack(ff, data["configs"], nl, und, n_configs, time_limit, jobs, solver=solver)
    if out.exists():
        shutil.rmtree(out)   # an earlier packing of this run: made again from the run
    (out / "faultsim").mkdir(parents=True)
    if (run / "work.json").exists():
        shutil.copy(run / "work.json", out / "work.json")
    passes = data.get("passes") or [{"configs": len(data["configs"]), "chains": data.get("chains")}]
    atomic_write_text(out / "configs.json", json.dumps({
        "configs": data["configs"] + new, "log": (data.get("log") or []) + log, "chains": fr.CHAINS,
        "passes": passes + [{"configs": len(new), "chains": fr.CHAINS, "packed": True}],
        "packed_from": str(run)}, indent=0))
    # fault simulation of the new configs only, on the run's sample
    idx = list(range(len(data["configs"]), len(data["configs"]) + len(new)))
    stims = _stims(spec, out / "configs.json", only=idx)
    ffr = fastsim.fault_free(nl, stims, jobs)
    keep = [st for st in stims if ffr[st.name] is None]
    for st in stims:
        if ffr[st.name] is not None:
            print(f"  left out {st.name}: {ffr[st.name]}", flush=True)
    kept0 = json.loads((fdir / "configs.json").read_text())
    names = [st.name for st in keep]
    for p in chunks:
        with p.open() as fh:
            rs = list(csv.DictReader(fh))
        _, stat = fastsim.run_faults("fabtest", keep, [faults[r["id"]] for r in rs], jobs,
                                     limit_factor=LIMIT_FACTOR, nl=nl, quiet=True) if keep else (None, [{} for _ in rs])
        with atomic_open(out / "faultsim" / p.name, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["id", "path", "polarity", *cols, *names])
            for r, st in zip(rs, stat):
                w.writerow([r["id"], r["path"], r["polarity"], *[r[c] for c in cols], *[st[c] for c in names]])
    meta["configs"] = hashlib.sha1((out / "configs.json").read_bytes()).hexdigest()
    atomic_write_json(out / "faultsim" / "sample.json", meta)
    atomic_write_json(out / "faultsim" / "configs.json",
                      {"kept": kept0["kept"] + names, "left_out": kept0["left_out"] + [st.name for st in stims if st not in keep]})
    rows2 = _rows(out / "faultsim")
    got = sum(1 for r in rows2 if not any(r[c] == "D" for c in cols) and any(r[c] == "D" for c in names))
    cov0 = sum(any(r[c] == "D" for c in cols) for r in rows)
    info.update({"from": str(run), "configs": len(new), "detected_before": cov0, "newly_detected": got,
                 "sampled": len(rows), "seconds": round(time.time() - t0)})
    atomic_write_text(out / "pack.json", json.dumps(info, indent=0))
    print(f"fabtest pack: {len(new)} configs packed, {got} of {len(und)} undetected faults now detected; "
          f"sample {100 * cov0 / len(rows):.2f}% -> {100 * (cov0 + got) / len(rows):.2f}% "
          f"({time.time() - t0:.0f} s) -> {out}", flush=True)
    return out


def clock_nets(nl) -> set:
    """Nets of the clock network: the flops' clock pins and the buffers / inverters behind them."""
    out = {int(nl.a[i]) for i in range(nl.n_cells) if nl.type[i] == 5}
    stack = list(out)
    while stack:
        d = nl.driver.get(stack.pop())
        if d is not None and nl.type[d] in (2, 3):
            x = int(nl.a[d])
            if x not in out:
                out.add(x)
                stack.append(x)
    return out


def clockcheck(spec, work: Path, run: Path, states: int = 4, n_configs: int = 8, seed: int = 4) -> str:
    """Every fault of the clock network (clock_nets) in the run's first n_configs configs,
    each with `states` random power-up states of the fabric's flops (Tile_* storage, not the
    latches): a stuck clock freezes the flops after it, which the fault simulation can only
    call X (P). A fault detected in every state by some config counts as detected (an
    estimate: the power-up check's proof is too slow on a whole fabric). run/clock.json."""
    import numpy as np
    from fabtest.sim.fastsim import fastsim, powerup as pu_mod
    t0 = time.time()
    netlist = work / "synth" / "netlist.json"
    nl = fastsim.Netlist("fabtest", netlist)
    cn = clock_nets(nl)
    faults = [f for f in csv.DictReader((work / "synth" / "faults.csv").open()) if nl.net_bit.get(f["net"]) in cn]
    n_all = len(json.loads((run / "configs.json").read_text())["configs"])
    stims = _stims(spec, run / "configs.json", only=list(range(min(n_configs, n_all))))
    pu = pu_mod.PowerUp(nl, stims, netlist, limit_factor=200)
    flops = [k for k in range(len(nl.store)) if not nl.store_is_latch[k] and nl.store_names[k].startswith("Tile_")]
    rnd = random.Random(seed)
    st = [{k: rnd.getrandbits(1) for k in flops} for _ in range(states)]
    lanes = [(nl.net_bit[f["net"]], int(f["polarity"]), st[r]) for f in faults for r in range(states)]
    det = np.zeros(len(lanes), dtype=bool)
    for c in stims:
        for i in range(0, len(lanes), 1000):
            res, *_ = pu.run(c.name, lanes[i:i + 1000])
            det[i:i + 1000] |= (res[1:len(lanes[i:i + 1000]) + 1] & 1).astype(bool)
    every = det.reshape(-1, states).all(axis=1) if faults else np.zeros(0, dtype=bool)
    by = defaultdict(lambda: [0, 0])
    for f, d in zip(faults, every):
        typ = re.sub(r"Tile_X\d+Y\d+_", "", f["path"].split(".")[0]) if "." in f["path"] else "top"
        by[typ][0] += 1
        by[typ][1] += int(d)
    from fabtest import synth
    atomic_write_text(run / "clock.json", json.dumps({
        "faults": len(faults), "detected": int(every.sum()), "states": states, "configs": len(stims),
        "configs_sha": hashlib.sha1((run / "configs.json").read_bytes()).hexdigest(),
        "netlist": synth.netlist_key(work), "by_tile": {k: v for k, v in sorted(by.items())},
        "missed": [f["path"] + f":{f['polarity']}" for f, d in zip(faults, every) if not d]}, indent=0))
    L = [f"fabtest clockcheck: {len(faults)} clock-network faults, {int(every.sum())} detected in every one of "
         f"{states} random power-up states by {len(stims)} configs ({time.time() - t0:.0f} s)"]
    L += [f"  {k:20s} {v[1]:4d} of {v[0]}" for k, v in sorted(by.items(), key=lambda kv: -kv[1][0])]
    return "\n".join(L) + "\n"


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
    from fabtest import synth
    fdir = run / "faultsim"
    rows = _rows(fdir)
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
    unt = _untestable_ids(work_of(run), run, rows, cfgs)
    utot = Counter()
    for r in rows if unt else ():
        if int(r["id"]) in unt:
            for kk in (part_of(spec, r["path"]), "all"):
                utot[kk] += 1
    every = len(rows) == meta["of"]
    L = [f"fabtest report: all {len(rows)} faults, {len(cfgs)} configs" if every else
         f"fabtest report: {len(rows)} of {meta['of']} faults sampled (seed {meta['seed']}), {len(cfgs)} configs"]
    for k in sorted(tot, key=lambda k: (k != "all", -tot[k])):
        p = det[k] / tot[k]
        tail = ""
        if unt is not None:   # coverage of the faults not proven untestable
            tail = f"   {100 * det[k] / max(tot[k] - utot[k], 1):6.2f}% of testable ({utot[k]} untestable)"
        if every:   # every fault simulated: exact
            L.append(f"  {k:20s} {tot[k]:8d} faults:  {100 * p:6.2f}%{tail}")
            continue
        ci = 1.96 * math.sqrt(p * (1 - p) / tot[k])
        L.append(f"  {k:20s} {tot[k]:8d} sampled: {100 * p:6.2f}% +- {100 * ci:.2f}{tail}")
    if unt is not None:
        L.append("  untestable: proven by SAT on the fault's tile (fabtest untestable)")
    left = json.loads((fdir / "configs.json").read_text()).get("left_out", []) if (fdir / "configs.json").exists() else []
    if left:
        L.append(f"  configs left out (fault-free check): {', '.join(left)}")
    ck = run / "clock.json"
    if ck.exists():
        m = json.loads(ck.read_text())
        w = work_of(run)
        if m.get("configs_sha") == hashlib.sha1((run / "configs.json").read_bytes()).hexdigest() and \
                (w is None or synth.netlist_ok(w, m.get("netlist"))):
            L.append(f"  clock network: {m['detected']} of {m['faults']} faults detected in every one of "
                     f"{m['states']} random power-up states ({m['configs']} configs; fabtest clockcheck)")
    op = opaque_bels(spec)
    if op:
        L.append("  opaque, outputs X and faults not tested: " + ", ".join(op))
    text = "\n".join(L) + "\n"
    atomic_write_text(run / "report.txt", text)
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
    rows = _rows(fdir)
    if not rows:
        raise SystemExit("fabtest powerup: no fault simulation results (run faultsim first)")
    _same_netlist(work, fdir, "powerup")
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
    atomic_write_text(run / "powerup.json", json.dumps(
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


def route_suite(spec, run: Path, passes: list, force: bool = False) -> Path:
    """Every pass of a suite, each on top of the passes before it; configs.json then gets
    the recipe (passes, route caps, spec), so `fabtest run` can tell it is complete.
    route_pending.json marks routing that `fabtest run` started: configs.json without a
    recipe is then an interrupted run (routed again, the partial set kept as
    configs.partial.json). Other configs (another recipe, or made by fabtest route,
    --configs-from or compact) are refused; force: moved to configs.<time>.json."""
    from fabtest import config
    out = run / "configs.json"
    pending = run / "route_pending.json"
    recipe = {"passes": passes, "route": spec.cfg["route"], "spec": config.sha(spec.d)}
    if out.exists():
        d = json.loads(out.read_text())
        if d.get("recipe") == recipe:
            print(f"fabtest route: {out} is up to date")
            return out
        interrupted = d.get("recipe") is None and pending.exists() and json.loads(pending.read_text()) == recipe
        if interrupted:
            print(f"fabtest route: {out} is incomplete (an interrupted fabtest run), routing again; "
                  f"the partial set is kept as configs.partial.json")
            out.replace(run / "configs.partial.json")
        elif not force:
            what = ("configs of another recipe" if d.get("recipe") is not None else
                    "configs that fabtest run did not route (fabtest route, --configs-from or compact)")
            raise SystemExit(f"fabtest run: {run} holds {what}: use another --run, or --force to set them aside")
        else:
            dest = run / f"configs.{time.strftime('%Y%m%d-%H%M%S')}.json"
            out.replace(dest)
            print(f"fabtest route: the run's configs moved to {dest} (--force)")
    atomic_write_json(pending, recipe)
    ff = model(spec)
    for i, p in enumerate(passes):
        route(spec, run, p["configs"], p["chains"], extend=i > 0, ff=ff)
    d = json.loads(out.read_text())
    d["recipe"] = recipe
    atomic_write_text(out, json.dumps(d, indent=0))
    pending.unlink(missing_ok=True)
    return out


def run_all(proj: Path, suite_name: str = "default", run_name: str | None = None, n_configs: int | None = None,
            sample: int | None = None, jobs: int | None = None, do_export: bool = True,
            work: Path | None = None, do_cfgtest: bool = False, force: bool = False) -> Path:
    """fabtest run: init, synth, route (the suite), faultsim, report, export; a step whose
    inputs did not change is skipped, so a second run (or one after a crash) only does
    what is left. A run that holds other configs or another fault sample is refused
    (force: they are moved aside)."""
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
    route_suite(s, run, passes, force)
    faultsim(s, w, run, sample, fs["seed"], fs["chunk"], jobs, force)
    print(report(s, run), end="")
    if do_export:   # with --cfgtest also the port test
        if export.up_to_date(run, ports=do_cfgtest, work=w):
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
