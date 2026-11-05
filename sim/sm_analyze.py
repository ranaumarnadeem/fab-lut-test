#!/usr/bin/env python3
"""Phase 8 step 3 report: reports/sm_summary.md.

Inputs: reports/<netlist>_sm_matrix.csv and _sm_seq_matrix.csv (sim/run_sm.py),
reports/<netlist>_sm_conesplit.json and _sm_seq_conesplit.json (cone-split on
X-only faults), the LUT-config results (reports/<netlist>_matrix.csv,
_conesplit.json, _exhaust.json via sim/tile_analyze.py), faults/<netlist>_regions.csv,
the model (configs/sm_configs.py, sim/sm_obs.py) and fault -> mux owners
(sim/sm_owner.py).
"""
from __future__ import annotations

import csv
import itertools
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "sim"), str(ROOT / "configs")]
import sm_configs as sc  # noqa: E402
import sm_obs  # noqa: E402
import sm_owner  # noqa: E402
import tile_analyze as ta  # noqa: E402
import tile_configs as tc  # noqa: E402

REPORTS = ROOT / "reports"
NETLISTS = ["tile_generic", "tile_generic_br"]
SM = list(sc.BASE_SM_CONFIGS)
LUT = list(tc.BASE_TILE_CONFIGS)
POPS = ["target", "sm", "other"]
POP_LABEL = {"target": "**LUT target**", "sm": "switch matrix", "other": "other (MUX8LUT, config access, pass-through)"}


def pct(a: int, b: int) -> str:
    return f"{100 * a / b:.2f}%" if b else "-"


def read_matrix(path: Path) -> dict[str, dict]:
    return {r["id"]: r for r in csv.DictReader(path.open())} if path.exists() else {}


