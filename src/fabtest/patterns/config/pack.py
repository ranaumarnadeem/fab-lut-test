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
     config; greedy first fit by weight (the fault weight of the target, then its router
     items), or CP-SAT (OR-Tools) maximizing the weight of every code the routes set;
  4. each config: backbone, its packed routes, then the router's greedy cover and fill.

On the demo (96 configs, 8 more, 550 undetected faults of the sample): greedy 199 and CP-SAT
232 detected against 180 with the router's own 8 (README).
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


def nearest_muxes(nl, mux_of: dict, bit: int) -> set:
    """The mux ids of the first BFS level (forward, combinational cells) from bit that reaches
    a switch-matrix output."""
    seen, front = {bit}, [bit]
    for _ in range(MAX_DEPTH):
        hit = {mux_of[n] for n in front if n in mux_of}
        if hit or not front:
            return hit
        nxt = []
        for n in front:
            for c in nl.rd_idx[nl.rd_off[n]:nl.rd_off[n + 1]]:
                if nl.type[c] in (LATCH, DFF):
                    continue
                o = int(nl.out[c])
                if o not in seen:
                    seen.add(o)
                    nxt.append(o)
        front = nxt
    return set()


def fault_weights(nl, g, faults) -> tuple[dict, dict]:
    """(mux, code) -> weight (each fault 1, over its nearest muxes, each mux over its codes)
    and a count of mapped / unmapped faults."""
    mux_of = mux_nets(nl)
    wm: dict = defaultdict(float)
    stat = {"mapped": 0, "unmapped": 0}
    for f in faults:
        hit = {i for i in nearest_muxes(nl, mux_of, int(nl.net_bit[f["net"]]))
               if i in g.pos and fr.MM(i).model != "direct"}
        stat["mapped" if hit else "unmapped"] += 1
        for i in hit:
            wm[i] += 1 / len(hit)
    w = {}
    for i, v in wm.items():
        ks = [k for k in range(fr.MM(i).fan_in) if not g.forbidden(i, k)]
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
    return n


_JOB = None   # (graph, open work, targets) for the forked workers


def _candidates(bias: int):
    """Routes of the targets, each alone on the backbone of config `bias` (pure Python Cfg:
    it can be copied)."""
    g, open_work, targets = _JOB
    base = fr.Cfg(g)
    base.bias, base._open_work = bias, open_work
    fr.backbone(base, bias)
    fr._ctrl(base)
    out = {}
    for i, k in targets:
        if i in base.sel:
            continue   # fixed by the backbone
        c = _clone(base)
        if c.try_cover(i, k, open_work):
            out[(i, k)] = {m: c.sel[m] for m in c.sel if m not in base.sel}
    return bias, out


def _grade(c: int) -> dict:
    arr, prior = _JOB
    return fabric.grade(arr, {t: dict(prior[c][arr.tn(t)]) for t in arr.tiles})


def _solve(cand: dict, weight, K: int, time_limit: float, solver: str = "greedy") -> tuple[list, str]:
    """cand: (target, bin) -> {mux: code}. Bins of targets: greedy first fit (by weight), or
    CP-SAT (solver="cpsat", when OR-Tools is installed)."""
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
        return out, "greedy first fit" + (" (no ortools)" if solver == "cpsat" else "")
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
         jobs: int = 4, max_targets: int = 4000, verbose: bool = True, solver: str = "greedy"):
    """K configs after the prior ones (selects per tile name) for the undetected faults.
    Returns (configs, log, info)."""
    global _JOB
    t0 = time.time()
    g = fr.Graph(arr)
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
    fw, stat = fault_weights(nl, g, faults)
    targets = sorted(fw, key=lambda ik: -fw[ik])[:max_targets]
    if verbose:
        print(f"  {len(faults)} undetected faults: {stat['mapped']} mapped to {len({i for i, _ in fw})} muxes, "
              f"{len(targets)} target codes ({time.time() - t0:.0f} s)", flush=True)
    idx0 = len(prior)
    _JOB = (g, open_work, targets)
    cand = {}
    with mp.get_context("fork").Pool(min(jobs, K)) as pool:
        for bias, out in pool.imap_unordered(_candidates, range(idx0, idx0 + K)):
            cand.update({(key, bias - idx0): r for key, r in out.items()})
    _JOB = None
    if verbose:
        print(f"  {len(cand)} candidate routes on {K} backbones ({time.time() - t0:.0f} s)", flush=True)

    def weight(ik):
        return 1000 * fw.get(ik, 0) + len(open_work.get((*ik[0], ik[1]), ()))
    bins, how = _solve(cand, weight, K, time_limit, solver)
    if verbose:
        print(f"  {how}: {[len(b) for b in bins]} routes per config ({time.time() - t0:.0f} s)", flush=True)
    configs, log = [], []
    for b, chosen in enumerate(bins):
        def seed(cfg, bias, chosen=chosen, b=b):
            rep = fr.backbone(cfg, bias)
            placed = 0
            for key in chosen:
                before = set(cfg.sel)
                if all(cfg.assign(m, code) for m, code in cand[(key, b)].items()):
                    placed += 1
                else:
                    fr._undo(cfg, before)
            return {**rep, "packed": placed, "routes": len(chosen)}
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
            print(f"  config {idx0 + b}: {g.last_seed['packed']} of {len(chosen)} packed routes, "
                  f"+{gained} router items ({time.time() - t0:.0f} s)", flush=True)
    return configs, log, {"faults": len(faults), **stat, "targets": len(targets), "candidates": len(cand),
                          "solver": how}
