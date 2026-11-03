#!/usr/bin/env python3
"""Phase 6 analysis of the detection matrices.

For each netlist: per-config and cumulative coverage, classification of
faults the strict (3-valued) simulation does not detect, unique detections
per config, greedy and exact minimum config sets. Two coverage models:

  strict  a config covers a fault if it detects it (D) in the X-initialised run
  pwrup   strict, plus faults from <netlist>_xresolve.csv: a set covers such a
          fault if, for both all-0 and all-1 power-up, some config in the set
          detects it

Writes reports/<netlist>_analysis.md.
"""
from __future__ import annotations

import csv
import itertools
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
PLAN_CONFIGS = ["A", "B", "A2", "C", "D0", "D1"]


def load(netlist: str):
    rows = list(csv.DictReader((REPORTS / f"{netlist}_matrix.csv").open()))
    configs = [c for c in rows[0] if c not in ("id", "path", "polarity", "type", "any")]
    strict = {c: {r["id"] for r in rows if r[c] == "D"} for c in configs}
    xres = {}
    xpath = REPORTS / f"{netlist}_xresolve.csv"
    if xpath.exists():
        for r in csv.DictReader(xpath.open()):
            xres[r["id"]] = (set(r["detecting_configs_pwrup0"].split()),
                             set(r["detecting_configs_pwrup1"].split()))
    return rows, configs, strict, xres


def covered(subset: tuple[str, ...], strict: dict, xres: dict, model: str) -> set[str]:
    cov = set().union(*(strict[c] for c in subset)) if subset else set()
    if model == "pwrup":
        s = set(subset)
        cov |= {fid for fid, (d0, d1) in xres.items() if s & d0 and s & d1}
    return cov


def greedy(configs, strict, xres, model):
    chosen: list[str] = []
    cov: set[str] = set()
    steps = []
    while True:
        best, gain = None, 0
        for c in configs:
            if c in chosen:
                continue
            g = len(covered(tuple(chosen + [c]), strict, xres, model) - cov)
            if g > gain:
                best, gain = c, g
        if best is None:
            break
        chosen.append(best)
        cov = covered(tuple(chosen), strict, xres, model)
        steps.append((best, gain, len(cov)))
    return steps


def exact(configs, strict, xres, model):
    full = len(covered(tuple(configs), strict, xres, model))
    for k in range(1, len(configs) + 1):
        hits = [s for s in itertools.combinations(configs, k)
                if len(covered(s, strict, xres, model)) == full]
        if hits:
            return full, hits
    return full, []


def classify(path: str) -> str:
    path = path.split("__br")[0]
    if path.startswith("FrameStrobe"):
        return "frame strobe stuck inactive: latches keep their power-up value (X in sim)"
    if path in ("EN", "UserCLK"):
        return "flop never loads: keeps its power-up value (X in sim)"
    return "unclassified"


def main(netlist: str) -> None:
    rows, configs, strict, xres = load(netlist)
    total = len(rows)
    by_id = {r["id"]: r for r in rows}
    nbr = sum(r["type"] == "branch" for r in rows)
    out = [f"# {netlist}: analysis\n",
           f"{total} faults ({total - nbr} stem, {nbr} branch), configs {', '.join(configs)}.\n"]

    out.append("## Coverage per config (strict)\n")
    out.append("| config | detected | % | cumulative | cum % |")
    out.append("|---|---|---|---|---|")
    cum: set[str] = set()
    for c in configs:
        cum |= strict[c]
        out.append(f"| {c} | {len(strict[c])} | {100 * len(strict[c]) / total:.2f} | "
                   f"{len(cum)} | {100 * len(cum) / total:.2f} |")
    pw = covered(tuple(configs), strict, xres, "pwrup")
    out.append(f"\nAll configs: strict {len(cum)}/{total} = {100 * len(cum) / total:.2f}%, "
               f"with power-up resolution {len(pw)}/{total} = {100 * len(pw) / total:.2f}%.\n")

    out.append("## Not detected in the strict run\n")
    out.append("| fault | class | detected with power-up 0 by | power-up 1 by |")
    out.append("|---|---|---|---|")
    for r in rows:
        if r["any"] == "D":
            continue
        d0, d1 = xres.get(r["id"], (set(), set()))
        out.append(f"| {r['path']} SA{r['polarity']} | {classify(r['path'])} | "
                   f"{' '.join(sorted(d0)) or '-'} | {' '.join(sorted(d1)) or '-'} |")

    out.append("\n## Unique detections (strict)\n")
    out.append("Faults a config detects that no other config in the set does.\n")
    for c in configs:
        others = set().union(*(strict[o] for o in configs if o != c))
        uniq = sorted(strict[c] - others, key=int)
        names = ", ".join(f"{by_id[f]['path']} SA{by_id[f]['polarity']}" for f in uniq)
        out.append(f"- {c}: {len(uniq)}" + (f" ({names})" if uniq else ""))
    out.append("")
    ab = strict.get("A", set()) | strict.get("B", set())
    for c in ("C", "D0", "D1"):
        if c in strict:
            extra = sorted(strict[c] - ab, key=int)
            names = ", ".join(f"{by_id[f]['path']} SA{by_id[f]['polarity']}" for f in extra)
            out.append(f"- {c} beyond A+B: {len(extra)}" + (f" ({names})" if extra else ""))

    pools = [("plan configs", [c for c in PLAN_CONFIGS if c in configs]), ("all configs", configs)]
    for model in ("strict", "pwrup"):
        out.append(f"\n## Set cover ({model})\n")
        for label, pool in pools:
            steps = greedy(pool, strict, xres, model)
            full, hits = exact(pool, strict, xres, model)
            out.append(f"- {label} ({', '.join(pool)}): max {full}/{total} = "
                       f"{100 * full / total:.2f}%")
            out.append("  - greedy: " + ", ".join(f"{c} (+{g}, {n})" for c, g, n in steps))
            out.append("  - exact minimum: " + "; ".join("{" + ", ".join(h) + "}" for h in hits))

    (REPORTS / f"{netlist}_analysis.md").write_text("\n".join(out) + "\n")
    print("\n".join(out))


if __name__ == "__main__":
    for n in sys.argv[1:] or ["sky130", "generic", "sky130_br", "generic_br"]:
        main(n)
