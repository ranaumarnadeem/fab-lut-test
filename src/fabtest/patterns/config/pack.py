"""Fault-targeted packing (fabtest pack): extra configs for the faults a run leaves undetected.

The router fills a config greedily, and a target fails when the mux it needs is already taken
by another route of that config. Packing chooses which targets share a config first:

  1. every undetected fault of the run's fault simulation is mapped to the switch-matrix
     outputs its value reaches first (forward over the combinational cells of the netlist):
     the muxes whose codes test it; a mux gets the faults' weight, spread over its codes;
  2. per new config (its own backbone, as the router's: bias = config index) every code of
     those muxes is routed alone (Cfg.try_cover on a copy: forward to an observed net and
     the inputs driven): a candidate is the set of mux codes its route needs;
  3. bin packing with conflicts: candidates into the new configs, a mux takes one code per
     config; CP-SAT (OR-Tools, the default) maximizing the weight of every code the routes
     set, or greedy first fit by weight (the fault weight of the target, then its router
     items; also when OR-Tools is not installed);
  4. each config: backbone, its packed routes, then the router's greedy cover and fill.

On the demo (96 configs, 8 more, 550 undetected faults of the sample): greedy 199 and CP-SAT
232 detected against 180 with the router's own 8 (README).

Target kinds (kinds): mux, the switch-matrix muxes above. carry: a fault whose first
switch-matrix output is a LUT's Ci (the direct mux from the previous LUT's Co, which no
ordinary config reads) targets that LUT in carry mode, I0 from Ci (a carry graph,
fab_router.Graph(carry=True); the config's "__ci"); its candidate drives the previous
LUT's I1 / I2 and conflicts with every route that reads the LUT's I0. ensr: the EN / SR
muxes of the LUT flops, pinned to 1 / 0 by the backbone, take a non-constant code (an
ensr graph: EN / SR -> O); the pin is freed for the packed route.
"""
from __future__ import annotations

import copy
import multiprocessing as mp
import re
import time
from collections import defaultdict

from fabtest.model import fabric
from fabtest.patterns.config import fab_router as fr

MUXNET = re.compile(r"^Tile_X(\d+)Y(\d+)_\w+?\.(?:Tile_X(\d+)Y(\d+)_\w+?\.)?Inst_\w+_switch_matrix\.(\w+)$")
LATCH, DFF = 4, 5
MAX_DEPTH = 40   # BFS levels from a fault net to the first switch-matrix output
KINDS = ("mux", "carry", "ensr")
CI_RE = re.compile(r"^L([A-H])_Ci$")
ENSR_RE = re.compile(r"^L[A-H]_(EN|SR)$")


def is_ci(i) -> bool:
    """A carry target: (tile, "__ci<LUT>"), code 1 (carry mode) or 0 (I0 read by a route)."""
    return isinstance(i[1], str) and i[1].startswith("__ci")


def mux_nets(nl) -> dict:
    """Netlist net -> model mux id (tile, mux) of every switch-matrix output (supertile
    sub-tiles at their own coordinates, as FullFabric)."""
    out = {}
    for name, b in nl.net_bit.items():
        m = MUXNET.match(name)
        if m:
            X, Y, dx, dy, o = m.groups()
            t = (int(X) + int(dx) - 1, int(Y) + int(dy)) if dx else (int(X) - 1, int(Y))
            out[int(b)] = (t, o)
    return out


LUTPIN = re.compile(r"^Tile_X(\d+)Y(\d+)_LUT4AB\.Inst_L([A-H])_LUT4c_frame_config_dffesr\.(Co|EN|SR|LUT_flop)$")


