#!/usr/bin/env python3
"""fabtest run on the demo (P5 gate): one command gives what the steps give one by one.

  python3 tests/cli/test_run.py [--sample 2000]

1. `fabtest run $FABTEST_DEMO --suite demo --configs 2 --sample N`: the two routed
   configs equal the routing golden (R2: selects hash, gain), every step ran, and the
   export holds both configs.
2. The same command again: every step is up to date (nothing routed or simulated).
3. `fabtest faultsim --configs-from` the run's configs: the same statuses, fault by fault.
Runs go to build/tests/run (FABTEST_RESULTS), not results/.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import DEMO_PROJECT, GOLDEN, ROOT, TEST_RUNS, demo_work  # noqa: E402

OUT = TEST_RUNS / "run"


def fabtest(*args) -> subprocess.CompletedProcess:
    env = dict(os.environ, FABTEST_RESULTS=str(OUT))
    return subprocess.run([str(ROOT / "scripts" / "fabtest"), *map(str, args)], capture_output=True, text=True, env=env)


def rows(d: Path) -> list:
    return [r for p in sorted(d.glob("chunk_*.csv")) for r in csv.DictReader(p.open())]


def main() -> None:
    sample = int(sys.argv[sys.argv.index("--sample") + 1]) if "--sample" in sys.argv else 2000
    if OUT.exists():
        shutil.rmtree(OUT)
    w = demo_work()
    run = OUT / w.name / "t_run"
    fails = []
    cmd = ["run", DEMO_PROJECT, "--suite", "demo", "--configs", 2, "--sample", sample, "--run", "t_run"]
    t0 = time.time()
    r = fabtest(*cmd)
    print(f"  first run: {time.time() - t0:.0f} s")
    if r.returncode != 0:
        print(r.stdout[-3000:] + r.stderr[-3000:])
        raise SystemExit("run FAIL: fabtest run failed")
    d = json.loads((run / "configs.json").read_text())
    gold = json.loads((GOLDEN / "demo_r2.json").read_text())
    for c, cf in enumerate(d["configs"]):
        got = {"sels": hashlib.sha1(json.dumps(cf, sort_keys=True).encode()).hexdigest(), "gain": d["log"][c]["gain"]}
        if got != gold[f"cfg{c}"]:
            fails.append(f"config {c} differs from the routing golden: {got}")
    if len(d["configs"]) != 2 or d["items"]["total"] != gold["total"]:
        fails.append(f"configs / items: {len(d['configs'])} / {d['items']['total']}")
    man = json.loads((run / "export" / "manifest.json").read_text())
    if len(man["configs"]) + len(man["left_out"]) != 2 or not (run / "report.txt").exists():
        fails.append("export / report missing")

    t0 = time.time()
    r = fabtest(*cmd)
    dt = time.time() - t0
    print(f"  second run: {dt:.0f} s")
    for step in ("synth", "route", "faultsim", "export"):
        if f"fabtest {step}: " not in r.stdout or "up to date" not in r.stdout.split(f"fabtest {step}: ", 1)[1].split("\n")[0]:
            fails.append(f"second run: {step} not up to date")

    r = fabtest("faultsim", w, "--run", "t_sep", "--configs-from", run / "configs.json", "--sample", sample)
    a, b = rows(run / "faultsim"), rows(OUT / w.name / "t_sep" / "faultsim")
    if r.returncode != 0 or a != b or len(a) != sample:
        fails.append(f"faultsim on its own: {len(b)} rows, {sum(x != y for x, y in zip(a, b))} differ")
    for f in fails:
        print("  FAIL", f)
    print(f"run {'pass' if not fails else 'FAIL'} ({len(a)} faults x {len(d['configs'])} configs)")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
