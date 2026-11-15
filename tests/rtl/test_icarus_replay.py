#!/usr/bin/env python3
"""RTL gate: exported test configs replay on the generated eFPGA_top RTL (Icarus Verilog).

  python3 tests/rtl/test_icarus_replay.py [--only 0,1] [--configs-from JSON]

Exports the configs into a scratch run (build/tests/rtl) and runs its tb/run_icarus.sh: the
bitstream is loaded through the ConfigFSM (SelfWriteData), the vectors applied, and every
expected output checked. About 75 s per config on the demo.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import SETS, TEST_RUNS, demo_work  # noqa: E402


def main() -> None:
    args = sys.argv[1:]
    opt = {k: args[i + 1] for i, k in enumerate(args) if k.startswith("--")}
    only = [int(v) for v in opt.get("--only", "0,1").split(",")]
    from fabtest import export, spec
    w = demo_work()
    run = TEST_RUNS / "rtl"
    if run.exists():
        shutil.rmtree(run)
    run.mkdir(parents=True)
    shutil.copy(opt.get("--configs-from", SETS["v3"]), run / "configs.json")
    export.export(spec.load(w), w, run, only=only)
    r = subprocess.run([str(run / "export" / "tb" / "run_icarus.sh")], capture_output=True, text=True)
    print(r.stdout[-3000:], r.stderr[-2000:], sep="")
    ok = r.returncode == 0 and r.stdout.count("PASS") == len(only)
    print(f"test_icarus_replay: {'pass' if ok else 'FAIL'} ({len(only)} configs)")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
