"""Plugins for the standard FABulous BELs (fabtest P3).

lut4c, mux8lut, io_pad, config_access: modelled by the lut / io tile kinds (configs/
sm_model.py, configs/wio_model.py); listed here so they are matched and labelled.
pass4: RAM_IO In/OutPass4 (outputs X unless a macro drives them, e.g. BlockRAM_1KB).
muladd: DSP_bot MULADD at config 0 (op M: Q = A * B + C, 8 x 8 + 20 bits).
regfile32x4: RegFile_32x4 read ports (op F; fabric.seq_expected keeps the memory).
blockram_1kb (macro): BlockRAM_1KB of eFPGA_top.v, read data on the RAM_IO InPass
outputs (op Q; fabric.seq_expected keeps the memory).
"""
from __future__ import annotations

import re

from fabtest.bels.base import Bel, MacroPlugin, Plugin


class Lut4c(Plugin):
    name, modules, role, group = "lut4c", r"LUT4c_frame_config(_dffesr)?", "chain", "LUT"


class Mux8lut(Plugin):
    name, modules, role, group = "mux8lut", r"MUX8LUT_frame_config(_mux)?", "comb", "LUT"


class IoPad(Plugin):
    name, modules, role, group = "io_pad", r"IO_1_bidirectional_frame_config_pass", "pad", "IO"


class ConfigAccess(Plugin):
    name, modules, role, group = "config_access", r"Config_access", "cbit", "IO"


class Pass4(Plugin):
    name, modules, role, group = "pass4", r"(In|Out)Pass4_frame_config(_mux)?", "pass", "RAM_IO"

    def fabric_side(self, bel: dict, ext: str) -> str:
        """The pin on the fabric side of external pin ext: InPass ext input k -> output k,
        OutPass ext output k -> input k."""
        if ext in bel["ext_in"]:
            return bel["outputs"][bel["ext_in"].index(ext)]
        return bel["inputs"][bel["ext_out"].index(ext)]


class Opaque(Plugin):
    pass


def _bus(pins: dict, p: str) -> list[str]:
    """Nets of pins p0, p1, ... in index order (until the first gap)."""
    out = []
    while f"{p}{len(out)}" in pins:
        out.append(pins[f"{p}{len(out)}"])
    return out


class Muladd(Plugin):
    name, modules, role, group = "muladd", r"MULADD", "comb", "DSP"
    WIDTHS = {"A": 8, "B": 8, "C": 20, "Q": 20}   # fabric._muladd

    def feasible(self, bel: Bel) -> bool:
        return all(len(_bus(bel.pins_in if p != "Q" else bel.pins_out, p)) == w for p, w in self.WIDTHS.items())

    def ops(self, fab, t, bel: Bel) -> dict:
        if not self.feasible(bel):
            return {}
        ins = [fab.canon(t, n) for p in "ABC" for n in _bus(bel.pins_in, p)]
        return {n: ("M", (ins, k)) for k, n in enumerate(_bus(bel.pins_out, "Q"))}


class Regfile32x4(Plugin):
    name, modules, role, group = "regfile32x4", r"RegFile_32x4", "stateful", "RegFile"

    def pins(self, fab, t, bel: Bel):
        pi = bel.pins_in
        need = [f"D{k}" for k in range(4)] + [f"{p}_ADR{k}" for p in "WAB" for k in range(5)] + ["W_en"]
        if any(n not in pi for n in need) or any(f"{p}D{k}" not in bel.pins_out for p in "AB" for k in range(4)):
            return None
        return {"D": [fab.canon(t, pi[f"D{k}"]) for k in range(4)],
                "W": [fab.canon(t, pi[f"W_ADR{k}"]) for k in range(5)],
                "A": [fab.canon(t, pi[f"A_ADR{k}"]) for k in range(5)],
                "B": [fab.canon(t, pi[f"B_ADR{k}"]) for k in range(5)],
                "en": fab.canon(t, pi["W_en"])}

    def ops(self, fab, t, bel: Bel) -> dict:
        p = self.pins(fab, t, bel)
        if p is None:
            return {}
        return {bel.pins_out[f"{port}D{k}"]: ("F", ([p["D"][k]] + p[port], (t, port, k)))
                for port in "AB" for k in range(4)}


class BlockRam1kb(MacroPlugin):
    name, modules = "blockram_1kb", r"BlockRAM_1KB"
    PINS = {"rd_data": 32, "rd_addr": 8, "wr_addr": 8, "wr_data": 32}

    def bind(self, fab, macro: dict):
        """Pins as fabric nets: the RAM_IO InPass outputs (read data, fabric side) and the
        OutPass inputs (addresses, write data, C0..C5). None when a pin does not reach a
        RAM_IO pass BEL (the macro then stays an X source)."""
        pins = macro["pins"]
        if any(len(pins.get(p, [])) != w for p, w in self.PINS.items()) or any(f"C{j}" not in pins for j in range(6)):
            return None
        out = {}
        try:
            for p in self.PINS:
                out[p] = [fab.macro_net(w, p == "rd_data") for w in pins[p]]
            out["C"] = [fab.macro_net(pins[f"C{j}"][0], False) for j in range(6)]
        except KeyError:
            return None
        out["tiles"] = sorted({fab.tile_of(n) for p in out for n in out[p]})
        return out

    def ops(self, fab, t, data) -> dict:
        k, p = data
        out = {}
        tn = fab.tn(t)
        for i, n in enumerate(p["rd_data"]):
            tile, loc = n.split("/", 1)
            if tile == tn:
                out[loc] = ("Q", ([p["wr_data"][i]], (k, i)))
        return out


PLUGINS = [Lut4c(), Mux8lut(), IoPad(), ConfigAccess(), Pass4(), Muladd(), Regfile32x4()]
MACROS = [BlockRam1kb()]
OPAQUE = Opaque()


def macro_for(module: str):
    return next((m for m in MACROS if re.fullmatch(m.modules, module)), None)
