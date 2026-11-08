#!/usr/bin/env python3
"""Baseline switch-matrix configs for one LUT4AB tile (Phase 8 step 2).

Config c (0 .. F-1, F = max fan-in = 16) sets every switch-matrix mux to
input c mod fan-in (configs/sm_model.py encoding). Every LUT: XOR truth
table, combinational output, I0 from I[0], reset value 0. MUX8LUT config
bits 0. A config that closes a combinational loop is repaired by moving one
mux of the loop to its next input, repeated until no loop is left
(`break_loops`, logged in LOOP_LOG); the netlist gate is sim/loop_check.py.

Stimulus: all M tile input ports get a unique w-bit code (w =
ceil(log2(M + 2)), no code all 0 or all 1), applied bit by bit and then
complemented: 2w vectors per config, each sampled. All tile output ports
are observed at every sample. UserCLK stays 0 (the LUTs are combinational).

Frames: all 20 written, rotated so that config c ends on LAST_FRAME[c]
(distinct per config, Phase 8 0a rule).

Stimulus word (64 hex digits): [31:0] FrameData, [51:32] FrameStrobe,
[52 + j] tile input INPUTS[j], [253] UserCLK, [254] sample.
Sample string: OUTPUTS[0] first.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "configs"), str(ROOT / "rtl")]
import configs as cell  # noqa: E402
import sm_model  # noqa: E402

TILE = sm_model.TILE
MUXES = sm_model.MUXES
N_FRAMES = 1 + max(f for f, _ in TILE.configmem.values())
LUTS = "ABCDEFGH"
LUT_LO = {x: TILE.bels[f"Inst_L{x}_LUT4c_frame_config_dffesr"][0] for x in LUTS}
MUX8_LO = TILE.bels["Inst_MUX8LUT_frame_config_mux"][0]
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
# Config suite: "baseline" (this file) or "router" (configs/sm_router.py,
# selects in configs/sm_router.json), from the SM_SUITE environment variable.
SUITE = os.environ.get("SM_SUITE", "baseline")
if SUITE not in ("baseline", "router"):
    raise SystemExit(f"SM_SUITE={SUITE}: baseline or router")
ROUTER_JSON = ROOT / "configs" / "sm_router.json"
N_CFG = len(json.loads(ROUTER_JSON.read_text())["configs"]) if SUITE == "router" else F
# Last frame written per config: distinct for all configs (0a rule) while
# there are at most N_FRAMES configs.
LAST_FRAME = {c: (N_FRAMES - 1 - c) % N_FRAMES for c in range(max(F, N_CFG))}


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


def evaluate(sel: dict[str, int], inputs: dict[str, int]) -> dict[str, int]:
    """Value of every tile output for one input vector (loop-free selects)."""
    val: dict[str, int] = dict(inputs)
    tt = CELL_BITS & 0xFFFF
    for o, m in MUXES.items():
        s = m.inputs[sel.get(o, 0)]
        if s.kind == "const":
            val[m.net] = 0 if s.port.startswith("GND") else 1
    d = deps(sel)
    srcnet = {MUXES[o].net: MUXES[o].inputs[sel.get(o, 0)].net for o in MUXES}

    def get(n: str, depth: int = 0) -> int:
        if n in val:
            return val[n]
        if depth > 10000:
            raise RuntimeError("loop")
        if n in PASS:
            v = get(PASS[n], depth + 1)
        elif n in srcnet:
            v = get(srcnet[n], depth + 1)
        elif re.fullmatch(r"L[A-H]_O", n):
            x = n[1]
            idx = sum(get(f"L{x}_I{j}", depth + 1) << j for j in range(4))  # I0 from I[0]
            v = (tt >> idx) & 1
        elif re.fullmatch(r"L[A-H]_Co", n):
            x = n[1]
            a, b, c = (get(f"L{x}_{p}", depth + 1) for p in ("Ci", "I1", "I2"))
            v = (a & b) | (a & c) | (b & c)
        elif n in ("M_AB", "M_AD", "M_AH", "M_EF"):
            g = {p: get(p, depth + 1) for p in d[n]}
            a, b, s = d[n]
            v = g[b] if g[s] else g[a]
        else:
            raise KeyError(n)
        val[n] = v
        return v

    return {o: get(o) for o in OUTPUTS}


def cycles(sel: dict[str, int]) -> list[set[str]]:
    """Strongly connected components with a cycle in the model graph."""
    sys.path.insert(0, str(ROOT / "sim"))
    from loop_check import tarjan
    d = deps(sel)
    nodes = sorted(d)
    comps = tarjan(nodes, lambda n: [p for p in d.get(n, ())])
    return [set(c) for c in comps if len(c) > 1 or c[0] in d.get(c[0], ())]


def baseline_selects(c: int) -> dict[str, int]:
    return {o: c % m.fan_in for o, m in MUXES.items() if m.model != "direct"}


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


def config_word(sel: dict[str, int]) -> dict[int, int]:
    """All 616 tile config bits."""
    v = {b: 0 for b in TILE.configmem}
    for x in LUTS:
        for k in range(cell.N_CONFIG_BITS):
            v[LUT_LO[x] + k] = (CELL_BITS >> k) & 1
    for o, k in sel.items():
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

@dataclass
class SStep:
    fd: int = 0
    fs: int = 0
    inp: int = 0          # bit j = INPUTS[j]
    clk: int = 0
    sample: int = 0
    note: str = ""

    def encode(self) -> str:
        w = (self.fd & 0xFFFFFFFF) | (self.fs & 0xFFFFF) << 32 | self.inp << 52
        w |= (self.clk & 1) << 253 | (self.sample & 1) << 254
        return f"{w:064x}"


@dataclass
class SMSeq:
    name: str
    description: str
    steps: list[SStep] = field(default_factory=list)
    selects: list[dict[str, int]] = field(default_factory=list)   # per config written
    configs_written: list[dict[int, int]] = field(default_factory=list)
    frame_orders: list[list[int]] = field(default_factory=list)
    seg_ends: list[int] = field(default_factory=list)
    sample_sel: list[int] = field(default_factory=list)   # per sample: index into selects

    def write_config(self, sel: dict[str, int], order: list[int]) -> None:
        if sorted(order) != list(range(N_FRAMES)):
            raise ValueError("order is not a permutation of the frames")
        cfg = config_word(sel)
        self.selects.append(dict(sel))
        self.configs_written.append(cfg)
        self.frame_orders.append(list(order))
        words = frame_words(cfg)
        inp = self.steps[-1].inp if self.steps else 0
        for f in order:
            self.steps.append(SStep(fd=words[f], fs=0, inp=inp, note=f"frame{f} data"))
            self.steps.append(SStep(fd=words[f], fs=1 << f, inp=inp, note=f"frame{f} strobe"))
            self.steps.append(SStep(fd=words[f], fs=0, inp=inp, note=f"frame{f} release"))
        # FABulous idle: FrameStrobe 0, FrameData keeps the last word (NOTES.md 0a.1)
        self.steps.append(SStep(fd=words[order[-1]], fs=0, inp=inp, note="config idle"))

    def vectors(self) -> None:
        fd = self.steps[-1].fd
        codes = {p: j + 1 for j, p in enumerate(INPUTS)}
        vecs = []
        for b in range(W):
            vecs.append(sum(((codes[p] >> b) & 1) << j for j, p in enumerate(INPUTS)))
        full = (1 << len(INPUTS)) - 1
        vecs += [v ^ full for v in vecs]
        for t, v in enumerate(vecs):
            self.steps.append(SStep(fd=fd, inp=v, sample=1, note=f"vector {t}"))
            self.sample_sel.append(len(self.selects) - 1)

    def write_stim(self, path: Path) -> None:
        path.write_text("".join(s.encode() + "\n" for s in self.steps))
        path.with_suffix(".order").write_text(
            "".join(" ".join(map(str, o)) + "\n" for o in self.frame_orders))


def expected(seq: SMSeq) -> list[str]:
    out = []
    samples = [s for s in seq.steps if s.sample]
    for s, k in zip(samples, seq.sample_sel):
        inputs = {p: (s.inp >> j) & 1 for j, p in enumerate(INPUTS)}
        v = evaluate(seq.selects[k], inputs)
        out.append("".join(str(v[o]) for o in OUTPUTS))
    return out


# ---------------------------------------------------------------- configs

def recover_codes(sels: dict[int, dict[str, int]], logs: dict[int, list]) -> None:
    """Re-place codes that loop breaking left unselected.

    For a code k of mux o selected by no config: take a config d whose
    selection of o is duplicated elsewhere (so d can give it up) and a
    config c2; set o = k in c2 and o = c2's old code in d (or just o = k in d
    when c2 = d). Accepted when both configs stay loop-free; logged."""
    for o, m in MUXES.items():
        if m.model == "direct":
            continue
        for k in range(m.fan_in):
            if any(sels[c][o] == k for c in sels):
                continue
            count = {j: sum(sels[c][o] == j for c in sels) for j in range(m.fan_in)}
            done = False
            for d in sorted(c for c in sels if count[sels[c][o]] > 1):
                for c2 in [d] + [c for c in sorted(sels) if c != d]:
                    old_d, old_c2 = sels[d][o], sels[c2][o]
                    sels[c2][o] = k
                    if c2 != d:
                        sels[d][o] = old_c2
                    if not cycles(sels[c2]) and (c2 == d or not cycles(sels[d])):
                        logs[c2].append((o, old_c2, k, ["recovered code"]))
                        if c2 != d:
                            logs[d].append((o, old_d, old_c2, ["swap for recovered code"]))
                        done = True
                        break
                    sels[c2][o], sels[d][o] = old_c2, old_d
                if done:
                    break


def build_configs() -> tuple[dict[int, dict[str, int]], dict[int, list]]:
    sels, logs = {}, {}
    for c in range(F):
        sel = baseline_selects(c)
        logs[c] = break_loops(c, sel)
        sels[c] = sel
    recover_codes(sels, logs)
    return sels, logs


BASE_SELECTS, LOOP_LOG = build_configs()
if SUITE == "router":
    SELECTS = dict(enumerate(json.loads(ROUTER_JSON.read_text())["configs"]))
    for _c, _sel in SELECTS.items():
        if set(_sel) != set(BASE_SELECTS[0]) or cycles(_sel):
            raise SystemExit(f"{ROUTER_JSON.name}: config {_c} incomplete or with a loop")
else:
    SELECTS = BASE_SELECTS
PREFIX = "RT" if SUITE == "router" else "SM"


def name_of(c: int) -> str:
    return f"{PREFIX}{c:02d}"


def make_seq(c: int) -> SMSeq:
    what = "routed (configs/sm_router.py)" if SUITE == "router" else f"every mux on input {c} mod fan-in"
    s = SMSeq(name_of(c), f"switch-matrix {SUITE} {c}: {what}")
    s.write_config(SELECTS[c], frame_order(c))
    s.vectors()
    s.seg_ends.append(len(s.steps) - 1)
    return s


SM_CONFIGS = {name_of(c): (lambda c=c: make_seq(c)) for c in range(len(SELECTS))}
# Fixed copy: runners may replace SM_CONFIGS with a sequence.
BASE_SM_CONFIGS = dict(SM_CONFIGS)


def decode_selects(state: dict[int, int]) -> dict[str, int]:
    return {o: m.decode(state) for o, m in MUXES.items() if m.model != "direct"}


def transition_order(prev: dict[int, int], c: int) -> tuple[list[int], int]:
    """Frame order from config word `prev` to config c with no combinational
    loop in any intermediate state (the tile model).

    Writing frames one at a time passes through mixes of the two configs; a
    mix can close a loop (in zero-delay simulation a fault can then make it
    oscillate forever). Greedy: take the frames in frame_order(c), keeping
    its last frame last (0a rule), and write the first remaining frame that
    leaves the state loop free (depth-first, bounded). If no order ends on
    that frame, a spare frame (no config's LAST_FRAME) is used as the last
    one. Returns the order and the number of intermediate states that still
    have a loop (0 unless no choice exists).""" 
    want = config_word(SELECTS[c])
    by_frame = defaultdict(list)
    for b, (f, _d) in TILE.configmem.items():
        by_frame[f].append(b)
    def ok(st: dict[int, int], f: int) -> dict[int, int] | None:
        trial = dict(st)
        for b in by_frame[f]:
            trial[b] = want[b]
        return None if cycles(decode_selects(trial)) else trial

    # depth-first, frames tried in frame_order(c) order; bounded search
    budget = [5000]

    def dfs(st: dict[int, int], left: list[int]) -> list[int] | None:
        if not left:
            return []
        for f in left:
            budget[0] -= 1
            if budget[0] < 0:
                return None
            nxt = ok(st, f)
            if nxt is not None:
                rest = dfs(nxt, [x for x in left if x != f])
                if rest is not None:
                    return [f] + rest
        return None

    # The 0a last frame first; if no loop-free order ends on it, a spare frame
    # (one that is no config's LAST_FRAME), so last frames stay distinct.
    spare = [f for f in range(N_FRAMES) if f not in LAST_FRAME.values()]
    for last in [LAST_FRAME[c]] + spare:
        base = cell.rotated(list(range(N_FRAMES)), last)
        budget[0] = 5000
        found = dfs(dict(prev), base[:-1])
        if found is not None:
            return found + [last], 0
    # fallback: greedy, counting the intermediate states left with a loop
    base = frame_order(c)
    last, todo, state, order, bad = base[-1], base[:-1], dict(prev), [], 0
    while todo:
        pick = next((f for f in todo if ok(state, f) is not None), None)
        if pick is None:
            pick, bad = todo[0], bad + 1
        for b in by_frame[pick]:
            state[b] = want[b]
        order.append(pick)
        todo.remove(pick)
    return order + [last], bad


def sequence(names: list[str]) -> SMSeq:
    """Sequence mode: the configs as one run from a single power-up.

    The first config is written as in per-config mode (from power-up, all
    latches X); every later one in a loop-free transition order."""
    s = SMSeq("SEQ_SM", "sequence " + " -> ".join(names))
    s.transition_bad = []
    prev = None
    for n in names:
        c = int(n[2:])
        if prev is None:
            order, bad = frame_order(c), 0
        else:
            order, bad = transition_order(prev, c)
        s.transition_bad.append(bad)
        s.write_config(SELECTS[c], order)
        s.vectors()
        s.seg_ends.append(len(s.steps) - 1)
        prev = s.configs_written[-1]
    return s


def selection_report() -> dict:
    """Inputs never selected by any config (after loop breaking)."""
    chosen = {o: {SELECTS[c][o] for c in SELECTS} for o in SELECTS[0]}
    never = [(o, k, MUXES[o].inputs[k].port) for o, ks in chosen.items()
             for k in range(MUXES[o].fan_in) if k not in ks]
    return {"never_selected": never,
            "codes": sum(MUXES[o].fan_in for o in chosen),
            "selected": sum(len(ks) for ks in chosen.values())}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=ROOT / "build" / "sm_stim")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    print(f"M = {len(INPUTS)} inputs, w = {W}, {2 * W} vectors per config, "
          f"{len(OUTPUTS)} outputs, F = {F} configs")
    for c in range(F if SUITE == "baseline" else 0):
        for o, old, new, comp in LOOP_LOG[c]:
            what = comp[0] if comp[0] in ("recovered code", "swap for recovered code") \
                else f"loop through {len(comp)} nets"
            print(f"  {name_of(c)}: {what}; {o} input {old} -> {new}")
    r = selection_report()
    print(f"selected {r['selected']} of {r['codes']} mux codes; never selected: {r['never_selected']}")
    for name, f in SM_CONFIGS.items():
        seq = f()
        seq.write_stim(a.out / f"{name}.stim")
        (a.out / f"{name}.expected").write_text("\n".join(expected(seq)) + "\n")


if __name__ == "__main__":
    main()
