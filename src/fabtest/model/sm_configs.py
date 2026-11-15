#!/usr/bin/env python3
"""LUT4AB tile config words: switch-matrix selects and LUT bits -> config bits -> frame words.

config_word / frame_words: a tile's selects (sm_model.MUXES encoding) and LUT functions as
its config bits and its per-frame FrameData words. frame_order(c): all frames written,
rotated so that config c ends on LAST_FRAME[c] (distinct per config: a stuck-at-1
FrameStrobe fault then shows in some config). lut_deps / mux8_deps / deps: the tile's BEL
edges; cycles / break_loops: combinational loop check and repair of a set of selects.
INPUTS / OUTPUTS: the tile ports in a fixed order; PASS: the tile's pass-through wires.
"""
from __future__ import annotations

import math
import re

from fabtest.paths import REPO as ROOT  # noqa: E402
from fabtest.model import configs as cell  # noqa: E402
from fabtest.model import sm_model  # noqa: E402

TILE = sm_model.TILE
MUXES = sm_model.MUXES
N_FRAMES = 1 + max(f for f, _ in TILE.configmem.values())
LUTS = "ABCDEFGH"
LUT_LO = {x: TILE.bels[f"Inst_L{x}_LUT4c_frame_config_dffesr"][0] for x in LUTS}
CELL_BITS = cell.make_bits(cell.XOR_TT, ff=0, i0_from_ci=0, reset_value=0)

def _port_key(n: str) -> tuple[str, int]:
    """(port, bit index): a total order, independent of set iteration (hash seed)."""
    m = re.fullmatch(r"(\w+)\[(\d+)\]", n)
    return (m.group(1), int(m.group(2))) if m else (n, -1)


INPUTS = sorted(TILE.tile_inputs, key=_port_key)
OUTPUTS = sorted(TILE.tile_outputs, key=_port_key)
if len({_port_key(n) for n in INPUTS}) != len(INPUTS) or len({_port_key(n) for n in OUTPUTS}) != len(OUTPUTS):
    raise SystemExit("port order: two ports share a key")
W = math.ceil(math.log2(len(INPUTS) + 2))
F = max(m.fan_in for m in MUXES.values())
# Last frame written per config: distinct for all configs (0a rule) while
# there are at most N_FRAMES configs.
LAST_FRAME = {c: (N_FRAMES - 1 - c) % N_FRAMES for c in range(F)}


def _pass_through() -> dict[str, str]:
    """Tile output -> tile input for the wires that bypass the switch matrix.

    From `assign X_i[hi-s:0] = Y_i[hi:s];` in <Tile>.v (X_i / Y_i are the
    buffered port nets, my_buf only)."""
    tv = (ROOT / "rtl" / "LUT4AB.v").read_text()
    out = {}
    for x, hi, s, lo, y, hi2, s2 in re.findall(
            r"assign (\w+)_i\[(\d+)-(\d+):(\d+)\] = (\w+)_i\[(\d+):(\d+)\];", tv):
        hi, s, hi2, s2 = int(hi), int(s), int(hi2), int(s2)
        if int(lo) != 0 or hi != hi2 or s != s2:
            raise SystemExit(f"unexpected pass-through assign for {x}")
        for j in range(hi - s + 1):
            out[f"{x}[{j}]"] = f"{y}[{j + s}]"
    return out


PASS = _pass_through()
if set(PASS) | {MUXES[o].net for o in MUXES if MUXES[o].sink == "tile_output"} != set(OUTPUTS):
    raise SystemExit("tile outputs: pass-through + switch matrix do not cover every output")


# ---------------------------------------------------------------- model

def lut_deps(x: str) -> dict[str, list[str]]:
    return {f"L{x}_O": [f"L{x}_I{j}" for j in range(4)],
            f"L{x}_Co": [f"L{x}_Ci", f"L{x}_I1", f"L{x}_I2"]}