def lut_pins(nl, g, kinds) -> tuple[dict, dict]:
    """Targets on LUT cell pins, which keep their names in the flat netlist (the switch-matrix
    names of the same nets do not): bit -> targets of a Co (carry: the next LUT of the chain in
    carry mode) and of an EN / SR (ensr: its mux); and flop output bit -> the LUT's EN / SR
    muxes (ensr: a fault that reaches the flop through its set / reset logic)."""
    nxt = {p: k for k, p in g.ci_prev.items()}
    pins: dict = defaultdict(set)
    flops: dict = defaultdict(set)
    for name, b in nl.net_bit.items():
        m = LUTPIN.match(name)
        if not m:
            continue
        t, x, p = (int(m.group(1)) - 1, int(m.group(2))), m.group(3), m.group(4)
        if p == "Co" and "carry" in kinds and (t, x) in nxt:
            tn, xn = nxt[(t, x)]
            pins[int(b)].add((tn, f"__ci{xn}"))
        elif p in ("EN", "SR") and "ensr" in kinds and (t, f"L{x}_{p}") in g.pos:
            pins[int(b)].add((t, f"L{x}_{p}"))
        elif p == "LUT_flop" and "ensr" in kinds:
            flops[int(b)] |= {(t, f"L{x}_{q}") for q in ("EN", "SR") if (t, f"L{x}_{q}") in g.pos}
    return pins, flops


def nearest_muxes(nl, mux_of: dict, bit: int, pins=None, flops=None) -> set:
    """The mux ids of the first BFS level (forward, combinational cells) from bit that reaches
    a switch-matrix output; pins / flops (lut_pins): LUT pins and flops that are targets too."""
    seen, front = {bit}, [bit]
    for _ in range(MAX_DEPTH):
        hit = {mux_of[n] for n in front if n in mux_of}
        if pins:
            for n in front:
                hit |= pins.get(n, set())
        if hit or not front:
            return hit
        nxt = []
        for n in front:
            for c in nl.rd_idx[nl.rd_off[n]:nl.rd_off[n + 1]]:
                if nl.type[c] in (LATCH, DFF):
                    if flops and int(nl.out[c]) in flops:
                        hit |= flops[int(nl.out[c])]
                    continue
                o = int(nl.out[c])
                if o not in seen:
                    seen.add(o)
                    nxt.append(o)
        if hit:
            return hit
        front = nxt
    return set()


def fault_weights(nl, g, faults, kinds=("mux",)) -> tuple[dict, dict]:
    """(target, code) -> weight (each fault 1, over its nearest targets, each mux over its
    codes) and a count of mapped / unmapped faults (and of those with a carry or EN / SR
    target). kinds: see the module docstring; with ensr an EN / SR mux takes only its
    non-constant codes."""
    mux_of = mux_nets(nl)
    pins, flops = lut_pins(nl, g, kinds) if {"carry", "ensr"} & set(kinds) else (None, None)
    wm: dict = defaultdict(float)
    stat = {"mapped": 0, "unmapped": 0, "carry": 0, "ensr": 0}
    for f in faults:
        near = nearest_muxes(nl, mux_of, int(nl.net_bit[f["net"]]), pins, flops)
        hit = {i for i in near if not is_ci(i) and i in g.pos and fr.MM(i).model != "direct"}
        if "mux" not in kinds:
            hit = {i for i in hit if ENSR_RE.match(i[1])} if "ensr" in kinds else set()
        if "carry" in kinds:
            hit |= {i for i in near if is_ci(i) and (i[0], i[1][4:]) in g.ci_ok}
            for t, o in near:   # a fabric whose netlist keeps the switch-matrix name of a Ci
                m = None if is_ci((t, o)) else CI_RE.match(o)
                if m and (t, o) in g.pos and fr.MM((t, o)).model == "direct" and (t, m.group(1)) in g.ci_ok:
                    hit.add((t, f"__ci{m.group(1)}"))
        stat["mapped" if hit else "unmapped"] += 1
        stat["carry"] += any(is_ci(i) for i in hit)
        stat["ensr"] += "ensr" in kinds and any(ENSR_RE.match(i[1]) for i in hit if not is_ci(i))
        for i in hit:
            wm[i] += 1 / len(hit)
    w = {}
    for i, v in wm.items():
        if is_ci(i):
            w[(i, 1)] = v
            continue
        ks = [k for k in range(fr.MM(i).fan_in) if not g.forbidden(i, k)
              and not ("ensr" in kinds and ENSR_RE.match(i[1]) and g.src(i, k) is None)]
        for k in ks:
            w[(i, k)] = v / len(ks)
    return w, stat


