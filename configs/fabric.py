#!/usr/bin/env python3
"""Model of an R x C LUT4AB array (Phase 10), wired as rtl/gen_array.py.

Nets: "X<x>Y<y>/<tile net>" (tile nets as configs/sm_configs.py). A tile
input with a neighbour inside the array is the neighbour's output net (the
same wire); one without is a border input. A tile routing output with no
reader inside the array is a border output (observed). The switch matrix,
LUTs (XOR, combinational unless given), MUX8LUT and pass-throughs of every
tile as in the tile model; the array model is their composition, evaluated
bit-parallel over the samples.

Fabric mode (fabric=True, rtl/gen_array.py --fabric): a W_IO column west of
column 0 (tiles (-1, y), named IOY<y>, configs/wio_model.py), N_term above the
north row and S_term below the south row as wires (a north-row <S>END is the
tile's own <N>BEG, bits reversed; south row likewise; south-row Ci = GND), the
east edge open (W*END inputs 0, E*BEG outputs not observed). Border inputs: the
W_IO pads; border outputs: the W_IO pad outputs (I_top, T_top) and
Config_access bits. With at most 10 pads every config applies all 2^pads
input vectors.

Config writing (FABulous frame-based config, ext/demo/Fabric/eFPGA.v):
FrameData_R<y> feeds row y west to east, FrameStrobe_C<x> column x south to
north. A frame write of column x puts every row's word for that column's
tile on FrameData_R<y> and pulses FrameStrobe_C<x>[f]: per column, per frame,
data / strobe / release, as in the single-tile flow.
"""
from __future__ import annotations

import math
import re
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "configs"), str(ROOT / "rtl"), str(ROOT / "sim"), str(ROOT / "sim" / "fastsim")]
import gen_array  # noqa: E402
import sm_configs as sc  # noqa: E402

M = sc.MUXES
REAL = [o for o, m in M.items() if m.model != "direct"]
TILE_IN = set(sc.INPUTS)
READ_BY = {src: (dx, dy, dst) for dst, (dx, dy, src) in gen_array.FROM.items()}


def _deps(op: str, a) -> list[str]:
    if op in ("b", "n"):
        return [a]
    if op in ("c", "q"):
        return []
    if op in ("l", "M"):
        return list(a[0])
    return list(a)


def _muladd(vals: list[int], width: int) -> list[int]:
    """MULADD at config 0 (DSP_bot BEL: no registers, unsigned): Q = A * B + C mod 2^20.
    vals: bit vectors (width lanes) of A0..A7, B0..B7, C0..C19 -> Q0..Q19."""
    import numpy as np
    nby = (width + 7) // 8

    def bits(x: int):
        return np.unpackbits(np.frombuffer(x.to_bytes(nby, "little"), np.uint8), bitorder="little")[:width]
    b = [bits(x).astype(np.int64) for x in vals]
    A = sum(b[i] << i for i in range(8))
    B = sum(b[8 + i] << i for i in range(8))
    C = sum(b[16 + i] << i for i in range(20))
    Q = (A * B + C) & ((1 << 20) - 1)
    return [int.from_bytes(np.packbits(((Q >> k) & 1).astype(np.uint8), bitorder="little").tobytes(), "little")
            for k in range(20)]


def _split(n: str) -> tuple[str, str]:
    m = re.fullmatch(r"(\w+)(\[\d+\])", n)
    return (m.group(1), m.group(2)) if m else (n, "")


