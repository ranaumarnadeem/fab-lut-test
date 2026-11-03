#!/usr/bin/env python3
"""Fill synth/synth_generic.ys.in for the cell or for a tile.

cell: top lut_cell_top, no region tags, `check -assert`.
tile: top tile_top; region tags L<x> per LUT BEL, MUX8, SM (switch matrix)
      and CM (ConfigMem latches), from the BEL instances of the tile
      Verilog; plain `check`, because a fabric tile has structural
      combinational loops (run_synth.sh rejects any other problem).
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "rtl"))

CELL_SOURCES = ["rtl/models_pack.v", "rtl/LUT4c_frame_config_dffesr.v", "rtl/lut_cell_top.v"]
TILE_SOURCES = ["rtl/models_pack.v", "rtl/LUT4c_frame_config_dffesr.v",
                "rtl/MUX8LUT_frame_config_mux.v", "rtl/LUT4AB_ConfigMem.v",
                "rtl/LUT4AB_switch_matrix.v", "rtl/LUT4AB.v", "rtl/tile_top.v"]

ap = argparse.ArgumentParser()
ap.add_argument("mode", choices=["cell", "tile"])
ap.add_argument("--out-ys", type=Path, required=True)
args = ap.parse_args()

tmpl = (ROOT / "synth" / "synth_generic.ys.in").read_text()
if args.mode == "cell":
    vals = {"TOP": "lut_cell_top", "SOURCES": " ".join(CELL_SOURCES), "TAGS": "",
            "CHECK": "check -assert", "OUT": "synth/netlist_generic"}
else:
    import tile_parse as tp
    tile = tp.parse_tile(ROOT / "rtl", "LUT4AB")
    tags = []
    for inst in sorted(tile.bels):
        if "LUT4c" in inst:
            region = inst.split("_")[1]          # Inst_LA_LUT4c... -> LA
        elif "MUX8LUT" in inst:
            region = "MUX8"
        else:
            raise SystemExit(f"unknown BEL {inst}")
        tags.append(f'setattr -set src "REGION:{region}" c:*{inst}*')
    tags.append('setattr -set src "REGION:SM" c:*Inst_LUT4AB_switch_matrix*')
    tags.append('setattr -set src "REGION:CM" c:*Inst_LUT4AB_ConfigMem*')
    vals = {"TOP": "tile_top", "SOURCES": " ".join(TILE_SOURCES), "TAGS": "\n".join(tags),
            "CHECK": "tee -o build/tile_generic_check.rpt check", "OUT": "synth/netlist_tile_generic"}
for k, v in vals.items():
    tmpl = tmpl.replace(f"@{k}@", v)
if "@" in tmpl.replace("$_", ""):
    raise SystemExit("unfilled template field")
args.out_ys.write_text(tmpl)
