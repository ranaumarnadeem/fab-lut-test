#!/usr/bin/env python3
"""Find a fixed "test routing" for one LUT4AB tile.

Inputs: every LUT input LX_I0..LX_I3 (32) plus the shared J_SR / J_EN jump
wires (which each LX_SR / LX_EN mux can select) get their own tile input
port, over vertex-disjoint paths through the switch matrix. Outputs: every
LX_O gets its own tile output port, disjoint from the input paths. The carry
chain (Ci[0] -> LA ... LH -> Co[0]) is fixed wiring and needs no routing.

Writes configs/tile_routing.json: per path the tile nets and the switch
matrix muxes / select values used, plus the resulting tile config bits.
All other switch-matrix bits and the MUX8LUT bits are 0.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "rtl"))
import tile_parse as tp  # noqa: E402

LUTS = "ABCDEFGH"


def build_edges(tile: tp.Tile):
    """Tile-net graph: (u, v) -> (mux output port, input index)."""
    edges = {}
    for out, m in tile.muxes.items():
        v = tile.port_net[out]
        for k, inp in enumerate(m.inputs):
            if inp.startswith(("GND", "VCC")):
                continue
            u = tile.port_net[inp]
            edges.setdefault((u, v), (out, k))
    return edges


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tile-dir", type=Path, required=True)
    ap.add_argument("--tile", default="LUT4AB")
    ap.add_argument("--out", type=Path, default=ROOT / "configs" / "tile_routing.json")
    args = ap.parse_args()

    tile = tp.parse_tile(args.tile_dir, args.tile)
    edges = build_edges(tile)
    elist = list(edges)

    # Do not route through LUT/MUX8LUT outputs or carry nets: they are BEL
    # outputs, not free wires. Sources are tile input ports only.
    bel_out = {tile.port_net[p] for p in tile.port_net
               if p.endswith(("_O", "_Co")) or p.startswith("M_")}
    in_edges = [(u, v) for (u, v) in elist if u not in bel_out]
    in_sinks = [f"L{x}_I{i}" for x in LUTS for i in range(4)] + ["J_SR_BEG[0]", "J_EN_BEG[0]"]
    in_sources = sorted(n for n in tile.tile_inputs if n != "Ci[0]")
    in_paths = tp.max_flow_paths(in_edges, in_sources, in_sinks)
    reached = {p[-1] for p in in_paths}
    print(f"input paths: {len(in_paths)}/{len(in_sinks)} disjoint")
    for s in in_sinks:
        if s not in reached:
            print(f"  NOT reached: {s}")

    used = {n for p in in_paths for n in p}
    out_sources = [f"L{x}_O" for x in LUTS]
    out_sinks = sorted(n for n in tile.tile_outputs if n != "Co[0]")
    out_edges = [(u, v) for (u, v) in elist
                 if u not in used and v not in used and (u not in bel_out or u in out_sources)]
    out_paths = tp.max_flow_paths(out_edges, out_sources, out_sinks)
    print(f"output paths: {len(out_paths)}/{len(out_sources)} disjoint")

    # Switch-matrix config bits
    sm_bits: dict[int, int] = {}
    routes = []
    for kind, paths in (("input", in_paths), ("output", out_paths)):
        for p in paths:
            hops = []
            for u, v in zip(p, p[1:]):
                out, k = edges[(u, v)]
                m = tile.muxes[out]
                sel = m.select_for(k)
                for b, val in sel.items():
                    if sm_bits.get(b, val) != val:
                        raise SystemExit(f"select bit {b} conflict on {out}")
                    sm_bits[b] = val
                hops.append({"mux": out, "kind": m.kind, "input": k, "from": u, "to": v,
                             "sel": {str(tile.sm_base + b): val for b, val in sel.items()}})
            routes.append({"type": kind, "nets": p, "hops": hops})

    # SR / EN of every LUT select the shared jump wire (input A1 of each 2:1).
    for x in LUTS:
        for sig, jw in (("SR", "J_SR_END0"), ("EN", "J_EN_END0")):
            m = tile.muxes[f"L{x}_{sig}"]
            k = m.inputs.index(jw)
            for b, val in m.select_for(k).items():
                sm_bits[b] = val
            routes.append({"type": "select", "nets": [f"J_{sig}_BEG[0]", f"L{x}_{sig}"],
                           "hops": [{"mux": f"L{x}_{sig}", "kind": m.kind, "input": k,
                                     "sel": {str(tile.sm_base + b): v for b, v in m.select_for(k).items()}}]})

    tile_bits = {tile.sm_base + b: v for b, v in sorted(sm_bits.items())}
    pin_map = {p["nets"][-1]: p["nets"][0] for p in routes if p["type"] == "input"}
    obs_map = {p["nets"][0]: p["nets"][-1] for p in routes if p["type"] == "output"}
    data = {
        "tile": args.tile,
        "lut_inputs": {s: pin_map.get(s) for s in in_sinks},
        "lut_outputs": {s: obs_map.get(s) for s in out_sources},
        "carry_in": "Ci[0]", "carry_out": "Co[0]",
        "switch_matrix_bits_set": {str(b): v for b, v in tile_bits.items()},
        "routes": routes,
    }
    args.out.write_text(json.dumps(data, indent=1))
    ones = sum(tile_bits.values())
    print(f"{len(tile_bits)} switch-matrix bits constrained ({ones} at 1), written to {args.out}")


if __name__ == "__main__":
    main()
