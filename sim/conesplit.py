"""Cone-split exhaustive power-up enumeration (exact, no k cap).

For a fault, U = storage elements it leaves unwritten (power-up value
unknown, held for the whole test, the same in every config). Instead of
enumerating 2^|U| assignments, U is split into groups whose forward cones
to the observed outputs share no cell; each group is enumerated on its own
with the rest of U at X.

Cones. In each config the netlist is evaluated in 3-valued logic with the
constants that hold after configuration: every written config latch (its
value from the faulty run's end-of-run state dump), tile inputs held at 0,
GND/VCC, and the fault's stuck net. Cells whose output stays non-constant
are live; the cone of element e in that config is the set of live cells on
a path from e to an observed output (forward from e, backward from the
outputs, through gates, latches and flops). The cone used is the union over
the configs of the set. Two elements whose cones share a cell go into the
same group (union-find). Disjoint cells imply disjoint observed outputs,
because an output port belongs to the cone of the cell driving it.

Pruning with end-of-run latch values is valid only after configuration.
It is used for a config only if no UserCLK edge happens before its last
frame strobe (nothing can store a U-dependent value while latches are
still X); otherwise no cell is treated as constant in that config (the
cell's A2, which clocks between its two writes).

Verdict. Strict D in some config: detected (monotone in X). Otherwise a
group escapes under a local assignment if no config shows a 0/1 mismatch on
the group's own observed sample bits. The fault is detected for every
power-up state iff some group has no escaping assignment; else it escapes,
with a witness made of one escaping assignment per group. A group larger
than `max_group` stops the decision for that fault (reported, not sampled).
"""
from __future__ import annotations

import itertools
import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "faults"))
import gen_faults  # noqa: E402

# cell type -> output pin, input pins (all other pins of known cells are inputs)
GATES = {"$_AND_": "Y", "$_OR_": "Y", "$_NOT_": "Y", "$_BUF_": "Y",
         "$_DLATCH_P_": "Q", "$_DFFE_PP_": "Q"}


@dataclass
class Graph:
    cells: dict[str, dict]                      # name -> {type, ins: [bit], out: bit}
    readers: dict[int, list[str]] = field(default_factory=dict)   # bit -> cells reading it
    driver: dict[int, str] = field(default_factory=dict)          # bit -> driving cell
    port_bits: dict[str, int] = field(default_factory=dict)       # port bit name -> bit
    net_bit: dict[str, int] = field(default_factory=dict)         # canonical net name -> bit


def load_graph(netlist_json: Path) -> Graph:
    _, mod = gen_faults.load_top(netlist_json, None)
    bits = gen_faults.analyse(mod)
    g = Graph(cells={})
    for name, cell in mod["cells"].items():
        t = cell["type"]
        if t in GATES:
            out_pin = GATES[t]
        else:
            # Library cell (sky130): pin directions from the JSON; its output is
            # never treated as constant, so cones are structural (larger, still exact).
            outs = [p for p, d in cell["port_directions"].items() if d == "output"]
            if len(outs) != 1:
                raise SystemExit(f"conesplit: {t} has outputs {outs}")
            out_pin = outs[0]
        ins = [b for p, sig in cell["connections"].items() if p != out_pin for b in sig]
        out = cell["connections"][out_pin][0]
        g.cells[name] = {"type": t, "ins": ins, "out": out, "conn": cell["connections"]}
        g.driver[out] = name
        for b in ins:
            if isinstance(b, int):
                g.readers.setdefault(b, []).append(name)
    for pname, port in mod["ports"].items():
        w = len(port["bits"])
        for k, b in enumerate(port["bits"]):
            g.port_bits[f"{pname}[{k}]"] = b
            if w == 1:
                g.port_bits[pname] = b
    for b, e in bits.items():
        g.net_bit[e["names"][0][0]] = b
        for a in e.get("aliases", []):
            g.net_bit.setdefault(a, b)
    return g


V = 2  # non-constant


