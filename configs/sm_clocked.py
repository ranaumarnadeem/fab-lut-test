#!/usr/bin/env python3
"""Clocked switch-matrix configs: LUT SR / EN muxes and flop logic (Phase 9 step 2).

The 16 LUT SR / EN 2:1 muxes (GND0 / J_SR_BEG[0], VCC0 / J_EN_BEG[0]) and
the 8:1 muxes J_SR_BEG0 / J_EN_BEG0 reach a tile output only through the
LUT flops, so a combinational config cannot test them. Here every LUT is
registered (FF = 1, O = flop) and the stimulus clocks UserCLK once per
vector.

Config c = 0 .. 7 (8 = fan-in of J_SR_BEG0 / J_EN_BEG0):
  - base: routed config RT0c (configs/sm_router.json), so the rest of the
    switch matrix stays loop free and mostly routed;
  - J_SR_BEG0 = J_EN_BEG0 = c; the jump wire each selects is driven from a
    tile input (its mux moved to a tile-input code), so SR / EN toggle with
    the stimulus;
  - LUT x (index i): SR / EN mux codes (sr, en) = bits of (i + c) mod 4, reset
    value (i + c div 4) mod 2: over the 8 configs every LUT sees every (sr, en)
    pair with both reset values;
  - every LUT output routed straight to a tile output (a tile-output mux
    reading L<x>_O directly);
  - LUTs: XOR table, I0 from I[0], FF = 1.
Stimulus: the 2 W unique-code vectors, then N_RAND random vectors; per
vector: inputs, UserCLK 1, UserCLK 0, sample. Samples start once every
flop holds a defined value in the model (power-up X until the first load).

Model: 3-valued, per step; the switch matrix and BELs as in
configs/sm_configs.py with L<x>_O = flop, flops updated on the UserCLK
rising edge (EN = 0: hold; EN = 1: SR ? reset value : LUT out).
"""
from __future__ import annotations

import itertools
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "configs")]
import sm_configs as sc  # noqa: E402

M = sc.MUXES
N_CK = 8
N_RAND = 48
X = 2
LUTS = sc.LUTS
ROUTER = json.loads((ROOT / "configs" / "sm_router.json").read_text())["configs"]
TT = sc.cell.XOR_TT


def name_of(c: int) -> str:
    return f"CK{c:02d}"


