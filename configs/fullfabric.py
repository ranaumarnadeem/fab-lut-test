#!/usr/bin/env python3
"""Model of the full FABulous demo fabric from its generated eFPGA.v (Phase 12).

rtl/fabric/eFPGA.v (copied from ext/demo/Fabric) instantiates 196 tiles; the DSP
supertile (rtl/fabric/DSP/DSP.v) holds DSP_top and DSP_bot. Every tile input
bit is resolved through the instance connections (both hierarchy levels) to
the tile output bit that drives it, a top-level input port (border) or
nothing (tied 0). Tile models: LUT4AB (configs/sm_configs.py), W_IO
(configs/wio_model.py), every other type configs/tile_model.TileType (BEL
outputs never selected: RegFile, DSP MULADD, RAM_IO pass BELs are not modelled).

Coordinates: model (x, y) = eFPGA (X - 1, Y), so the W_IO column is x = -1 as
in configs/fabric.Array (fabric mode) and the router / stimulus code applies
unchanged. Border: the 28 W_IO pads in; the W_IO pad outputs and Config_access
bits out (the RAM2FAB / FAB2RAM ports face the BRAMs).

Interface: configs/fabric.Array (ops, grade, cycles, stim, seq_expected).
"""
from __future__ import annotations

import math
import random
import re
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "configs"), str(ROOT / "rtl"), str(ROOT / "sim"), str(ROOT / "sim" / "fastsim")]
import fabric  # noqa: E402
import sm_configs as sc  # noqa: E402
import tile_model  # noqa: E402

FAB = ROOT / "rtl" / "fabric"
TYPE_DIR = {"RegFile": FAB / "RegFile", "RAM_IO": FAB / "RAM_IO", "DSP_top": FAB / "DSP", "DSP_bot": FAB / "DSP",
            "N_term_single": ROOT / "rtl" / "term", "S_term_single": ROOT / "rtl" / "term",
            "N_term_DSP": FAB / "N_term_DSP", "S_term_DSP": FAB / "S_term_DSP",
            "N_term_RAM_IO": FAB / "N_term_RAM_IO", "S_term_RAM_IO": FAB / "S_term_RAM_IO",
            "N_term_single2": FAB / "N_term_single2", "S_term_single2": FAB / "S_term_single2"}
CFG_PORTS = {"FrameData", "FrameStrobe", "FrameData_O", "FrameStrobe_O", "UserCLK", "UserCLKo"}


def _strip(text: str) -> str:
    return re.sub(r"/\*.*?\*/", "", re.sub(r"//[^\n]*", "", text), flags=re.S)


def _ports(text: str, mod: str) -> dict[str, tuple[str, int]]:
    """port -> (direction, width) of module `mod` (FABulous generated headers)."""
    text = _strip(text)
    m = re.search(rf"module\s+{mod}\b.*?\((.*?)\);", text, re.S)
    hdr = m.group(1)
    hdr = hdr[hdr.rfind("#") + 1:] if False else hdr
    out = {}
    for d, hi, lo, n in re.findall(r"(input|output)\s+(?:wire\s+)?(?:\[\s*([^:\]]+):\s*(\d+)\s*\]\s*)?(\w+)", hdr):
        if hi:
            hi = hi.replace("FrameBitsPerRow", "32").replace("MaxFramesPerCol", "20")
            w = eval(hi, {}, {}) - int(lo) + 1   # only integer arithmetic from the header
        else:
            w = 1
        out[n] = (d, w)
    return out


def _instances(text: str) -> list[tuple[str, str, dict[str, str]]]:
    """(module type, instance, {port: expression}) of a FABulous-generated top."""
    text = _strip(text)
    text = re.sub(r"`ifdef EMULATION.*?`endif", "", text, flags=re.S)
    out = []
    for typ, inst, body in re.findall(r"(?:\(\* keep \*\)\s*)?(\w+)\s+(Tile_\w+)\s*\((.*?)\);", text, re.S):
        conns = dict(re.findall(r"\.(\w+)\(\s*([^()]*?)\s*\)", body))
        out.append((typ, inst, conns))
    return out


