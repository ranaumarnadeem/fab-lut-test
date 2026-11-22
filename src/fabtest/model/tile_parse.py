"""Parse a generated FABulous tile: ConfigMem, switch matrix, tile wiring.

Everything is read from the files `run_FABulous_fabric` writes for a tile
(here LUT4AB): <Tile>_ConfigMem.v, <Tile>_switch_matrix.v, <Tile>.v.
Nothing about FABulous internals is assumed beyond what these files say;
the parsers fail loudly on anything they do not recognise.

Net naming: tile-level nets are the expressions the tile Verilog connects to
switch-matrix ports, e.g. `N1END[0]`, `J2MID_ABa_BEG[0]`, `LA_I0`, `Ci[0]`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

LATCH_RE = re.compile(
    r"config_latch\s+(\w+)\s*\(\s*"
    r"\.D\(FrameData\[(\d+)\]\),\s*"
    r"\.E\(FrameStrobe\[(\d+)\]\),\s*"
    r"\.Q\(ConfigBits\[(\d+)\]\),\s*"
    r"\.QN\(ConfigBits_N\[(\d+)\]\)\s*\);"
)


def parse_configmem(path: Path) -> dict[int, tuple[int, int]]:
    """tile config bit -> (frame, FrameData bit), from the config_latch instances of a
    FABulous ConfigMem (the `else branch of its `ifdef EMULATION); one per config bit."""
    text = path.read_text()
    if "`else" not in text:
        raise SystemExit(f"{path}: no `ifdef EMULATION ... `else block: not a FABulous ConfigMem")
    body = text.split("`else", 1)[1]
    out = {}
    for _inst, d, s, q, qn in LATCH_RE.findall(body):
        if q != qn:
            raise SystemExit(f"{path}: Q/QN index mismatch for bit {q}")
        out[int(q)] = (int(s), int(d))
    m = re.search(r"\bNoConfigBits\s*=\s*(\d+)", text)
    if m and len(out) != int(m.group(1)):
        raise SystemExit(f"{path}: {len(out)} config_latch instances for NoConfigBits={m.group(1)} "
                         "(another latch cell, or another port order: fabtest reads FABulous's config_latch)")
    return out


@dataclass
class Mux:
    out: str            # switch-matrix port driven
    inputs: list[str]   # switch-matrix ports, A0 first
    sel: list[int]      # switch-matrix local config bits, S0 first
    kind: str           # "direct" or the cus_mux module name (the equivalent one for generic)
    style: str = "custom"   # MultiplexerStyle: custom (cus_mux cell) or generic (vector index)


BLOCK_RE = re.compile(r"//switch matrix multiplexer (\w+) MUX-(\d+)\n(.*?)(?=\n //switch matrix multiplexer |\Z)", re.S)


def parse_switch_matrix(path: Path) -> tuple[dict[str, Mux], int, set[str], set[str]]:
    text = path.read_text()
    m = re.search(r"parameter NoConfigBits=(\d+)", text)
    nbits = int(m.group(1))
    hdr = text.split(");", 1)[0]
    ins = set(re.findall(r"input\s+(\w+)\s*,", hdr))
    outs = set(re.findall(r"output\s+(\w+)\s*,", hdr))
    muxes: dict[str, Mux] = {}
    for out, n, body in BLOCK_RE.findall(text):
        n = int(n)
        if n == 1:
            d = re.match(rf"\s*assign {out} = (\w+);", body)
            if not d:
                raise SystemExit(f"{out}: cannot parse direct assign")
            muxes[out] = Mux(out, [d.group(1)], [], "direct")
            continue
        a = re.search(rf"assign {out}_input = \{{([^}}]*)\}};", body)
        if not a:
            raise SystemExit(f"{out}: cannot parse MUX-{n}")
        vec = [s.strip() for s in a.group(1).split(",")]
        inputs = list(reversed(vec))  # {msb, ..., lsb}: A0 = lsb
        g = re.search(rf"assign (?:#\d+ )?{out} = {out}_input\[ConfigBits\[(\d+)(?::(\d+))?\]\];", body)
        if g:   # MultiplexerStyle generic: out = in[ConfigBits[hi:lo]], same code as cus_mux
            hi = int(g.group(1))
            lo = int(g.group(2)) if g.group(2) else hi
            width = 2 ** (n - 1).bit_length()
            if len(inputs) != n or hi - lo + 1 != (width - 1).bit_length():
                raise SystemExit(f"{out}: inconsistent generic mux block")
            muxes[out] = Mux(out, inputs, list(range(lo, hi + 1)), f"cus_mux{width}1", "generic")
            continue
        inst = re.search(r"(cus_mux\d+)\s+\w+\s*\((.*?)\);", body, re.S)
        if not inst:
            raise SystemExit(f"{out}: cannot parse MUX-{n}")
        sel_pins = {p: int(b) + int(o) for p, b, o in
                    re.findall(r"\.(S\d?)\(ConfigBits\[(\d+)\+(\d+)\]\)", inst.group(2))}
        order = ["S"] if "S" in sel_pins else [f"S{i}" for i in range(len(sel_pins))]
        sel = [sel_pins[p] for p in order]
        x = re.search(r"\.X\((\w+)\)", inst.group(2)).group(1)
        if x != out or len(inputs) != n:
            raise SystemExit(f"{out}: inconsistent mux block")
        muxes[out] = Mux(out, inputs, sel, inst.group(1))
    undriven = outs - set(muxes)
    if undriven:
        raise SystemExit(f"switch matrix outputs without a mux: {sorted(undriven)[:5]}")
    return muxes, nbits, ins, outs


@dataclass
class Tile:
    name: str
    configmem: dict[int, tuple[int, int]]
    muxes: dict[str, Mux]
    sm_base: int                     # tile config bit of switch-matrix bit 0
    port_net: dict[str, str]         # switch-matrix port -> tile net
    tile_inputs: set[str] = field(default_factory=set)   # tile input port bits
    tile_outputs: set[str] = field(default_factory=set)  # tile output port bits
    bels: dict[str, tuple[int, int]] = field(default_factory=dict)  # BEL -> (lo, hi) config bits


def parse_tile(tile_dir: Path, name: str) -> Tile:
    tv = (tile_dir / f"{name}.v").read_text()
    cm = parse_configmem(tile_dir / f"{name}_ConfigMem.v")
    muxes, nbits, _ins, _outs = parse_switch_matrix(tile_dir / f"{name}_switch_matrix.v")

    hdr = tv.split(");", 1)[0]
    tin, tout = set(), set()
    for d, rng, pname in re.findall(r"(input|output)\s*(\[[^\]]+\])?\s*(\w+)", hdr):
        if pname in ("FrameData", "FrameStrobe", "FrameData_O", "FrameStrobe_O", "UserCLK", "UserCLKo"):
            continue
        if rng:
            hi, lo = rng.strip("[]").split(":")
            if not (hi.isdigit() and lo == "0"):
                raise SystemExit(f"{name}.v: unexpected range {rng} on port {pname}")
            bits = [f"{pname}[{i}]" for i in range(int(hi) + 1)]
        else:
            bits = [pname]
        (tin if d == "input" else tout).update(bits)

    inst = re.search(rf"{name}_switch_matrix Inst_{name}_switch_matrix \((.*?)\);", tv, re.S)
    port_net = dict(re.findall(r"\.(\w+)\(([\w\[\]]+)\)", inst.group(1)))
    port_net.pop("ConfigBits", None)
    port_net.pop("ConfigBits_N", None)
    m = re.search(rf"Inst_{name}_switch_matrix \(.*?\.ConfigBits\(ConfigBits\[(\d+)-1:(\d+)\]\)", tv, re.S)
    hi, lo = int(m.group(1)), int(m.group(2))
    if hi - lo != nbits:
        raise SystemExit(f"switch matrix config slice {lo}..{hi - 1} != {nbits} bits")

    bels = {}
    for btype, bname, hi_, lo_ in re.findall(
            r"(\w+) (Inst_\w+) \(.*?\.ConfigBits\(ConfigBits\[(\d+)-1:(\d+)\]\)", tv, re.S):
        if "switch_matrix" in btype or "ConfigMem" in btype:
            continue
        bels[bname] = (int(lo_), int(hi_) - 1)
    return Tile(name, cm, muxes, lo, port_net, tin, tout, bels)


# ---------------------------------------------------------------- routing
