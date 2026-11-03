#!/usr/bin/env bash
# Build the cell netlists (S, G, branch variants) and the LUT4AB tile netlist
# (G and G_br). Run from the repo root after sourcing env.sh.
set -euo pipefail
cd "$(dirname "$0")/.."

mkdir -p build
cp -f "$SKY130_LIB" build/sky130_fd_sc_hd__tt_025C_1v80.lib

"$YOSYS" -q -l build/synth_sky130.log -s synth/synth_sky130.ys
python3 synth/gen_generic_ys.py cell --out-ys build/synth_generic.ys
"$YOSYS" -q -l build/synth_generic.log -s build/synth_generic.ys

python3 synth/bundle_sky130_models.py synth/netlist_sky130.json \
    --sc-hd "$SKY130_SC_HD" --out synth/sky130_cells.v
# Yosys gate-level simulation models for netlist G (same yosys version)
python3 -c 'import pathlib, yowasp_yosys; print(pathlib.Path(yowasp_yosys.__file__).parent / "share" / "simcells.v")' \
    | xargs -I{} cp -f {} synth/simcells.v

# LUT4AB tile, netlist G only (same template, region tags added)
python3 synth/gen_generic_ys.py tile --out-ys build/synth_tile_generic.ys
"$YOSYS" -q -l build/synth_tile_generic.log -s build/synth_tile_generic.ys
python3 synth/check_loops_only.py build/tile_generic_check.rpt

# Branch-fault variants: one $_BUF_ per fanout branch
for n in sky130 generic tile_generic; do
    top=lut_cell_top
    chk="check -assert"
    if [ "$n" = tile_generic ]; then
        top=tile_top
        chk="tee -o build/write_branch_${n}_check.rpt check"
    fi
    python3 synth/insbuf_branches.py "synth/netlist_$n.json" --out "build/${n}_br_in.json"
    sed -e "s/@NAME@/$n/g" -e "s/@TOP@/$top/g" -e "s|@CHECK@|$chk|" \
        synth/write_branch.ys.in > "build/write_branch_$n.ys"
    "$YOSYS" -q -l "build/write_branch_$n.log" -s "build/write_branch_$n.ys"
    if [ "$n" = tile_generic ]; then
        python3 synth/check_loops_only.py "build/write_branch_${n}_check.rpt"
    fi
done