class FullFabric(fabric.Array):
    def __init__(self, n_rand: int = 54, bram: bool = False):
        """bram: the fabric with its 7 BlockRAMs (synth/build_fabbram.sh, netlist fullbram):
        the RAM_IO data paths become BRAM reads and writes (bel_ops op Q)."""
        self.fabric = True
        self.n_rand = n_rand
        self._cmemo, self._static, self._mop = {}, {}, {}
        self.name = "fullbram" if bram else "fullfabric"
        self.bram = bram
        self.width = {n: w for _, w, n in __import__("gen_array").tile_ports()}
        top = (FAB / "eFPGA.v").read_text()
        dsp = (FAB / "DSP" / "DSP.v").read_text()
        self.top_ports = _ports(top, "eFPGA")
        self.types: dict = {}        # tile -> type name
        self.gen: dict = {}          # type -> TileType (generic types)
        conns: list = []             # (tile, port, expression in the eFPGA scope)
        for typ, inst, cn in _instances(top):
            X, Y = map(int, re.match(r"Tile_X(\d+)Y(\d+)_", inst).groups())
            if typ == "DSP":
                # supertile: DSP port <p> is the eFPGA expression; inside, sub-tiles connect
                # to DSP ports or internal wires (top2bot) by name
                inner = _instances(dsp)
                for styp, sinst, scn in inner:
                    dx, dy = map(int, re.match(r"Tile_X(\d+)Y(\d+)_", sinst).groups())
                    t = (X + dx - 1, Y + dy)
                    self.types[t] = styp
                    for p, e in scn.items():
                        if p in CFG_PORTS:
                            continue
                        e2 = cn.get(e, f"{inst}/{e}")     # a DSP port -> its eFPGA wire, else internal
                        conns.append((t, p, e2))
                continue
            t = (X - 1, Y)
            self.types[t] = typ
            for p, e in cn.items():
                if p not in CFG_PORTS:
                    conns.append((t, p, e))
        for typ in set(self.types.values()):
            if typ not in ("LUT4AB", "W_IO"):
                self.gen[typ] = tile_model.TileType(TYPE_DIR[typ], typ)
        # port widths and directions per type
        self.tports = {}
        for typ in set(self.types.values()):
            if typ == "LUT4AB":
                txt, mod = (ROOT / "rtl" / "LUT4AB.v").read_text(), "LUT4AB"
            elif typ == "W_IO":
                txt, mod = (ROOT / "rtl" / "W_IO" / "W_IO.v").read_text(), "W_IO"
            else:
                txt, mod = (TYPE_DIR[typ] / f"{typ}.v").read_text(), typ
            self.tports[typ] = _ports(txt, mod)
        # wire bit -> driver: (tile, output port bit) or ("PAD", top input port)
        drv, readers = {}, []
        for t, p, e in conns:
            d, w = self.tports[self.types[t]].get(p, (None, 0))
            if d is None:
                continue
            for k in range(w):
                bitname = f"{p}[{k}]" if self._is_bus(t, p) else p
                wb = (e, k)
                if d == "output":
                    drv[wb] = (t, bitname)
                else:
                    readers.append((t, bitname, wb))
        for n, (d, w) in self.top_ports.items():
            if d == "input":
                for k in range(w):
                    drv[(n, k)] = ("PAD", n if w == 1 else f"{n}[{k}]")
        self.in_drv = {}
        for t, bitname, wb in readers:
            self.in_drv[(t, bitname)] = drv.get(wb)
        self.out_port = {}           # (tile, output port bit) -> top output port
        for t, p, e in conns:
            d, w = self.tports[self.types[t]].get(p, (None, 0))
            if d == "output" and e in self.top_ports and self.top_ports[e][0] == "output":
                for k in range(w):
                    self.out_port[(t, f"{p}[{k}]" if self._is_bus(t, p) else p)] = e if w == 1 else f"{e}[{k}]"
        self.lut_tiles = sorted((t for t, ty in self.types.items() if ty == "LUT4AB"), key=lambda t: (t[1], t[0]))
        self.io_tiles = sorted((t for t, ty in self.types.items() if ty == "W_IO"), key=lambda t: t[1])
        self.other_tiles = sorted((t for t, ty in self.types.items() if ty not in ("LUT4AB", "W_IO")),
                                  key=lambda t: (t[1], t[0]))
        self.tiles = self.lut_tiles + self.io_tiles + self.other_tiles
        self.R, self.C = 16, 10
        import wio_model as wm
        self.border_in = [f"PAD/{self.out_or_pad(t, p)}" for t in self.io_tiles for p in wm.PADS_IN]
        self.border_out = [self.net(t, o) for t in self.io_tiles for o in wm.PADS_OUT + wm.CBITS]
        nb = len(self.border_in)
        self.W = math.ceil(math.log2(nb + 2))
        codes = {n: j + 1 for j, n in enumerate(self.border_in)}
        self.vecs = [sum(((codes[n] >> b) & 1) << j for j, n in enumerate(self.border_in)) for b in range(self.W)]
        self.vecs += [v ^ ((1 << nb) - 1) for v in self.vecs]
        rnd = random.Random(9100)
        self.vecs += [rnd.getrandbits(nb) for _ in range(n_rand)]
        self.NS = len(self.vecs)
        self.FULL = (1 << self.NS) - 1
        self.invec = {n: sum(((self.vecs[s] >> j) & 1) << s for s in range(self.NS))
                      for j, n in enumerate(self.border_in)}
        self.invec["GND"] = 0
        self.rf_pins = {t: {"D": [self.canon(t, f"D{k}") for k in range(4)],
                            "W": [self.canon(t, f"W_ADR{k}") for k in range(5)],
                            "A": [self.canon(t, f"A_ADR{k}") for k in range(5)],
                            "B": [self.canon(t, f"B_ADR{k}") for k in range(5)],
                            "en": self.canon(t, "W_en")} for t in self.tiles if self.types[t] == "RegFile"}
        self.bram_pins = self._bram_pins() if bram else {}
        self.tainted = self._taint()
        self.route_max_pop = 3000   # fab_router: cap the Dijkstra exploration per route
        self.route_max_reach = 10000  # and the incremental loop check (exact check per config)
        self.loop_max_moves = 20000

    def _bram_pins(self) -> dict:
        """BRAM k -> {"rd_data": [32 model nets: the RAM_IO InPass outputs], "rd_addr", "wr_addr",
        "wr_data", "C": [the RAM_IO OutPass inputs, C0..C5], "tiles"} (eFPGA_top.v wiring)."""
        top = Path("/home/user/ext/demo/Fabric/eFPGA_top.v").read_text()
        inst = re.search(r"\neFPGA eFPGA_inst \((.*?)\n\);", top, re.S).group(1)
        wire = {}   # (bus, index) -> (tile, BEL pin net)
        for p, bus, i in re.findall(r"\.(Tile_X\d+Y\d+_(?:RAM2FAB|FAB2RAM)\w+)\((\w+)\[(\d+)\]\)", inst):
            X, Y, loc = re.match(r"Tile_X(\d+)Y(\d+)_(\w+)", p).groups()
            t = (int(X) - 1, int(Y))
            # RAM2FAB_Dj_Ik: InPass input -> its output Ok (the fabric side); FAB2RAM_xx_Ok:
            # OutPass output -> its input Ik (the fabric side)
            loc = re.sub(r"_I(\d)$", r"_O\1", loc) if loc.startswith("RAM2FAB") else re.sub(r"_O(\d)$", r"_I\1", loc)
            wire[(bus, int(i))] = (t, self.canon(t, loc))
        out = {}
        for k, body in re.findall(r"BlockRAM_1KB Inst_BlockRAM_(\d+) \((.*?)\n\);", top, re.S):
            pins = {}
            for pin, bus, hi, lo in re.findall(r"\.(\w+)\((\w+)\[(\d+)(?::(\d+))?\]\)", body):
                lo = int(lo) if lo else int(hi)
                pins[pin] = [wire[(bus, j)][1] for j in range(lo, int(hi) + 1)]
            pins["C"] = [pins.pop(f"C{j}")[0] for j in range(6)]
            pins["tiles"] = sorted({wire[(bus, int(j))][0] for bus, j in
                                    re.findall(r"\((\w+)\[(\d+)", body) if (bus, int(j)) in wire})
            out[int(k)] = pins
        return out

    def bel_ops(self, t) -> dict:
        """DSP_bot: MULADD at config 0 (Q = A * B + C, combinational). RegFile: op F, read
        port data (fabric.seq_expected: 32 x 4 memory, X until written; two-valued model:
        D); defined once the test config pins W_en and the addresses (fab_router.backbone,
        constant LUTs). RAM_IO (BRAM data) stays opaque."""
        typ = self.types.get(t)
        if typ not in ("DSP_bot", "RegFile") and not (typ == "RAM_IO" and self.bram):
            return {}
        c = self.__dict__.setdefault("_belops", {})
        if t in c:
            return c[t]
        if typ == "RAM_IO":   # BRAM read data (op Q: state; two-valued model: the write data)
            c[t] = {}
            for k, p in self.bram_pins.items():
                for i, n in enumerate(p["rd_data"]):
                    tile, loc = n.split("/", 1)
                    if tile == self.tn(t):
                        c[t][loc] = ("Q", ([p["wr_data"][i]], (k, i)))
            return c[t]
        if typ == "RegFile":
            p = self.rf_pins[t]
            c[t] = {f"{port}D{k}": ("F", ([p["D"][k]] + p[port], (t, port, k))) for port in "AB" for k in range(4)}
            return c[t]
        gt = self.gen["DSP_bot"]
        pin = {p.split(".", 1)[1]: n for n, p in {**gt.bel_in, **gt.bel_out}.items() if "MULADD" in p}
        ins = [self.canon(t, pin[f"{x}{i}"]) for x, w in (("A", 8), ("B", 8), ("C", 20)) for i in range(w)]
        c[t] = {pin[f"Q{k}"]: ("M", (ins, k)) for k in range(20)}
        return c[t]

    def _taint(self) -> set:
        """Nets that carry an unknown value whatever the selects: the BEL outputs of the
        unmodelled tile types (registers X at power-up) and the W_IO registered pad
        inputs, and every net they reach through fixed wiring (direct wires,
        pass-throughs, tile borders). Never selected (fab_router.Graph.forbidden)."""
        import wio_model as wm
        succ: dict = {}

        def edge(a, b):
            succ.setdefault(a, []).append(b)
        for t in self.tiles:
            typ = self.types[t]
            mt = self.MT(t)
            for m in mt.values():
                if m.model == "direct" and m.inputs[0].kind != "const":
                    edge(self.canon(t, m.inputs[0].net), self.canon(t, m.net))
            passes = sc.PASS if typ == "LUT4AB" else {} if typ == "W_IO" else self.gen[typ].passes
            for o, i in passes.items():
                edge(self.canon(t, i), self.canon(t, o))
        seeds = [self.canon(t, n) for t in self.other_tiles for n in self.gen[self.types[t]].bel_out
                 if n not in self.bel_ops(t)]
        seeds += [self.canon(t, q) for t in self.io_tiles for q in wm.Q_SOURCES]
        out, stack = set(seeds), list(seeds)
        while stack:
            n = stack.pop()
            for m in succ.get(n, ()):
                if m not in out:
                    out.add(m)
                    stack.append(m)
        return out

    def _is_bus(self, t, p) -> bool:
        typ = self.types[t]
        hdr = self.tports[typ].get(p)
        if hdr is None:
            return False
        if typ in ("LUT4AB", "W_IO"):
            return hdr[1] > 1 or p in ("Ci", "Co")
        return f"{p}[0]" in (self.gen[typ].tile_inputs | self.gen[typ].tile_outputs)

    def out_or_pad(self, t, p) -> str:
        d = self.in_drv.get((t, p))
        return d[1] if d and d[0] == "PAD" else p

    # ------------------------------------------------------------ tile types

    def ttype(self, t) -> str:
        return self.types[t]

    def generic(self, t):
        return self.gen.get(self.types.get(t))

    def MT(self, t) -> dict:
        typ = self.types[t]
        if typ == "W_IO":
            import wio_model
            return wio_model.MUXES
        if typ == "LUT4AB":
            return fabric.M
        return self.gen[typ].muxes

    @staticmethod
    def tn(t) -> str:
        return f"IOY{t[1]}" if t[0] < 0 else f"X{t[0]}Y{t[1]}"

    def _canon(self, t, n: str) -> str:
        if (t, n) in self.in_drv:
            d = self.in_drv[(t, n)]
            if d is None:
                return "GND"     # undriven input (fabric edge)
            if d[0] == "PAD":
                return f"PAD/{d[1]}"
            return self.canon(d[0], d[1])
        return self.net(t, n)

    def read_inside(self, t, o):
        return True

    def port(self, n: str) -> str:
        if n.startswith("PAD/"):
            return n[4:]
        tname, local = n.split("/", 1)
        t = next(tt for tt in self.tiles if self.tn(tt) == tname)
        return self.out_port[(t, local)]

    # ------------------------------------------------------------ stimulus

    def stim(self, name: str, sels: dict, order: list[int], lut_bits: dict | None = None):
        """Config write through FrameData[512] (row y: bits 32 y ..) and FrameStrobe[200]
        (column X: bits 20 X ..), column by column, then the vectors (eFPGA.v)."""
        import fastsim
        import wio_model as wm
        in_names = [f"FrameData[{k}]" for k in range(512)] + [f"FrameStrobe[{k}]" for k in range(200)] + \
                   [self.port(n) for n in self.border_in] + ["UserCLK"]
        nb = len(self.border_in)
        words = {}
        for t in self.tiles:
            typ = self.types[t]
            if typ == "LUT4AB":
                words[t] = sc.frame_words(sc.config_word(sels[t], (lut_bits or {}).get(t)))
            elif typ == "W_IO":
                words[t] = wm.frame_words(wm.config_word(sels[t], sels[t].get("__cbits", 0)))
            elif self.gen[typ].configmem:
                words[t] = self.gen[typ].frame_words(self.gen[typ].config_word(sels[t]))
        rows, sample = [], []

        def row(fd, strobe, inp):
            r = np.zeros(len(in_names), dtype=np.uint8)
            for y, w in fd.items():
                for k in range(32):
                    r[32 * y + k] = (w >> k) & 1
            if strobe is not None:
                r[512 + 20 * strobe[0] + strobe[1]] = 1
            for j in range(nb):
                r[712 + j] = (inp >> j) & 1
            return r
        fd = {}
        for X in range(10):
            col = [t for t in words if t[0] + 1 == X]
            if not col:
                continue
            for f in order:
                fd = {t[1]: (words[t][f] if f < len(words[t]) else 0) for t in col}
                rows += [row(fd, None, 0), row(fd, (X, f), 0), row(fd, None, 0)]
                sample += [0, 0, 0]
        rows.append(row(fd, None, 0))
        sample.append(0)
        for v in self.vecs:
            rows.append(row(fd, None, v))
            sample.append(1)
        vals = self.run(self.ops(sels))
        expected = ["".join(str((vals[n] >> s) & 1) for n in self.border_out) for s in range(self.NS)]
        return fastsim.Stim(name, in_names, np.array(rows), np.array(sample, dtype=np.uint8),
                            [self.port(n) for n in self.border_out], expected)


if __name__ == "__main__":
    from collections import Counter
    ff = FullFabric()
    print(len(ff.tiles), "tiles", Counter(ff.types.values()))
    print("border in", len(ff.border_in), ff.border_in[:2], "out", len(ff.border_out))
    undriven = sum(1 for v in ff.in_drv.values() if v is None)
    print("tile input bits", len(ff.in_drv), "undriven", undriven)
