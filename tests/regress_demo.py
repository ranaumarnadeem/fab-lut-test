#!/usr/bin/env python3
"""Regression gates on the FABulous demo fabric (fabtest plan, P0).

  python3 tests/regress_demo.py --level R0 [--record]   stimulus hashes, grade counts (seconds)
  python3 tests/regress_demo.py --level R1               fault-free = model, fault statuses (minutes)
  python3 tests/regress_demo.py --level R2 [--record]   routing of configs 0, 1 (about 25 min)

R0: for two stored configs of each of the four config sets (MULADD, RegFile, BRAM v2, BRAM v3)
on the fabric with its BlockRAMs, the SHA1 of the clocked stimulus (rows, sample flags,
expected strings) and of the model-level grade (items shown per mux code) match
tests/golden/demo_r0.json.
R1: the same configs on the gate-level netlist: the fault-free lane equals the model, and
the first 10000 faults of the 50k sample (random.Random(1) over faults/fullbram.csv) get
exactly the statuses stored in reports/fullbram_chain_sample_matrix_<set>_50k.csv.
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
import sys
import time
from pathlib import Path

os.environ.setdefault("FASTSIM_OSC", "local")
ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "configs"), str(ROOT / "sim"), str(ROOT / "sim" / "fastsim")]

SETS = {"muladd": "configs/fab_router_fullfabric_muladd.json",
        "regfile": "configs/fab_router_fullfabric.json",
        "v2": "configs/fab_router_fullbram_v2.json",
        "v3": "configs/fab_router_fullbram_v3.json"}
PICK = [0, 1]
GOLD = ROOT / "tests" / "golden"


def _sha(*parts) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update(p if isinstance(p, bytes) else str(p).encode())
    return h.hexdigest()


def r0(record: bool) -> bool:
    import fabric
    import fullfabric
    import run_fullfabric as rf
    ff = fullfabric.FullFabric(bram=True)
    inv = {ff.tn(t): t for t in ff.tiles}
    got = {}
    for name, path in SETS.items():
        data = json.loads((ROOT / path).read_text())
        stims = rf.build_stims(ff, ROOT / path, only=PICK, quiet=True)
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
    import fastsim
    import fullfabric
    import run_fullfabric as rf
    t0 = time.time()
    ff = fullfabric.FullFabric(bram=True)
    stims = {name: rf.build_stims(ff, ROOT / path, only=PICK, quiet=True) for name, path in SETS.items()}
    del ff
    faults = list(csv.DictReader((ROOT / "faults" / "fullbram.csv").open()))
    smp = sorted(random.Random(1).sample(range(len(faults)), 50000))[:10000]
    fl = [faults[j] for j in smp]
    del faults
    nl = fastsim.Netlist("fullbram")
    ok = True
    for name, sts in stims.items():
        rows = list(csv.DictReader((ROOT / "reports" / f"fullbram_chain_sample_matrix_{name}_50k.csv").open()))[:10000]
        assert [r["id"] for r in rows] == [f["id"] for f in fl], f"{name}: sample order differs"
        for st in sts:   # fault-free lane = model (raises otherwise)
            fastsim.Engine(nl, [st], W=1, limit_factor=200).run_batch([])
        cfgs, stat = fastsim.run_faults("fullbram", sts, fl, 4, limit_factor=200, nl=nl, quiet=True)
        for c in cfgs:
            bad = sum(1 for r, s in zip(rows, stat) if r[c] != s[c])
            print(f"  R1 {name} {c}: {len(stat) - bad} of {len(stat)} statuses equal", flush=True)
            ok &= bad == 0
    print(f"R1 {'pass' if ok else 'FAIL'} ({time.time() - t0:.0f} s)")
    return ok


def r2(record: bool) -> bool:
    import fab_router as fr
    import fullfabric
    ff = fullfabric.FullFabric(bram=True)
    fr.CHAINS = 2
    configs, _, total, log, _ = fr.route_all(ff, 2, do_polish=False, verbose=False,
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
    a = ap.parse_args()
    ok = {"R0": lambda: r0(a.record), "R1": r1, "R2": lambda: r2(a.record)}[a.level]()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