class Array:
    def __init__(self, R: int, C: int, n_rand: int = 0, fabric: bool = False):
        """n_rand: random vectors (seeded) after the 2 W code vectors, in the
        model and the stimulus. fabric: W_IO west, N / S term, east open."""
        self.R, self.C = R, C
        self.n_rand = n_rand
        self.fabric = fabric
        self._cmemo: dict = {}
        self._static: dict = {}
        self._mop: dict = {}
        self.name = f"{'fabric' if fabric else 'array'}_{R}x{C}"
        self.width = {n: w for _, w, n in gen_array.tile_ports()}
        self.lut_tiles = [(x, y) for y in range(R) for x in range(C)]
        self.io_tiles = [(-1, y) for y in range(R)] if fabric else []
        self.tiles = self.lut_tiles + self.io_tiles
        if fabric:
            import wio_model as wm
            self.border_in = [self.net(t, p) for t in self.io_tiles for p in wm.PADS_IN]
            self.border_out = [self.net(t, o) for t in self.io_tiles for o in wm.PADS_OUT + wm.CBITS]
        else:
            self.border_in = [self.net(t, p) for t in self.tiles for p in sc.INPUTS
                              if self.canon(t, p) == self.net(t, p)]
            self.border_out = [self.net(t, o) for t in self.tiles for o in sc.OUTPUTS if not self.read_inside(t, o)]
        self.W = math.ceil(math.log2(len(self.border_in) + 2))
        nb = len(self.border_in)
        if fabric and nb <= 10:
            self.vecs = list(range(1 << nb))     # every input combination
        else:
            codes = {n: j + 1 for j, n in enumerate(self.border_in)}
            self.vecs = [sum(((codes[n] >> b) & 1) << j for j, n in enumerate(self.border_in)) for b in range(self.W)]
            full = (1 << nb) - 1
            self.vecs += [v ^ full for v in self.vecs]
        import random
        rnd = random.Random(9000 + R * 100 + C)
        self.vecs += [rnd.getrandbits(nb) for _ in range(n_rand)]
        self.NS = len(self.vecs)
        self.FULL = (1 << self.NS) - 1
        self.invec = {n: sum(((self.vecs[t] >> j) & 1) << t for t in range(self.NS))
                      for j, n in enumerate(self.border_in)}
        self.invec["GND"] = 0

    def MT(self, t) -> dict:
        """Mux table of the tile at t (W_IO or LUT4AB)."""
        if t[0] < 0:
            import wio_model
            return wio_model.MUXES
        return M

    def ttype(self, t) -> str:
        return "W_IO" if t[0] < 0 else "LUT4AB"

    def generic(self, t):
        """configs/tile_model.TileType of a tile of another type (full fabric), else None."""
        return None

    def forbidden_src(self, t, o: str, k: int) -> bool:
        """Code k of mux o of tile t selects a source with an unknown value (never selected):
        the W_IO registered pad inputs; in the full fabric also the BEL outputs of the
        unmodelled tile types and every net they reach through fixed wiring."""
        s = self.MT(t)[o].inputs[k]
        if s.kind == "const":
            return False
        tainted = getattr(self, "tainted", None)
        if tainted is not None and self.canon(t, s.net) in tainted:
            return True
        if self.generic(t) is not None:
            return s.kind == "bel_output" and s.net not in self.bel_ops(t)
        if t[0] < 0:
            import wio_model
            return s.net in wio_model.Q_SOURCES
        return False

    def bel_ops(self, t) -> dict:
        """Modelled BELs of a generic tile: BEL output net -> op (none here)."""
        return {}

    def real(self, t) -> list[str]:
        return [o for o, m in self.MT(t).items() if m.model != "direct"]

    @staticmethod
    def tn(t) -> str:
        return f"IOY{t[1]}" if t[0] < 0 else f"X{t[0]}Y{t[1]}"

    def net(self, t, n: str) -> str:
        return f"{self.tn(t)}/{n}"

    def inside(self, x: int, y: int) -> bool:
        return 0 <= x < self.C and 0 <= y < self.R

    def canon(self, t, n: str) -> str:
        """Array net of tile net n: a driven tile input is the neighbour's output."""
        key = (t, n)
        r = self._cmemo.get(key)
        if r is None:
            r = self._cmemo[key] = self._canon(t, n)
        return r

    def _canon(self, t, n: str) -> str:
        x, y = t
        if x < 0:   # W_IO: the west-going wires come from tile (0, y)
            base, idx = _split(n)
            if base in gen_array.WIO_W:
                return self.canon((0, y), gen_array.WIO_W[base] + idx)
            return self.net(t, n)
        if n in TILE_IN:
            base, idx = _split(n)
            dx, dy, src = gen_array.FROM[base]
            nx, ny = x + dx, y + dy
            if self.inside(nx, ny):
                return self.net((nx, ny), src + idx)
            if self.fabric:
                k = int(idx[1:-1]) if idx else 0
                w = self.width.get(base, 1)
                if base in gen_array.N_TERM and y == 0:
                    return self.canon(t, f"{gen_array.N_TERM[base]}[{w - 1 - k}]")
                if base in gen_array.S_TERM and y == self.R - 1:
                    return self.canon(t, f"{gen_array.S_TERM[base]}[{w - 1 - k}]")
                if base in gen_array.WIO_E and x == 0:
                    return self.net((-1, y), gen_array.WIO_E[base] + idx)
                return "GND"      # south-row Ci (S_term Co0 = GND0), east edge W*END
        return self.net(t, n)

    def read_inside(self, t, o: str) -> bool:
        base, _ = _split(o)
        dx, dy, _dst = READ_BY[base]
        return self.inside(t[0] - dx, t[1] - dy)

    def port(self, n: str) -> str:
        """Array port bit of a border net ("X0Y0/N1END[3]" -> "X0Y0_N1END[3]")."""
        return n.replace("/", "_")

    # ------------------------------------------------------------ model

    def ops(self, sels: dict, luts: dict | None = None) -> list[tuple[str, str, object]]:
        """(net, op, args) of the whole array, topologically sorted.
        sels: tile -> mux selects; luts: tile -> LUT -> (truth table, I0 from
        Ci, FF, reset value), default XOR, I0 = I[0], combinational. ops: b copy,
        c const, x XOR-4 LUT, l LUT (args: index nets I0mux..I3, table), q
        registered LUT output (the flop), m carry majority, s MUX8LUT 2:1."""
        raw = {"GND": ("c", 0)} if self.fabric else {}
        for t in self.tiles:
            sel = sels[t]
            cn = lambda n, t=t: self.canon(t, n)  # noqa: E731
            mop = self._mop
            for o, m in self.MT(t).items():
                k = sel.get(o, 0) if m.model != "direct" else 0
                op = mop.get((t, o, k))
                if op is None:
                    s = m.inputs[k]
                    op = mop[(t, o, k)] = (cn(m.net), ("c", 0 if s.port.startswith("GND") else self.FULL)
                                          if s.kind == "const" else ("b", cn(s.net)))
                raw[op[0]] = op[1]
            gt = self.generic(t)
            if gt is not None:   # other tile types: pass-throughs; BEL outputs never selected
                for o, i in gt.passes.items():
                    raw[cn(o)] = ("b", cn(i))
                bel = self.bel_ops(t)
                for n in gt.bel_out:
                    raw.setdefault(cn(n), bel.get(n, ("c", 0)))
                continue
            if self.ttype(t) == "W_IO":   # W_IO BELs (configs/wio_model.py)
                import wio_model as wm
                for p in "AB":
                    raw[cn(f"{p}_O")] = ("b", cn(f"{p}_O_top"))
                    raw[cn(f"{p}_Q")] = ("c", 0)          # never selected (X until clocked)
                    raw[cn(f"{p}_I_top")] = ("b", cn(f"{p}_I"))
                    raw[cn(f"{p}_T_top")] = ("n", cn(f"{p}_T"))
                cb = sel.get("__cbits", 0)
                for k, n in enumerate(wm.CBITS):
                    raw[cn(n)] = ("c", self.FULL if (cb >> k) & 1 else 0)
                continue
            st = self._static.get(t)
            if st is None:
                st = {}
                for o, i in sc.PASS.items():
                    st[cn(o)] = ("b", cn(i))
                for x in sc.LUTS:
                    st[cn(f"L{x}_O")] = ("x", [cn(f"L{x}_I{j}") for j in range(4)])
                    st[cn(f"L{x}_Co")] = ("m", [cn(p) for p in sc.lut_deps(x)[f"L{x}_Co"]])
                for out, ins in sc.mux8_deps(0, 0).items():
                    st[cn(out)] = ("s", [cn(p) for p in ins])
                self._static[t] = st
            raw.update(st)
            for x in sc.LUTS:
                lc = (luts or {}).get(t, {}).get(x)
                if lc is None:
                    continue
                elif lc[2]:
                    raw[cn(f"L{x}_O")] = ("q", None)
                else:
                    idx = [cn(f"L{x}_Ci") if lc[1] else cn(f"L{x}_I0")] + [cn(f"L{x}_I{j}") for j in (1, 2, 3)]
                    raw[cn(f"L{x}_O")] = ("l", (idx, lc[0]))
        deps = {n: _deps(op, a) for n, (op, a) in raw.items()}
        order, state = [], {}
        for n0 in sorted(raw):
            if n0 in state:
                continue
            stack = [(n0, iter(deps[n0]))]
            state[n0] = 1
            while stack:
                n, it = stack[-1]
                for p in it:
                    if p in raw and p not in state:
                        state[p] = 1
                        stack.append((p, iter(deps[p])))
                        break
                else:
                    stack.pop()
                    state[n] = 2
                    order.append(n)
        return [(n, *raw[n]) for n in order]

    def deps(self, sels: dict, luts: dict | None = None) -> dict[str, list[str]]:
        return {n: _deps(op, a) for n, op, a in self.ops(sels, luts)}

    def cycles(self, sels: dict, luts: dict | None = None) -> list[set[str]]:
        """Combinational loops (luts: LUT configs, e.g. I0 from Ci adds Ci -> O)."""
        from loop_check import tarjan
        d = self.deps(sels, luts)
        comps = tarjan(sorted(d), lambda n: [p for p in d.get(n, ()) if p in d])
        return [set(c) for c in comps if len(c) > 1 or c[0] in d.get(c[0], ())]

    def run(self, ops, forced: str | None = None, fval: int = 0) -> dict[str, int]:
        v = dict(self.invec)
        if forced is not None:
            v[forced] = fval
        F = self.FULL
        mc: dict = {}
        for n, op, a in ops:
            if n == forced:
                continue
            if op == "b":
                v[n] = v[a]
            elif op == "M":
                key = tuple(a[0])
                if key not in mc:
                    mc[key] = _muladd([v[p] for p in key], self.NS)
                v[n] = mc[key][a[1]]
            elif op == "c":
                v[n] = a
            elif op == "n":
                v[n] = ~v[a] & F
            elif op == "x":
                v[n] = v[a[0]] ^ v[a[1]] ^ v[a[2]] ^ v[a[3]]
            elif op == "m":
                x, y, z = v[a[0]], v[a[1]], v[a[2]]
                v[n] = (x & y) | (x & z) | (y & z)
            else:
                x, y, s = v[a[0]], v[a[1]], v[a[2]]
                v[n] = (s & y) | (~s & F & x)
        return v

    def break_loops(self, sels: dict, max_moves: int = 2000, luts: dict | None = None) -> list:
        """Move one switch-matrix mux per array-level loop to its next input
        (smallest fan-in first, as sm_configs.break_loops) until none is left."""
        log, moves = [], {}
        for _ in range(max_moves):
            comps = self.cycles(sels, luts)
            if not comps:
                return log
            comp = comps[0]
            cand = []
            for t in self.tiles:
                for o in self.real(t):
                    m = self.MT(t)[o]
                    if self.canon(t, m.net) not in comp:
                        continue
                    s = m.inputs[sels[t][o]]
                    if s.kind != "const" and self.canon(t, s.net) in comp and moves.get((t, o), 0) < m.fan_in - 1:
                        cand.append((m.fan_in, self.tn(t), o, t))
            if not cand:
                raise SystemExit(f"array loop {sorted(comp)[:6]} has no movable mux")
            _, _, o, t = min(cand)
            old = sels[t][o]
            fan = self.MT(t)[o].fan_in
            k = (old + 1) % fan
            while self.forbidden_src(t, o, k) and k != old:   # never move onto an unknown source
                k = (k + 1) % fan
                moves[(t, o)] = moves.get((t, o), 0) + 1
            sels[t][o] = k
            moves[(t, o)] = moves.get((t, o), 0) + 1
            log.append((self.tn(t), o, old, sels[t][o], len(comp)))
        raise SystemExit("array loop breaking did not converge")

    # ------------------------------------------------------------ stimulus

    def stim(self, name: str, sels: dict, order: list[int], lut_bits: dict | None = None):
        """fastsim.Stim for one array config: config write, then the 2 W vectors."""
        import fastsim
        R, C = self.R, self.C
        ncw = 20 if self.fabric else 0
        in_names = [f"FrameData_R{y}[{k}]" for y in range(R) for k in range(32)] + \
                   [f"FrameStrobe_C{x}[{k}]" for x in range(C) for k in range(20)] + \
                   [f"FrameStrobe_CW[{k}]" for k in range(ncw)] + \
                   [self.port(n) for n in self.border_in] + ["UserCLK"]
        nb = len(self.border_in)
        words = {t: sc.frame_words(sc.config_word(sels[t], (lut_bits or {}).get(t))) for t in self.lut_tiles}
        if self.fabric:
            import wio_model as wm
            for t in self.io_tiles:
                words[t] = wm.frame_words(wm.config_word(sels[t], sels[t].get("__cbits", 0)))
        rows = []

        def row(fd: dict[int, int], strobe: tuple[int, int] | None, inp: int, clk: int = 0):
            r = np.zeros(len(in_names), dtype=np.uint8)
            for y, w in fd.items():
                for k in range(32):
                    r[y * 32 + k] = (w >> k) & 1
            if strobe:
                x, f = strobe
                r[R * 32 + (C if x < 0 else x) * 20 + f] = 1
            base = R * 32 + C * 20 + ncw
            for j in range(nb):
                r[base + j] = (inp >> j) & 1
            r[-1] = clk
            return r

        sample = []
        fd = {y: 0 for y in range(R)}
        if self.fabric:   # the W_IO column first (frames 0..3)
            import wio_model as wm
            for f in [f for f in order if f < wm.N_FRAMES]:
                fd = {y: words[(-1, y)][f] for y in range(R)}
                rows += [row(fd, None, 0), row(fd, (-1, f), 0), row(fd, None, 0)]
                sample += [0, 0, 0]
        for x in range(C):
            for f in order:
                fd = {y: words[(x, y)][f] for y in range(R)}
                rows += [row(fd, None, 0), row(fd, (x, f), 0), row(fd, None, 0)]
                sample += [0, 0, 0]
        rows.append(row(fd, None, 0))
        sample.append(0)
        for v in self.vecs:
            rows.append(row(fd, None, v))
            sample.append(1)
        vals = self.run(self.ops(sels))
        expected = ["".join(str((vals[n] >> t) & 1) for n in self.border_out) for t in range(self.NS)]
        return fastsim.Stim(name, in_names, np.array(rows), np.array(sample, dtype=np.uint8),
                            [self.port(n) for n in self.border_out], expected)


