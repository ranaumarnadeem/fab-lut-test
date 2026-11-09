#!/usr/bin/env python3
"""Routed switch-matrix configs vs the baseline (Phase 9 step 1).

Reads reports/<netlist>_rt_matrix.csv / _rt_seq_matrix.csv (configs of
configs/sm_router.py, python3 sim/run_sm.py --suite router faultsim ...)
and the baseline results (sim/sm_analyze.py inputs). Router numbers are
strict (3-valued, all storage X at power-up); the baseline is shown strict
and exhaustive (cone-split). Writes reports/rt_summary.md.
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "sim"), str(ROOT / "configs")]
import sm_analyze as sa  # noqa: E402
import sm_owner  # noqa: E402

sys.path.insert(0, str(ROOT / "sim" / "fastsim"))
import fastsim  # noqa: E402

REPORTS = ROOT / "reports"
RJ = json.loads((ROOT / "configs" / "sm_router.json").read_text())


def comb_reach(netlist: str) -> callable:
    """net -> True when it reaches a tile output through gates only (no latch, no flop)."""
    nl = fastsim.Netlist(netlist)
    outs = set(nl.port_bits[o] for o in sa.sc.OUTPUTS)
    memo: dict[int, bool] = {}

    def reach(b: int) -> bool:
        stack, seen = [b], {b}
        while stack:
            x = stack.pop()
            if x in outs or memo.get(x):
                return True
            for ci in nl.rd_idx[nl.rd_off[x]:nl.rd_off[x + 1]]:
                if nl.type[ci] <= 3:
                    o = int(nl.out[ci])
                    if o not in seen:
                        seen.add(o)
                        stack.append(o)
        return False

    def f(net: str) -> bool:
        b = nl.net_bit[net]
        if b not in memo:
            memo[b] = reach(b)
        return memo[b]
    return f


def main() -> None:
    nets = [sa.Net(n) for n in sa.NETLISTS]
    rt = {t.n: sa.read_matrix(REPORTS / f"{t.n}_rt_matrix.csv") for t in nets}
    rtq = {t.n: sa.read_matrix(REPORTS / f"{t.n}_rt_seq_matrix.csv") for t in nets}
    rcs = {t.n: sa.cone(REPORTS / f"{t.n}_rt_conesplit.json") for t in nets}

    ck = {t.n: sa.read_matrix(REPORTS / f"{t.n}_ck_matrix.csv") for t in nets}
    ccs = {t.n: sa.cone(REPORTS / f"{t.n}_ck_conesplit.json") for t in nets}

    def cexh(t, i):   # clocked SR / EN configs, exhaustive
        return bool(ck[t.n]) and (ck[t.n][i]["any"] == "D" or ccs[t.n].get(i) == "detected")

    def rexh(t, i):   # router exhaustive: strict D or detected for every power-up state
        return rt[t.n][i]["any"] == "D" or rcs[t.n].get(i) == "detected"
    it = RJ["items"]
    out = ["# LUT4AB tile: routed switch-matrix configs (Phase 9 step 1)", "",
           f"{len(RJ['configs'])} configs RT00..RT{len(RJ['configs']) - 1:02d} from configs/sm_router.py "
           "(greedy router: per mux, an open code routed to an observed output, Dijkstra over free muxes, "
           "loop-free; exact model grading; hill-climb polish; compaction). Same stimulus, LUT setup and "
           "observation as the baseline (reports/sm_summary.md). Tile-level observability: an upper bound.",
           "",
           f"- Model-level items (code observed at 0, at 1, and against every input one select bit away): "
           f"router {it['shown']} of {it['total']} = {sa.pct(it['shown'], it['total'])}, "
           f"baseline {it['baseline_shown']} = {sa.pct(it['baseline_shown'], it['total'])}.",
           f"- Not reachable by combinational configs ({len(RJ['unreachable'])} muxes, SR / EN only): "
           f"{', '.join(RJ['unreachable'])}.", "",
           "## Coverage", "",
           "Exhaustive = strict D, or detected for every power-up state of the unwritten elements "
           "(cone-split, sim/run_conesplit.py --suite router --xonly; FrameStrobe stems left out as for "
           "the baseline). \"+ {BC1, D0C, D1}\": either suite detects the fault exhaustively.", "",
           "| netlist | population | faults | baseline strict | baseline exhaustive | router strict | "
           "router exhaustive | router sequence strict | baseline + {BC1, D0C, D1} | router + {BC1, D0C, D1} |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for t in nets:
        r, rq = rt[t.n], rtq[t.n]
        for p in sa.POPS + ["all"]:
            ids = [i for i in t.ids if p == "all" or t.pop(i) == p]
            vals = [sum(t.sm_strict(i) for i in ids), sum(t.sm_exh(i) for i in ids),
                    sum(r[i]["any"] == "D" for i in ids), sum(rexh(t, i) for i in ids),
                    sum(rq[i]["any"] == "D" for i in ids),
                    sum(t.sm_exh(i) or t.lut_exh(i) for i in ids),
                    sum(rexh(t, i) or t.lut_exh(i) for i in ids)]
            out.append(f"| {t.n} | {sa.POP_LABEL.get(p, 'all')} | {len(ids)} | " +
                       " | ".join(f"{v} = {sa.pct(v, len(ids))}" for v in vals) + " |")
    out += ["", "## Switch-matrix faults per mux group", "",
            "| netlist | group | faults | baseline exhaustive | router exhaustive |", "|---|---|---|---|---|"]
    owners = {}
    for t in nets:
        ow = sm_owner.Owner(t.n)
        owners[t.n] = {i: ow.of_fault(t.faults[i]) for i in t.ids if t.pop(i) == "sm"}
        groups = defaultdict(list)
        for i, o in owners[t.n].items():
            m = sa.sc.MUXES.get(o)
            key = (o if m is None else "direct wire" if m.model == "direct" else f"{m.fan_in}:1")
            if m is not None and o in RJ["unreachable"]:
                key += " (SR / EN)"
            groups[key].append(i)
        for key in sorted(groups, key=lambda k: (k[0].isdigit() is False, k)):
            ids = groups[key]
            b = sum(t.sm_exh(i) for i in ids)
            g = sum(rexh(t, i) for i in ids)
            out.append(f"| {t.n} | {key} | {len(ids)} | {b} = {sa.pct(b, len(ids))} | {g} = {sa.pct(g, len(ids))} |")
    out += ["", "## Switch-matrix faults the router misses", "",
            "Owner: sim/sm_owner.py (nearest mux-named net forward). \"Only through a latch or flop\": "
            "no gate-only path to a tile output (LUT flop / SR / EN logic, config access).", "",
            "| netlist | owner group | status | faults | examples |", "|---|---|---|---|---|"]
    for t in nets:
        miss = Counter()
        ex = defaultdict(list)
        cr = comb_reach(t.n)
        for i, o in owners[t.n].items():
            st = rt[t.n][i]["any"]
            if rexh(t, i):
                continue
            m = sa.sc.MUXES.get(o)
            grp = ("SR / EN mux" if o in RJ["unreachable"] else
                   o if m is None else "direct wire" if m.model == "direct" else f"{m.fan_in}:1 mux")
            if not cr(t.faults[i]["net"]):
                grp += ", reaches outputs only through a latch or flop"
            miss[(grp, st)] += 1
            if len(ex[(grp, st)]) < 3:
                f = t.faults[i]
                ex[(grp, st)].append(f"{f['path']} SA{f['polarity']}")
        for (g, st), n in miss.most_common():
            out.append(f"| {t.n} | {g} | {st} | {n} | {', '.join(ex[(g, st)])} |")
    if all(ck[t.n] for t in nets):
        out += ["", "## Router + clocked SR / EN configs (Phase 9 step 2)", "",
                "8 clocked configs CK00..CK07 (configs/sm_clocked.py): LUTs registered, J_SR_BEG0 = "
                "J_EN_BEG0 = c driven from tile inputs, every LUT through all (SR code, EN code, reset "
                "value) combinations, flop outputs routed to tile outputs, 64 clocked vectors. "
                "Exhaustive (cone-split) where the run exists. A fault counts when some suite detects it.", "",
                "| netlist | population | faults | router | clocked | router + clocked | "
                "router + clocked + {BC1, D0C, D1} |", "|---|---|---|---|---|---|---|"]
        for t in nets:
            for p in sa.POPS + ["all"]:
                ids = [i for i in t.ids if p == "all" or t.pop(i) == p]
                vals = [sum(rexh(t, i) for i in ids), sum(cexh(t, i) for i in ids),
                        sum(rexh(t, i) or cexh(t, i) for i in ids),
                        sum(rexh(t, i) or cexh(t, i) or t.lut_exh(i) for i in ids)]
                out.append(f"| {t.n} | {sa.POP_LABEL.get(p, 'all')} | {len(ids)} | " +
                           " | ".join(f"{v} = {sa.pct(v, len(ids))}" for v in vals) + " |")
        out += ["", "Switch-matrix faults left (router + clocked):", "",
                "| netlist | owner group | router status | clocked status | faults | examples |",
                "|---|---|---|---|---|---|"]
        for t in nets:
            miss, ex = Counter(), defaultdict(list)
            cr = comb_reach(t.n)
            for i, o in owners[t.n].items():
                if rexh(t, i) or cexh(t, i):
                    continue
                m = sa.sc.MUXES.get(o)
                grp = ("SR / EN mux" if o in RJ["unreachable"] else
                       o if m is None else "direct wire" if m.model == "direct" else f"{m.fan_in}:1 mux")
                if not cr(t.faults[i]["net"]):
                    grp += ", only through a latch or flop"
                key = (grp, rt[t.n][i]["any"], ck[t.n][i]["any"])
                miss[key] += 1
                if len(ex[key]) < 3:
                    f = t.faults[i]
                    ex[key].append(f"{f['path']} SA{f['polarity']}")
            for (g, a, b), n in miss.most_common():
                out.append(f"| {t.n} | {g} | {a} | {b} | {n} | {', '.join(ex[(g, a, b)])} |")
    (REPORTS / "rt_summary.md").write_text("\n".join(out) + "\n")
    print("reports/rt_summary.md")


if __name__ == "__main__":
    main()
