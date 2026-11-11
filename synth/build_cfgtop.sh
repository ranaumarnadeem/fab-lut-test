#!/usr/bin/env bash
# Configuration-logic test netlist (Phase 13): rtl/config/cfg_top.v (rtl/gen_cfgtop.py)
# with FABulous's config sources (ext/demo/Fabric), generic gates as the tiles:
# synth/netlist_cfgtop.{json,v}, faults/cfgtop.csv (config logic only: the cfg_mem
# latches stand in for the fabric's ConfigMem and carry no faults). Async resets become
# synchronous (async2sync): the test holds resetn low over clock edges, so the same.
set -euo pipefail
cd "$(dirname "$0")/.."
YOSYS=${YOSYS:-yosys}
F=/home/user/ext/demo/Fabric
mkdir -p build
python3 rtl/gen_cfgtop.py
SRCS="rtl/config/cfg_top.v $F/eFPGA_Config.v $F/ConfigFSM.v $F/config_UART.v $F/bitbang.v $F/Frame_Data_Reg.v $F/Frame_Select.v"
sed -e "s|@TOP@|cfg_top|g" -e "s|@SOURCES@|$SRCS|" -e "s|@TAGS@||" -e "s|@CHECK@||" \
    -e "s|^techmap -map synth/mux_decompose.v|async2sync t:\$_DFF_* t:\$_DFFE_* t:\$_DFFSR_* t:\$_DFFSRE_*\ndfflegalize -cell \$_DFFE_PP_ 01 -cell \$_DLATCH_P_ x\ntechmap -map synth/xor_decompose.v t:\$_XOR_\nsimplemap\ntechmap -map synth/mux_decompose.v|" \
    -e "s|@OUT@|synth/netlist_cfgtop|g" synth/synth_generic.ys.in > build/synth_cfgtop.ys
"$YOSYS" -q -l build/synth_cfgtop.log -s build/synth_cfgtop.ys > /dev/null 2>&1 || { echo "FAILED"; tail -5 build/synth_cfgtop.log; exit 1; }
grep -E "cells|_" synth/netlist_cfgtop.stat | tail -8 || true
python3 faults/gen_faults.py synth/netlist_cfgtop.json --out build/cfgtop_all.csv
python3 - <<'PY'
import csv
rows = list(csv.DictReader(open("build/cfgtop_all.csv")))
keep = [r for r in rows if not r["path"].startswith(("Inst_cfg_mem.", "CM["))]
for k, r in enumerate(keep):
    r["id"] = k
with open("faults/cfgtop.csv", "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=list(rows[0]))
    w.writeheader(); w.writerows(keep)
print(f"faults/cfgtop.csv: {len(keep)} faults (of {len(rows)}; cfg_mem model left out)")
PY
