#!/usr/bin/env python3
"""All fabtest tests on the demo fabric.

  python3 tests/run_all.py            quick: spec, R0, model (2 configs per set), CLI, export
                                      + bit_gen (2 configs); about 10 min
  python3 tests/run_all.py --full     also R1 (stored 50k-fault run, local), model on all 96
                                      configs, Icarus replay (2 configs), R2 routing; about 1 h
  python3 tests/run_all.py NAME ...   only these (names below)
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

T = Path(__file__).resolve().parent
QUICK = {
    "spec": [T / "model" / "test_spec_demo.py"],
    "r0": [T / "patterns" / "regress_demo.py", "--level", "R0"],
    "model": [T / "model" / "test_model_demo.py", "--quick"],
    "cli": [T / "cli" / "test_cli.py"],
    "loops": [T / "patterns" / "test_loop_graph.py", "--moves", "10"],
    "export": [T / "export" / "test_export_demo.py"],
}
FULL = {
    "r1": [T / "patterns" / "regress_demo.py", "--level", "R1"],
    "model_full": [T / "model" / "test_model_demo.py"],
    "rtl": [T / "rtl" / "test_icarus_replay.py"],
    "r2": [T / "patterns" / "regress_demo.py", "--level", "R2"],
}


def main() -> None:
    args = sys.argv[1:]
    tests = dict(QUICK, **FULL) if "--full" in args else dict(QUICK)
    names = [a for a in args if not a.startswith("--")]
    if names:
        allt = dict(QUICK, **FULL)
        tests = {n: allt[n] for n in names}
    res = {}
    for n, cmd in tests.items():
        t0 = time.time()
        r = subprocess.run([sys.executable, *map(str, cmd)], capture_output=True, text=True)
        last = (r.stdout.strip().splitlines() or [""])[-1]
        res[n] = r.returncode == 0
        print(f"{'PASS' if res[n] else 'FAIL'} {n:10s} {time.time() - t0:6.0f} s  {last}", flush=True)
        if not res[n]:
            print((r.stdout + r.stderr)[-3000:])
    print(f"run_all: {sum(res.values())} of {len(res)} pass")
    sys.exit(0 if all(res.values()) else 1)


if __name__ == "__main__":
    main()
