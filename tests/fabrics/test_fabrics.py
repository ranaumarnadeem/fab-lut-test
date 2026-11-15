#!/usr/bin/env python3
"""Validation fabrics end to end (P7): fabtest on fabrics other than the demo.

  python3 tests/fabrics/test_fabrics.py [F1 F2] [--configs 4] [--sample 2000] [--replay 2]

Per fabric (build/fabrics/<name>, made by make.py when missing): init, synth, route,
fault-free check + sampled fault simulation, export, bit_gen byte equality and the Icarus
replay of the first configs. Scratch runs in build/tests/<name>. Slow (synth: minutes).
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


def main() -> None:
    args = sys.argv[1:]
    opt = {k: args[i + 1] for i, k in enumerate(args) if k.startswith("--")}
    names = [a for i, a in enumerate(args) if not a.startswith("--") and (i == 0 or not args[i - 1].startswith("--"))]
    names = names or ["F1", "F2"]
    n_cfg, sample, replay = int(opt.get("--configs", 4)), int(opt.get("--sample", 2000)), int(opt.get("--replay", 2))
    from fabtest import pipeline, project
    fails = []
    for name in names:
        proj = ROOT / "build" / "fabrics" / name
        if not (proj / "Fabric" / "eFPGA.v").exists():
            subprocess.run([sys.executable, str(HERE / "make.py"), name], check=True)
        w = project.init(proj)
        steps = [("synth", ["synth", w, "--jobs", "2"])]
        run_dir = TEST_RUNS / name
        if run_dir.exists():
            shutil.rmtree(run_dir)
        steps.append(("route", ["route", w, "--run", "_test", "--configs", n_cfg]))
        for step, cmd in steps:
            if run(*cmd).returncode != 0:
                fails.append(f"{name}: {step}")
        # the run lives in results/<fabric>/_test (fabtest's run layout); move it to scratch
        res = pipeline.run_dir(w, "_test")
        run_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(res), run_dir)
        from fabtest import export, spec
        s = spec.load(w)
        pipeline.faultsim(s, w, run_dir, sample=sample, chunk=sample, jobs=2)
        kept = (run_dir / "faultsim" / "configs.json").read_text()
        if '"left_out": []' not in kept:
            fails.append(f"{name}: configs fail the fault-free check: {kept}")
        rep = pipeline.report(s, run_dir)
        print(f"  {name}: " + rep.splitlines()[1].strip())
        export.export(s, w, run_dir, only=list(range(replay)))
        if not export.bitgen_check(s, w, run_dir / "configs.json", list(range(replay))):
            fails.append(f"{name}: bitgen-check")
        r = subprocess.run([str(run_dir / "export" / "tb" / "run_icarus.sh")], capture_output=True, text=True)
        if r.returncode != 0 or r.stdout.count("PASS") != replay:
            fails.append(f"{name}: Icarus replay\n{r.stdout[-1500:]}")
        print(f"  {name}: replay {r.stdout.count('PASS')} of {replay} PASS", flush=True)
    for f in fails:
        print("  FAIL " + f)
    print(f"test_fabrics: {'pass' if not fails else 'FAIL'} ({', '.join(names)}, {len(fails)} failures)")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
