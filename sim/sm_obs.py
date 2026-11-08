"""Model-level observability of the switch-matrix baseline (Phase 8 step 3).

Bit-parallel version of sm_configs.evaluate: every net is an int whose bit t
is its value at sample t of the config (16 vectors). Checked against
sm_configs.expected by `selfcheck()`.

- `net_values(c)`: every net's sample vector in config c.
- `observable(c, net)`: samples at which flipping `net` changes some tile
  output (the single-net flip propagated through the model).
- `code_tests(m)`: per code k of mux m, whether some config selects k while
  the mux output is observable at a sample with A_k = 0, and with A_k = 1;
  and per select bit i, whether some config with that code is observable at
  a sample where A_k differs from A_(k xor 2^i) (a stuck select bit shows).
"""
from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "configs"), str(ROOT / "sim")]
import sm_configs as sc  # noqa: E402

MUXES = sc.MUXES
NS = 2 * sc.W                      # samples per config
FULL = (1 << NS) - 1


def _input_vectors() -> dict[str, int]:
    seq = sc.make_seq(0)
    samples = [s for s in seq.steps if s.sample]
    return {p: sum(((s.inp >> j) & 1) << t for t, s in enumerate(samples))
            for j, p in enumerate(sc.INPUTS)}


INVEC = _input_vectors()


def _eval(sel: dict[str, int], forced: dict[str, int] | None = None) -> dict[str, int]:
    val: dict[str, int] = dict(INVEC)
    if forced:
        val.update(forced)
    srcnet, const = {}, {}
    for o, m in MUXES.items():
        s = m.inputs[sel.get(o, 0)]
        if s.kind == "const":
            const[m.net] = 0 if s.port.startswith("GND") else FULL
        else:
            srcnet[m.net] = s.net
    if sc.CELL_BITS & 0xFFFF != sc.cell.XOR_TT:
        raise NotImplementedError("bit-parallel LUT model is XOR only")
    d = sc.deps(sel)
    order, seen = [], set(val)

    def visit(n: str) -> None:   # iterative DFS post-order
        stack = [(n, iter(d.get(n, ())))]
        seen.add(n)
        while stack:
            x, it = stack[-1]
            for p in it:
                if p not in seen:
                    seen.add(p)
                    stack.append((p, iter(d.get(p, ()))))
                    break
            else:
                stack.pop()
                order.append(x)

    for o in list(sc.OUTPUTS) + sorted(d):
        if o not in seen:
            visit(o)
    for n in order:
        if n in val:
            continue
        if n in const:
            v = const[n]
        elif n in sc.PASS:
            v = val[sc.PASS[n]]
        elif n in srcnet:
            v = val[srcnet[n]]
        elif n.endswith("_O") and n[0] == "L":
            x = n[1]
            v = val[f"L{x}_I0"] ^ val[f"L{x}_I1"] ^ val[f"L{x}_I2"] ^ val[f"L{x}_I3"]   # XOR table
        elif n.endswith("_Co") and n[0] == "L":
            x = n[1]
            a, b, c = val[f"L{x}_Ci"], val[f"L{x}_I1"], val[f"L{x}_I2"]
            v = (a & b) | (a & c) | (b & c)
        elif n in ("M_AB", "M_AD", "M_AH", "M_EF"):
            a, b, s = (val[p] for p in d[n])
            v = (s & b) | (~s & FULL & a)
        else:
            raise KeyError(n)
        val[n] = v
    return val


@lru_cache(maxsize=None)
def net_values(c: int) -> dict[str, int]:
    return _eval(sc.SELECTS[c])


def outputs_of(vals: dict[str, int]) -> list[int]:
    return [vals[o] for o in sc.OUTPUTS]


@lru_cache(maxsize=None)
def observable(c: int, net: str) -> int:
    """Sample mask where flipping `net` (all samples) changes a tile output."""
    base = net_values(c)
    if net not in base:
        return 0
    flipped = _eval(sc.SELECTS[c], {net: base[net] ^ FULL})
    diff = 0
    for o in sc.OUTPUTS:
        diff |= base[o] ^ flipped[o]
    return diff


def selfcheck() -> None:
    for c in range(len(sc.SELECTS)):
        exp = sc.expected(sc.make_seq(c))
        vals = net_values(c)
        for j, o in enumerate(sc.OUTPUTS):
            got = "".join(str((vals[o] >> t) & 1) for t in range(NS))
            want = "".join(e[j] for e in exp)
            if got != want:
                raise SystemExit(f"bit-parallel model differs: config {c}, {o}")


def code_tests(o: str) -> dict:
    """Per code k: (tested at A_k = 0, tested at A_k = 1); per (k, bit i):
    select bit i distinguishable while code k is selected."""
    m = MUXES[o]
    t0, t1 = [False] * m.fan_in, [False] * m.fan_in
    selb = {}
    for c in range(len(sc.SELECTS)):
        k = sc.SELECTS[c][o]
        vals = net_values(c)
        ob = observable(c, m.net)
        if not ob:
            continue
        a = vals[m.inputs[k].net] if m.inputs[k].kind != "const" else \
            (0 if m.inputs[k].port.startswith("GND") else FULL)
        t0[k] |= bool(ob & ~a & FULL)
        t1[k] |= bool(ob & a)
        for i in range(len(m.sel)):
            k2 = k ^ (1 << i)
            if k2 >= m.fan_in:
                continue
            s2 = m.inputs[k2]
            b = vals[s2.net] if s2.kind != "const" else (0 if s2.port.startswith("GND") else FULL)
            selb[(k, i)] = selb.get((k, i), False) or bool(ob & (a ^ b))
    return {"t0": t0, "t1": t1, "sel": selb}
