#!/usr/bin/env bash
# Build both netlists. Run from the repo root after sourcing env.sh.
set -euo pipefail
cd "$(dirname "$0")/.."

mkdir -p build
cp -f "$SKY130_LIB" build/sky130_fd_sc_hd__tt_025C_1v80.lib

"$YOSYS" -q -l build/synth_sky130.log -s synth/synth_sky130.ys
"$YOSYS" -q -l build/synth_generic.log -s synth/synth_generic.ys

python3 synth/bundle_sky130_models.py synth/netlist_sky130.json \
    --sc-hd "$SKY130_SC_HD" --out synth/sky130_cells.v
# Yosys gate-level simulation models for netlist G (same yosys version)
python3 -c 'import pathlib, yowasp_yosys; print(pathlib.Path(yowasp_yosys.__file__).parent / "share" / "simcells.v")' \
    | xargs -I{} cp -f {} synth/simcells.v
