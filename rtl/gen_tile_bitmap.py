#!/usr/bin/env python3
"""Write configs/tile_bitmap.csv: every LUT4AB config bit with owner and frame.

owner: L<x>[k] for LUT x bit k (k as in the cell: [15:0] table, 16 FF,
17 I0mux, 18 reset value), MUX8LUT[k], SM[k] (switch-matrix local bit k).
Prints the per-frame summary used in NOTES.md.
"""
import csv
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "rtl"))
import tile_parse as tp  # noqa: E402

tile = tp.parse_tile(ROOT / "rtl", "LUT4AB")


def owner(b: int) -> str:
    for inst, (lo, hi) in tile.bels.items():
        if lo <= b <= hi:
            name = inst.split("_")[1] if "LUT4c" in inst else "MUX8LUT"
            return f"{name}[{b - lo}]"
    if b >= tile.sm_base:
        return f"SM[{b - tile.sm_base}]"
    raise SystemExit(f"bit {b} has no owner")


rows = []
for b in sorted(tile.configmem):
    f, d = tile.configmem[b]
    rows.append((b, owner(b), f, d))
out = ROOT / "configs" / "tile_bitmap.csv"
with out.open("w", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["tile_config_bit", "owner", "frame", "frame_data_bit"])
    w.writerows(rows)

per_frame = defaultdict(lambda: defaultdict(int))
for _, o, f, _ in rows:
    per_frame[f][o.split("[")[0]] += 1
print("frame | " + " | ".join(["LA", "LB", "LC", "LD", "LE", "LF", "LG", "LH", "MUX8LUT", "SM"]) + " | total")
for f in sorted(per_frame):
    c = per_frame[f]
    cols = [str(c.get(k, 0)) for k in ["LA", "LB", "LC", "LD", "LE", "LF", "LG", "LH", "MUX8LUT", "SM"]]
    print(f"{f} | " + " | ".join(cols) + f" | {sum(c.values())}")
pos = {(f, d) for _, _, f, d in rows}
print(f"{len(rows)} bits, {len(pos)} distinct (frame, FrameData bit) positions")