# ---------------------------------------------------------------- grading

def work_items(o: str, k: int) -> set:
    """Model-level items of code k of LUT4AB mux o (as configs/sm_router.work_items)."""
    import sm_router
    return sm_router.work_items(o, k)


def work_items_m(m, k: int) -> set:
    """sm_router.work_items for any mux object (LUT4AB or W_IO)."""
    def cv(s):
        return 0 if s.port.startswith("GND") else 1
    s = m.inputs[k]
    items = set()
    if s.kind != "const" or cv(s) == 0:
        items.add("t0")
    if s.kind != "const" or cv(s) == 1:
        items.add("t1")
    for i in range(len(m.sel)):
        k2 = k ^ (1 << i)
        if k2 >= m.fan_in:
            continue
        s2 = m.inputs[k2]
        if s.kind == "const" and s2.kind == "const" and cv(s) == cv(s2):
            continue
        items.add(i)
    return items


GRADE_CHUNK = 1500   # muxes per pass: the lane vectors are (chunk + 1) x samples bits wide


def grade(arr: Array, sels: dict, only: list | None = None) -> dict:
    """grade_chunk over chunks of at most GRADE_CHUNK muxes (memory)."""
    ops = arr.ops(sels)
    vals = {n for n, _, _ in ops}
    muxes = [(t, o) for t in arr.tiles for o, m in arr.MT(t).items() if arr.canon(t, m.net) in vals] \
        if only is None else only
    shown = {}
    for c0 in range(0, len(muxes), GRADE_CHUNK):
        shown.update(grade_chunk(arr, sels, muxes[c0:c0 + GRADE_CHUNK], ops))
    return shown


