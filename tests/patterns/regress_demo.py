#!/usr/bin/env python3
"""Regression gates on the FABulous demo fabric (fabtest plan, P0).

  python3 tests/patterns/regress_demo.py --level R0 [--record]   stimulus hashes, grade counts (seconds)
  python3 tests/patterns/regress_demo.py --level R1               fault statuses (about 15 min)
  python3 tests/patterns/regress_demo.py --level R2 [--record]   routing of configs 0, 1 (about 25 min)

R0: for two stored configs of each of the four config sets (MULADD, RegFile, BRAM v2, BRAM v3)
on the fabric with its BlockRAMs, the SHA1 of the clocked stimulus (rows, sample flags,
expected strings) and of the model-level grade (items shown per mux code) match
tests/golden/demo_r0.json. The config sets: tests/golden/configs/<set>.json.
R1: the same configs on the gate-level netlist: the fault-free lane equals the model, and
the first 10000 faults of the 50k sample (random.Random(1) over the run's fault list) get
exactly the statuses of the stored run results/demo_fullbram_96cfg_50k (local: netlist/,
coverage/matrix_<set>.csv); skipped when that run is not there.
With --spec WORK, R0 / R1 build the model from the fabtest spec and R1 runs on the fabtest
netlist (WORK/synth): faults on named nets must match exactly; auto-named nets (nNNN) are
numbered per synth run, so for those only the detected count is compared (within 1%).
R2: configs 0 and 1 routed from scratch (v3 recipe: --bram, 2 chains) give the selects of
tests/golden/demo_r2.json.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import random
import re
import sys
import time
from pathlib import Path

os.environ.setdefault("FASTSIM_OSC", "local")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import ROOT  # noqa: E402,F401

from common import REF_RUN, SETS  # noqa: E402

PICK = [0, 1]
SPEC = None   # --spec: a fabtest work dir (the model from the FABulous project copy)
GOLD = ROOT / "tests" / "golden"
AUTO = re.compile(r"\.n\d+(\[\d+\])?$")   # yosys auto names


def _sha(*parts) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update(p if isinstance(p, bytes) else str(p).encode())
    return h.hexdigest()


def _fabric():
    """FullFabric of the demo with its BlockRAMs; from a fabtest spec when --spec is given."""
    from fabtest.model import fullfabric
    spec = None
    if SPEC:
        from fabtest import spec as specmod
        spec = specmod.load(Path(SPEC))
    return fullfabric.FullFabric(bram=True, spec=spec)


def r0(record: bool) -> bool:
    from fabtest.model import fabric
    from fabtest.sim import stimuli as rf
    ff = _fabric()
    inv = {ff.tn(t): t for t in ff.tiles}
    got = {}
    for name, path in SETS.items():
        data = json.loads(path.read_text())
        stims = rf.build_stims(ff, path, only=PICK, quiet=True)
        for c, st in zip(PICK, stims):
            sels = {inv[n]: s for n, s in data["configs"][c].items()}
            for t in ff.tiles:
                sels.setdefault(t, {})
            shown = fabric.grade(ff, sels)
            g = sorted((ff.tn(t), o, k, len(v)) for (t, o, k), v in shown.items() if v)
            got[f"{name}:{c}"] = {"stim": _sha(st.rows.tobytes(), st.sample.tobytes(), "\n".join(st.expected)),
                                  "grade": _sha(json.dumps(g)), "items": sum(x[3] for x in g)}
    return _compare("demo_r0.json", got, record)


def r1() -> bool:
    from fabtest.sim.fastsim import fastsim
    from fabtest.sim import stimuli as rf
    t0 = time.time()
    ff = _fabric()
    if not (REF_RUN / "netlist" / "faults.csv").exists():
        print(f"R1 skipped: {REF_RUN} not there (local results)")
        return True
    stims = {name: rf.build_stims(ff, path, only=PICK, quiet=True) for name, path in SETS.items()}
    del ff
    faults = list(csv.DictReader((REF_RUN / "netlist" / "faults.csv").open()))
    smp = sorted(random.Random(1).sample(range(len(faults)), 50000))[:10000]
    fl = [faults[j] for j in smp]
    del faults
    if SPEC:   # the fabtest netlist: same faults, looked up by (path, polarity, type, pin)
        key = lambda r: (r["path"], r["polarity"], r["type"], r["pin"])
        new = {key(r): r for r in csv.DictReader((Path(SPEC) / "synth" / "faults.csv").open())}
        fl = [dict(new[key(f)], id=f["id"]) for f in fl]
        nl = fastsim.Netlist("fullbram", Path(SPEC) / "synth" / "netlist.json")
    else:
        nl = fastsim.Netlist("fullbram", REF_RUN / "netlist" / "netlist.json")
    ok = True
    for name, sts in stims.items():
        rows = list(csv.DictReader((REF_RUN / "coverage" / f"matrix_{name}.csv").open()))[:10000]
        assert [r["id"] for r in rows] == [f["id"] for f in fl], f"{name}: sample order differs"
        for st in sts:   # fault-free lane = model (raises otherwise)
            fastsim.Engine(nl, [st], W=1, limit_factor=200).run_batch([])
        cfgs, stat = fastsim.run_faults("fullbram", sts, fl, 4, limit_factor=200, nl=nl, quiet=True)
        for c in cfgs:
            if SPEC:   # auto-named nets (nNNN) are numbered per synth run: those only in total
                named = [(r, s) for r, f, s in zip(rows, fl, stat) if not AUTO.search(f["path"])]
                bad = sum(1 for r, s in named if r[c] != s[c])
                auto = [(r, s) for r, f, s in zip(rows, fl, stat) if AUTO.search(f["path"])]
                dd = sum(s[c] == "D" for _, s in auto) - sum(r[c] == "D" for r, _ in auto)
                print(f"  R1 {name} {c}: {len(named) - bad} of {len(named)} named statuses equal; "
                      f"auto-named D {dd:+d} of {len(auto)}", flush=True)
                ok &= bad == 0 and abs(dd) <= len(auto) // 100
                continue
            bad = sum(1 for r, s in zip(rows, stat) if r[c] != s[c])
            print(f"  R1 {name} {c}: {len(stat) - bad} of {len(stat)} statuses equal", flush=True)
            ok &= bad == 0
    print(f"R1 {'pass' if ok else 'FAIL'} ({time.time() - t0:.0f} s)")
    return ok


def r2(record: bool) -> bool:
    from fabtest.patterns.config import fab_router as fr
    ff = _fabric()
    fr.CHAINS = 2
    configs, _, total, log, _ = fr.route_all(ff, 2, verbose=False,
                                             seed=lambda cfg, b: fr.backbone(cfg, b))
    got = {f"cfg{c}": {"sels": _sha(json.dumps(cf, sort_keys=True)), "gain": log[c]["gain"]}
           for c, cf in enumerate(configs)}
    got["total"] = total
    return _compare("demo_r2.json", got, record)


def _compare(fname: str, got: dict, record: bool) -> bool:
    path = GOLD / fname
    if record:
        GOLD.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n")
        print(f"recorded {path.relative_to(ROOT)} ({len(got)} entries)")
        return True
    want = json.loads(path.read_text())
    diff = [k for k in sorted(set(want) | set(got)) if want.get(k) != got.get(k)]
    for k in diff:
        print(f"  differs: {k}: golden {want.get(k)} now {got.get(k)}")
    print(f"{fname}: {'pass' if not diff else 'FAIL'} ({len(want)} entries)")
    return not diff


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--level", choices=["R0", "R1", "R2"], default="R0")
    ap.add_argument("--record", action="store_true", help="write the golden instead of comparing")
    ap.add_argument("--spec", help="fabtest work dir: build the model from its spec")
    a = ap.parse_args()
    global SPEC
    SPEC = a.spec
    ok = {"R0": lambda: r0(a.record), "R1": r1, "R2": lambda: r2(a.record)}[a.level]()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
