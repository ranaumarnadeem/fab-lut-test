#!/usr/bin/env bash
# Build an R x C LUT4AB array netlist and its fault list (Phase 10):
#   rtl/array_RxC.v (rtl/gen_array.py), synth/netlist_array_RxC.{json,v}
#   (the tile netlist instantiated R x C times, flattened, no optimisation),
#   faults/array_RxC.csv. Run from the repo root after sourcing env.sh.
set -euo pipefail
cd "$(dirname "$0")/.."
R=$1; C=$2; A="${R}x${C}"
mkdir -p build
python3 rtl/gen_array.py "$R" "$C"
sed -e "s/@A@/$A/g" synth/synth_array.ys.in > "build/synth_array_$A.ys"
"$YOSYS" -q -l "build/synth_array_$A.log" -s "build/synth_array_$A.ys" > /dev/null 2>&1
python3 faults/gen_faults.py "synth/netlist_array_$A.json" --out "faults/array_$A.csv"
