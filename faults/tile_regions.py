#!/usr/bin/env python3
"""Classify tile faults: LUT target, switch-matrix by-product, other.

Cell regions come from the `src` tags of synth/gen_generic_ys.py (L<x>,
SM, MUX8, CM). A ConfigMem latch is assigned to the config bit it holds:
its GATE/E pin is FrameStrobe[f] and its D pin FrameData[d] (tile ports),
and the tile ConfigMem maps (f, d) to a bit, owned by a LUT, MUX8LUT or the
switch matrix.

  stem fault: by the net's driver; a net driven from outside the LUTs
              (switch-matrix mux, tile port) whose sinks all lie in LUT x
              is a LUT x input (target, "boundary"); if all its sinks lie in
              LUTs but in several, it is target "shared" (UserCLK).
  branch:     by the sink cell of the branch.

class: target (lut A..H or shared), sm, other (MUX8LUT, config access
       ports FrameData/FrameStrobe, pass-through wires).
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "faults"))
sys.path.insert(0, str(ROOT / "rtl"))
import gen_faults  # noqa: E402
import tile_parse as tp  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("netlist")
    args = ap.parse_args()
    js = ROOT / "synth" / f"netlist_{args.netlist}.json"
    _, mod = gen_faults.load_top(js, None)
    bits = gen_faults.analyse(mod)
    tile = tp.parse_tile(ROOT / "rtl", "LUT4AB")
    pos2bit = {v: b for b, v in tile.configmem.items()}

    def bit_owner(b: int) -> str:
        for inst, (lo, hi) in tile.bels.items():
            if lo <= b <= hi:
                return inst.split("_")[1] if "LUT4c" in inst else "MUX8"
        return "SM"

    def net_name(b) -> str:
        """Name of the net, looking back through branch buffers to the stem."""
        drv = bits[b]["drivers"][0] if bits[b]["drivers"] else ""
        cname = drv.rsplit(".", 1)[0]
        if cname in mod["cells"] and mod["cells"][cname]["type"] == "$_BUF_":
            return net_name(mod["cells"][cname]["connections"]["A"][0])
        return bits[b]["names"][0][0]

    region: dict[str, str] = {}
    for cname, cell in mod["cells"].items():
        src = cell.get("attributes", {}).get("src", "")
        tag = src.split("|")[0]
        if cell["type"] == "$_BUF_":
            region[cname] = "BUF"
        elif tag == "REGION:CM":
            e = net_name(cell["connections"]["E"][0])
            d = net_name(cell["connections"]["D"][0])
            if not (e.startswith("FrameStrobe[") and d.startswith("FrameData[")):
                raise SystemExit(f"latch {cname}: E={e} D={d} are not tile frame ports")
            f, dd = int(e[12:-1]), int(d[10:-1])
            region[cname] = bit_owner(pos2bit[(f, dd)])
        elif tag.startswith("REGION:"):
            r = tag[7:]
            region[cname] = r
        else:
            raise SystemExit(f"cell {cname} ({cell['type']}) has no region tag: {src}")

    def reg_of_pin(pin: str) -> str:
        if pin.startswith("port:"):
            return "PORT"
        cname = pin.rsplit(".", 1)[0]
        r = region[cname]
        if r == "BUF":   # branch buffer: look through to where its output goes
            ybit = mod["cells"][cname]["connections"]["Y"][0]
            sinks = {reg_of_pin(s) for s in bits[ybit]["sinks"]}
            return sinks.pop() if len(sinks) == 1 else "MIXED"
        return r

    luts = {f"L{x}" for x in "ABCDEFGH"}
    faults = list(csv.DictReader((ROOT / "faults" / f"{args.netlist}.csv").open()))
    by_net = {}
    for b, e in bits.items():
        by_net[e["names"][0][0]] = e
    out = ROOT / "faults" / f"{args.netlist}_regions.csv"
    counts: dict[tuple, int] = {}
    with out.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "class", "lut", "detail"])
        for flt in faults:
            e = by_net[flt["net"]]
            if flt["type"] == "branch":
                r = reg_of_pin(flt["pin"])
                detail = f"branch into {r}"
            else:
                drv = e["drivers"][0]
                r = "PORT" if drv.startswith("port:") else reg_of_pin(drv)
                detail = f"driver {r}"
                if r in ("SM", "PORT"):
                    # judge by the cells it feeds; output-port sinks are pass-throughs
                    sinks = {reg_of_pin(s) for s in e["sinks"]} - {"PORT"}
                    if sinks and sinks <= luts:
                        r = sinks.pop() if len(sinks) == 1 else "SHARED"
                        detail += " -> LUT input" if r != "SHARED" else " -> shared LUT input"
                    elif sinks and sinks <= {"SM"}:
                        r = "SM"
                        detail += " -> switch matrix"
                    elif not sinks:
                        detail += " -> pass-through only"
            if r in luts:
                cls, lut = "target", r[1]
            elif r == "SHARED":
                cls, lut = "target", "shared"
            elif r == "SM":
                cls, lut = "sm", ""
            else:
                cls, lut = "other", ""
                if flt["net"].startswith(("FrameData[", "FrameStrobe[")):
                    detail += " (config access)"
            w.writerow([flt["id"], cls, lut, detail])
            counts[(cls, lut)] = counts.get((cls, lut), 0) + 1
    print(f"{out}: " + ", ".join(f"{c}{'/' + l if l else ''}={n}" for (c, l), n in sorted(counts.items())))


if __name__ == "__main__":
    main()
