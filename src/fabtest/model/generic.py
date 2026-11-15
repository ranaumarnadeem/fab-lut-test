"""GenericFabric: the fabric model from a fabtest spec and the BEL plugins (fabtest P3).

configs/fullfabric.FullFabric with the tile-type checks replaced by the plugin registry
(fabtest/bels): BEL ops, RegFile pins and macro pins (BlockRAM_1KB) come from the plugins
matched to the spec's BELs and eFPGA_top macros; a BEL or macro no plugin models stays
opaque (outputs tainted). The lut / io kinds still need the type names LUT4AB / W_IO.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from fabtest.paths import REPO as ROOT  # noqa: E402

from fabtest.model import fullfabric  # noqa: E402

from fabtest import bels as belreg  # noqa: E402
from fabtest.bels import base, std  # noqa: E402


class GenericFabric(fullfabric.FullFabric):
    def __init__(self, spec, n_rand: int = 54, macros: bool = True):
        self.spec = spec
        self.kinds = belreg.classify(spec)
        for typ, k in self.kinds.items():
            want = {"lut": "LUT4AB", "io": "W_IO"}.get(k["kind"])
            if want and typ != want:
                raise SystemExit(f"fabtest: tile type {typ} is a {want} but named otherwise (not supported yet)")
            if typ in ("LUT4AB", "W_IO") and k["kind"] == "generic":
                raise SystemExit(f"fabtest: {typ} {k['why']} (not supported yet)")
        self._macros_on = macros
        super().__init__(n_rand=n_rand, bram=macros and bool(spec.d["top"]["macros"]), spec=spec)
        self.unmodelled = self._unmodelled()

    # ------------------------------------------------------------ plugins

    def bels(self, typ: str) -> list:
        c = self.__dict__.setdefault("_bels", {})
        if typ not in c:
            gt = self.gen.get(typ)
            c[typ] = [] if gt is None else [(belreg.for_module(b.module), b)
                                            for b in base.bels_of(gt, self.spec.types[typ])]
            if self.kinds[typ]["kind"] == "generic" and self.kinds[typ]["why"]:
                c[typ] = [(belreg.OPAQUE, b) for _, b in c[typ]]
        return c[typ]

    def _rf_pins(self) -> dict:
        out = {}
        for t in self.tiles:
            for plug, b in self.bels(self.types[t]):
                p = plug.pins(self, t, b)
                if p is not None and plug.name == "regfile32x4":
                    out[t] = p
        return out

    def macro_net(self, wire: str, fabric_out: bool) -> str:
        """Fabric net of a macro pin wire: the eFPGA port on that wire, at the fabric side of
        the pass BEL behind it."""
        inv = self.__dict__.setdefault("_wire_port", {m["wire"]: p for p, m in self.spec.d["top"]["port_map"].items()
                                                       if "wire" in m})
        port = inv[wire]
        X, Y, loc = re.match(r"Tile_X(\d+)Y(\d+)_(\w+)", port).groups()
        t = (int(X) - 1, int(Y))
        typ = self.types[t]
        for b in self.spec.types[typ]["bels"]:
            if loc in b["ext_in"] or loc in b["ext_out"]:
                plug = belreg.for_module(b["module"])
                if not hasattr(plug, "fabric_side"):
                    raise KeyError(loc)
                return self.canon(t, plug.fabric_side(b, loc))
        raise KeyError(loc)

    def tile_of(self, net: str):
        tn = net.split("/", 1)[0]
        return next(t for t in self.tiles if self.tn(t) == tn)

    def _bram_pins(self) -> dict:
        """Macro index (spec order, per plugin) -> bound pins; only BlockRAM_1KB so far."""
        out = {}
        self.macro_status = {}
        k = 0
        for m in self.spec.d["top"]["macros"]:
            plug = std.macro_for(m["module"])
            data = plug.bind(self, m) if plug else None
            self.macro_status[m["inst"]] = plug.name if data else "opaque"
            if data is not None and plug.name == "blockram_1kb":
                out[k] = data
                k += 1
        return out

    def bel_ops(self, t) -> dict:
        c = self.__dict__.setdefault("_belops", {})
        if t in c:
            return c[t]
        typ = self.types.get(t)
        if typ is None or self.kinds.get(typ, {}).get("kind") != "generic":
            return {}
        ops = {}
        for plug, b in self.bels(typ):
            ops.update(plug.ops(self, t, b))
        tn = self.tn(t)
        for k, p in getattr(self, "bram_pins", {}).items():
            if any(self.tn(x) == tn for x in p["tiles"]):
                ops.update(std.BlockRam1kb().ops(self, t, (k, p)))
        c[t] = ops
        return ops

    def _unmodelled(self) -> dict:
        """type -> [(BEL instance, plugin, fabric-side outputs without an op)] for the report
        (outputs to external tile ports, e.g. OutPass to a macro, are not counted)."""
        out = {}
        for typ in sorted(set(self.types.values())):
            ts = [t for t in self.tiles if self.types[t] == typ]
            for plug, b in self.bels(typ):
                ops = self.bel_ops(ts[0]) if ts else {}
                miss = [n for q, n in b.pins_out.items() if b.prefix + q in b.internal_out and n not in ops]
                if miss:
                    out.setdefault(typ, []).append((b.inst, plug.name, len(miss)))
        return out