def propagate(g: Graph, const: dict[int, int]) -> dict[int, int]:
    """3-valued fixpoint: bit -> 0 / 1 / V. `const` pins nets (fault, inputs, latches)."""
    val: dict[int, int] = {}

    def get(b) -> int:
        if isinstance(b, str):   # yosys JSON constant bit: "0", "1", "x", "z"
            return 0 if b == "0" else 1 if b == "1" else V
        return val.get(b, V)

    for b, v in const.items():
        val[b] = v
    order = list(g.cells)
    changed = True
    while changed:
        changed = False
        for name in order:
            c = g.cells[name]
            o = c["out"]
            if o in const:
                continue
            t = c["type"]
            if t not in GATES or t in ("$_DLATCH_P_", "$_DFFE_PP_"):
                nv = V   # state elements are pinned through `const` when known
            else:
                ins = [get(b) for b in c["ins"]]
                if t == "$_AND_":
                    nv = 0 if 0 in ins else 1 if all(x == 1 for x in ins) else V
                elif t == "$_OR_":
                    nv = 1 if 1 in ins else 0 if all(x == 0 for x in ins) else V
                elif t == "$_NOT_":
                    nv = V if ins[0] == V else 1 - ins[0]
                else:  # $_BUF_
                    nv = ins[0]
            if val.get(o, V) != nv:
                val[o] = nv
                changed = True
    return val


def cone(g: Graph, val: dict[int, int], start_bit: int, out_bits: set[int],
         back_live: set[str]) -> set[str]:
    """Live cells reachable from start_bit that can reach an observed output."""
    seen: set[str] = set()
    q = deque([start_bit])
    vis_bits = {start_bit}
    while q:
        b = q.popleft()
        for c in g.readers.get(b, ()):
            if c in seen or c not in back_live:
                continue
            seen.add(c)
            o = g.cells[c]["out"]
            if o not in vis_bits:
                vis_bits.add(o)
                q.append(o)
    return seen


def backward_live(g: Graph, val: dict[int, int], out_bits: set[int]) -> set[str]:
    """Live cells from which an observed output is reachable through live cells."""
    live = {n for n, c in g.cells.items() if val.get(c["out"], V) == V}
    res: set[str] = set()
    q = deque(b for b in out_bits)
    seen_bits = set(out_bits)
    while q:
        b = q.popleft()
        c = g.driver.get(b)
        if c is None or c not in live or c in res:
            continue
        res.add(c)
        for i in g.cells[c]["ins"]:
            if isinstance(i, int) and i not in seen_bits:
                seen_bits.add(i)
                q.append(i)
    return res


def groups_for(g: Graph, elem_cells: dict[int, str], U: list[int],
               consts_per_cfg: list[dict[int, int] | None], out_bits: set[int]) -> tuple[list[list[int]], dict[int, set[str]]]:
    """Union-find groups of U by shared cone cells (union over configs)."""
    cones: dict[int, set[str]] = {e: set() for e in U}
    for const in consts_per_cfg:
        val = propagate(g, const or {})
        back = backward_live(g, val, out_bits)
        for e in U:
            qbit = g.cells[elem_cells[e]]["out"]
            c = cone(g, val, qbit, out_bits, back)
            cell_e = elem_cells[e]
            if cell_e in back:
                c.add(cell_e)
            cones[e] |= c
    parent = {e: e for e in U}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    owner: dict[str, int] = {}
    for e in U:
        for c in cones[e]:
            if c in owner:
                a, b = find(owner[c]), find(e)
                if a != b:
                    parent[a] = b
            else:
                owner[c] = e
    groups: dict[int, list[int]] = {}
    for e in U:
        groups.setdefault(find(e), []).append(e)
    return sorted(groups.values(), key=lambda x: (len(x), x)), cones


def group_outputs(g: Graph, cells: set[str], out_bits: set[int]) -> set[int]:
    return {g.cells[c]["out"] for c in cells if g.cells[c]["out"] in out_bits}


def mismatch_on(golden: list[str], faulty: list[str], positions: set[int]) -> bool:
    for gs, fs in zip(golden, faulty):
        for p in positions:
            if gs[p] in "01" and fs[p] in "01" and gs[p] != fs[p]:
                return True
    return False


