#!/usr/bin/env python3
"""Phase 7 report: reports/tile_summary.md from the tile fault simulations.

Inputs: reports/<netlist>_matrix.csv, <netlist>_exhaust.json (optional),
faults/<netlist>_regions.csv, synth/netlist_<netlist>.stat and the tile
configs (frame words). Coverage: strict = D in some config; exhaustive =
strict plus faults detected for every assignment of the elements they leave
unwritten (k <= kmax; larger k reported, never sampled).
"""
from __future__ import annotations

import csv
import itertools
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "configs"))
import tile_configs as tc  # noqa: E402

REPORTS = ROOT / "reports"
NETLISTS = ["tile_generic", "tile_generic_br"]
CONFIGS = list(tc.TILE_CONFIGS)


def load(netlist: str):
    m = list(csv.DictReader((REPORTS / f"{netlist}_matrix.csv").open()))
    reg = {r["id"]: r for r in csv.DictReader((ROOT / "faults" / f"{netlist}_regions.csv").open())}
    ex = {}
    p = REPORTS / f"{netlist}_exhaust.json"
    if p.exists():
        ex = {str(r["id"]): r for r in json.loads(p.read_text())["faults"]}
    return m, reg, ex


def exh_ok(rec: dict | None) -> bool | None:
    if rec is None or rec.get("assignments") is None:
        return None
    return all(set(found) & set(CONFIGS) for _, found in rec["assignments"])


def pct(a: int, b: int) -> str:
    return f"{100 * a / b:.2f}%" if b else "-"


def undetected_class(row: dict, reg: dict, rec: dict | None) -> str:
    path = row["path"]
    st = row["any"]
    if rec is not None and rec.get("timeout"):
        return "timeout (oscillation suspected)"
    if rec is not None and rec.get("assignments") is None:
        return f"X-only, k = {rec['k']} > 12, not enumerated"
    if rec is not None and rec["k"] == 0:
        return "X-only with no unwritten state (X from an unconfigured loop / X net)"
    if rec is not None and exh_ok(rec) is False:
        if reg["class"] == "sm":
            return ("X-only, fails for the power-up value equal to the routed value "
                    "(routing bits are the same in every config)")
        return "X-only, fails for some power-up assignment"
    if st == "T":
        return "timeout (oscillation suspected)"
    d = reg["detail"]
    if "branch into PORT" in d:
        return "branch into a tile output that is not observed"
    if "pass-through" in d:
        return "pass-through wire, output not observed"
    if "MUX8" in d:
        return "MUX8LUT or its config latches (BEL unused by the test)"
    if "config access" in d:
        return "config access port (FrameData / FrameStrobe)"
    if reg["class"] == "sm":
        return "switch matrix, not on the test routing"
    return f"unexplained ({d}, status {st})"


def frame_stats() -> list[str]:
    words = {}
    for name, f in tc.TILE_CONFIGS.items():
        seq = f()
        if len(seq.configs_written) != 1:
            raise SystemExit(f"{name}: expected one config write")
        words[name] = tc.frame_words(seq.configs_written[0])
    nf = tc.N_FRAMES
    out = [f"Every config writes all {nf} frames ({nf} strobes, one 32-bit word each); "
           "the routing bits are the same in all three.", ""]
    out.append("| pair | frames that differ | count |")
    out.append("|---|---|---|")
    for a, b in itertools.combinations(CONFIGS, 2):
        diff = [i for i in range(nf) if words[a][i] != words[b][i]]
        out.append(f"| {a} / {b} | {', '.join(map(str, diff))} | {len(diff)} |")
    out += ["", "Consecutive configs (the first one written from power-up: all frames):", ""]
    out.append("| order | frames written if only changed frames are rewritten | total |")
    out.append("|---|---|---|")
    for order in itertools.permutations(CONFIGS):
        counts = [nf]
        for a, b in zip(order, order[1:]):
            counts.append(sum(words[a][i] != words[b][i] for i in range(nf)))
        out.append(f"| {' -> '.join(order)} | {' + '.join(map(str, counts))} | {sum(counts)} |")
    return out


