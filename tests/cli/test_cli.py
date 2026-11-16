#!/usr/bin/env python3
"""fabtest command line smoke test on the demo: every command parses, init is cached, info
lists the plugin of every BEL, and the run directory rules hold.

  python3 tests/cli/test_cli.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import ROOT, TEST_RUNS, demo_work  # noqa: E402

CMDS = ["run", "init", "info", "synth", "route", "faultsim", "report", "export", "cfgtest", "bitgen-check"]


def fabtest(*args) -> subprocess.CompletedProcess:
    return subprocess.run([str(ROOT / "scripts" / "fabtest"), *map(str, args)], capture_output=True, text=True)


def main() -> None:
    fails = []
    r = fabtest("--help")
    if r.returncode != 0 or any(c not in r.stdout for c in CMDS):
        fails.append("--help lists every command")
    for c in CMDS:
        if fabtest(c, "--help").returncode != 0:
            fails.append(f"{c} --help")
    w = demo_work()
    r = fabtest("init", "/home/user/ext/demo")
    if "up to date" not in r.stdout:
        fails.append("init: cached work dir")
    r = fabtest("info", w, "--bels")
    want = ["kind lut", "kind io", "muladd", "regfile32x4", "pass4", "blockram_1kb"]
    if r.returncode != 0 or any(x not in r.stdout for x in want) or " opaque" in r.stdout:
        fails.append("info --bels: plugins (no opaque BEL on the demo)")
    from fabtest import pipeline
    run = TEST_RUNS / "cli_run"
    (run / "configs.json").parent.mkdir(parents=True, exist_ok=True)
    (run / "configs.json").write_text('{"configs": []}')
    try:
        pipeline.use_configs(run, ROOT / "tests" / "golden" / "configs" / "v3.json")
        fails.append("use_configs: a run keeps its configs")
    except SystemExit:
        pass
    for f in fails:
        print("  FAIL " + f)
    print(f"test_cli: {'pass' if not fails else 'FAIL'} ({len(fails)} failures)")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