def _no_escape(run, golden, p_cfgs, elems: list[int], pos: set[int]):
    """First assignment of `elems` (others at X) with no mismatch on `pos`, or None."""
    runs = 0
    for bits in itertools.product((0, 1), repeat=len(elems)):
        dep = dict(zip(elems, bits))
        hit = False
        for c in (p_cfgs if pos else []):
            runs += 1
            if mismatch_on(golden[c], run(c, dep), pos):
                hit = True
                break
        if not hit:
            return dep, runs
    return None, runs


def decide(run, golden: dict[str, list[str]], p_cfgs: list[str], regroup,
           branchable: set[int], max_group: int = 16, max_depth: int = 8,
           fixed: dict[int, int] | None = None) -> dict:
    """run(cfg, deposits) -> samples; regroup(fixed) -> (groups, group_pos, elem_pos)
    for the elements not in `fixed`, with the fixed ones pinned as constants.

    1. Groups (smallest first): one escaping local assignment decides a group;
       the fault is detected iff some group has none.
    2. A group is larger than max_group and no small group decided: per
       observed output o, S_o = elements whose cone reaches o (the rest
       cannot affect o); if |S_o| <= max_group and every assignment of S_o
       mismatches on o in some config, detected (a sufficient condition).
    3. Still undecided: case split (Shannon expansion) on one element of the
       largest group that is never possibly written in any config (it holds
       its power-up value for the whole test, so pinning it is exact):
       detected iff both values give detected; an escape in either value is
       an escape with that value in the witness.
    4. Depth or branchable elements exhausted: "group too large".
    """
    fixed = dict(fixed or {})
    groups, group_pos, elem_pos = regroup(fixed)
    sizes = [len(x) for x in groups]

    def wrap(run_dep):
        return lambda c, dep: run(c, {**fixed, **dep})

    r_run = wrap(run)
    witness: dict[int, int] = dict(fixed)
    runs = 0
    big = []
    for grp, pos in sorted(zip(groups, group_pos), key=lambda gp: len(gp[0])):
        if len(grp) > max_group:
            big.append(grp)
            continue
        esc, n = _no_escape(r_run, golden, p_cfgs, grp, pos)
        runs += n
        if esc is None:
            return {"verdict": "detected", "method": "group", "group_sizes": sizes,
                    "detecting_group": grp, "runs": runs, "fixed": fixed}
        witness.update(esc)
    if not big:
        return {"verdict": "escapes", "group_sizes": sizes, "witness": witness, "runs": runs,
                "fixed": fixed}
    outs = sorted({p for ps in elem_pos.values() for p in ps})
    tried = []
    for o in outs:
        s_o = sorted(e for e, ps in elem_pos.items() if o in ps)
        tried.append(len(s_o))
        if len(s_o) > max_group:
            continue
        esc, n = _no_escape(r_run, golden, p_cfgs, s_o, {o})
        runs += n
        if esc is None:
            return {"verdict": "detected", "method": "output", "group_sizes": sizes,
                    "detecting_output": o, "detecting_elements": s_o, "runs": runs, "fixed": fixed}
    cand = [e for e in max(big, key=len) if e in branchable]
    if len(fixed) >= max_depth or not cand:
        return {"verdict": "group too large", "group_sizes": sizes, "too_large": max(sizes),
                "output_set_sizes": tried, "runs": runs, "fixed": fixed}
    # branch on the candidate reaching the most outputs (most likely to split the group)
    e = max(cand, key=lambda x: (len(elem_pos.get(x, ())), -x))
    branches = []
    for v in (0, 1):
        r = decide(run, golden, p_cfgs, regroup, branchable, max_group, max_depth, {**fixed, e: v})
        runs += r["runs"]
        branches.append(r)
        if r["verdict"] != "detected":
            return {**r, "runs": runs, "split_on": e}
    return {"verdict": "detected", "method": "case split", "split_on": e,
            "group_sizes": sizes, "runs": runs, "fixed": fixed,
            "branch_methods": [b.get("method") for b in branches]}