def _clone(c):
    n = copy.copy(c)
    n.sel, n.obs, n.cut, n._obs_saved = dict(c.sel), set(c.obs), set(c.cut), []
    n.lut_const = {t: dict(v) for t, v in c.lut_const.items()}
    n.consts = {v: list(lst) for v, lst in c.consts.items()}
    n.__dict__["_ctrl_set"] = set(c.__dict__["_ctrl_set"])
    n.__dict__["_root"] = dict(c.__dict__["_root"])
    n.__dict__.pop("_om_src", None)
    n.lut_ci = {t: set(v) for t, v in getattr(c, "lut_ci", {}).items()}
    if "_lutcut" in c.__dict__:
        n.__dict__["_lutcut"] = c.__dict__["_lutcut"]
    return n


_JOB = None   # (graph, open work, targets, kinds) for the forked workers


def _candidates(bias: int):
    """Routes of the targets, each alone on the backbone of config `bias` (pure Python Cfg:
    it can be copied)."""
    g, open_work, targets, kinds = _JOB
    base = fr.Cfg(g)
    base.bias, base._open_work = bias, open_work
    fr.backbone(base, bias)
    fr._ctrl(base)
    cn = g.arr.canon
    out = {}
    for i, k in targets:
        if is_ci(i):   # carry mode: the LUT output routed to an observed net (along its chain:
            t, x = i[0], i[1][4:]   # no new mux; the seed's backbone links the chain into I1..I3)
            m0 = g.mux_of.get(cn(t, f"L{x}_I0"))
            c = _clone(base)
            if not fr.route_net(c, cn(t, f"L{x}_O")):
                continue
            p = g.ci_prev.get((t, x))
            if p is not None:   # the previous LUT's I1 / I2 (its majority with Ci) driven
                for j in (1, 2):
                    fr.drive_input(c, cn(p[0], f"L{p[1]}_I{j}"))
            if m0 is not None and m0 in c.sel and m0 not in base.sel:
                continue   # the drive reads the LUT's own I0
            r = {m: v for m, v in c.sel.items() if base.sel.get(m) != v}
            r[i] = 1
            out[(i, k)] = r
            continue
        if i in base.sel:
            if "ensr" not in kinds or not ENSR_RE.match(i[1]) or base.sel[i] == k:
                continue   # fixed by the backbone
            c = _clone(base)
            del c.sel[i]   # the backbone's EN = 1 / SR = 0, freed for this target
        else:
            c = _clone(base)
        if c.try_cover(i, k, open_work):
            out[(i, k)] = {m: v for m, v in c.sel.items() if base.sel.get(m) != v}
    return bias, out


def _grade(c: int) -> dict:
    arr, prior = _JOB
    return fabric.grade(arr, {t: dict(prior[c][arr.tn(t)]) for t in arr.tiles})


