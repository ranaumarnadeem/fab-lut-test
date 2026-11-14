"""BEL plugin registry (fabtest P3).

Every BEL of a tile type is matched, by module name, to one plugin; a BEL no plugin
matches gets `opaque`: its outputs carry X whatever the config (tainted, never routed
through), and its faults are reported as untested. A tile type then gets a kind:

  lut      the LUT4AB model (configs/sm_model.py, fabric.M): 8 x lut4c + mux8lut
  io       the W_IO model (configs/wio_model.py): io_pad + config_access
  generic  configs/tile_model.TileType, BEL ops from the plugins

lut / io need the tile to equal the hand model (config memory, mux sources and select
bits); a variant of those tiles falls back to generic with its BELs opaque.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from fabtest.bels.base import Plugin
from fabtest.bels import std

ROOT = Path(__file__).resolve().parent.parent.parent
REGISTRY: list[Plugin] = std.PLUGINS
OPAQUE = std.OPAQUE


def for_module(module: str) -> Plugin:
    for p in REGISTRY:
        if re.fullmatch(p.modules, module):
            return p
    return OPAQUE


def _paths():
    for d in (ROOT / "configs", ROOT / "rtl"):
        if str(d) not in sys.path:
            sys.path.insert(0, str(d))


def same_tile(a, b) -> list[str]:
    """Differences between two parsed tiles (rtl/tile_parse.Tile): config memory map,
    mux sources and select bits, tile ports."""
    out = []
    if a.configmem != b.configmem:
        out.append("config memory")
    if set(a.muxes) != set(b.muxes):
        out.append("mux outputs")
    else:
        for o, m in a.muxes.items():
            n = b.muxes[o]
            if (m.inputs, m.sel, m.kind) != (n.inputs, n.sel, n.kind):
                out.append(f"mux {o}")
                break
    if (a.tile_inputs, a.tile_outputs) != (b.tile_inputs, b.tile_outputs):
        out.append("tile ports")
    if (a.sm_base, a.port_net, a.bels) != (b.sm_base, b.port_net, b.bels):
        out.append("switch-matrix wiring or BEL config bits")
    return out


def classify(spec) -> dict:
    """type -> {"kind", "plugins": [(prefix, module, plugin name)], "why"}."""
    _paths()
    import tile_parse as tp
    out = {}
    for name, ty in spec.types.items():
        bels = ty["bels"]
        plugs = [(b["prefix"], b["module"], for_module(b["module"]).name) for b in bels]
        names = sorted(p for _, _, p in plugs)
        kind, why = "generic", ""
        want = None
        if names == sorted(["lut4c"] * 8 + ["mux8lut"]):
            kind, want = "lut", ("LUT4AB", ROOT / "rtl")
        elif names == sorted(["io_pad"] * 2 + ["config_access"] * 2):
            kind, want = "io", ("W_IO", ROOT / "rtl" / "W_IO")
        if want:
            ref = tp.parse_tile(want[1], want[0])
            got = tp.parse_tile(Path(ty["dir"]), name)
            diff = same_tile(got, ref)
            if diff:
                kind, why = "generic", f"differs from the {want[0]} model: {', '.join(diff)}"
                plugs = [(p, m, OPAQUE.name) for p, m, _ in plugs]
        out[name] = {"kind": kind, "plugins": plugs, "why": why}
    return out