def grade_chunk(arr: Array, sels: dict, only: list, ops=None) -> dict:
    """(tile, mux, code) -> items shown, exact at model level, for every mux
    (or the (tile, mux) pairs in `only`) in one pass: lane block 0 is the
    fault-free array, block i + 1 flips the output net of mux i at every
    sample (Python ints as wide bit vectors: samples x blocks)."""
    ops = ops or arr.ops(sels)
    muxes = only
    NS, FULL = arr.NS, arr.FULL
    nb = len(muxes) + 1
    REP = sum(1 << (b * NS) for b in range(nb))
    ALL = FULL * REP
    flip = {}
    for i, (t, o) in enumerate(muxes):
        n = arr.canon(t, arr.MT(t)[o].net)
        flip[n] = flip.get(n, 0) | (FULL << ((i + 1) * NS))
    v = {n: val * REP for n, val in arr.invec.items()}
    mc: dict = {}
    for n, op, a in ops:
        if op == "b":
            r = v[a]
        elif op == "M":
            key = tuple(a[0])
            if key not in mc:
                mc[key] = _muladd([v[p] for p in key], NS * nb)
            r = mc[key][a[1]]
        elif op == "c":
            r = ALL if a else 0
        elif op == "n":
            r = ~v[a] & ALL
        elif op == "x":
            r = v[a[0]] ^ v[a[1]] ^ v[a[2]] ^ v[a[3]]
        elif op == "m":
            x, y, z = v[a[0]], v[a[1]], v[a[2]]
            r = (x & y) | (x & z) | (y & z)
        else:
            x, y, s = v[a[0]], v[a[1]], v[a[2]]
            r = (s & y) | (~s & ALL & x)
        if n in flip:
            r ^= flip[n]
        v[n] = r
    diff = 0
    for n in arr.border_out:
        w = v[n]
        diff |= w ^ ((w & FULL) * REP)
    base = {n: w & FULL for n, w in v.items()}
    shown = {}
    for i, (t, o) in enumerate(muxes):
        ob = (diff >> ((i + 1) * NS)) & FULL
        if not ob:
            continue
        m = arr.MT(t)[o]
        k = sels[t].get(o, 0) if m.model != "direct" else 0
        s = m.inputs[k]

        def val(src):
            return (0 if src.port.startswith("GND") else FULL) if src.kind == "const" else base[arr.canon(t, src.net)]
        a = val(s)
        items = set()
        if ob & ~a & FULL:
            items.add("t0")
        if ob & a:
            items.add("t1")
        for b in range(len(m.sel)):
            k2 = k ^ (1 << b)
            if k2 < m.fan_in and ob & (a ^ val(m.inputs[k2])):
                items.add(b)
        shown[(t, o, k)] = items
    return shown


