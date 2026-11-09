#!/usr/bin/env bash
# W_IO tile netlist (Phase 10d): synth/netlist_wio.{json,v}, generic gates as the
# LUT4AB tile (synth/synth_generic.ys.in), module wio_top; the pad register
# (a plain $_DFF_P_) legalized to $_DFFE_PP_ with enable 1 (fastsim / X models).
set -euo pipefail
cd "$(dirname "$0")/.."
SRC="rtl/models_pack.v rtl/W_IO/IO_1_bidirectional_frame_config_pass.v rtl/W_IO/Config_access.v rtl/W_IO/W_IO_ConfigMem.v rtl/W_IO/W_IO_switch_matrix.v rtl/W_IO/W_IO.v rtl/W_IO/wio_top.v"
sed -e "s|@TOP@|wio_top|g" -e "s|@SOURCES@|$SRC|" -e "s|@TAGS@||" \
    -e "s|@CHECK@|dfflegalize -cell \$_DFFE_PP_ 01 -cell \$_DLATCH_P_ x\nopt_clean -purge\nrename -enumerate -pattern n%|" \
    -e "s|@OUT@|synth/netlist_wio|g" synth/synth_generic.ys.in > build/synth_wio.ys
"$YOSYS" -q -l build/synth_wio.log -s build/synth_wio.ys > /dev/null 2>&1
