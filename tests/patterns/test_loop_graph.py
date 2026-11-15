#!/usr/bin/env python3
"""The incremental loop check (native and Python LoopGraph, as model/fabric.break_loops
uses them) finds the same first loop as a full rebuild (fabric.cycles), on the demo with
v3 config 0 and random mux moves.

  python3 tests/patterns/test_loop_graph.py [--moves 25]
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import SETS, demo_work  # noqa: E402


def main() -> None:
    moves = int(sys.argv[sys.argv.index("--moves") + 1]) if "--moves" in sys.argv else 25
    from fabtest import native, spec
    from fabtest.model.fabric import _deps
    from fabtest.model.generic import GenericFabric
    from fabtest.sim import graph, stimuli
    ff = GenericFabric(spec.load(demo_work()))
    sels, _, luts = stimuli.config_setup(ff, json.loads(SETS["v3"].read_text()), 0)
    for t in ff.tiles:
        for o in ff.real(t):
            sels[t].setdefault(o, 0)
    d = {n: _deps(op, a) for n, op, a in ff.ops(sels)}
    names = sorted(d)
    idx = {n: j for j, n in enumerate(names)}
    succ = [[idx[p] for p in d[n] if p in idx] for n in names]
    impl = {"python": graph.LoopGraph(succ)}
    if native.load() is not None:
        impl["native"] = native.load().LoopGraph(succ)
    muxes = [(t, o) for t in ff.tiles for o in ff.real(t)]
    rnd = random.Random(5)
    fails, loops = [], 0
    for step in range(moves + 1):
        full = ff.cycles(sels)
        want = full[0] if full else set()
        loops += bool(full)
        for name, lg in impl.items():
            got = {names[j] for j in lg.first_cycle()}
            if got != want:
                fails.append(f"step {step} {name}: {len(got)} nets, full rebuild {len(want)}")
        t, o = rnd.choice(muxes)
        m = ff.MT(t)[o]
        k = rnd.randrange(m.fan_in)
        sels[t][o] = k
        s = m.inputs[k]
        src = None if s.kind == "const" else idx.get(ff.canon(t, s.net))
        for lg in impl.values():
            lg.set_succ(idx[ff.canon(t, m.net)], [] if src is None else [src])
    for f in fails[:10]:
        print("  FAIL " + f)
    print(f"test_loop_graph: {'pass' if not fails else 'FAIL'} ({moves} moves, {loops} with a loop, "
          f"{', '.join(impl)})")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
