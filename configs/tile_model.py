#!/usr/bin/env python3
"""Generic switch-matrix model of any FABulous tile type (Phase 12).

configs/sm_model.py and configs/wio_model.py, without the tile-specific
parts: from the generated <Tile>.v, <Tile>_switch_matrix.v and (if the tile
has config) <Tile>_ConfigMem.v (rtl/tile_parse.py), plus the BEL module
files for the BEL pin directions. Every switch-matrix output becomes an
sm_model.SMMux (sink: tile output, BEL input or jump wire; sources: tile
input, BEL output, jump wire or constant), every `assign X_i[a:b] =
Y_i[c:d];` of <Tile>.v a pass-through. Termination tiles (no ConfigMem):
direct wires only.

The LUT4AB result equals configs/sm_model.MUXES (sources, sinks, select
bits) and configs/sm_configs.PASS: `python3 configs/tile_model.py check`.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "rtl"), str(ROOT / "configs")]
import sm_model  # noqa: E402
import tile_parse as tp  # noqa: E402

CONST = re.compile(r"(GND|VCC)\d*")
SKIP_PORTS = {"FrameData", "FrameStrobe", "FrameData_O", "FrameStrobe_O", "UserCLK", "UserCLKo"}


def _module_dirs(text: str, mod: str) -> dict[str, str]:
    """port -> input / output of module `mod` defined in `text` (ANSI header)."""
    text = re.sub(r"/\*.*?\*/", "", re.sub(r"//[^\n]*", "", text), flags=re.S)
    m = re.search(rf"module\s+{mod}\b(.*?)\);", text, re.S)
    if not m:
        return {}
    hdr = re.sub(r"#\s*\((.*?)\)\s*\(", "(", m.group(1), count=1, flags=re.S)
    return {n: d for d, n in re.findall(r"(input|output)\s+(?:wire\s+|reg\s+)?(?:\[[^\]]+\]\s*)?(\w+)", hdr)}


def _flatten(expr: str) -> list[str]:
    expr = expr.strip()
    if expr.startswith("{"):
        return [e.strip() for e in expr.strip("{}").split(",")][::-1]   # LSB first
    return [expr]


class TileType:
    def __init__(self, tile_dir: Path, name: str):
        self.name, self.dir = name, tile_dir
        tv = (tile_dir / f"{name}.v").read_text()
        self.has_config = (tile_dir / f"{name}_ConfigMem.v").exists()
        if self.has_config:
            t = tp.parse_tile(tile_dir, name)
            self.configmem, muxes, sm_base, port_net = t.configmem, t.muxes, t.sm_base, t.port_net
            tin, tout = t.tile_inputs, t.tile_outputs
        else:
            smv = (tile_dir / f"{name}_switch_matrix.v").read_text()
            muxes = {o: tp.Mux(o, [i], [], "direct")
                     for o, i in re.findall(r"^\s*assign\s+(\w+)\s*=\s*(\w+)\s*;", smv, re.M)}
            self.configmem, sm_base = {}, 0
            inst = re.search(rf"{name}_switch_matrix Inst_{name}_switch_matrix\s*\((.*?)\);", tv, re.S)
            port_net = dict(re.findall(r"\.(\w+)\(([\w\[\]]+)\)", inst.group(1)))
            hdr = tv.split(");", 1)[0]
            tin, tout = set(), set()
            for d, rng, pname in re.findall(r"(input|output)\s*(\[[^\]]+\])?\s*(\w+)", hdr):
                if pname in SKIP_PORTS:
                    continue
                bits = [f"{pname}[{i}]" for i in range(int(rng.strip("[]").split(":")[0]) + 1)] if rng else [pname]
                (tin if d == "input" else tout).update(bits)
        self.tile_inputs, self.tile_outputs = set(tin), set(tout)
        # BEL pins: every instance other than the switch matrix / config memory
        srcs = "\n".join(p.read_text() for p in tile_dir.glob("*.v"))
        self.bel_in, self.bel_out = {}, {}
        for btype, inst, body in re.findall(r"^(\w+) (Inst_\w+) \((.*?)\n\);", tv, re.S | re.M):
            if "switch_matrix" in btype or "ConfigMem" in btype:
                continue
            dirs = _module_dirs(srcs, btype)
            for pin, expr in re.findall(r"\.(\w+)\((.*?)\)\s*(?:,|$)", body, re.M):
                if pin in ("ConfigBits", "UserCLK") or pin not in dirs:
                    continue
                for k, n in enumerate(_flatten(expr)):
                    (self.bel_out if dirs[pin] == "output" else self.bel_in)[n] = f"{inst}.{pin}{k}"
        sm_nets = {port_net[o] for o in muxes}
        jump = sm_nets - self.tile_outputs - set(self.bel_in)

        def source(p: str) -> sm_model.Source:
            if CONST.fullmatch(p):
                return sm_model.Source(p, p, "const")
            n = port_net[p]
            if n in self.tile_inputs:
                return sm_model.Source(p, n, "tile_input")
            if n in self.bel_out:
                return sm_model.Source(p, n, "bel_output", self.bel_out[n])
            if n in jump:
                return sm_model.Source(p, n, "jump")
            raise SystemExit(f"{name}: switch-matrix input {p} ({n}): unknown source")
        self.muxes: dict[str, sm_model.SMMux] = {}
        for o, m in muxes.items():
            net = port_net[o]
            sink = "tile_output" if net in self.tile_outputs else "bel_input" if net in self.bel_in else "jump"
            pins = ["S"] if m.kind == "cus_mux21" else [f"S{i}" for i in range(len(m.sel))]
            sel = []
            for pin, b in zip(pins, m.sel):
                tb = sm_base + b
                fr, fd = self.configmem[tb]
                sel.append(sm_model.SelBit(pin, b, tb, fr, fd))
            self.muxes[o] = sm_model.SMMux(o, net, sink, self.bel_in.get(net, ""), m.kind,
                                           [source(p) for p in m.inputs], sel, list(m.inputs))
        # pass-throughs: assign X_i[hi-s:0] = Y_i[hi:s] (my_buf port nets)
        self.passes: dict[str, str] = {}
        for x, hi, s, lo, y, hi2, s2 in re.findall(
                r"assign (\w+)_i\[(\d+)-(\d+):(\d+)\] = (\w+)_i\[(\d+):(\d+)\];", tv):
            hi, s, s2 = int(hi), int(s), int(s2)
            for j in range(hi - s + 1):
                self.passes[f"{x}[{j}]"] = f"{y}[{j + s2}]"
        self.n_frames = 1 + max((f for f, _ in self.configmem.values()), default=-1)

    def config_word(self, sel: dict[str, int]) -> dict[int, int]:
        """Mux selects; every other config bit (BEL config) 0."""
        v = {b: 0 for b in self.configmem}
        for o, k in sel.items():
            m = self.muxes.get(o)
            if m is not None and m.model != "direct":
                v.update(m.code_for(k))
        return v

    def frame_words(self, cfg: dict[int, int]) -> list[int]:
        words = [0] * 20
        for b, val in cfg.items():
            f, d = self.configmem[b]
            if val:
                words[f] |= 1 << d
        return words


def check() -> None:
    import sm_configs as sc
    t = TileType(ROOT / "rtl", "LUT4AB")
    ref = sm_model.MUXES
    assert set(t.muxes) == set(ref), "mux names"
    for o, m in ref.items():
        g = t.muxes[o]
        assert (g.net, g.sink, g.model, [(s.net, s.kind) for s in g.inputs],
                [(b.tile_bit, b.frame, b.frame_data) for b in g.sel]) == \
               (m.net, m.sink, m.model, [(s.net, s.kind) for s in m.inputs],
                [(b.tile_bit, b.frame, b.frame_data) for b in m.sel]), o
    assert t.passes == sc.PASS, "pass-throughs"
    print(f"LUT4AB: {len(t.muxes)} muxes, {len(t.passes)} pass-throughs = sm_model / sm_configs")


if __name__ == "__main__":
    check()