def _solve(cand: dict, weight, K: int, time_limit: float, solver: str = "cpsat") -> tuple[list, str]:
    """cand: (target, bin) -> {mux: code}. Bins of targets: CP-SAT (solver="cpsat", when
    OR-Tools is installed), else greedy first fit (by weight)."""
    cp_model = None
    if solver == "cpsat":
        try:
            from ortools.sat.python import cp_model
        except ImportError:
            pass
    if cp_model is None:
        bins = [dict() for _ in range(K)]
        out = [[] for _ in range(K)]
        done = set()
        for (key, b) in sorted(cand, key=lambda kb: -weight(kb[0])):
            if key in done:
                continue
            r = cand[(key, b)]
            if all(bins[b].get(m, c) == c for m, c in r.items()):
                bins[b].update(r)
                out[b].append(key)
                done.add(key)
        return out, "greedy first fit" + (" (OR-Tools not installed: pip install ortools)" if solver == "cpsat" else "")
    mdl = cp_model.CpModel()
    x = {kb: mdl.NewBoolVar("") for kb in cand}
    uses = defaultdict(list)
    for (key, b), r in cand.items():
        for m, code in r.items():
            uses[(m, code, b)].append((key, b))
    y, codes = {}, defaultdict(list)
    for (m, code, b), kbs in uses.items():
        v = y[(m, code, b)] = mdl.NewBoolVar("")
        for kb in kbs:
            mdl.AddImplication(x[kb], v)
        mdl.AddBoolOr([x[kb] for kb in kbs]).OnlyEnforceIf(v)
        codes[(m, b)].append(v)
    for vs in codes.values():
        if len(vs) > 1:
            mdl.AddAtMostOne(vs)
    per_key = defaultdict(list)
    for (key, b), v in x.items():
        per_key[key].append(v)
    for vs in per_key.values():
        mdl.AddAtMostOne(vs)
    by_code = defaultdict(list)
    for (m, code, b), v in y.items():
        by_code[(m, code)].append(v)
    obj = []
    for mc, vs in by_code.items():
        w = int(round(weight(mc)))
        if w:
            cov = mdl.NewBoolVar("")
            mdl.AddBoolOr(vs).OnlyEnforceIf(cov)
            obj.append(w * cov)
    mdl.Maximize(sum(obj))
    sol = cp_model.CpSolver()
    sol.parameters.max_time_in_seconds = time_limit
    sol.parameters.num_workers = 4
    st = sol.Solve(mdl)
    if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return [[] for _ in range(K)], f"CP-SAT {sol.StatusName(st)}"
    out = [[] for _ in range(K)]
    for (key, b), v in x.items():
        if sol.Value(v):
            out[b].append(key)
    return out, f"CP-SAT {sol.StatusName(st)} (objective {sol.ObjectiveValue():.0f}, bound {sol.BestObjectiveBound():.0f})"


