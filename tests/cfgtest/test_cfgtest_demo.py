#!/usr/bin/env python3
"""fabtest cfgtest on the demo (P8 gate).

  python3 tests/cfgtest/test_cfgtest_demo.py           build + fault-free check (a few min)
  python3 tests/cfgtest/test_cfgtest_demo.py --full    also every fault (about 30 min)
  ... --record                                         write tests/golden/demo_cfgtest.json

The config bits and cfg_top.v (hashes), the fault count, the fault-free load (steps,
ports the netlist does not deliver, the sequence) and, with --full, every fault's status
(strict, power-up) equal the golden. Configs: the RegFile set, final FF00, fill FF01..FF04.
The golden is for the default Yosys.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import GOLDEN, SETS, TEST_RUNS, demo_work  # noqa: E402

GOLD = GOLDEN / "demo_cfgtest.json"
CONFIGS = [0, 1, 2, 3, 4]   # final FF00, fill FF01..FF04 of the RegFile set


def sha(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()


def main() -> None:
    full, record = "--full" in sys.argv, "--record" in sys.argv
    from fabtest import cfgtest, spec
    w = demo_work()
    s = spec.load(w)
    d = cfgtest.build(s, w)
    bits = json.loads((d / "cfg_bits.json").read_text())
    got = {"n_bits": len(bits), "bits": sha(json.dumps(bits).encode()), "cfg_top": sha((d / "cfg_top.v").read_bytes()),
           "faults": sum(1 for _ in (d / "faults.csv").open()) - 1}
    run = TEST_RUNS / "cfgtest"
    if run.exists():
        shutil.rmtree(run)
    run.mkdir(parents=True)
    shutil.copy(SETS["regfile"], run / "configs.json")
    _, _, _, pl, stims, seq, pf, ports, left = cfgtest.setup(s, w, run, CONFIGS)
    got["steps"] = int(stims[0].rows.shape[0])
    got["not_delivered"] = left
    got["sequence"] = [list(x) for x in seq]
    if full:
        print(cfgtest.run(s, w, run, CONFIGS), end="")
        rows = (run / "cfgtest" / "matrix.csv").read_text().splitlines()
        st = [r.split(",")[-5:] for r in rows[1:]]   # CFG, PINS, powerup, any, best
        got["statuses"] = sha("\n".join(",".join(x) for x in st).encode())
        got["any"] = dict(sorted(Counter(x[-2] for x in st).items()))
        got["best"] = dict(sorted(Counter(x[-1] for x in st).items()))
    gold = json.loads(GOLD.read_text()) if GOLD.exists() else {}
    if record:
        gold.update(got)
        GOLD.write_text(json.dumps(gold, indent=1, sort_keys=True) + "\n")
        print(f"cfgtest recorded -> {GOLD}")
        return
    bad = {k: (v, gold.get(k)) for k, v in got.items() if gold.get(k) != v}
    for k, (v, g) in bad.items():
        print(f"  {k}: {v} (golden {g})")
    print(f"cfgtest {'pass' if not bad else 'FAIL'} ({len(got)} checks{', every fault' if full else ''})")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
