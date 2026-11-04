"""Fault -> switch-matrix mux, structurally (gate-level netlists keep no cell
hierarchy, only some net names).

Walk forward from the fault net through the netlist until the nearest net
that names a mux: an internal mux net (`inst_cus_mux<n>_<OUT>.B0`, ...), a
switch-matrix output (`..._switch_matrix.<OUT>`), the tile net the mux
drives, or a config latch output (`ConfigBits[b]`, owner from the select
bit). One mux at the nearest distance: that mux. Several: a net shared by
several muxes (a mux input net). None within the depth limit: unowned.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / d) for d in ("sim", "configs", "faults", "rtl")]
import conesplit as cs  # noqa: E402
import sm_model  # noqa: E402

MUXES = sm_model.MUXES
SEL_OWNER = {s.tile_bit: o for o, m in MUXES.items() for s in m.sel}
NET_OWNER = {}
for _o, _m in MUXES.items():
    NET_OWNER[_m.net] = _o
    NET_OWNER[f"Inst_LUT4AB.{_m.net}"] = _o
_INT = re.compile(r"switch_matrix\.inst_cus_mux\d+_(\w+?)\.(?:B\d|cus_mux)")
_OUT = re.compile(r"switch_matrix\.(\w+)$")
_CB = re.compile(r"^Inst_LUT4AB\.ConfigBits\[(\d+)\]$")
# LUT input pins: the I mux output net keeps only the BEL pin name
_LUTI = re.compile(r"^Inst_LUT4AB\.Inst_(L[A-H])_LUT4c_frame_config_dffesr\.I\[(\d)\]$")


class Owner:
    def __init__(self, netlist: str):
        self.g = cs.load_graph(ROOT / "synth" / f"netlist_{netlist}.json")
        self.names: dict[int, list[str]] = {}
        for nm, b in self.g.net_bit.items():
            self.names.setdefault(b, []).append(nm)

    def anchors(self, b: int) -> set[str]:
        """Muxes named by net b; a mux's own output name wins over the
        internal names of muxes reading the net."""
        outs = set()
        for nm in self.names.get(b, ()):
            m = _OUT.search(nm)
            if m and m.group(1) in MUXES:
                outs.add(m.group(1))
            elif nm in NET_OWNER:
                outs.add(NET_OWNER[nm])
        if outs:
            return outs
        s = set()
        for nm in self.names.get(b, ()):
            m = _CB.match(nm)
            if m:
                s.add(SEL_OWNER.get(int(m.group(1)), "BEL config"))
                continue
            m = _INT.search(nm)
            if m and m.group(1) in MUXES:
                s.add(m.group(1))
                continue
            m = _OUT.search(nm)
            if m and m.group(1) in MUXES:
                s.add(m.group(1))
                continue
            if nm in NET_OWNER:
                s.add(NET_OWNER[nm])
                continue
            m = _LUTI.match(nm)
            if m:
                s.add(f"{m.group(1)}_I{m.group(2)}")
        return s

    def of_net(self, net: str, maxd: int = 8) -> set[str]:
        b = self.g.net_bit.get(net)
        if b is None:
            return set()
        front, seen = {b}, {b}
        for _ in range(maxd):
            a = set().union(*(self.anchors(x) for x in front))
            if a:
                return a
            nxt = set()
            for x in front:
                for c in self.g.readers.get(x, ()):
                    o = self.g.cells[c]["out"]
                    if o not in seen:
                        seen.add(o)
                        nxt.add(o)
            front = nxt
            if not front:
                break
        return set()

    def of_fault(self, f: dict) -> str:
        """Mux name, 'shared input', or 'unowned'. Branch faults: the reading cell's output."""
        if f.get("type") == "branch":
            stem = self._pick(self.of_net(f["path"].split("__br", 1)[0]))
            if stem not in ("unowned", "shared input"):
                return stem
        if f.get("pin") and not f["pin"].startswith("port:") and "." in f["pin"]:
            cell = f["pin"].rsplit(".", 1)[0]
            if cell in self.g.cells:
                a = self.of_net_bit(self.g.cells[cell]["out"])
                return self._pick(a)
        return self._pick(self.of_net(f["net"]))

    def of_net_bit(self, b: int) -> set[str]:
        for nm in self.names.get(b, ()):
            return self.of_net(nm)
        return set()

    @staticmethod
    def _pick(a: set[str]) -> str:
        if not a:
            return "unowned"
        return next(iter(a)) if len(a) == 1 else "shared input"