def pack(arr: fabric.Array, prior: list, nl, faults: list, K: int, time_limit: float = 300,
         jobs: int = 4, max_targets: int = 4000, verbose: bool = True, solver: str = "cpsat",
         kinds=("mux",)):
    """K configs after the prior ones (selects per tile name) for the undetected faults.
    kinds: target kinds (KINDS, module docstring). Returns (configs, log, info)."""
    global _JOB
    t0 = time.time()
    bad = set(kinds) - set(KINDS)
    if bad or not kinds:
        raise SystemExit(f"fabtest pack: target kinds are {', '.join(KINDS)} (got {', '.join(sorted(bad)) or 'none'})")
    ensr = "ensr" in kinds
    g = fr.Graph(arr, carry="carry" in kinds, ensr=ensr)
    reach = g.reachable()
    open_work = {(t, o, k): fabric.work_items_m(fr.MM((t, o)), k) for t, o in g.ids if (t, o) in reach
                 for k in range(fr.MM((t, o)).fan_in) if not g.forbidden((t, o), k)}
    _JOB = (arr, prior)
    with mp.get_context("fork").Pool(jobs) as pool:
        for shown in pool.imap_unordered(_grade, range(len(prior))):
            for key, items in shown.items():
                if key in open_work:
                    open_work[key] -= items
    if verbose:
        print(f"  {len(prior)} prior configs graded: {sum(len(v) for v in open_work.values())} router items open "
              f"({time.time() - t0:.0f} s)", flush=True)
    fw, stat = fault_weights(nl, g, faults, kinds)
    targets = sorted(fw, key=lambda ik: -fw[ik])[:max_targets]
    if verbose:
        extra = "".join(f", {stat[k]} with a {k} target" for k in ("carry", "ensr") if k in kinds)
        print(f"  {len(faults)} undetected faults: {stat['mapped']} mapped to {len({i for i, _ in fw})} targets"
              f"{extra}, {len(targets)} target codes ({time.time() - t0:.0f} s)", flush=True)
    idx0 = len(prior)
    _JOB = (g, open_work, targets, tuple(kinds))
    cand = {}
    with mp.get_context("fork").Pool(min(jobs, K)) as pool:
        for bias, out in pool.imap_unordered(_candidates, range(idx0, idx0 + K)):
            cand.update({(key, bias - idx0): r for key, r in out.items()})
    _JOB = None
    if g.ci_nodes:   # a route that reads a LUT's I0 excludes that LUT's carry mode (code 0 vs 1)
        cn = arr.canon
        i0_of = {g.mux_of[cn(t, f"L{x}_I0")]: (t, x) for t, x in g.ci_nodes if cn(t, f"L{x}_I0") in g.mux_of}
        for kb in list(cand):
            r = cand[kb]
            for m in [m for m in r if m in i0_of]:
                ci = (i0_of[m][0], f"__ci{i0_of[m][1]}")
                if r.get(ci) == 1:
                    del cand[kb]
                    break
                r[ci] = 0
    if verbose:
        n_ci = sum(1 for (key, _b) in cand if is_ci(key[0]))
        n_es = sum(1 for (key, _b) in cand if not is_ci(key[0]) and ENSR_RE.match(key[0][1])) if ensr else 0
        print(f"  {len(cand)} candidate routes on {K} backbones"
              + (f" ({n_ci} carry)" if g.ci_nodes else "") + (f" ({n_es} EN / SR)" if ensr else "")
              + f" ({time.time() - t0:.0f} s)", flush=True)

    def weight(ik):
        return 1000 * fw.get(ik, 0) + len(open_work.get((*ik[0], ik[1]), ()))
    bins, how = _solve(cand, weight, K, time_limit, solver)
    if verbose:
        print(f"  {how}: {[len(b) for b in bins]} routes per config ({time.time() - t0:.0f} s)", flush=True)
    configs, log = [], []
    for b, chosen in enumerate(bins):
        ci_b = {(key[0][0], key[0][1][4:]) for key in chosen if is_ci(key[0])}

        def seed(cfg, bias, chosen=chosen, b=b, ci_b=ci_b):
            rep = fr.backbone(cfg, bias, ci=ci_b)   # the carry-mode LUTs cut from the start
            placed = 0
            for key in chosen:
                before, freed, ok = set(cfg.sel), {}, True
                for m, code in cand[(key, b)].items():
                    if is_ci(m):
                        continue   # set by the backbone
                    if ensr and m in cfg.sel and cfg.sel[m] != code and ENSR_RE.match(m[1]):
                        freed[m] = cfg.sel[m]   # the backbone's EN / SR pin
                        del cfg.sel[m]
                    if not cfg.assign(m, code):
                        ok = False
                        break
                if ok:
                    placed += 1
                    continue
                fr._undo(cfg, before)
                for m, old in freed.items():   # the pins back
                    if cfg.sel.get(m) != old:
                        if m in cfg.sel:
                            del cfg.sel[m]
                        cfg.assign(m, old)
            return {**rep, "packed": placed, "routes": len(chosen), "carry": len(ci_b)}
        sels = fr.build_config(g, open_work, defaultdict(int), idx0 + b, seed)
        gained = 0
        for key, items in fabric.grade(arr, sels).items():
            if key in open_work:
                n0 = len(open_work[key])
                open_work[key] -= items
                gained += n0 - len(open_work[key])
        configs.append({arr.tn(t): s for t, s in sels.items()})
        log.append({"gain": gained, "backbone": g.last_seed})
        if verbose:
            print(f"  config {idx0 + b}: {g.last_seed['packed']} of {len(chosen)} packed routes"
                  + (f" ({len(ci_b)} carry-mode LUTs)" if ci_b else "")
                  + f", +{gained} router items ({time.time() - t0:.0f} s)", flush=True)
    return configs, log, {"faults": len(faults), **stat, "targets": len(targets), "candidates": len(cand),
                          "solver": how, "kinds": list(kinds)}
