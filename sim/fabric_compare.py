#!/usr/bin/env python3
"""Fabric block: coverage of configuration-built observation variants (Phase 11).

  python3 sim/fabric_compare.py [R C] [--n 30]

Strict D over the first n configs of: the router without backbone
(router_nopolish), the router with the combinational backbone
(router_nopolish_bb_n<n>), the registered backbone (chain_nopolish_bb_n<n>),
and their union with the clocked and LUT suites.
"""
import argparse
import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"


def dset(path: Path, n: int) -> set | None:
    if not path.exists():
        return None
    rows = list(csv.DictReader(path.open()))
    cf = [c for c in rows[0] if c not in ("id", "path", "polarity", "type", "any")][:n]
    return {r["id"] for r in rows if any(r[c] == "D" for c in cf)}, len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("R", type=int, nargs="?", default=4)
    ap.add_argument("C", type=int, nargs="?", default=3)
    ap.add_argument("--n", type=int, default=30)
    a = ap.parse_args()
    name = f"fabric_{a.R}x{a.C}"
    variants = {"router, no backbone": f"{name}_router_nopolish_matrix.csv",
                "router + combinational backbone": f"{name}_router_nopolish_bb_n{a.n}_matrix.csv",
                "registered backbone (chain)": f"{name}_chain_nopolish_bb_n{a.n}_matrix.csv"}
    extra = set()
    for su in ("clocked", "lut"):
        r = dset(REPORTS / f"{name}_{su}_matrix.csv", 99)
        if r:
            extra |= r[0]
    for lab, f in variants.items():
        r = dset(REPORTS / f, a.n)
        if r is None:
            print(f"  {lab}: (not simulated)")
            continue
        d, tot = r
        print(f"  {lab}, {a.n} configs: {100 * len(d) / tot:.2f}%; with clocked + LUT: "
              f"{100 * len(d | extra) / tot:.2f}%")


if __name__ == "__main__":
    main()
