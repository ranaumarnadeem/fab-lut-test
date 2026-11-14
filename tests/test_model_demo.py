#!/usr/bin/env python3
"""fabtest P3 gate: GenericFabric (spec + BEL plugins) = FullFabric on the demo.

  python3 tests/test_model_demo.py [<work dir>] [--quick]

Checks the plugin-derived parts (BEL ops per tile, RegFile pins, BlockRAM pins, tainted
nets), then builds the stimulus of every stored config of the four sets (R0 sets; --quick:
configs 0, 1) with both models and compares rows, sample flags and expected strings.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "configs"), str(ROOT / "sim"), str(ROOT / "tests")]


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    quick = "--quick" in sys.argv
    from fabtest import project, spec as specmod
    from fabtest.model import GenericFabric
    import fullfabric
    import run_fullfabric as rf
    from regress_demo import SETS
    w = Path(args[0]) if args else project.init(Path("/home/user/ext/demo"))
    s = specmod.load(w)
    ff = fullfabric.FullFabric(bram=True, spec=s)
    gf = GenericFabric(s)
    fails = []

    def check(name, got, want):
        if got != want:
            fails.append(name)
            print(f"  FAIL {name}", flush=True)
    check("tiles", gf.tiles, ff.tiles)
    for t in ff.tiles:
        check(f"bel_ops {ff.tn(t)}", gf.bel_ops(t), ff.bel_ops(t))
    check("rf_pins", gf.rf_pins, ff.rf_pins)
    check("bram_pins", gf.bram_pins, ff.bram_pins)
    check("tainted", gf.tainted, ff.tainted)
    print("  kinds: " + ", ".join(f"{t}={k['kind']}" for t, k in sorted(gf.kinds.items())))
    print(f"  macros: {gf.macro_status}")
    for typ, rows in gf.unmodelled.items():
        print(f"  unmodelled in {typ}: " + ", ".join(f"{i} ({p}, {n} outputs)" for i, p, n in rows))
    n = 0
    for name, path in SETS.items():
        cfgs = json.loads((ROOT / path).read_text())["configs"]
        only = [0, 1] if quick else list(range(len(cfgs)))
        a = rf.build_stims(ff, ROOT / path, only=only, quiet=True)
        b = rf.build_stims(gf, ROOT / path, only=only, quiet=True)
        for c, x, y in zip(only, a, b):
            h = [hashlib.sha1(st.rows.tobytes() + st.sample.tobytes() + "\n".join(st.expected).encode()).hexdigest()
                 for st in (x, y)]
            check(f"stim {name}:{c}", h[1], h[0])
            n += 1
        print(f"  {name}: {len(only)} configs compared", flush=True)
    print(f"test_model_demo: {'pass' if not fails else 'FAIL'} ({n} configs, {len(fails)} differences)")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
