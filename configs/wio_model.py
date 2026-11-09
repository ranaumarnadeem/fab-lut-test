#!/usr/bin/env python3
"""Switch-matrix and BEL model of the W_IO tile (Phase 10d).

From the FABulous-generated files in rtl/W_IO (copied from the demo fabric,
ext/demo/Tile/W_IO), parsed by rtl/tile_parse.py, in the form of
configs/sm_model.py (SMMux, Source, SelBit), so the array model and router
treat W_IO muxes like LUT4AB muxes.

VERIFY (rtl/W_IO): W_IO.v instances Inst_A/B_IO_1_bidirectional_frame_config_pass
(.I(A_I) .T(A_T) .O(A_O) .Q(A_Q) .I_top .T_top .O_top) and
Inst_A/B_config_Config_access (.C_bit({..C_bit3..0}) .ConfigBits(ConfigBits[3:0] /
[7:4])); IO_1_bidirectional_frame_config_pass.v: O = O_top, Q <= O_top on posedge
UserCLK, I_top = I, T_top = ~T; Config_access.v: C_bit = ConfigBits. The switch
matrix holds ConfigBits[113:8] (sm_base 8), frames 0..3.

BEL ops (tile-local nets): A_O <- A_O_top, A_I_top <- A_I, A_T_top <- NOT A_T,
A_config_C_bit<k> <- config bit k (B: bit 4 + k). A_Q / B_Q (registered pad
inputs, X until clocked) are never selected by the combinational flow
(Q_SOURCES).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "rtl"), str(ROOT / "configs")]
import sm_model  # noqa: E402
import tile_parse  # noqa: E402

TILE = tile_parse.parse_tile(ROOT / "rtl" / "W_IO", "W_IO")
BEL_OUT = {"A_O", "A_Q", "B_O", "B_Q"}
BEL_IN = {"A_I", "A_T", "B_I", "B_T"}
Q_SOURCES = {"A_Q", "B_Q"}
PADS_IN = ["A_O_top", "B_O_top"]
PADS_OUT = ["A_I_top", "A_T_top", "B_I_top", "B_T_top"]
CBITS = [f"{p}_config_C_bit{k}" for p in "AB" for k in range(4)]   # config bits 0..7
N_CBITS = 8


def _build() -> dict[str, sm_model.SMMux]:
    out = {}
    for name, m in TILE.muxes.items():
        net = TILE.port_net[name]
        if net in TILE.tile_outputs:
            sink, bel = "tile_output", ""
        elif net in BEL_IN:
            sink, bel = "bel_input", f"{net[0]}.{net[2:]}"
        else:
            raise SystemExit(f"W_IO {name} ({net}): unknown sink")
        srcs = []
        for p in m.inputs:
            if re.fullmatch(r"(GND|VCC)\d*", p):
                srcs.append(sm_model.Source(p, p, "const"))
                continue
            n = TILE.port_net[p]
            kind = "bel_output" if n in BEL_OUT else "tile_input" if n in TILE.tile_inputs else None
            if kind is None:
                raise SystemExit(f"W_IO {name} input {p} ({n}): unknown source")
            srcs.append(sm_model.Source(p, n, kind, n if kind == "bel_output" else ""))
        pins = ["S"] if m.kind == "cus_mux21" else [f"S{i}" for i in range(len(m.sel))]
        sel = []
        for pin, b in zip(pins, m.sel):
            tb = TILE.sm_base + b
            fr, fd = TILE.configmem[tb]
            sel.append(sm_model.SelBit(pin, b, tb, fr, fd))
        model = "direct" if m.kind == "direct" else m.kind
        if model != "direct" and len(srcs) != 1 << len(sel):
            raise SystemExit(f"W_IO {name}: fan-in {len(srcs)} with {len(sel)} select bits")
        out[name] = sm_model.SMMux(name, net, sink, bel, model, srcs, sel, [s.port for s in srcs])
    used = sorted(s.local for mm in out.values() for s in mm.sel)
    if used != list(range(len(used))) or TILE.sm_base + len(used) != len(TILE.configmem):
        raise SystemExit("W_IO select bits: not the contiguous switch-matrix bits")
    return out


MUXES = _build()
INPUTS = sorted(TILE.tile_inputs)
OUTPUTS = sorted(TILE.tile_outputs)
N_FRAMES = 1 + max(f for f, _ in TILE.configmem.values())


def config_word(sel: dict[str, int], cbits: int) -> dict[int, int]:
    """All W_IO config bits: mux selects, Config_access bits 0..7 = cbits."""
    v = {b: 0 for b in TILE.configmem}
    for k in range(N_CBITS):
        v[k] = (cbits >> k) & 1
    for o, k in sel.items():
        if o in MUXES and MUXES[o].model != "direct":
            v.update(MUXES[o].code_for(k))
    return v


def frame_words(cfg: dict[int, int]) -> list[int]:
    words = [0] * N_FRAMES
    for b, val in cfg.items():
        f, d = TILE.configmem[b]
        if val:
            words[f] |= 1 << d
    return words


if __name__ == "__main__":
    from collections import Counter
    print(len(MUXES), "muxes", Counter(m.model for m in MUXES.values()), len(TILE.configmem), "config bits,",
          N_FRAMES, "frames")
    print("inputs", len(INPUTS), "outputs", len(OUTPUTS))