def mux8_deps(c0: int, c1: int) -> dict[str, list[str]]:
    if (c0, c1) != (0, 0):
        raise NotImplementedError("MUX8LUT model only for c0 = c1 = 0")
    # c0 = c1 = 0: sCD = S1, sEF = S2, sEH = sGH = S3; M_AD = CD, M_AH = GH
    return {"M_AB": ["A", "B", "S0"], "M_AD": ["C", "D", "S1"],
            "M_AH": ["G", "H", "S3"], "M_EF": ["E", "F", "S2"]}


def deps(sel: dict[str, int]) -> dict[str, list[str]]:
    """Net -> nets it depends on combinationally, for the given mux selects."""
    d: dict[str, list[str]] = {}
    for o, m in MUXES.items():
        k = sel.get(o, 0)
        src = m.inputs[k]
        d[m.net] = [] if src.kind == "const" else [src.net]
    for x in LUTS:
        d.update(lut_deps(x))
    d.update(mux8_deps(0, 0))
    for o, i in PASS.items():
        d[o] = [i]
    return d


def cycles(sel: dict[str, int]) -> list[set[str]]:
    """Strongly connected components with a cycle in the model graph."""
    from fabtest.sim.graph import tarjan
    d = deps(sel)
    nodes = sorted(d)
    comps = tarjan(nodes, lambda n: [p for p in d.get(n, ())])
    return [set(c) for c in comps if len(c) > 1 or c[0] in d.get(c[0], ())]


def break_loops(c: int, sel: dict[str, int]) -> list[tuple[str, int, int, list[str]]]:
    """Move one mux per loop to its next input until no loop is left.

    The mux moved is a switch-matrix mux whose output and selected input are
    both on the loop. Smallest fan-in first: a k:1 mux's code recurs in F/k
    configs, so moving it keeps the code selected elsewhere, while a 16:1
    code exists in one config only. Then muxes whose selected input is a BEL
    output (the loop closes through a BEL), then by name. A mux that has
    already tried every other input is not moved again. Returns the log."""
    log = []
    moves: dict[str, int] = {}
    for _ in range(10000):
        comps = cycles(sel)
        if not comps:
            return log
        comp = comps[0]
        cand = []
        for o, m in MUXES.items():
            if m.model == "direct" or m.net not in comp:
                continue
            s = m.inputs[sel[o]]
            if s.kind != "const" and s.net in comp and moves.get(o, 0) < m.fan_in - 1:
                cand.append((m.fan_in, s.kind != "bel_output", o))
        if not cand:
            raise SystemExit(f"config {c}: loop {sorted(comp)[:8]} has no movable switch-matrix mux")
        *_, o = min(cand)
        old = sel[o]
        sel[o] = (old + 1) % MUXES[o].fan_in
        moves[o] = moves.get(o, 0) + 1
        log.append((o, old, sel[o], sorted(comp)))
    raise SystemExit(f"config {c}: loop breaking did not converge")


def config_word(sel: dict[str, int], lut_bits: dict[str, int] | None = None) -> dict[int, int]:
    """All 616 tile config bits (LUT bits: CELL_BITS unless given per LUT)."""
    v = {b: 0 for b in TILE.configmem}
    for x in LUTS:
        bits = (lut_bits or {}).get(x, CELL_BITS)
        for k in range(cell.N_CONFIG_BITS):
            v[LUT_LO[x] + k] = (bits >> k) & 1
    for o, k in sel.items():
        if not o.startswith("__"):    # "__lut": constant LUTs (full-fabric RegFile configs)
            v.update(MUXES[o].code_for(k))
    return v


def frame_words(cfg: dict[int, int]) -> list[int]:
    words = [0] * N_FRAMES
    for b, val in cfg.items():
        f, d = TILE.configmem[b]
        if val:
            words[f] |= 1 << d
    return words


def frame_order(c: int) -> list[int]:
    return cell.rotated(list(range(N_FRAMES)), LAST_FRAME[c])


# ---------------------------------------------------------------- stimulus


# ---------------------------------------------------------------- configs
