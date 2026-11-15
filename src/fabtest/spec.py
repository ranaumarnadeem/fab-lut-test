"""FabricSpec: what fabtest knows about a FABulous fabric (fabtest P1).

Built by fabtest/project.py from FABulous's parser output (fabtest/fabulous_loader.py)
plus the generated top-level RTL of the project copy:
  - params: rows, cols, frame_bits (32), frames (20); FrameData / FrameStrobe widths
  - grid[y][x]: tile type name or None (FABulous coordinates, X east, Y south)
  - types: per tile type its directory and BELs (module, prefix, ports, external ports,
    config bits, feature names)
  - supertiles: name -> {dir, map}
  - efpga: the eFPGA ports (direction, width) from Fabric/eFPGA.v
  - top: from Fabric/eFPGA_top.v, the chip pins and, per eFPGA port bit, what drives or
    reads it: a chip pin bit ("pin", "I_top[3]") or a wire to macro instances ("wire",
    "RAM2FAB_D_I[5]"); the macro instances (module, pin -> wire bits)
Saved as spec.json; load() reads it back.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

CFG_MODULES = {"eFPGA", "eFPGA_Config", "Frame_Data_Reg", "Frame_Select"}


def _strip(text: str) -> str:
    return re.sub(r"/\*.*?\*/", "", re.sub(r"//[^\n]*", "", text), flags=re.S)


def ports(text: str, mod: str, params: dict[str, int]) -> dict[str, tuple[str, int]]:
    """port -> (direction, width) of module `mod`; parameter names in ranges from `params`
    and from the module's own parameter defaults."""
    text = _strip(text)
    m = re.search(rf"module\s+{mod}\b\s*(#\s*\((.*?)\))?\s*\((.*?)\);", text, re.S)
    if not m:
        raise SystemExit(f"fabtest: module {mod} not found")
    env = dict(params)
    for k, v in re.findall(r"parameter\s+(?:integer\s+)?(\w+)\s*=\s*(\d+)", m.group(2) or ""):
        env.setdefault(k, int(v))
    out = {}
    for d, hi, lo, n in re.findall(r"(input|output|inout)\s+(?:wire\s+|reg\s+)?(?:\[\s*([^:\]]+):\s*([^\]]+)\s*\]\s*)?(\w+)", m.group(3)):
        w = 1
        if hi:
            w = eval(hi, {"__builtins__": {}}, env) - eval(lo, {"__builtins__": {}}, env) + 1
        out[n] = (d, w)
    return out


def instances(text: str) -> list[tuple[str, str, dict[str, str]]]:
    """(module, instance, {port: expression}) of every instance in a module body."""
    text = _strip(text)
    text = re.sub(r"`ifdef EMULATION.*?`endif", "", text, flags=re.S)
    body = text.split(");", 1)[1] if ");" in text else text
    out = []
    for typ, inst, conns in re.findall(r"\n\s*(\w+)\s*(?:#\s*\(.*?\)\s*)?(\w+)\s*\(\s*(\..*?)\);", body, re.S):
        if typ in ("module", "assign", "wire", "reg", "input", "output"):
            continue
        out.append((typ, inst, dict(re.findall(r"\.(\w+)\(\s*([^()]*?)\s*\)", conns))))
    return out


def _bits(expr: str, widths: dict[str, int]) -> list[str]:
    """`bus[hi:lo]`, `bus[i]` or `name` -> bit names, LSB first."""
    m = re.fullmatch(r"(\w+)\s*\[\s*(\d+)\s*:\s*(\d+)\s*\]", expr)
    if m:
        hi, lo = int(m.group(2)), int(m.group(3))
        return [f"{m.group(1)}[{k}]" for k in range(lo, hi + 1)]
    m = re.fullmatch(r"(\w+)\s*\[\s*(\d+)\s*\]", expr)
    if m:
        return [f"{m.group(1)}[{m.group(2)}]"]
    if re.fullmatch(r"\w+", expr):
        w = widths.get(expr, 1)
        return [expr] if w == 1 else [f"{expr}[{k}]" for k in range(w)]
    return []


def build(raw: dict, proj: Path) -> dict:
    """raw: fabulous_loader output -> full spec dict (adds efpga and top)."""
    p = raw["params"]
    params = {"FrameBitsPerRow": p["frame_bits"], "MaxFramesPerCol": p["frames"],
              "NumberOfRows": p["rows"] - 2, "NumberOfCols": p["cols"]}
    efpga_v = proj / "Fabric" / "eFPGA.v"
    top_v = proj / "Fabric" / "eFPGA_top.v"
    for f in (efpga_v, top_v):
        if not f.exists():
            raise SystemExit(f"fabtest: {f} missing: run FABulous on the project first "
                             f"(FABulous -p <project> run \"load_fabric; run_fab\")")
    efpga = ports(efpga_v.read_text(), "eFPGA", params)
    top_text = top_v.read_text()
    top_ports = ports(top_text, "eFPGA_top", params)
    insts = instances(top_text)
    fab = [c for t, i, c in insts if t == "eFPGA"]
    if len(fab) != 1:
        raise SystemExit("fabtest: eFPGA_top.v: no single eFPGA instance")
    port_map = {}
    for port, expr in fab[0].items():
        if port not in efpga:
            continue
        mine = [f"{port}[{k}]" for k in range(efpga[port][1])] if efpga[port][1] > 1 else [port]
        theirs = _bits(expr, {n: w for n, (_, w) in top_ports.items()})
        for a, b in zip(mine, theirs):
            base = b.split("[")[0]
            port_map[a] = {"pin": b} if base in top_ports else {"wire": b}
    macros = []
    for typ, inst, conns in insts:
        if typ in CFG_MODULES:
            continue
        macros.append({"module": typ, "inst": inst,
                       "pins": {pin: _bits(expr, {}) for pin, expr in conns.items()}})
    spec = dict(raw)
    spec["params"] = dict(p, frame_data=p["frame_bits"] * p["rows"], frame_strobe=p["frames"] * p["cols"])
    spec["efpga"] = {"path": "Fabric/eFPGA.v", "ports": {n: list(v) for n, v in efpga.items()}}
    spec["top"] = {"path": "Fabric/eFPGA_top.v", "ports": {n: list(v) for n, v in top_ports.items()},
                   "port_map": port_map, "macros": macros}
    return spec


class Spec:
    def __init__(self, d: dict, work: Path):
        self.d, self.work = d, work
        self.proj = Path(d["project"])
        self.params = d["params"]
        self.rows, self.cols = self.params["rows"], self.params["cols"]
        self.grid = d["grid"]
        self.types = d["types"]
        self.supertiles = d["supertiles"]

    def tile(self, x: int, y: int):
        return self.grid[y][x]

    def tiles(self):
        for y, row in enumerate(self.grid):
            for x, t in enumerate(row):
                if t is not None:
                    yield (x, y), t

    def type_dir(self, name: str) -> Path:
        return Path(self.types[name]["dir"])

    def bels(self, name: str) -> list[dict]:
        return self.types[name]["bels"]

    def summary(self) -> str:
        from collections import Counter
        c = Counter(t for _, t in self.tiles())
        return (f"{self.rows} x {self.cols} tiles ({', '.join(f'{k} {v}' for k, v in sorted(c.items()))}); "
                f"FrameData {self.params['frame_data']}, FrameStrobe {self.params['frame_strobe']}; "
                f"{len(self.supertiles)} supertiles; {len(self.d['top']['macros'])} macros in eFPGA_top")


def load(work: Path) -> Spec:
    return Spec(json.loads((work / "spec.json").read_text()), work)
