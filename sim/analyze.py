#!/usr/bin/env python3
"""Coverage analysis of the detection matrices.

For each netlist: per-config and cumulative coverage, the faults strict
simulation leaves undetected with their exhaustive power-up result, unique
detections per config, greedy and exact minimum config sets. Two coverage
models:

  strict      a config covers a fault if it detects it (D) in the run with
              every latch and flop starting at X
  exhaustive  strict, plus faults from <netlist>_exhaust.json: a config set
              covers such a fault if, for every assignment of the k storage
              elements the fault leaves unwritten, some config in the set
              detects it (other elements stay X)

Writes reports/<netlist>_analysis.md and reports/coverage_summary.csv.
"""
from __future__ import annotations

import csv
import itertools
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
PLAN_CONFIGS = ["A", "B", "A2", "C", "D0", "D1"]
RECOMMENDED = ["BC1", "D0C", "D1"]
NETLISTS = ["sky130", "generic", "sky130_br", "generic_br"]


def load(netlist: str):
    rows = list(csv.DictReader((REPORTS / f"{netlist}_matrix.csv").open()))
    configs = [c for c in rows[0] if c not in ("id", "path", "polarity", "type", "any")]
    strict = {c: {r["id"] for r in rows if r[c] == "D"} for c in configs}
    exh = {}
    path = REPORTS / f"{netlist}_exhaust.json"
    if path.exists():
        data = json.loads(path.read_text())
        if set(data["configs"]) != set(configs):
            raise SystemExit(f"{path}: configs {data['configs']} differ from the matrix")
        exh = {str(r["id"]): r for r in data["faults"]}
    return rows, configs, strict, exh


def exh_covers(rec: dict, subset: set[str]) -> bool | None:
    if rec["assignments"] is None:
        return None
    return all(subset & set(found) for _, found in rec["assignments"])


def covered(subset, strict: dict, exh: dict, model: str) -> set[str]:
    cov = set().union(*(strict[c] for c in subset)) if subset else set()
    if model == "exhaustive":
        s = set(subset)
        cov |= {fid for fid, rec in exh.items() if exh_covers(rec, s)}
    return cov


def greedy(configs, strict, exh, model):
    chosen: list[str] = []
    cov: set[str] = set()
    steps = []
    while True:
        best, gain = None, 0
        for c in configs:
            if c in chosen:
                continue
            g = len(covered(chosen + [c], strict, exh, model) - cov)
            if g > gain:
                best, gain = c, g
        if best is None:
            break
        chosen.append(best)
        cov = covered(chosen, strict, exh, model)
        steps.append((best, gain, len(cov)))
    return steps


def exact(configs, strict, exh, model):
    full = len(covered(configs, strict, exh, model))
    for k in range(1, len(configs) + 1):
        hits = [s for s in itertools.combinations(configs, k)
                if len(covered(s, strict, exh, model)) == full]
        if hits:
            return full, hits
    return full, []


def classify(path: str) -> str:
    path = path.split("__br")[0]
    if path.startswith("FrameStrobe"):
        return "frame strobe stuck inactive: its latches are never written"
    if path in ("EN", "UserCLK"):
        return "flop never loads"
    return "unclassified"


def pct(n: int, total: int) -> str:
    return f"{100 * n / total:.2f}%"


def main(netlist: str, summary_rows: list[list]) -> None:
    rows, configs, strict, exh = load(netlist)
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

    pools = [("plan configs", [c for c in PLAN_CONFIGS if c in configs]),
             ("all configs", configs),
             ("recommended", [c for c in RECOMMENDED if c in configs])]
    out.append("\n## Coverage per config set\n")
    out.append("| set | strict | exhaustive power-up | not proven |")
    out.append("|---|---|---|---|")
    for label, pool in pools:
        s = covered(pool, strict, exh, "strict")
        e = covered(pool, strict, exh, "exhaustive")
        missing = sorted({r["id"] for r in rows} - e, key=int)
        miss = ", ".join(f"{by_id[f]['path']} SA{by_id[f]['polarity']}" for f in missing)
        out.append(f"| {label} ({', '.join(pool)}) | {len(s)}/{total} = {pct(len(s), total)} | "
                   f"{len(e)}/{total} = {pct(len(e), total)} | {miss or '-'} |")
        summary_rows.append([netlist, label, " ".join(pool), total, len(s), pct(len(s), total),
                             len(e), pct(len(e), total), miss])

    out.append("\n## Faults with X-only (P) results: exhaustive enumeration\n")
    out.append("k = storage elements the fault leaves unwritten (enumerated: 2^k "
               "assignments, everything else X). Last columns: detected for every "
               "assignment by that config set.\n")
    out.append("| fault | class | k | unwritten elements | all configs | recommended |")
    out.append("|---|---|---|---|---|---|")
    rec_set = set(pools[2][1])
    for fid, rec in sorted(exh.items(), key=lambda kv: int(kv[0])):
        r = by_id[fid]
        a = exh_covers(rec, set(configs))
        b = exh_covers(rec, rec_set) or bool(rec_set & {c for c in configs if fid in strict[c]})
        fmt = lambda v: "not enumerated" if v is None else ("yes" if v else "NO")  # noqa: E731
        out.append(f"| {r['path']} SA{r['polarity']} | {classify(r['path'])} | {rec['k']} | "
                   f"{' '.join(rec['element_labels'])} | {fmt(a)} | {fmt(b)} |")

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

    for model in ("strict", "exhaustive"):
        out.append(f"\n## Set cover ({model})\n")
        for label, pool in pools[:2]:
            steps = greedy(pool, strict, exh, model)
            full, hits = exact(pool, strict, exh, model)
            out.append(f"- {label} ({', '.join(pool)}): max {full}/{total} = {pct(full, total)}")
            out.append("  - greedy: " + ", ".join(f"{c} (+{g}, {n})" for c, g, n in steps))
            out.append("  - exact minimum: " + "; ".join("{" + ", ".join(h) + "}" for h in hits))

    (REPORTS / f"{netlist}_analysis.md").write_text("\n".join(out) + "\n")
    print("\n".join(out))


if __name__ == "__main__":
    summary: list[list] = []
    for n in sys.argv[1:] or NETLISTS:
        main(n, summary)
    if not sys.argv[1:]:
        with (REPORTS / "coverage_summary.csv").open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["netlist", "set", "configs", "faults", "strict", "strict_pct",
                        "exhaustive", "exhaustive_pct", "not_proven"])
            w.writerows(summary)
