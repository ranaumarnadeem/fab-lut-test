#!/usr/bin/env python3
"""Switch-matrix model of the LUT4AB tile.

Built only from the files FABulous generated for the tile (rtl/LUT4AB.v,
LUT4AB_switch_matrix.v, LUT4AB_ConfigMem.v, parsed by model/tile_parse.py) and the mux
models (rtl/models_pack.v). For every switch-matrix output (a mux or a direct wire):
  - output net and what it drives: a tile output port, a BEL input (LUT I0..I3, Ci, SR,
    EN; MUX8LUT A..H, S0..S3) or an internal jump wire (J*_BEG, read back as J*_END),
  - ordered inputs (A0 first), each a tile input port, jump wire, BEL output (LUT O / Co,
    MUX8LUT M_*) or constant (GND0 / VCC0),
  - fan-in, select config bits S0.. with tile config bit, frame and FrameData position,
    and the code -> input table (binary, S0 = LSB).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from fabtest.paths import REPO as ROOT  # noqa: E402
from fabtest.model import tile_parse  # noqa: E402

TILE_NAME = "LUT4AB"
RTL = ROOT / "rtl"
CONST_RE = re.compile(r"(GND|VCC)\d*")


@dataclass(frozen=True)
class SelBit:
    pin: str          # S, S0, S1, ...
    local: int        # switch-matrix ConfigBits index
    tile_bit: int     # tile ConfigBits index (local + sm_base)
    frame: int
    frame_data: int


@dataclass(frozen=True)
class Source:
    port: str         # switch-matrix input port
    net: str          # tile net
    kind: str         # tile_input | jump | bel_output | const
    bel: str = ""     # BEL pin for bel_output, e.g. "LA.O", "MUX8LUT.M_AB"


@dataclass
class SMMux:
    out: str                  # switch-matrix output port
    net: str                  # tile net it drives
    sink: str                 # tile_output | bel_input | jump
    bel: str                  # BEL pin for bel_input, e.g. "LA.I0", "MUX8LUT.S0"
    model: str                # direct | cus_mux21 | cus_mux41 | cus_mux81 | cus_mux161
    inputs: list[Source]
    sel: list[SelBit]
    codes: list[str] = field(default_factory=list)  # code k -> input port, "GND0 (padding)" if padded

    @property
    def fan_in(self) -> int:
        return len(self.inputs)

    def code_for(self, k: int) -> dict[int, int]:
        """Tile config bit values that select input k."""
        if not 0 <= k < self.fan_in:
            raise ValueError(f"{self.out}: input {k} of {self.fan_in}")
        return {s.tile_bit: (k >> i) & 1 for i, s in enumerate(self.sel)}


def _bel_pins(tile_v: str) -> tuple[dict[str, str], dict[str, str]]:
    """tile net -> BEL input pin, tile net -> BEL output pin (from <Tile>.v)."""
    ins, outs = {}, {}
    for btype, inst, body in re.findall(r"^(\w+) (Inst_\w+) \((.*?)\n\);", tile_v, re.S | re.M):
        if "switch_matrix" in btype or "ConfigMem" in btype:
            continue
        m = re.match(r"Inst_(L[A-H])_LUT4c|Inst_(MUX8LUT)_", inst)
        if not m:
            raise SystemExit(f"{TILE_NAME}.v: unknown BEL instance {inst}")
        bel = m.group(1) or m.group(2)
        for pin, expr in re.findall(r"\.(\w+)\((.*?)\)\s*(?:,|$)", body, re.M):
            if pin in ("ConfigBits", "UserCLK"):
                continue
            nets = [n.strip() for n in expr.strip("{}").split(",")]
            if len(nets) > 1:  # {MSB, ..., LSB}
                named = [(f"{pin}{len(nets) - 1 - i}" if pin != "I" else f"I{len(nets) - 1 - i}", n)
                         for i, n in enumerate(nets)]
            else:
                named = [(pin, nets[0])]
            for p, n in named:
                is_out = p in ("O", "Co") or p.startswith("M_")
                (outs if is_out else ins)[n] = f"{bel}.{p}"
    return ins, outs


def build() -> tuple[tile_parse.Tile, dict[str, SMMux]]:
    tile = tile_parse.parse_tile(RTL, TILE_NAME)
    tv = (RTL / f"{TILE_NAME}.v").read_text()
    bel_in, bel_out = _bel_pins(tv)
    sm_text = (RTL / f"{TILE_NAME}_switch_matrix.v").read_text()
    jump_nets = {tile.port_net[o] for o in tile.muxes} - tile.tile_outputs - set(bel_in)

    def source(port: str) -> Source:
        if CONST_RE.fullmatch(port):
            return Source(port, port, "const")
        net = tile.port_net[port]
        if net in tile.tile_inputs:
            return Source(port, net, "tile_input")
        if net in bel_out:
            return Source(port, net, "bel_output", bel_out[net])
        if net in jump_nets:
            return Source(port, net, "jump")
        raise SystemExit(f"switch-matrix input {port} ({net}): unknown source")

    out: dict[str, SMMux] = {}
    for name, m in tile.muxes.items():
        net = tile.port_net[name]
        if net in tile.tile_outputs:
            sink, bel = "tile_output", ""
        elif net in bel_in:
            sink, bel = "bel_input", bel_in[net]
        elif net in jump_nets:
            sink, bel = "jump", ""
        else:
            raise SystemExit(f"switch-matrix output {name} ({net}): unknown sink")
        pins = ["S"] if m.kind == "cus_mux21" else [f"S{i}" for i in range(len(m.sel))]
        sel = []
        for pin, b in zip(pins, m.sel):
            tb = tile.sm_base + b
            fr, fd = tile.configmem[tb]
            sel.append(SelBit(pin, b, tb, fr, fd))
        # padding: A<k> pins tied to GND0 beyond the input vector
        padded = []
        if m.kind != "direct":
            blk = re.search(rf"inst_{m.kind}_{name}\s*\((.*?)\);", sm_text, re.S).group(1)
            padded = [int(k) for k in re.findall(r"\.A(\d+)\(GND0\)", blk)]
        n_codes = 1 << len(sel)
        codes = [m.inputs[k] if k < len(m.inputs) else "GND0 (padding)" for k in range(n_codes)]
        if sorted(padded) != list(range(len(m.inputs), n_codes)):
            raise SystemExit(f"{name}: padding pins {padded} do not match {len(m.inputs)} of {n_codes}")
        out[name] = SMMux(name, net, sink, bel, m.kind, [source(p) for p in m.inputs], sel, codes)
    return tile, out


TILE, MUXES = build()