def lut_plan(c: int) -> dict[str, tuple[int, int, int]]:
    """LUT -> (SR code, EN code, reset value) in config c."""
    out = {}
    for i, x in enumerate(LUTS):
        k = (i + c) % 4
        out[x] = (k & 1, k >> 1, (i + c // 4) % 2)
    return out


def _tile_input_code(o: str, avoid: set[str]) -> int | None:
    for k, s in enumerate(M[o].inputs):
        if s.kind == "tile_input" and s.net not in avoid:
            return k
    return None


def selects(c: int) -> dict[str, int]:
    sel = dict(ROUTER[c % len(ROUTER)])
    used: set[str] = set()
    for jm in ("J_SR_BEG0", "J_EN_BEG0"):
        sel[jm] = c
        src = M[jm].inputs[c]
        o = next(q for q, m in M.items() if m.net == src.net)
        k = _tile_input_code(o, used)
        if k is None:
            raise SystemExit(f"{jm} input {c} ({src.net}): no tile-input code on {o}")
        sel[o] = k
        used.add(M[o].inputs[k].net)
    for x, (sr, en, _rv) in lut_plan(c).items():
        sel[f"L{x}_SR"], sel[f"L{x}_EN"] = sr, en
    # observe every flop: a tile-output mux reading L<x>_O directly, distinct per LUT
    taken = set()
    for x in LUTS:
        cand = sorted((o, k) for o, m in M.items() if m.sink == "tile_output" and m.model != "direct"
                      for k, s in enumerate(m.inputs) if s.net == f"L{x}_O" and o not in taken)
        o, k = cand[(c * 3) % len(cand)]
        sel[o] = k
        taken.add(o)
    if sc.cycles(sel):
        raise SystemExit(f"{name_of(c)}: loop after the SR / EN changes")
    return sel


def lut_bits(c: int) -> dict[str, int]:
    return {x: sc.cell.make_bits(TT, ff=1, i0_from_ci=0, reset_value=rv) for x, (_, _, rv) in lut_plan(c).items()}


# ---------------------------------------------------------------- 3-valued model

def _f3(fn, args):
    """Function of 0 / 1 / X args: X unless every completion gives the same value."""
    xs = [i for i, a in enumerate(args) if a == X]
    if not xs:
        return fn(*args)
    vals = set()
    for comb in itertools.product((0, 1), repeat=len(xs)):
        a = list(args)
        for i, v in zip(xs, comb):
            a[i] = v
        vals.add(fn(*a))
        if len(vals) > 1:
            return X
    return vals.pop()


def _lut(i0, i1, i2, i3):
    return (TT >> (i0 | i1 << 1 | i2 << 2 | i3 << 3)) & 1


def _maj(a, b, c_):
    return (a & b) | (a & c_) | (b & c_)


def _mux(a, b, s):
    return b if s else a


def compile_ops(sel: dict[str, int]):
    d = sc.deps(sel)
    for x in LUTS:
        d[f"L{x}_O"] = []          # registered: O is the flop
    order, seen = [], set(sc.INPUTS)

    def visit(n):
        stack = [(n, iter(d.get(n, ())))]
        seen.add(n)
        while stack:
            y, it = stack[-1]
            for p in it:
                if p not in seen:
                    seen.add(p)
                    stack.append((p, iter(d.get(p, ()))))
                    break
            else:
                stack.pop()
                order.append(y)

    for n in sorted(set(d) | {f"L{x}_I{j}" for x in LUTS for j in range(4)}
                    | {f"L{x}_{p}" for x in LUTS for p in ("SR", "EN")}):
        if n not in seen:
            visit(n)
    srcnet = {}
    for o, m in M.items():
        s = m.inputs[sel.get(o, 0)] if m.model != "direct" else m.inputs[0]
        srcnet[m.net] = s
    ops = []
    for n in order:
        if n in sc.INPUTS:
            continue
        if n in srcnet:
            s = srcnet[n]
            ops.append((n, "c", 0 if s.port.startswith("GND") else 1) if s.kind == "const" else (n, "b", s.net))
        elif n in sc.PASS:
            ops.append((n, "b", sc.PASS[n]))
        elif n.endswith("_O") and n[0] == "L":
            ops.append((n, "q", n[1]))
        elif n.endswith("_Co"):
            ops.append((n, "m", d[n]))
        elif n.startswith("M_"):
            ops.append((n, "s", d[n]))
        else:
            raise KeyError(n)
    return ops


def comb(ops, inputs: dict[str, int], q: dict[str, int]) -> dict[str, int]:
    v = dict(inputs)
    for n, op, a in ops:
        if op == "b":
            v[n] = v[a]
        elif op == "c":
            v[n] = a
        elif op == "q":
            v[n] = q[a]
        elif op == "m":
            v[n] = _f3(_maj, [v[p] for p in a])
        else:
            x0, x1, s = (v[p] for p in a)
            v[n] = _f3(_mux, [x0, x1, s])
    return v


def clock(v: dict[str, int], q: dict[str, int], plan) -> dict[str, int]:
    """Flop states after a UserCLK rising edge (v: net values just before it)."""
    nq = {}
    for x in LUTS:
        rv = plan[x][2]
        d = _f3(_lut, [v[f"L{x}_I{j}"] for j in range(4)])
        en, sr = v[f"L{x}_EN"], v[f"L{x}_SR"]
        load = rv if sr == 1 else d if sr == 0 else (rv if rv == d else X)
        nq[x] = q[x] if en == 0 else load if en == 1 else (q[x] if q[x] == load else X)
    return nq


def model(seq) -> list[str]:
    ops = compile_ops(seq.selects[0])
    q = {x: X for x in LUTS}
    out, clk = [], 0
    for s in seq.steps:
        inputs = {p: (s.inp >> j) & 1 for j, p in enumerate(sc.INPUTS)}
        if s.clk and not clk:
            q = clock(comb(ops, inputs, q), q, seq.lut_plan)
        clk = s.clk
        if s.sample:
            v = comb(ops, inputs, q)
            out.append("".join(str(v[o]) if v[o] != X else "x" for o in sc.OUTPUTS))
    return out


# ---------------------------------------------------------------- sequences

def make_seq(c: int):
    s = sc.SMSeq(name_of(c), f"clocked SR / EN config {c}: J_SR / J_EN on input {c}, LUTs registered")
    s.lut_plan = lut_plan(c)
    s.model = model
    s.write_config(selects(c), sc.frame_order(c), lut_bits(c))
    fd = s.steps[-1].fd
    codes = {p: j + 1 for j, p in enumerate(sc.INPUTS)}
    vecs = [sum(((codes[p] >> b) & 1) << j for j, p in enumerate(sc.INPUTS)) for b in range(sc.W)]
    full = (1 << len(sc.INPUTS)) - 1
    vecs += [v ^ full for v in vecs]
    rnd = random.Random(1000 + c)
    vecs += [rnd.getrandbits(len(sc.INPUTS)) for _ in range(N_RAND)]
    # the model decides when every flop is defined; sample from then on
    ops = compile_ops(s.selects[0])
    q = {x: X for x in LUTS}
    for t, v in enumerate(vecs):
        inputs = {p: (v >> j) & 1 for j, p in enumerate(sc.INPUTS)}
        s.steps.append(sc.SStep(fd=fd, inp=v, note=f"vector {t}"))
        s.steps.append(sc.SStep(fd=fd, inp=v, clk=1, note=f"vector {t} clock"))
        q = clock(comb(ops, inputs, q), q, s.lut_plan)
        ready = all(val != X for val in q.values())
        s.steps.append(sc.SStep(fd=fd, inp=v, sample=int(ready), note=f"vector {t} sample"))
        if ready:
            s.sample_sel.append(0)
    s.seg_ends.append(len(s.steps) - 1)
    return s


CONFIGS = {name_of(c): (lambda c=c: make_seq(c)) for c in range(N_CK)}


def main() -> None:
    for c in range(N_CK):
        s = make_seq(c)
        exp = model(s)
        n = sum(st.sample for st in s.steps)
        print(f"{name_of(c)}: {len(s.steps)} steps, {n} samples, plan "
              + " ".join(f"{x}:{sr}{en}{rv}" for x, (sr, en, rv) in s.lut_plan.items())
              + f", X in expected: {sum(e.count('x') for e in exp)}")


if __name__ == "__main__":
    main()