# ---------------------------------------------------------------- clocked (registered LUTs)

def lut_index_nets(arr: Array, t, x: str, i0ci: int) -> list[str]:
    return [arr.canon(t, f"L{x}_Ci") if i0ci else arr.canon(t, f"L{x}_I0")] + \
        [arr.canon(t, f"L{x}_I{j}") for j in (1, 2, 3)]


def _lut3(tt: int, ins: list[tuple[int, int]]) -> tuple[int, int]:
    """Dual-rail LUT: any X input -> X (as pessimistic as the gate-level mux tree)."""
    if any(h and l for h, l in ins):
        return (1, 1)
    b = (tt >> sum(h << j for j, (h, _l) in enumerate(ins))) & 1
    return (b, 1 - b)


def seq_expected(arr: Array, sels: dict, plan: dict, vecs: list[int],
                 luts: dict | None = None, partial: bool = False) -> tuple[list[int], list[str]]:
    """Clocked array: per vector inputs, UserCLK rising edge, sample.
    luts: tile -> LUT -> (truth table, I0 from Ci, FF, reset value); default
    (from plan, the clocked SR / EN configs): XOR, I0 = I[0], registered,
    reset value plan[t][x][2]. Flop: EN ? (SR ? reset value : LUT out) : hold.
    Dual-rail 3-valued (h, l), at least as pessimistic as the gates (an X
    select, EN, SR or LUT input gives X). Returns (sample flag per vector,
    expected strings): a vector is sampled once every flop of a registered
    LUT is defined. partial: every vector with a defined border output is
    sampled, X outputs as "x" (not compared)."""
    if luts is None:
        luts = {t: {x: (sc.cell.XOR_TT, 0, 1, plan[t][x][2]) for x in sc.LUTS} for t in arr.lut_tiles}
    ops = arr.ops(sels, luts)
    q = {(t, x): (1, 1) for t in arr.lut_tiles for x in sc.LUTS}
    flop_of = {arr.canon(t, f"L{x}_O"): (t, x) for t in arr.lut_tiles for x in sc.LUTS}
    regs = [(t, x) for t in arr.lut_tiles for x in sc.LUTS if luts[t][x][2]]

    def comb(inp: int):
        v = {}
        for j, n in enumerate(arr.border_in):
            b = (inp >> j) & 1
            v[n] = (b, 1 - b)
        v["GND"] = (0, 1)
        mc: dict = {}
        for n, op, a in ops:
            if op == "n":
                v[n] = v[a][::-1]
            elif op == "M":   # any X input: every output X (at least as pessimistic as the gates)
                key = tuple(a[0])
                if key not in mc:
                    iv = [v[p] for p in key]
                    mc[key] = [(1, 1)] * 20 if any(h and l for h, l in iv) else \
                        [(q, 1 - q) for q in _muladd([h for h, _ in iv], 1)]
                v[n] = mc[key][a[1]]
            elif op == "q":
                v[n] = q[flop_of[n]]
            elif op == "b":
                v[n] = v[a]
            elif op == "c":
                v[n] = (1, 0) if a else (0, 1)
            elif op == "l":
                v[n] = _lut3(a[1], [v[p] for p in a[0]])
            elif op == "x":
                h, l = v[a[0]]
                for p in a[1:]:
                    h2, l2 = v[p]
                    h, l = (h & l2) | (l & h2), (h & h2) | (l & l2)
                v[n] = (h, l)
            elif op == "m":
                (h1, l1), (h2, l2), (h3, l3) = (v[p] for p in a)
                v[n] = ((h1 & h2) | (h1 & h3) | (h2 & h3), (l1 & l2) | (l1 & l3) | (l2 & l3))
            else:
                (ha, la), (hb, lb), (hs, ls) = (v[p] for p in a)
                # X select: X (at least as pessimistic as the gates)
                v[n] = (1, 1) if hs and ls else ((hs & hb) | (ls & ha), (hs & lb) | (ls & la))
        return v

    flags, exp = [], []
    for inp in vecs:
        v = comb(inp)
        nq = {}
        for (t, x), old in q.items():
            tt, i0ci, _ff, rv = luts[t][x]
            d = _lut3(tt, [v[n] for n in lut_index_nets(arr, t, x, i0ci)])
            sh, sl = v[arr.canon(t, f"L{x}_SR")]
            load = (1, 1) if sh and sl else ((rv, 1 - rv) if sh else d)
            eh, el = v[arr.canon(t, f"L{x}_EN")]
            nq[(t, x)] = (1, 1) if eh and el else (load if eh else old)
        q = nq
        if partial:
            v = comb(inp)
            e = "".join("x" if v[n][0] and v[n][1] else str(v[n][0]) for n in arr.border_out)
            flags.append(int(e.count("x") < len(e)))
            if flags[-1]:
                exp.append(e)
            continue
        ready = all(not (q[r][0] and q[r][1]) for r in regs)
        flags.append(int(ready))
        if ready:
            v = comb(inp)
            if any(v[n][0] and v[n][1] for n in arr.border_out):
                raise RuntimeError("defined flops but X at a border output")
            exp.append("".join(str(v[n][0]) for n in arr.border_out))
    return flags, exp


