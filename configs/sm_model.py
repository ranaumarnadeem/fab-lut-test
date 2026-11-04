#!/usr/bin/env python3
"""Switch-matrix model of the LUT4AB tile (Phase 8 step 1).

Built only from the files FABulous generated for the tile (rtl/LUT4AB.v,
rtl/LUT4AB_switch_matrix.v, rtl/LUT4AB_ConfigMem.v, parsed by
rtl/tile_parse.py) and the mux models (rtl/models_pack.v). The VERIFY
findings behind every rule here are in NOTES.md, "Phase 8 step 1".

For every switch-matrix output (a mux or a direct wire):
  - output net and what it drives: a tile output port, a BEL input (LUT I0..I3,
    Ci, SR, EN; MUX8LUT A..H, S0..S3) or an internal jump wire (J*_BEG, read
    back by the switch matrix as J*_END),
  - ordered inputs (A0 first), each classified as tile input port, jump wire,
    BEL output (LUT O / Co, MUX8LUT M_*) or constant (GND0 / VCC0),
  - fan-in, select config bits S0.. with tile config bit, frame and FrameData
    position, and the code -> input table (binary, S0 = LSB), including codes
    that select padding (none in this tile) or a constant.

Run: python3 configs/sm_model.py [--md FILE] [--json FILE]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "rtl"))
import tile_parse  # noqa: E402

TILE_NAME = "LUT4AB"
RTL = ROOT / "rtl"
ROUTING_JSON = ROOT / "configs" / "tile_routing.json"
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

    def decode(self, bits: dict[int, int]) -> int:
        """Selected input index for the given tile config bits (missing = 0)."""
        return sum(bits.get(s.tile_bit, 0) << i for i, s in enumerate(self.sel))


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


def check(tile: tile_parse.Tile, muxes: dict[str, SMMux]) -> list[str]:
    """Self-checks; returns findings for NOTES, raises on inconsistency."""
    notes = []
    nbits = int(re.search(r"parameter NoConfigBits=(\d+)",
                          (RTL / f"{TILE_NAME}_switch_matrix.v").read_text()).group(1))
    used = [s.local for m in muxes.values() for s in m.sel]
    if sorted(used) != list(range(nbits)):
        dup = sorted({b for b in used if used.count(b) > 1})
        missing = sorted(set(range(nbits)) - set(used))
        raise SystemExit(f"select bits: duplicates {dup[:5]}, not selects {missing[:5]}")
    notes.append(f"all {nbits} switch-matrix config bits are mux selects, each used once")
    for m in muxes.values():
        if m.model != "direct" and m.fan_in != 1 << len(m.sel) and "GND0 (padding)" not in m.codes:
            raise SystemExit(f"{m.out}: fan-in {m.fan_in} with {len(m.sel)} select bits")
        # contiguous select bits, S0 lowest (gen_switchmatrix.py)
        loc = [s.local for s in m.sel]
        if loc != list(range(loc[0], loc[0] + len(loc))) if loc else False:
            raise SystemExit(f"{m.out}: select bits not contiguous: {loc}")
    # test routing: every constrained bit is a select, and its value picks the path input
    r = json.loads(ROUTING_JSON.read_text())
    owner = {s.tile_bit: m for m in muxes.values() for s in m.sel}
    fixed = {int(b): v for b, v in r["switch_matrix_bits_set"].items()}
    bad = [b for b in fixed if b not in owner]
    if bad:
        raise SystemExit(f"test-routing bits that are not selects: {bad[:10]}")
    hops = 0
    for route in r["routes"]:
        for h in route["hops"]:
            m = muxes[h["mux"]]
            if m.code_for(h["input"]) != {int(b): v for b, v in h["sel"].items()}:
                raise SystemExit(f"{h['mux']}: routing select {h['sel']} != code for input {h['input']}")
            if m.decode(fixed) != h["input"]:
                raise SystemExit(f"{h['mux']}: fixed bits decode to {m.decode(fixed)}, path uses {h['input']}")
            hops += 1
    notes.append(f"test routing: {len(fixed)} constrained bits, all selects; "
                 f"{hops} path hops decode to the path input")
    return notes


def summary(tile: tile_parse.Tile, muxes: dict[str, SMMux]) -> dict:
    from collections import Counter
    real = [m for m in muxes.values() if m.model != "direct"]
    return {
        "outputs": len(muxes),
        "direct": sum(m.model == "direct" for m in muxes.values()),
        "muxes": len(real),
        "by_model": dict(Counter(m.model for m in real)),
        "fan_in_hist": dict(sorted(Counter(m.fan_in for m in real).items())),
        "max_fan_in": max(m.fan_in for m in real),
        "select_bits": sum(len(m.sel) for m in real),
        "sinks": dict(Counter(m.sink for m in muxes.values())),
        "sinks_mux_only": dict(Counter(m.sink for m in real)),
        "bel_inputs": dict(Counter(m.bel.split(".")[1].rstrip("0123456789") for m in muxes.values()
                                   if m.sink == "bel_input")),
        "sources": dict(Counter(s.kind for s in {(s.port, s.kind): s for m in muxes.values()
                                                  for s in m.inputs}.values())),
        "padded_codes": sum(c == "GND0 (padding)" for m in real for c in m.codes),
        "const_codes": [(m.out, k, s.port) for m in real for k, s in enumerate(m.inputs)
                        if s.kind == "const"],
        "frames": dict(sorted(Counter(s.frame for m in real for s in m.sel).items())),
    }


def markdown(muxes: dict[str, SMMux]) -> str:
    def src(s: Source) -> str:
        return {"tile_input": s.net, "jump": f"{s.net} (J)", "const": s.port,
                "bel_output": f"{s.net} ({s.bel})"}[s.kind]

    lines = ["| output | drives | model | fan-in | inputs A0.. | selects S0..: tile bit (frame, FrameData) |",
             "|---|---|---|---|---|---|"]
    for m in muxes.values():
        drv = {"tile_output": "tile out", "jump": "jump", "bel_input": m.bel}[m.sink]
        sel = ", ".join(f"{s.tile_bit} ({s.frame}, {s.frame_data})" for s in m.sel) or "-"
        lines.append(f"| {m.net} | {drv} | {m.model} | {m.fan_in} | "
                     f"{', '.join(src(s) for s in m.inputs)} | {sel} |")
    return "\n".join(lines) + "\n"


TILE, MUXES = build()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--md", type=Path)
    ap.add_argument("--json", type=Path)
    a = ap.parse_args()
    for n in check(TILE, MUXES):
        print("check:", n)
    for k, v in summary(TILE, MUXES).items():
        print(f"{k}: {v}")
    if a.md:
        a.md.write_text(markdown(MUXES))
    if a.json:
        a.json.write_text(json.dumps({k: asdict(m) for k, m in MUXES.items()}, indent=1))


if __name__ == "__main__":
    main()