def cone(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return {str(r["id"]): r["verdict"] for r in json.loads(path.read_text())}


class Net:
    """Everything known about one netlist."""

    def __init__(self, n: str):
        self.n = n
        self.reg = {r["id"]: r for r in csv.DictReader((ROOT / "faults" / f"{n}_regions.csv").open())}
        self.faults = {f["id"]: f for f in csv.DictReader((ROOT / "faults" / f"{n}.csv").open())}
        self.sm = read_matrix(REPORTS / f"{n}_sm_matrix.csv")
        self.smq = read_matrix(REPORTS / f"{n}_sm_seq_matrix.csv")
        self.sm_cs = cone(REPORTS / f"{n}_sm_conesplit.json")
        self.smq_cs = cone(REPORTS / f"{n}_sm_seq_conesplit.json")
        m, _reg, ex = ta.load(n)
        self.lut = {r["id"]: r for r in m}
        self.lut_ex = ex
        self.ids = sorted(self.reg, key=int)

    # strict D / exhaustive verdicts per suite
    def sm_strict(self, i: str, configs=None) -> bool:
        r = self.sm.get(i)
        return bool(r) and any(r[c] == "D" for c in (configs or SM))

    def sm_exh(self, i: str) -> bool:
        return self.sm_strict(i) or self.sm_cs.get(i) == "detected"

    def smq_strict(self, i: str) -> bool:
        r = self.smq.get(i)
        return bool(r) and r["any"] == "D"

    def smq_exh(self, i: str) -> bool:
        return self.smq_strict(i) or self.smq_cs.get(i) == "detected"

    def lut_strict(self, i: str, configs=None) -> bool:
        r = self.lut[i]
        return any(r[c] == "D" for c in (configs or LUT))

    def lut_exh(self, i: str) -> bool:
        return self.lut_strict(i) or bool(ta.exh_ok(self.lut_ex.get(i)))

    def pop(self, i: str) -> str:
        return self.reg[i]["class"]


# ---------------------------------------------------------------- sections

def coverage(nets: list[Net]) -> list[str]:
    out = ["## Coverage, SM baseline (16 configs)", "",
           "Strict = 3-valued, all storage X at power-up. Exhaustive = strict D, or "
           "detected for every power-up state of the unwritten elements by cone-split "
           "enumeration (U = never definitely written).", ""]
    out.append("| netlist | population | faults | per-config strict | per-config exhaustive | "
               "sequence strict | sequence exhaustive |")
    out.append("|---|---|---|---|---|---|---|")
    for t in nets:
        for p in POPS + ["all"]:
            ids = [i for i in t.ids if p == "all" or t.pop(i) == p]
            row = [sum(f(i) for i in ids) for f in (t.sm_strict, t.sm_exh, t.smq_strict, t.smq_exh)]
            lab = POP_LABEL.get(p, "all")
            out.append(f"| {t.n} | {lab} | {len(ids)} | " +
                       " | ".join(f"{v} = {pct(v, len(ids))}" for v in row) + " |")
    out.append("")
    out.append("The FrameStrobe stem SA0 faults (20 per netlist, config access) were left out of the "
               "SM cone-split (`--exclude`): with 16 configs and all outputs live each takes hours, and "
               "the LUT run decides them (reports/tile_summary.md). The 10 tile_generic stems enumerated "
               "before that are kept (8 detected, 2 timed out); the others count as not detected in the "
               "exhaustive columns above.")
    out.append("")
    for t in nets:
        a = {i for i in t.ids if t.sm_exh(i)}
        b = {i for i in t.ids if t.smq_exh(i)}
        sa = {i for i in t.ids if t.sm_strict(i)}
        sb = {i for i in t.ids if t.smq_strict(i)}
        out.append(f"- {t.n}: per-config vs sequence, strict: {len(sa - sb)} only per-config, "
                   f"{len(sb - sa)} only sequence; exhaustive: {len(a - b)} only per-config, "
                   f"{len(b - a)} only sequence.")
    out.append("")
    return out


def combined(nets: list[Net]) -> list[str]:
    out = ["## Coverage, SM baseline + {BC1, D0C, D1} (19 configs, per-config, exhaustive)", "",
           "A fault counts when either suite detects it for every power-up state (a lower bound: "
           "states caught by one suite and not the other are not combined).", "",
           "| netlist | population | faults | SM baseline | {BC1, D0C, D1} | both |",
           "|---|---|---|---|---|---|"]
    for t in nets:
        for p in POPS + ["all"]:
            ids = [i for i in t.ids if p == "all" or t.pop(i) == p]
            a = sum(t.sm_exh(i) for i in ids)
            b = sum(t.lut_exh(i) for i in ids)
            c = sum(t.sm_exh(i) or t.lut_exh(i) for i in ids)
            out.append(f"| {t.n} | {POP_LABEL.get(p, 'all')} | {len(ids)} | {a} = {pct(a, len(ids))} | "
                       f"{b} = {pct(b, len(ids))} | {c} = {pct(c, len(ids))} |")
    out.append("")
    return out


def by_mux(nets: list[Net], owners: dict) -> list[str]:
    out = ["## Switch-matrix faults per mux, by fan-in", "",
           "Fault -> mux: sim/sm_owner.py (nearest mux-named net forward from the fault). "
           "\"shared input\": a tile input stem feeding several muxes.", ""]
    out.append("| netlist | mux group | muxes | faults | per-config strict | per-config exhaustive | "
               "muxes with every fault detected (exhaustive) |")
    out.append("|---|---|---|---|---|---|---|")
    for t in nets:
        groups = defaultdict(list)
        for i in t.ids:
            if t.pop(i) != "sm":
                continue
            o = owners[t.n][i]
            if o in sc.MUXES:
                m = sc.MUXES[o]
                key = "direct wire" if m.model == "direct" else f"{m.fan_in}:1"
            else:
                key = o
            groups[key].append((o, i))
        order = ["2:1", "4:1", "8:1", "16:1", "direct wire", "shared input", "BEL config", "unowned"]
        for key in [k for k in order if k in groups]:
            ids = [i for _, i in groups[key]]
            muxes = sorted({o for o, _ in groups[key]})
            full = sum(all(t.sm_exh(i) for o2, i in groups[key] if o2 == o) for o in muxes)
            s, e = sum(t.sm_strict(i) for i in ids), sum(t.sm_exh(i) for i in ids)
            out.append(f"| {t.n} | {key} | {len(muxes)} | {len(ids)} | {s} = {pct(s, len(ids))} | "
                       f"{e} = {pct(e, len(ids))} | {full} |")
    out.append("")
    # by sink
    out.append("By what the mux drives (per-config exhaustive):")
    out.append("")
    out.append("| netlist | sink | faults | detected |")
    out.append("|---|---|---|---|")
    for t in nets:
        cnt = Counter()
        det = Counter()
        for i in t.ids:
            if t.pop(i) != "sm":
                continue
            o = owners[t.n][i]
            k = sc.MUXES[o].sink if o in sc.MUXES else o
            cnt[k] += 1
            det[k] += t.sm_exh(i)
        for k in sorted(cnt):
            out.append(f"| {t.n} | {k} | {cnt[k]} | {det[k]} = {pct(det[k], cnt[k])} |")
    out.append("")
    return out


def lut_target(nets: list[Net]) -> list[str]:
    out = ["## LUT target: baseline alone and with {BC1, D0C, D1}", "",
           "| netlist | LUT target faults | SM baseline | {BC1, D0C, D1} | SM + {BC1, D0C, D1} | "
           "SM + one LUT config: BC1 / D0C / D1 |",
           "|---|---|---|---|---|---|"]
    notes = []
    for t in nets:
        ids = [i for i in t.ids if t.pop(i) == "target"]
        sm = {i for i in ids if t.sm_exh(i)}
        lu = {i for i in ids if t.lut_exh(i)}
        one = []
        for c in LUT:
            s = {i for i in ids if t.lut_strict(i, [c])} | sm
            one.append(f"{len(s)}")
        out.append(f"| {t.n} | {len(ids)} | {len(sm)} = {pct(len(sm), len(ids))} | "
                   f"{len(lu)} = {pct(len(lu), len(ids))} | {len(sm | lu)} = {pct(len(sm | lu), len(ids))} | "
                   f"{' / '.join(one)} |")
        miss = sorted(set(ids) - sm, key=int)
        need = Counter()
        for i in miss:
            need[tuple(c for c in LUT if t.lut[i][c] == "D")] += 1
        notes.append(f"- {t.n}: {len(miss)} target faults the baseline misses; detected (strict) by "
                     + ", ".join(f"{'/'.join(k) or 'no single LUT config'}: {v}" for k, v in need.most_common()))
        ex = Counter(_target_kind(t.faults[i]["path"]) for i in miss)
        notes.append(f"  - by kind: " + ", ".join(f"{k} {v}" for k, v in ex.most_common()))
    out.append("")
    out += notes
    out.append("")
    return out


def _target_kind(path: str) -> str:
    if "ConfigBits" in path:
        m = re.search(r"ConfigBits\[(\d+)\]", path)
        b = int(m.group(1)) % 19 if m and int(m.group(1)) < 152 else None
        return {16: "output-mux select (bit 16)", 17: "I0-mux select (bit 17)",
                18: "reset value (bit 18)"}.get(b, "truth-table latch")
    if "FrameStrobe" in path or "FrameData" in path:
        return "config access into LUT latches"
    if re.search(r"LUT_flop|dff|\.Q\b|SR|EN|UserCLK", path):
        return "flop / SR / EN / clock"
    return "LUT logic"


def config_memory() -> list[str]:
    out = ["## Config memory: every latch written to both values", ""]
    words_sm = [sc.config_word(sc.SELECTS[c]) for c in range(sc.F)]
    words_lut = [f().configs_written[-1] for f in tc.BASE_TILE_CONFIGS.values()]
    owner = {}
    for x, (lo, hi) in tc.TILE.bels.items():
        for b in range(lo, hi + 1):
            owner[b] = x.replace("Inst_", "").replace("_frame_config_dffesr", "").replace("_frame_config_mux", "")
    for label, words in (("SM baseline", words_sm), ("SM baseline + {BC1, D0C, D1}", words_sm + words_lut)):
        one = [b for b in sorted(tc.TILE.configmem) if len({w[b] for w in words}) < 2]
        grp = Counter(owner.get(b, "switch matrix") for b in one)
        out.append(f"- {label}: {616 - len(one)} of 616 latches written to both 0 and 1; "
                   f"exceptions {len(one)}: " + (", ".join(f"{k} {v}" for k, v in sorted(grp.items())) or "none"))
        if len(one) <= 12:
            out.append(f"  ({', '.join(str(b) for b in one)})")
    lut_only = [b for b in range(152) if len({w[b] for w in words_sm}) < 2]
    out.append(f"- In the baseline every LUT holds the XOR table and bits 16..18 = 0, so its {len(lut_only)} "
               "LUT latches each see one value; the LUT configs supply the other. MUX8LUT bits "
               "152, 153 are 0 in every config (BEL unused).")
    out.append("")
    return out


def frame_stems(nets: list[Net]) -> list[str]:
    out = ["## FrameStrobe / FrameData stems: SM baseline vs LUT configs", "",
           "Exhaustive verdict per stem (D = detected, - = not, skip = X-only and left out of the SM "
           "cone-split). LUT = {BC1, D0C, D1} "
           "(reports/tile_summary.md).", ""]
    for t in nets:
        out.append(f"{t.n}:")
        out.append("")
        out.append("| stem | SA0 LUT | SA0 SM | SA1 LUT | SA1 SM |")
        out.append("|---|---|---|---|---|")
        idx = {(f["path"], f["polarity"]): i for i, f in t.faults.items()}
        for name in [f"FrameStrobe[{k}]" for k in range(20)] + [f"FrameData[{k}]" for k in range(32)]:
            cells = []
            for pol in ("0", "1"):
                i = idx.get((name, pol))
                if i is None:
                    cells += ["-", "-"]
                    continue
                sm = "D" if t.sm_exh(i) else ("-" if (i in t.sm_cs or t.sm.get(i, {}).get("any") != "P")
                                                else "skip")
                cells += ["D" if t.lut_exh(i) else "-", sm]
            mark = " **(SM-only frame)**" if name in ("FrameStrobe[0]", "FrameStrobe[1]", "FrameStrobe[2]",
                                                      "FrameStrobe[4]", "FrameStrobe[19]") else ""
            out.append(f"| {name}{mark} | {cells[0]} | {cells[1]} | {cells[2]} | {cells[3]} |")
        out.append("")
    return out


def undetected(nets: list[Net], owners: dict) -> list[str]:
    never_obs = {o for o, m in sc.MUXES.items()
                 if not any(sm_obs.observable(c, m.net) for c in range(sc.F))}
    tests = {o: sm_obs.code_tests(o) for o, m in sc.MUXES.items() if m.model != "direct"}

    def klass(t: Net, i: str) -> str:
        if t.sm.get(i, {}).get("any") == "P" or i in t.sm_cs:
            if t.sm_cs.get(i) == "escapes":
                return "X-only, escapes for some power-up state (cone-split witness)"
        o = owners[t.n][i]
        if o == "shared input":
            return "tile input stem feeding several muxes"
        if o not in sc.MUXES:
            return f"owner {o}"
        m = sc.MUXES[o]
        if o in never_obs:
            why = ("drives LUT SR/EN (flops not observed)" if m.bel.endswith((".SR", ".EN")) else
                   "drives MUX8LUT select" if m.bel.startswith("MUX8LUT") else
                   "J_SR / J_EN (SR/EN only)" if o in ("J_SR_BEG0", "J_EN_BEG0") else "")
            return f"selected but not observable ({why})" if why else "selected but not observable"
        if m.model == "direct":
            return "direct wire, observable (masked)"
        r = tests[o]
        bad = [k for k in range(m.fan_in) if not (r["t0"][k] and r["t1"][k])]
        if bad:
            if any(m.inputs[k].kind == "bel_output" for k in bad):
                return "LUT-driven input"
            return "selected but not observable (code selected only while the mux output is not observed)"
        sb = [k for (k, _i), v in r["sel"].items() if not v]
        if sb:
            if any(m.inputs[k].kind == "bel_output" for k in sb):
                return "LUT-driven input"
            return "selected and observable, wrong selection masked (equal values)"
        return "every code and select tested at mux level (gate-level masking)"

    out = ["## Not detected (per-config, exhaustive), by class", "",
           "Classes from the model (sim/sm_obs.py): a mux is observable in a config when flipping "
           "its output changes a tile output at some sample; a code is tested when it is selected "
           "while observable at a sample with its input at 0 and one at 1; a select bit is tested "
           "when, with the mux observable, the input it would wrongly pick differs. "
           "\"Unselectable after loop breaking\": 0 (all 1248 codes selected, NOTES step 2). "
           "\"LUT-driven input\": the untested code or select picks a LUT / MUX8LUT output "
           "(XOR outputs of a LUT pair are equal, or constant).", ""]
    for t in nets:
        cnt = Counter()
        ex = defaultdict(list)
        for i in t.ids:
            if t.sm_exh(i):
                continue
            p = t.pop(i)
            if p == "sm":
                k = klass(t, i)
            elif p == "target":
                k = _target_kind(t.faults[i]["path"]) + (" (detected by {BC1, D0C, D1})" if t.lut_exh(i) else "")
            else:
                k = f"{p}: " + t.reg[i]["detail"]
            cnt[(p, k)] += 1
            ex[(p, k)].append(f"{t.faults[i]['path']} SA{t.faults[i]['polarity']}")
        out.append(f"{t.n}:")
        out.append("")
        out.append("| population | class | faults | examples |")
        out.append("|---|---|---|---|")
        for (p, k), v in sorted(cnt.items(), key=lambda kv: (POPS.index(kv[0][0]), -kv[1])):
            out.append(f"| {p} | {k} | {v} | {', '.join(ex[(p, k)][:3])} |")
        out.append("")
    out.append(f"Muxes never observable in any baseline config: {len(never_obs)} "
               f"({', '.join(sorted(never_obs))}).")
    out.append("")
    return out


def frames_and_order() -> list[str]:
    words = {c: sc.frame_words(sc.config_word(sc.SELECTS[c])) for c in range(sc.F)}
    diff = {(a, b): sum(words[a][f] != words[b][f] for f in range(sc.N_FRAMES))
            for a in words for b in words if a != b}
    naive = sc.N_FRAMES + sum(diff[(c, c + 1)] for c in range(sc.F - 1))
    best = None
    for s in range(sc.F):   # greedy nearest neighbour from every start
        path, left = [s], set(words) - {s}
        while left:
            nxt = min(left, key=lambda b: (diff[(path[-1], b)], b))
            path.append(nxt)
            left.remove(nxt)
        cost = sc.N_FRAMES + sum(diff[(a, b)] for a, b in zip(path, path[1:]))
        if best is None or cost < best[0]:
            best = (cost, path)
    hist = Counter(diff[(a, b)] for a in words for b in words if a < b)
    out = ["## Config count, frames, ordering", "",
           f"- Configs: {sc.F} = F (max fan-in); every mux code is selected by at least one "
           "(after loop breaking and recovery).",
           f"- Frames per config: all {sc.N_FRAMES} written (one word each; 3 steps per frame + 1 idle "
           f"+ {2 * sc.W} vectors = {len(sc.make_seq(0).steps)} steps per config).",
           f"- Frames that differ between two configs: min {min(hist)}, max {max(hist)} "
           f"(histogram {dict(sorted(hist.items()))}).",
           f"- Writing only changed frames after the first config: naive order SM00..SM15 = "
           f"{naive} frame writes; greedy nearest-neighbour order (best start, 16 configs > 8 so "
           f"not exact) = {best[0]} ({' '.join(sc.name_of(c) for c in best[1])}); all frames every "
           f"config = {sc.F * sc.N_FRAMES}.",
           ""]
    return out


def set_cover(nets: list[Net]) -> list[str]:
    out = ["## Set cover over SM baseline + {BC1, D0C, D1} (strict D per config)", ""]
    for t in nets:
        cfgs = SM + LUT
        det = {}
        for c in cfgs:
            if c in SM:
                det[c] = {i for i in t.ids if t.sm.get(i, {}).get(c) == "D"}
            else:
                det[c] = {i for i in t.ids if t.lut[i][c] == "D"}
        allset = set().union(*det.values())
        # greedy
        chosen, cov = [], set()
        while cov != allset:
            c = max(cfgs, key=lambda x: (len(det[x] - cov), -cfgs.index(x)))
            if not det[c] - cov:
                break
            chosen.append((c, len(det[c] - cov)))
            cov |= det[c]
        # note: per-config "-" (not run after an earlier D) makes per-config sets incomplete
        out.append(f"- {t.n}: union strict {len(allset)} of {len(t.ids)}; greedy: "
                   + ", ".join(f"{c} (+{n})" for c, n in chosen) + f" -> {len(chosen)} configs.")
    out.append("")
    out.append("Per-config sets are lower bounds: faultsim stops at the first detecting config "
               "(status \"-\" for configs not run), so a config's set holds only the faults it "
               "detected first in the run order.")
    out.append("")
    return out


def main() -> None:
    nets = [Net(n) for n in NETLISTS if (REPORTS / f"{n}_sm_matrix.csv").exists()]
    owners = {}
    for t in nets:
        ow = sm_owner.Owner(t.n)
        owners[t.n] = {i: ow.of_fault(t.faults[i]) for i in t.ids if t.pop(i) == "sm"}
    sm_obs.selfcheck()
    out = ["# LUT4AB tile: switch-matrix baseline (Phase 8 step 3)", "",
           f"{sc.F} configs SM00..SM{sc.F - 1:02d} (configs/sm_configs.py): every switch-matrix mux on "
           "input c mod fan-in, loops broken (NOTES.md step 2); LUTs XOR, comb, I0 = I[0]. All "
           f"{len(sc.INPUTS)} tile inputs driven with unique {sc.W}-bit codes and complements "
           f"({2 * sc.W} vectors per config), all {len(sc.OUTPUTS)} tile outputs observed at every "
           "sample. **Tile-level observability is an upper bound** (in a fabric the outputs reach "
           "a tester only through neighbouring tiles).", ""]
    out += coverage(nets)
    out += combined(nets)
    out += by_mux(nets, owners)
    out += lut_target(nets)
    out += config_memory()
    out += frame_stems(nets)
    out += undetected(nets, owners)
    out += frames_and_order()
    out += set_cover(nets)
    (REPORTS / "sm_summary.md").write_text("\n".join(out) + "\n")
    print(f"reports/sm_summary.md ({len(nets)} netlists)")


if __name__ == "__main__":
    main()
