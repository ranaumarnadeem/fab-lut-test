#!/usr/bin/env bash
# Full demo fabric with its BlockRAMs (Phase 14): rtl/bram/fab_bram.v (rtl/gen_fabbram.py).
# The tile netlists of synth/build_fullfabric.sh (build/tiles), FABulous's BlockRAM_1KB
# (its SRAM macro a blackbox there) and rtl/bram/sram_model.v synthesized the same way,
# flattened with eFPGA.v under fab_bram: synth/netlist_fullbram.{json,v},
# faults/fullbram.csv (no faults in the SRAM model: Inst_BlockRAM_*.memory_cell.*).
# The xgen cells of the SRAM model are removed afterwards: their output stays undriven
# (X in the fast kernel), the value of a read during a write of the same address.
set -euo pipefail
cd "$(dirname "$0")/.."
YOSYS=${YOSYS:-yosys}
F=/home/user/ext/demo/Fabric
[ -f build/tiles/LUT4AB.json ] || { echo "run synth/build_fullfabric.sh first"; exit 1; }
python3 rtl/gen_fabbram.py
for t in BlockRAM_1KB sram_1rw1r_32_256_8_sky130; do
  src=$([ "$t" = BlockRAM_1KB ] && echo "$F/BlockRAM_1KB.v" || echo "rtl/bram/sram_model.v")
  sed -e "s|@TOP@|$t|g" -e "s|@SOURCES@|$src|" -e "s|@TAGS@||" -e "s|@CHECK@||" \
      -e "s|^techmap -map synth/mux_decompose.v|dfflegalize -cell \$_DFFE_PP_ 01 -cell \$_DLATCH_P_ x\ntechmap -map synth/xor_decompose.v t:\$_XOR_\nsimplemap\ntechmap -map synth/mux_decompose.v|" \
      -e "s|@OUT@|build/tiles/$t|g" synth/synth_generic.ys.in > "build/tiles/$t.ys"
  "$YOSYS" -q -l "build/tiles/$t.log" -s "build/tiles/$t.ys" > /dev/null 2>&1 || { echo "FAILED $t"; tail -5 "build/tiles/$t.log"; exit 1; }
  echo "$t: $(grep -E 'cells' build/tiles/$t.stat | tail -1)"
done
# BlockRAM_1KB.json also holds FABulous's blackbox SRAM module: the model replaces it
python3 -c "
import json; p = 'build/tiles/BlockRAM_1KB.json'; d = json.load(open(p))
d['modules'] = {'BlockRAM_1KB': d['modules']['BlockRAM_1KB']}; json.dump(d, open(p, 'w'))"
{
  for j in build/tiles/*.json; do echo "read_json $j"; done
  echo "read_verilog -defer rtl/fabric/eFPGA.v rtl/bram/fab_bram.v"
  echo "hierarchy -top fab_bram"
  echo "flatten -noscopeinfo"
  echo "stat"
  echo "write_json build/netlist_fullbram_raw.json"
} > build/synth_fullbram.ys
"$YOSYS" -q -l build/synth_fullbram.log -s build/synth_fullbram.ys > /dev/null 2>&1 || { echo "FAILED top"; tail -5 build/synth_fullbram.log; exit 1; }
python3 - <<'PY'
import json
d = json.load(open("build/netlist_fullbram_raw.json"))
m = d["modules"]["fab_bram"]
x = [c for c, v in m["cells"].items() if v["type"] == "xgen"]
xbits = [m["cells"][c]["connections"]["y"][0] for c in x]
for c in x:
    del m["cells"][c]
# their outputs: top input XSRC[], never driven by a stimulus (X in the fast kernel)
m["ports"]["XSRC"] = {"direction": "input", "bits": xbits}
m["netnames"]["XSRC"] = {"hide_name": 0, "bits": xbits, "attributes": {}}
# names as in netlist_fullfabric (eFPGA the top there): no eFPGA_inst. prefix; the
# wiring aliases of the BRAM nets (top wires, BRAM and SRAM ports) go when every bit
# keeps another name (gen_faults wants one name per net)
import re
m["netnames"] = {re.sub(r"^eFPGA_inst\.", "", n): v for n, v in m["netnames"].items()}
m["cells"] = {re.sub(r"^eFPGA_inst\.", "", n): v for n, v in m["cells"].items()}
alias = re.compile(r"^(RAM2FAB_D_I|FAB2RAM_[DAC]_O|CLK)$|^Inst_BlockRAM_\d+\.(rd_addr|rd_data|wr_addr|wr_data|C\d|clk)$"
                   r"|^Inst_BlockRAM_\d+\.memory_cell\.(addr\d|din\d|dout\d|clk\d|csb\d|web\d|wmask\d)$"
                   r"|^Tile_X\d+Y\d+_(RAM2FAB|FAB2RAM)_\w+$|^Tile_X\d+Y\d+_RAM_IO\.RAM2FAB_\w+_I\d$")
cnt = {}
for n, v in m["netnames"].items():
    for b in v["bits"]:
        if isinstance(b, int):
            cnt[b] = cnt.get(b, 0) + 1
gone = 0
for n in [n for n in m["netnames"] if alias.match(n)]:
    bits = [b for b in m["netnames"][n]["bits"] if isinstance(b, int)]
    if all(cnt[b] > 1 for b in bits):
        for b in bits:
            cnt[b] -= 1
        del m["netnames"][n]
        gone += 1
print(f"  {gone} alias names removed")
d["modules"] = {"fab_bram": m}
json.dump(d, open("synth/netlist_fullbram.json", "w"))
print(f"netlist_fullbram: {len(m['cells'])} cells, {len(x)} xgen removed")
PY
python3 faults/gen_faults.py synth/netlist_fullbram.json --out build/fullbram_all.csv
python3 - <<'PY'
import csv
rows = list(csv.DictReader(open("build/fullbram_all.csv")))
keep = [r for r in rows if ".memory_cell." not in r["path"] and not r["path"].startswith("XSRC")]
for k, r in enumerate(keep):
    r["id"] = k
with open("faults/fullbram.csv", "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=list(rows[0])); w.writeheader(); w.writerows(keep)
print(f"faults/fullbram.csv: {len(keep)} faults (of {len(rows)}; SRAM model left out)")
PY
