#!/usr/bin/env bash
# Full demo fabric (Phase 12): every tile type of ext/demo (copied to rtl/fabric,
# rtl/W_IO, rtl/term, rtl/) synthesized to generic gates as the LUT4AB tile
# (synth/synth_generic.ys.in, storage legalized to $_DFFE_PP_ / $_DLATCH_P_),
# module = tile type, then FABulous's eFPGA.v flattened on top:
# synth/netlist_fullfabric.{json,v}, faults/fullfabric.csv.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p build/tiles
M=rtl/models_pack.v
declare -A SRC=(
  [LUT4AB]="rtl/LUT4c_frame_config_dffesr.v rtl/MUX8LUT_frame_config_mux.v rtl/LUT4AB_ConfigMem.v rtl/LUT4AB_switch_matrix.v rtl/LUT4AB.v"
  [W_IO]="rtl/W_IO/IO_1_bidirectional_frame_config_pass.v rtl/W_IO/Config_access.v rtl/W_IO/W_IO_ConfigMem.v rtl/W_IO/W_IO_switch_matrix.v rtl/W_IO/W_IO.v"
  [N_term_single]="rtl/term/N_term_single_switch_matrix.v rtl/term/N_term_single.v"
  [S_term_single]="rtl/term/S_term_single_switch_matrix.v rtl/term/S_term_single.v"
)
for d in RegFile RAM_IO N_term_DSP S_term_DSP N_term_RAM_IO S_term_RAM_IO N_term_single2 S_term_single2 DSP; do
  SRC[$d]="$(ls rtl/fabric/$d/*.v | tr '\n' ' ')"
done
for t in "${!SRC[@]}"; do
  sed -e "s|@TOP@|$t|g" -e "s|@SOURCES@|$M ${SRC[$t]}|" -e "s|@TAGS@||" -e "s|@CHECK@||" \
      -e "s|^techmap -map synth/mux_decompose.v|dfflegalize -cell \$_DFFE_PP_ 01 -cell \$_DLATCH_P_ x\ntechmap -map synth/xor_decompose.v t:\$_XOR_\nsimplemap\ntechmap -map synth/mux_decompose.v|" \
      -e "s|@OUT@|build/tiles/$t|g" synth/synth_generic.ys.in > "build/tiles/$t.ys"
  "$YOSYS" -q -l "build/tiles/$t.log" -s "build/tiles/$t.ys" > /dev/null 2>&1 || { echo "FAILED $t"; tail -5 "build/tiles/$t.log"; exit 1; }
  echo "$t: $(grep -E '^ +[0-9]+ +cells' build/tiles/$t.stat | tail -1)"
done
{
  for t in "${!SRC[@]}"; do echo "read_json build/tiles/$t.json"; done
  echo "read_verilog -defer rtl/fabric/eFPGA.v"
  echo "hierarchy -top eFPGA"
  echo "flatten -noscopeinfo"
  echo "stat"
  echo "write_json synth/netlist_fullfabric.json"
  echo "write_verilog -noattr -noexpr -nohex -nodec synth/netlist_fullfabric.v"
} > build/synth_fullfabric.ys
"$YOSYS" -q -l build/synth_fullfabric.log -s build/synth_fullfabric.ys > /dev/null 2>&1 || { echo "FAILED top"; tail -5 build/synth_fullfabric.log; exit 1; }
grep -E "^ +[0-9]+ +(cells|.._)" build/synth_fullfabric.log | tail -7