def stim_clocked(arr: Array, name: str, sels: dict, order: list[int], plan: dict, vecs: list[int],
                 luts: dict | None = None, partial: bool = False):
    """fastsim.Stim: config write (LUTs registered, reset values from plan), then per
    vector: inputs, UserCLK 1, UserCLK 0 + sample (sampled once every flop is defined)."""
    import fastsim
    if luts is None:
        luts = {t: {x: (sc.cell.XOR_TT, 0, 1, plan[t][x][2]) for x in sc.LUTS} for t in arr.lut_tiles}
    lut_bits = {t: {x: sc.cell.make_bits(tt, ff=ff, i0_from_ci=i0, reset_value=rv)
                    for x, (tt, i0, ff, rv) in luts[t].items()} for t in arr.lut_tiles}
    base = arr.stim(name, sels, order, lut_bits)
    n_cfg = int(np.argmax(base.sample))           # config-write steps (the first sample is the first vector)
    rows = [base.rows[k] for k in range(n_cfg)]
    sample = [0] * n_cfg
    nb, col0 = len(arr.border_in), base.in_names.index(arr.port(arr.border_in[0]))
    flags, exp = seq_expected(arr, sels, plan, vecs, luts, partial)
    for v, f in zip(vecs, flags):
        r = base.rows[n_cfg - 1].copy()
        for j in range(nb):
            r[col0 + j] = (v >> j) & 1
        r0, r1 = r.copy(), r.copy()
        r1[-1] = 1
        rows += [r0, r1, r0.copy()]
        sample += [0, 0, f]
    return fastsim.Stim(name, base.in_names, np.array(rows), np.array(sample, dtype=np.uint8),
                        base.out_names, exp)
