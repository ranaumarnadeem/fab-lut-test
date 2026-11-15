#!/usr/bin/env python3
"""fabtest P1 gate: the spec of the demo project equals what the hand-written demo model uses.

  python3 tests/model/test_spec_demo.py [<work dir>]   (default: fabtest init of the demo)

Checks: geometry and FrameData / FrameStrobe widths; the DSP supertile; per tile type the
config bit count FABulous reports = the ConfigMem bits tile_model reads from the
generated RTL; the pad inputs (order) = FullFabric.border_in; the BlockRAM pins = the
pins FullFabric(bram=True) found in eFPGA_top.v.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import ROOT, demo_work  # noqa: E402,F401


def main() -> None:
    from fabtest import spec as specmod
    w = Path(sys.argv[1]) if len(sys.argv) > 1 else demo_work()
    s = specmod.load(w)
    from fabtest.model import fullfabric
    from fabtest.model import tile_model
    ff = fullfabric.FullFabric(bram=True)
    fails = []

    def check(name, got, want):
        if got != want:
            fails.append(f"{name}: spec {got!r} model {want!r}")

    check("rows x cols", (s.rows, s.cols), (ff.R, ff.C))
    check("FrameData / FrameStrobe", (s.params["frame_data"], s.params["frame_strobe"]), (512, 200))
    check("DSP supertile", s.supertiles.get("DSP", {}).get("map"), [["DSP_top"], ["DSP_bot"]])
    # tile types and positions: model (x, y) = FABulous (X - 1, Y)
    got = {(x - 1, y): t for (x, y), t in s.tiles()}
    want = {t: ff.types[t] for t in ff.tiles}
    check("tile grid (model tiles)", {k: v for k, v in got.items() if k in want}, want)
    for name, ty in s.types.items():
        if ty["config_bits"] == 0:
            continue
        tt = tile_model.TileType(Path(ty["dir"]), name)
        check(f"{name} config bits", ty["config_bits"], len(tt.configmem))
    # pad inputs in the model's order (W_IO tiles by y, BEL order A_, B_)
    pads = [f"PAD/Tile_X{x}Y{y}_{e}" for (x, y), t in sorted(s.tiles(), key=lambda kv: (kv[0][1], kv[0][0]))
            for b in s.bels(t) if b["module"].startswith("IO_1_bidirectional") for e in b["ext_in"]]
    check("pad inputs", pads, list(ff.border_in))
    # BlockRAM pins: eFPGA port names on both sides
    inv = {}
    for port, m in s.d["top"]["port_map"].items():
        if "wire" in m:
            inv[m["wire"]] = port

    def efpga_port(net: str) -> str:   # model net X8Y14/RAM2FAB_D0_O0 -> Tile_X9Y14_RAM2FAB_D0_I0
        tn, loc = net.split("/", 1)
        x, y = map(int, re.match(r"X(\d+)Y(\d+)", tn).groups())
        loc = re.sub(r"_O(\d)$", r"_I\1", loc) if loc.startswith("RAM2FAB") else re.sub(r"_I(\d)$", r"_O\1", loc)
        return f"Tile_X{x + 1}Y{y}_{loc}"
    macros = {m["inst"]: m for m in s.d["top"]["macros"] if m["module"] == "BlockRAM_1KB"}
    check("BlockRAM count", len(macros), len(ff.bram_pins))
    for k, bp in ff.bram_pins.items():
        m = macros[f"Inst_BlockRAM_{k}"]
        for pin in ("rd_data", "rd_addr", "wr_addr", "wr_data"):
            check(f"BRAM {k} {pin}", [inv.get(b) for b in m["pins"][pin]], [efpga_port(n) for n in bp[pin]])
        check(f"BRAM {k} C", [inv.get(m["pins"][f"C{j}"][0]) for j in range(6)], [efpga_port(n) for n in bp["C"]])
    for f in fails:
        print("  FAIL " + f)
    print(f"test_spec_demo: {'pass' if not fails else 'FAIL'} ({len(fails)} differences)")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
