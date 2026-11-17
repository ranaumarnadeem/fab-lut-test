#!/usr/bin/env python3
"""SAT untestable check (sim/untestable.py) on small hand-built netlists with known answers.

  python3 tests/sat/test_untestable.py

Tile T: a mux of a and b to a pad, an AND with a constant 0 (s-a-0 redundant, s-a-1
testable), an inverter nobody reads (dead), a flop (D = a & b; observed through its next
state), a combinational loop, a clock net (not decided). Tile N reads one T output: the
window takes both tiles.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from fabtest.sim import untestable as ut  # noqa: E402

AND, OR, NOT, BUF, LATCH, DFF = 0, 1, 2, 3, 4, 5


class Net:
    """The Netlist fields the checker reads."""

    def __init__(self, cells, ports, consts):
        self.cell_names = [c[0] for c in cells]
        ty = [c[1] for c in cells]
        pins = [c[2] + [-1] * (3 - len(c[2])) for c in cells]
        self.type = np.array(ty, dtype=np.int32)
        self.a, self.b, self.c = (np.array([p[k] for p in pins], dtype=np.int32) for k in range(3))
        self.out = np.array([c[3] for c in cells], dtype=np.int32)
        self.n_nets = 1 + max([*self.out, *[x for p in pins for x in p], *ports.values(), *consts])
        self.driver = {int(o): i for i, o in enumerate(self.out)}
        rd = [[] for _ in range(self.n_nets)]
        for i, p in enumerate(pins):
            for x in p:
                if x >= 0:
                    rd[x].append(i)
        self.rd_off = np.zeros(self.n_nets + 1, dtype=np.int32)
        self.rd_off[1:] = np.cumsum([len(r) for r in rd])
        self.rd_idx = np.array([i for r in rd for i in r], dtype=np.int32)
        self.port_bits = ports
        self.const_net = np.array(list(consts), dtype=np.int32)
        self.const_val = np.array(list(consts.values()), dtype=np.int32)
        self.n_const = len(consts)


def main() -> None:
    # nets: 0 a, 1 b, 2 s (config bit), 3 clk, 4 en, 5 GND
    # 6 ~s, 7 a&s, 8 b&~s, 9 mux -> pad o1
    # 10 a&GND -> pad o2 (s-a-0 redundant), 11 ~a (dead)
    # 12 a&b -> flop D, 13 flop Q -> pad o3
    # 14 loop: 14 = 15 | a, 15 = buf 14 -> pad o4 through tile N: 16 = ~14 in N -> pad o5
    cells = [
        ("T.inv_s", NOT, [2], 6), ("T.and1", AND, [0, 2], 7), ("T.and2", AND, [1, 6], 8),
        ("T.or", OR, [7, 8], 9), ("T.gnd", AND, [0, 5], 10), ("T.dead", NOT, [0], 11),
        ("T.and3", AND, [0, 1], 12), ("T.ff", DFF, [3, 4, 12], 13),
        ("T.loop", OR, [15, 0], 14), ("T.buf", BUF, [14], 15), ("N.inv", NOT, [14], 16),
        ("T.cfg", LATCH, [4, 0], 2),
    ]
    ports = {"a": 0, "b": 1, "clk": 3, "en": 4, "o1": 9, "o2": 10, "o3": 13, "o5": 16}
    nl = Net(cells, ports, {5: 0})
    ck = ut.Checker(nl)
    want = [((9, 0), "T", ""), ((9, 1), "T", ""), ((6, 0), "T", ""),
            ((10, 0), "U", "redundant"), ((10, 1), "T", ""),
            ((11, 0), "U", "dead"), ((11, 1), "U", "dead"),
            ((12, 0), "T", ""), ((13, 1), "T", ""),
            ((14, 0), "T", ""), ((15, 1), "T", ""),
            ((3, 0), "C", ""), ((2, 1), "T", "")]
    fails = []
    for (n, p), st, why in want:
        got = ck.check(n, p)
        gw = ck.why if got == "U" else ""
        if (got, gw) != (st, why):
            fails.append(f"net {n} s-a-{p}: {got} {gw} (want {st} {why})")
    for f in fails:
        print("  FAIL", f)
    print(f"test_untestable: {'pass' if not fails else 'FAIL'} ({len(want)} faults, {len(fails)} failures)")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
