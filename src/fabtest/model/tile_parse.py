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
from collections import deque
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
    """tile config bit -> (frame, FrameData bit)."""
    body = path.read_text().split("`else", 1)[1]
    out = {}
    for _inst, d, s, q, qn in LATCH_RE.findall(body):
        if q != qn:
            raise SystemExit(f"{path}: Q/QN index mismatch for bit {q}")
        out[int(q)] = (int(s), int(d))
    return out


@dataclass
class Mux:
    out: str            # switch-matrix port driven
    inputs: list[str]   # switch-matrix ports, A0 first
    sel: list[int]      # switch-matrix local config bits, S0 first
    kind: str           # "direct" or the cus_mux module name

    def select_for(self, k: int) -> dict[int, int]:
        """Local config bit values that make input k drive the output."""
        if self.kind == "direct":
            if k != 0:
                raise ValueError("direct wire has one input")
            return {}
        n = len(self.inputs)
        if n != 1 << len(self.sel):
            raise SystemExit(f"{self.out}: {n} inputs for {len(self.sel)} select bits")
        # cus_mux21: S ? A1 : A0; cus_mux41/81/161: binary S[n-1..0] picks A[S]
        # (models_pack.v: S0 selects within pairs, S1 the pair, S2/S3 above).
        return {b: (k >> i) & 1 for i, b in enumerate(self.sel)}


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
        inst = re.search(r"(cus_mux\d+)\s+\w+\s*\((.*?)\);", body, re.S)
        if not a or not inst:
            raise SystemExit(f"{out}: cannot parse MUX-{n}")
        vec = [s.strip() for s in a.group(1).split(",")]
        inputs = list(reversed(vec))  # {msb, ..., lsb}: A0 = lsb
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

    def tile_bits_for(self, sm_bits: dict[int, int]) -> dict[int, int]:
        return {self.sm_base + b: v for b, v in sm_bits.items()}


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

def max_flow_paths(edges: list[tuple[str, str]], sources: list[str], sinks: list[str],
                   node_cap: dict[str, int] | None = None) -> list[list[str]]:
    """Vertex-disjoint paths (unit node capacity) from distinct sources to sinks.

    Edmonds-Karp on the split-node graph. Returns one path per sink reached.
    """
    INF = 10 ** 9
    cap: dict[tuple[str, str], int] = {}
    # dicts as ordered sets: the result must not depend on hash order
    adj: dict[str, dict[str, None]] = {}

    def add(u: str, v: str, c: int) -> None:
        cap[(u, v)] = cap.get((u, v), 0) + c
        cap.setdefault((v, u), 0)
        adj.setdefault(u, {})[v] = None
        adj.setdefault(v, {})[u] = None

    nodes = list(dict.fromkeys([u for e in edges for u in e] + list(sources) + list(sinks)))
    for n in nodes:
        add(f"{n}#in", f"{n}#out", (node_cap or {}).get(n, 1))
    for u, v in edges:
        add(f"{u}#out", f"{v}#in", 1)
    for s in sources:
        add("SRC", f"{s}#in", 1)
    for t in sinks:
        add(f"{t}#out", "SNK", 1)

    flow = 0
    while True:
        prev = {"SRC": None}
        q = deque(["SRC"])
        while q and "SNK" not in prev:
            u = q.popleft()
            for v in adj.get(u, ()):
                if v not in prev and cap[(u, v)] > 0:
                    prev[v] = u
                    q.append(v)
        if "SNK" not in prev:
            break
        v = "SNK"
        while prev[v] is not None:
            u = prev[v]
            cap[(u, v)] -= 1
            cap[(v, u)] += 1
            v = u
        flow += 1

    # Recover paths from residual capacities on original edges.
    used = {(u, v) for (u, v) in edges if cap[(f"{u}#out", f"{v}#in")] == 0}
    succ: dict[str, str] = {}
    for u, v in used:
        succ.setdefault(u, v)
    paths = []
    for s in sources:
        if cap[("SRC", f"{s}#in")] != 0:
            continue
        path, n = [s], s
        while n not in sinks:
            n = succ[n]
            path.append(n)
        paths.append(path)
    if len(paths) != flow:
        raise SystemExit("path recovery failed")
    return paths
