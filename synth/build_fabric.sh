#!/usr/bin/env bash
# Build an R x C fabric block (LUT4AB tiles, W_IO west, N / S term, east open) and
# its fault list: rtl/fabric_RxC.v, synth/netlist_fabric_RxC.{json,v},
# faults/fabric_RxC.csv. Needs synth/netlist_wio.json (synth/build_wio.sh).
set -euo pipefail
cd "$(dirname "$0")/.."
R=$1; C=$2; A="${R}x${C}"
python3 rtl/gen_array.py "$R" "$C" --fabric
sed -e "s/@A@/$A/g" synth/synth_fabric.ys.in > "build/synth_fabric_$A.ys"
"$YOSYS" -q -l "build/synth_fabric_$A.log" -s "build/synth_fabric_$A.ys" > /dev/null 2>&1
python3 faults/gen_faults.py "synth/netlist_fabric_$A.json" --out "faults/fabric_$A.csv"
