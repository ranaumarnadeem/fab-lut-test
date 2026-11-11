#!/usr/bin/env python3
"""Configuration-logic test top (Phase 13): rtl/config/cfg_top.v.

FABulous's eFPGA_top.v (ext/demo) without the eFPGA instance and the
BlockRAMs: eFPGA_Config (config_UART, bitbang, ConfigFSM), the 14 frame
data registers and the 10 frame selects, unchanged. In place of the fabric,
cfg_mem: one transparent latch for every configuration bit the fabric's
ConfigMem modules use (enable FrameStrobe[20 X + frame], data FrameData[32 y
+ bit], as the generated <Tile>_ConfigMem.v), all brought out as CM[]. A
config-logic fault is detected when some used config bit ends up different
from the fault-free load (the fabric itself is not simulated).

  python3 rtl/gen_cfgtop.py      -> rtl/config/cfg_top.v, rtl/config/cfg_bits.json
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "configs"), str(ROOT / "rtl")]
SRC = Path("/home/user/ext/demo/Fabric/eFPGA_top.v")
OUT = ROOT / "rtl" / "config"


def used_bits() -> list[tuple[int, int, int, int]]:
    """(column X, frame, row y, frame-data bit) of every ConfigMem bit in the fabric."""
    import fullfabric
    import tile_model
    ff = fullfabric.FullFabric()
    cm = {"LUT4AB": tile_model.TileType(ROOT / "rtl", "LUT4AB").configmem,
          "W_IO": tile_model.TileType(ROOT / "rtl" / "W_IO", "W_IO").configmem}
    cm.update({k: v.configmem for k, v in ff.gen.items()})
    out = set()
    for t in ff.tiles:
        for fr, fd in cm[ff.types[t]].values():
            out.add((t[0] + 1, fr, t[1], fd))
    return sorted(out)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    bits = used_bits()
    v = SRC.read_text()
    # drop the fabric and the BlockRAMs (instance blocks up to their closing ");")
    v = re.sub(r"\neFPGA eFPGA_inst \(.*?\n\);", "\n", v, flags=re.S)
    v = re.sub(r"\nBlockRAM_1KB Inst_BlockRAM_\d+ \(.*?\n\);", "\n", v, flags=re.S)
    assert "eFPGA_inst" not in v and "Inst_BlockRAM" not in v
    v = v.replace("module eFPGA_top", "module cfg_top", 1)
    # the fabric's pad ports have no driver any more: keep only the config ports, add CM
    hdr, body = v.split(");", 1)
    keep = [ln for ln in hdr.split("\n") if not re.search(r"\b(A_config_C|B_config_C|Config_accessC|I_top|O_top|T_top)\b", ln)]
    hdr = "\n".join(keep).replace("input  s_data", f"input  s_data,\n        output  [{len(bits) - 1}:0] CM")
    lines = [f"cfg_mem Inst_cfg_mem (.FrameData(FrameData), .FrameStrobe(FrameSelect), .CM(CM));"]
    v = hdr + ");" + body.replace("endmodule", "\n".join(lines) + "\nendmodule", 1)
    m = ["module cfg_mem (input wire [511:0] FrameData, input wire [199:0] FrameStrobe,",
         f"                output reg [{len(bits) - 1}:0] CM);"]
    by = {}
    for k, (x, fr, y, fd) in enumerate(bits):
        by.setdefault(20 * x + fr, []).append((k, 32 * y + fd))
    for s_, lst in sorted(by.items()):   # one latch group per frame strobe
        m.append(f"    always @(*) if (FrameStrobe[{s_}]) begin")
        m += [f"        CM[{k}] = FrameData[{d}];" for k, d in lst]
        m.append("    end")
    m += ["endmodule", ""]
    (OUT / "cfg_top.v").write_text(v + "\n\n" + "\n".join(m))
    (OUT / "cfg_bits.json").write_text(json.dumps(bits))
    print(f"rtl/config/cfg_top.v: {len(bits)} config bits")


if __name__ == "__main__":
    main()
