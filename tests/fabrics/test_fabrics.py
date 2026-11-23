#!/usr/bin/env python3
"""Validation fabrics end to end (P7): fabtest on fabrics other than the demo.

  python3 tests/fabrics/test_fabrics.py [F1 F2 F3 F4] [--configs 4] [--sample 2000] [--replay 2]

Per fabric (build/fabrics/<name>, made by make.py when missing): init, synth, route,
fault-free check + sampled fault simulation, export (with the port test), bit_gen byte
equality and the Icarus replay of the first configs and the port test. Scratch runs in
build/tests/<name>. Slow (synth: minutes). OPAQUE: BELs a fabric's report must name as
opaque (F3: the ALU4 BEL no plugin knows); every other fabric has none. SAT soundness:
SAT_SAMPLE detected faults per fabric, none proven untestable. compact: 2 configs, the
report of the new run equals the cover. pack: 1 config more, up to date and no worse; with
carry / EN-SR targets, 1 config that passes the fault-free check.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from common import ROOT, TEST_RUNS  # noqa: E402


def run(*cmd) -> subprocess.CompletedProcess:
    r = subprocess.run([str(ROOT / "scripts" / "fabtest"), *map(str, cmd)], capture_output=True, text=True)
    if r.returncode != 0:
        print((r.stdout + r.stderr)[-2500:])
    return r


ALL = ["F1", "F2", "F3", "F4"]
SAT_SAMPLE = 200


def compact_ok(s, w: Path, run_dir: Path) -> str:
    """fabtest compact to 2 configs: the new run reports the cover's coverage without
    simulating again."""
    import json
    from fabtest import pipeline
    pipeline.compact(s, w, run_dir, max_configs=2, name=run_dir.name + "_c")
    c = run_dir.parent / (run_dir.name + "_c")
    m = json.loads((c / "compact.json").read_text())
    want = 100 * m["curve"][m["picked"] - 1][2] / m["faults"]
    got = pipeline.report(s, c).splitlines()[1]
    if f"{want:6.2f}%" not in got or not pipeline.faultsim_done(w, c, m["faults"], 1, m["faults"]):
        return f"report {got.strip()} (cover {want:.2f}%)"
    return ""


def pack_ok(s, w: Path, run_dir: Path) -> str:
    """fabtest pack, 1 config: the new run has it, its fault simulation is up to date (only
    the new config simulated) and detects at least what the run did, with routes packed."""
    import json
    from fabtest import pipeline
    out = pipeline.pack(s, w, run_dir, 1, name=run_dir.name + "_p", time_limit=20, jobs=2)
    m = json.loads((out / "pack.json").read_text())
    n = len(json.loads((run_dir / "configs.json").read_text())["configs"])
    meta = json.loads((out / "faultsim" / "sample.json").read_text())
    if len(json.loads((out / "configs.json").read_text())["configs"]) != n + 1:
        return "no config added"
    if not pipeline.faultsim_done(w, out, meta["n"], meta["seed"], meta["chunk"]):
        return "fault simulation of the new run not up to date"
    if m["newly_detected"] < 0 or json.loads((out / "configs.json").read_text())["log"][-1]["backbone"]["packed"] == 0 and m["mapped"]:
        return f"nothing packed ({m})"
    # carry / EN-SR targets: the new config passes the fault-free check (carry-mode LUTs and
    # freed EN / SR modelled as the gates run them)
    out = pipeline.pack(s, w, run_dir, 1, name=run_dir.name + "_pk", time_limit=20, jobs=2,
                        kinds=("mux", "carry", "ensr"))
    if json.loads((out / "faultsim" / "configs.json").read_text())["left_out"]:
        return f"carry / EN-SR config fails the fault-free check ({json.loads((out / 'pack.json').read_text())})"
    return ""


def sat_sound(w: Path, run_dir: Path) -> list[str]:
    """SAT untestable check (sim/untestable.py) on detected faults of the sample: none may come
    out untestable (a detected fault has a test)."""
    import csv
    import random
    from fabtest.sim import untestable
    from fabtest.sim.fastsim import fastsim
    rows = [r for p in sorted((run_dir / "faultsim").glob("chunk_*.csv")) for r in csv.DictReader(p.open())]
    cfgs = [c for c in rows[0] if c not in ("id", "path", "polarity")]
    det = [r for r in rows if any(r[c] == "D" for c in cfgs)]
    det = random.Random(3).sample(det, min(SAT_SAMPLE, len(det)))
    nets = {int(r["id"]): r["net"] for r in csv.DictReader((w / "synth" / "faults.csv").open())}
    nl = fastsim.Netlist("fabtest", w / "synth" / "netlist.json")
    ck = untestable.Checker(nl)
    bad = [r["path"] for r in det if ck.check(nl.net_bit[nets[int(r["id"])]], int(r["polarity"])) == "U"]
    print(f"  SAT on {len(det)} detected faults: {len(bad)} untestable", flush=True)
    return bad
OPAQUE = {"F3": ["ALU4 in CUST x3 (no plugin for the module)"]}


def main() -> None:
    args = sys.argv[1:]
    opt = {k: args[i + 1] for i, k in enumerate(args) if k.startswith("--")}
    names = [a for i, a in enumerate(args) if not a.startswith("--") and (i == 0 or not args[i - 1].startswith("--"))]
    names = names or ALL
    n_cfg, sample, replay = int(opt.get("--configs", 4)), int(opt.get("--sample", 2000)), int(opt.get("--replay", 2))
    from fabtest import pipeline, project
    fails = []
    for name in names:
        proj = ROOT / "build" / "fabrics" / name
        if not (proj / "Fabric" / "eFPGA.v").exists():
            subprocess.run([sys.executable, str(HERE / "make.py"), name], check=True)
        w = project.init(proj)
        if run("synth", w, "--jobs", "2").returncode != 0:
            fails.append(f"{name}: synth")
            continue
        run_dir = TEST_RUNS / name
        if run_dir.exists():
            shutil.rmtree(run_dir)
        run_dir.mkdir(parents=True)
        from fabtest import export, spec
        s = spec.load(w)
        pipeline.route(s, run_dir, n_cfg, 2)
        pipeline.faultsim(s, w, run_dir, sample=sample, chunk=sample, jobs=2)
        kept = (run_dir / "faultsim" / "configs.json").read_text()
        if '"left_out": []' not in kept:
            fails.append(f"{name}: configs fail the fault-free check: {kept}")
        rep = pipeline.report(s, run_dir)
        print(f"  {name}: " + rep.splitlines()[1].strip())
        if pipeline.opaque_bels(s) != OPAQUE.get(name, []):
            fails.append(f"{name}: opaque BELs {pipeline.opaque_bels(s)}, want {OPAQUE.get(name, [])}")
        bad = compact_ok(s, w, run_dir)
        if bad:
            fails.append(f"{name}: compact: {bad}")
        bad = pack_ok(s, w, run_dir)
        if bad:
            fails.append(f"{name}: pack: {bad}")
        bad = sat_sound(w, run_dir)
        if bad:
            fails.append(f"{name}: SAT calls detected faults untestable: {bad[:3]}")
        export.export(s, w, run_dir, only=list(range(replay)), ports=True)   # with the port test
        if not export.bitgen_check(s, w, run_dir / "configs.json", list(range(replay))):
            fails.append(f"{name}: bitgen-check")
        r = subprocess.run([str(run_dir / "export" / "tb" / "run_icarus.sh")], capture_output=True, text=True)
        if r.returncode != 0 or r.stdout.count("PASS") != replay + 1 or "ports + " not in r.stdout:
            fails.append(f"{name}: Icarus replay\n{r.stdout[-1500:]}")
        print(f"  {name}: replay {r.stdout.count('PASS')} of {replay + 1} PASS (configs, port test)", flush=True)
    for f in fails:
        print("  FAIL " + f)
    print(f"test_fabrics: {'pass' if not fails else 'FAIL'} ({', '.join(names)}, {len(fails)} failures)")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
