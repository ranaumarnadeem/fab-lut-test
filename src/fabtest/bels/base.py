"""BEL plugin interface (fabtest P3).

A plugin models one kind of BEL for the generic tile kind (model/tile_model.TileType):

  ops(fab, t, bel)    tile-local output net -> fabric.Array op (two-valued model; codes
                      b c n x l q m s M F Q of model/fabric.py)
  pins(fab, t, bel)   pin groups the router and the sequential model need (RegFile: D, W,
                      A, B, en), or None

BEL outputs without an op carry X whatever the config: fab._taint seeds them (never
selected, the faults behind them are not tested). `role` and `group` label the BEL in
reports. Macros outside the fabric (eFPGA_top.v, e.g. BlockRAM_1KB) are MacroPlugins:
they give ops to the tile nets their outputs reach.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Bel:
    """One BEL instance of a tile type: pin -> tile-local net."""
    prefix: str
    module: str
    inst: str
    pins_in: dict
    pins_out: dict
    features: list
    internal_out: list   # outputs into the switch matrix (not external tile ports)


class Plugin:
    name = "opaque"
    modules = r"(?!)"
    role = "opaque"
    group = "other"

    def ops(self, fab, t, bel: Bel) -> dict:
        return {}

    def pins(self, fab, t, bel: Bel):
        return None


class MacroPlugin:
    name = "opaque_macro"
    modules = r"(?!)"

    def bind(self, fab, macro: dict):
        """Per macro instance of eFPGA_top: model data (pins as fabric nets), or None (X source)."""
        return None

    def ops(self, fab, t, data) -> dict:
        return {}


def bels_of(gt, spec_type: dict) -> list[Bel]:
    """BEL instances of a tile type from the spec and its TileType pin maps."""
    out = []
    for b in spec_type["bels"]:
        inst = f"Inst_{b['prefix']}{b['name']}"
        known = set(b["inputs"]) | set(b["outputs"])

        def pin(p):   # tile_model names a 1-bit pin <p>0 (W_en -> W_en0)
            p = p.split(".", 1)[1]
            return p[:-1] if p not in known and p.endswith("0") and p[:-1] in known else p
        pi = {pin(p): n for n, p in gt.bel_in.items() if p.split(".", 1)[0] == inst}
        po = {pin(p): n for n, p in gt.bel_out.items() if p.split(".", 1)[0] == inst}
        out.append(Bel(b["prefix"], b["module"], inst, pi, po, b.get("features", []), list(b["outputs"])))
    return out