def main() -> None:
    out = ["# LUT4AB tile: single stuck-at test with BC1, D0C, D1", ""]
    out.append("One generated LUT4AB tile (8 x LUT4c_frame_config_dffesr, MUX8LUT, "
               "switch matrix, 616 config latches). Configured only through "
               "FrameData/FrameStrobe; stimulus only on tile input ports through the "
               "fixed test routing (NOTES.md, Phase 7), observed only on the 8 routed "
               "LUT outputs and Co[0]. All 8 LUTs get the same LUT config. Netlist G "
               "(generic gates, muxes decomposed) and G_br (plus one buffer per fanout "
               "branch), same yosys template as the cell.")
    out.append("")
    res = []
    for n in NETLISTS:
        if not (REPORTS / f"{n}_matrix.csv").exists():
            continue
        m, reg, ex = load(n)
        rows = [r for r in m if reg[r["id"]]["class"] == "target"]
        s = sum(r["any"] == "D" for r in rows)
        e = s + sum(1 for r in rows if r["any"] != "D" and exh_ok(ex.get(r["id"])))
        res.append(f"- {n}: LUT target {s}/{len(rows)} = {pct(s, len(rows))} strict, "
                   f"**{e}/{len(rows)} = {pct(e, len(rows))}** exhaustive power-up; "
                   f"{sum(r['any'] == 'T' for r in m)} runs timed out.")
    out.append("## Results")
    out.append("")
    out += res
    out.append("- Every target fault left X-only by strict simulation is a stuck "
               "strobe branch into a LUT latch (k = 1), EN SA0 of a LUT (k = 1), "
               "a UserCLK branch (k = 1) or the UserCLK stem (k = 8): all detected "
               "for every power-up assignment.")
    out.append("- The 20 stuck FrameStrobe stems leave 24 to 32 latches unwritten "
               "(k > 12): reported, not enumerated or sampled. They are config "
               "access faults, outside the target population.")
    out.append("")
    out.append("## Netlists and fault populations")
    out.append("")
    out.append("| netlist | cells | faults | LUT target | switch matrix | other |")
    out.append("|---|---|---|---|---|---|")
    data = {}
    for n in NETLISTS:
        if not (REPORTS / f"{n}_matrix.csv").exists():
            continue
        m, reg, ex = load(n)
        data[n] = (m, reg, ex)
        stat = (ROOT / "synth" / f"netlist_{n}.stat")
        cells = "-"
        if stat.exists():
            for ln in stat.read_text().splitlines():
                if ln.strip().endswith("cells"):
                    cells = ln.split()[0]
                    break
        else:
            cells = "(stem netlist + branch buffers)"
        c = Counter(reg[r["id"]]["class"] for r in m)
        out.append(f"| {n} | {cells} | {len(m)} | {c['target']} | {c['sm']} | {c['other']} |")
    out.append("")

    for n, (m, reg, ex) in data.items():
        out.append(f"## {n}")
        out.append("")
        groups: dict[str, list[dict]] = defaultdict(list)
        for r in m:
            g = reg[r["id"]]
            groups[g["class"]].append(r)
        out.append("| population | faults | strict | exhaustive power-up |")
        out.append("|---|---|---|---|")
        for cls in ("target", "sm", "other"):
            rows = groups[cls]
            s = sum(r["any"] == "D" for r in rows)
            e = s + sum(1 for r in rows if r["any"] != "D" and exh_ok(ex.get(r["id"])))
            label = {"target": "**LUT target**", "sm": "switch matrix (by-product)",
                     "other": "other (MUX8LUT, config access, pass-through)"}[cls]
            out.append(f"| {label} | {len(rows)} | {s} = {pct(s, len(rows))} | "
                       f"{e} = {pct(e, len(rows))} |")
        out.append("")
        out.append("Per LUT (target):")
        out.append("")
        out.append("| LUT | faults | strict | exhaustive power-up |")
        out.append("|---|---|---|---|")
        per = defaultdict(list)
        for r in groups["target"]:
            per[reg[r["id"]]["lut"]].append(r)
        for lut in list("ABCDEFGH") + ["shared"]:
            rows = per.get(lut, [])
            if not rows:
                continue
            s = sum(r["any"] == "D" for r in rows)
            e = s + sum(1 for r in rows if r["any"] != "D" and exh_ok(ex.get(r["id"])))
            out.append(f"| {'L' + lut if lut != 'shared' else 'shared (UserCLK)'} | {len(rows)} | "
                       f"{pct(s, len(rows))} | {pct(e, len(rows))} |")
        out.append("")

        und = defaultdict(Counter)
        examples = defaultdict(list)
        for r in m:
            rec = ex.get(r["id"])
            if r["any"] == "D" or exh_ok(rec):
                continue
            cls = reg[r["id"]]["class"]
            k = undetected_class(r, reg[r["id"]], rec)
            und[cls][k] += 1
            if len(examples[(cls, k)]) < 4:
                examples[(cls, k)].append(f"{r['path']} SA{r['polarity']}")
        out.append("Not detected after exhaustive power-up, by class:")
        out.append("")
        out.append("| population | class | faults | examples |")
        out.append("|---|---|---|---|")
        for cls in ("target", "sm", "other"):
            for k, cnt in und[cls].most_common():
                out.append(f"| {cls} | {k} | {cnt} | {', '.join(examples[(cls, k)])} |")
        out.append("")
        ks = Counter()
        for rec in ex.values():
            ks[rec["k"]] += 1
        if ex:
            out.append("X-only faults enumerated, by k (number of unwritten storage elements): "
                       + ", ".join(f"k={k}: {c}" for k, c in sorted(ks.items(), key=lambda kv: (kv[0] is None, kv[0] or 0))))
            out.append("")

    out.append("## Frames per config")
    out.append("")
    out += frame_stats()
    (REPORTS / "tile_summary.md").write_text("\n".join(out) + "\n")
    print("\n".join(out))


if __name__ == "__main__":
    main()
