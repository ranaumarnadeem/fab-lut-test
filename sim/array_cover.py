#!/usr/bin/env python3
"""Config compaction for an array test by set cover (Phase 10c).

  python3 sim/array_cover.py R C [--suites router clocked lut]
  python3 sim/array_cover.py --matrix reports/fullfabric_chain_sample_matrix.csv

The fast kernel fills every (fault, config) entry, so the strict D sets of
all configs of all suites are known exactly. Greedy set cover picks configs
until the union of all detected faults is covered; every config keeps its
own stimulus (per-config mode: written from power-up), so the chosen subset
detects exactly the same faults. Prints the chosen configs and the coverage
reached by the first n of them; writes reports/array_<R>x<C>_cover.json.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("R", type=int, nargs="?")
    ap.add_argument("C", type=int, nargs="?")
    ap.add_argument("--matrix", help="one matrix CSV instead of R C --suites (e.g. the full-fabric sample)")
    ap.add_argument("--suites", nargs="+", default=["router", "clocked", "lut"])
    ap.add_argument("--fabric", action="store_true", help="fabric block (fabric_<R>x<C>)")
    a = ap.parse_args()
    if a.matrix:
        name, a.suites = Path(a.matrix).stem.replace("_matrix", ""), ["m"]
    else:
        name = f"{'fabric' if a.fabric else 'array'}_{a.R}x{a.C}"
    sets: dict[str, set[int]] = {}
    nfault = 0
    for su in a.suites:
        path = Path(a.matrix) if a.matrix else REPORTS / f"{name}_{su}_matrix.csv"
        rows = list(csv.DictReader(path.open()))
        nfault = len(rows)
        cfgs = [c for c in rows[0] if c not in ("id", "path", "polarity", "type", "any")]
        for c in cfgs:
            sets[f"{su}:{c}"] = {int(r["id"]) for r in rows if r[c] == "D"}
    universe = set().union(*sets.values())
    chosen, covered, curve = [], set(), []
    left = dict(sets)
    while covered != universe:
        best = max(left, key=lambda k: (len(left[k] - covered), k))
        gain = len(left[best] - covered)
        chosen.append(best)
        covered |= left.pop(best)
        curve.append((best, gain, len(covered)))
    total = len(sets)
    print(f"{name}: {total} configs ({', '.join(a.suites)}), {len(universe)} of {nfault} faults detected; "
          f"set cover keeps {len(chosen)} configs")
    by = {su: sum(1 for c in chosen if c.startswith(su + ":")) for su in a.suites}
    print("  kept per suite:", by)
    for n in (4, 8, 12, 16, 20, 24, 28, 32):
        if n <= len(curve):
            print(f"  first {n}: {100 * curve[n - 1][2] / nfault:.3f}% of all faults")
    for frac in (0.95, 0.99, 0.999, 0.9999):
        n = next(i + 1 for i, (_, _, cov) in enumerate(curve) if cov >= frac * len(universe))
        print(f"  {100 * frac:g}% of the detected faults: {n} configs")
    print(f"  all {len(chosen)}: {100 * len(universe) / nfault:.3f}%; last 5 add "
          f"{[g for _, g, _ in curve[-5:]]} faults")
    (REPORTS / f"{name}_cover.json").write_text(json.dumps(
        {"suites": a.suites, "configs_total": total, "chosen": chosen,
         "curve": curve, "faults": nfault, "detected": len(universe)}, indent=0))


if __name__ == "__main__":
    main()
