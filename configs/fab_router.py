#!/usr/bin/env python3
"""PIP test router for an R x C LUT4AB array (Phase 10).

configs/sm_router.py on the array graph (configs/fabric.Array): a target
is (tile, mux, code) with the items of sm_router.work_items; observation
only at the array border, stimulus only at the border inputs. Per config,
greedy over the open targets:
  - (tile, mux) ordered by history (PathFinder-style: an open code was
    selected but not shown in an earlier config), then open work;
  - select an open code, route the mux output forward to an observed net
    (a border output or a path routed earlier in this config): Dijkstra over
    free muxes of every tile, inter-tile wires and pass-throughs are fixed
    edges (cost 0), LUT I -> O 2, Ci / I1 / I2 -> Co and MUX8LUT 4; a free mux
    costs 1, 0.4 when its code on the path is open, 4 when the mux still
    has other open codes;
  - every edge added only if it closes no loop; free muxes filled with an
    open loop-free code (loop repair by freeing a mux on the loop);
  - local polish: for the targets selected but not fully shown, the drivers
    of their sibling inputs and their LUT input muxes are moved while the
    exact gain (weighted by history) grows;
  - exact grading of the whole array (fabric.grade).
Configs are added until nothing open is left or configs stop adding items.

Run: python3 configs/fab_router.py R C [--max N]  -> configs/fab_router_<R>x<C>.json
"""
from __future__ import annotations

import argparse
import heapq
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "configs"), str(ROOT / "sim")]
import fabric  # noqa: E402
import sm_configs as sc  # noqa: E402
import sm_router  # noqa: E402

M = fabric.M
_ARR = None   # the array of the current Graph (tile types: fabric.Array.MT)
CBITS_PATTERN = [0x0F, 0xF0, 0x33, 0xCC, 0x55, 0xAA]   # W_IO Config_access bits per config


def MM(i):
    """Mux object of id (tile, mux name)."""
    return _ARR.MT(i[0])[i[1]]


class Graph:
    def __init__(self, arr: fabric.Array):
        global _ARR
        _ARR = self.arr = arr
        cn = arr.canon
        self.ids = [(t, o) for t in arr.tiles for o in arr.MT(t)]
        self.real = [(t, o) for t, o in self.ids if MM((t, o)).model != "direct"]
        self.net_of = {(t, o): cn(t, MM((t, o)).net) for t, o in self.ids}
        self.mux_of = {n: i for i, n in self.net_of.items()}
        self.pos = {i: k for k, i in enumerate(self.ids)}
        self.max_pop = getattr(arr, "route_max_pop", None)
        self.max_reach = getattr(arr, "route_max_reach", None)
        self.readers: dict[str, list] = defaultdict(list)
        for t, o in self.real:
            for k, s in enumerate(MM((t, o)).inputs):
                if s.kind != "const" and not self.forbidden((t, o), k):
                    self.readers[cn(t, s.net)].append(((t, o), k))
        self.fixed: dict[str, list] = defaultdict(list)
        self.fixed_pred: dict[str, list] = defaultdict(list)

        def edge(a, b, c):
            self.fixed[a].append((b, c))
            self.fixed_pred[b].append(a)
        for t in arr.io_tiles:   # W_IO BELs: pad in -> A_O, A_I / A_T -> pad out
            for p in "AB":
                edge(cn(t, f"{p}_O_top"), cn(t, f"{p}_O"), 0.0)
                edge(cn(t, f"{p}_I"), cn(t, f"{p}_I_top"), 0.0)
                edge(cn(t, f"{p}_T"), cn(t, f"{p}_T_top"), 0.0)
        for t in getattr(arr, "other_tiles", []):   # full fabric: other tile types
            gt = arr.generic(t)
            for o, m in gt.muxes.items():
                if m.model == "direct" and m.inputs[0].kind != "const":
                    edge(cn(t, m.inputs[0].net), cn(t, m.net), 0.0)
            for o, i in gt.passes.items():
                edge(cn(t, i), cn(t, o), 0.0)
        for t in arr.lut_tiles:
            for o, m in M.items():
                if m.model == "direct":
                    edge(cn(t, m.inputs[0].net), cn(t, m.net), 0.0)
            for o, i in sc.PASS.items():
                edge(cn(t, i), cn(t, o), 0.0)
            for out, ins in sm_router.BELDEPS.items():
                for i in ins:
                    edge(cn(t, i), cn(t, out), 2.0 if out.endswith("_O") else 4.0)
        self.out_nets = set(arr.border_out)

    def src(self, i, k):
        t, o = i
        s = MM(i).inputs[k]
        return None if s.kind == "const" else self.arr.canon(t, s.net)

    @staticmethod
    def forbidden(i, k) -> bool:
        """Sources with an unknown value are never selected (fabric.Array.forbidden_src)."""
        return _ARR.forbidden_src(i[0], i[1], k)

    def reachable(self) -> set:
        """Mux ids whose output can reach a border output for some selection."""
        pred = defaultdict(set)
        for a, lst in self.fixed.items():
            for b, _c in lst:
                pred[b].add(a)
        for n, lst in self.readers.items():
            for i, _k in lst:
                pred[self.net_of[i]].add(n)
        seen, stack = set(self.out_nets), list(self.out_nets)
        while stack:
            n = stack.pop()
            for p in pred.get(n, ()):
                if p not in seen:
                    seen.add(p)
                    stack.append(p)
        return {i for i in self.ids if self.net_of[i] in seen}


class Cfg:
    def __init__(self, g: Graph):
        self.g = g
        self.sel: dict = {}
        self.obs = set(g.out_nets)
        for n in list(g.out_nets):
            self._mark(n)

    def _open_muxes(self, open_work: dict) -> set:
        """Muxes with open work (cached per open_work dict; it does not change during a config)."""
        if getattr(self, "_om_src", None) is not open_work:
            self._om_src = open_work
            self._om = {(t, o) for (t, o, _k), v in open_work.items() if v}
        return self._om

    def succ(self, n):
        for t, _c in self.g.fixed.get(n, ()):
            yield t
        for i, k in self.g.readers.get(n, ()):
            if self.sel.get(i) == k:
                yield self.g.net_of[i]

    def preds(self, n):
        out = list(self.g.fixed_pred.get(n, ()))
        i = self.g.mux_of.get(n)
        if i is not None and i in self.sel:
            s = self.g.src(i, self.sel[i])
            if s is not None:
                out.append(s)
        return out

    def _mark(self, n):
        stack = [n]
        while stack:
            x = stack.pop()
            for p in self.preds(x):
                if p not in self.obs:
                    self.obs.add(p)
                    stack.append(p)

    def reaches(self, a, b) -> bool:
        """a reaches b (an edge b -> a would close a loop). Large models cap the search
        (fabric.route_max_reach): past it no loop is assumed; build_config's exact
        check (cycles + break_loops) catches the rest."""
        seen, stack = {a}, [a]
        cap = self.g.max_reach
        while stack:
            n = stack.pop()
            if n == b:
                return True
            if cap and len(seen) > cap:
                return False
            for t in self.succ(n):
                if t not in seen:
                    seen.add(t)
                    stack.append(t)
        return False

    def path_muxes(self, a, b) -> list:
        prev, stack = {a: None}, [a]
        while stack:
            n = stack.pop()
            if n == b:
                out = []
                while n is not None:
                    i = self.g.mux_of.get(n)
                    if i is not None and i in self.sel:
                        out.append(i)
                    n = prev[n]
                return out
            for t in self.succ(n):
                if t not in prev:
                    prev[t] = n
                    stack.append(t)
        return []

    def assign(self, i, k) -> bool:
        if i in self.sel:
            return self.sel[i] == k
        if self.g.forbidden(i, k):
            return False
        s = self.g.src(i, k)
        if s is not None and self.reaches(self.g.net_of[i], s):
            return False
        self.sel[i] = k
        if self.g.net_of[i] in self.obs:
            self._mark(self.g.net_of[i])
        return True

    def route(self, start, open_work, banned):
        dist, prev, pq = {start: 0.0}, {}, [(0.0, start)]
        cap = self.g.max_pop
        pops = 0
        while pq:
            d, n = heapq.heappop(pq)
            if d > dist.get(n, 1e18):
                continue
            pops += 1
            if cap and pops > cap:
                return None   # large models: give up far searches (fabric.route_max_pop)
            if n != start and n in self.obs:
                self.last_end = n
                path = []
                while n != start:
                    p, step = prev[n]
                    if step:
                        path.append(step)
                    n = p
                return path[::-1]
            cand = [(t, c, None) for t, c in self.g.fixed.get(n, ())]
            for i, k in self.g.readers.get(n, ()):
                if (i, k) in banned:
                    continue
                if i in self.sel:
                    if self.sel[i] == k:
                        cand.append((self.g.net_of[i], 0.0, None))
                else:
                    if open_work.get((*i, k)):
                        c = 0.4
                    elif i in self._open_muxes(open_work):
                        c = 4.0
                    else:
                        c = 1.0
                    cand.append((self.g.net_of[i], c, (i, k)))
            for t, c, step in cand:
                nd = d + c
                if nd < dist.get(t, 1e18):
                    dist[t] = nd
                    prev[t] = (n, step)
                    heapq.heappush(pq, (nd, t))
        return None

    def try_cover(self, i, k, open_work) -> bool:
        direct = MM(i).model == "direct"
        if not direct and not self.assign(i, k):
            return False
        start = self.g.net_of[i]
        if start in self.obs:
            return True
        banned: set = set()
        for _ in range(6):
            path = self.route(start, open_work, banned)
            if path is None:
                break
            done = []
            for i2, k2 in path:
                if self.assign(i2, k2):
                    done.append(i2)
                else:
                    banned.add((i2, k2))
                    break
            else:
                self._mark(self.last_end)   # the only new edges are the path's: mark back from its end
                return True
            for i2 in done:
                del self.sel[i2]
        if not direct:
            del self.sel[i]
        return False


def sels_of(g: Graph, sel: dict) -> dict:
    out = {t: {} for t in g.arr.tiles}
    for (t, o), k in sel.items():
        out[t][o] = k
    return out


def build_config(g: Graph, open_work: dict, hist: dict, bias: int, seed=None) -> dict:
    cfg = Cfg(g)
    if seed is not None:
        seed(cfg, bias)

    def work(i):
        return sum(len(open_work.get((*i, k), ())) for k in range(MM(i).fan_in))
    order = sorted((i for i in g.ids if work(i)),
                   key=lambda i: (-hist.get(i, 0), -work(i), (g.pos[i] + bias * 7) % len(g.ids)))
    for i in order:
        if i in cfg.sel:
            continue
        ks = sorted((k for k in range(MM(i).fan_in) if open_work.get((*i, k))),
                    key=lambda k: (-len(open_work[(*i, k)]), (k + bias) % MM(i).fan_in))
        for k in ks:
            if cfg.try_cover(i, k, open_work):
                break
    return fill(g, cfg, open_work, bias)


def fill(g: Graph, cfg: Cfg, open_work: dict, bias: int) -> dict:
    """Free muxes: an open loop-free code, else any loop-free code (loop
    repair: free a mux on the loop, fill it again later). Returns selects."""
    todo = [i for i in g.real if i not in cfg.sel]
    # Decorrelation: muxes that share inputs (the LUT pairs A / B, C / D, ...
    # and their J2MID jump muxes read the same wires) would otherwise pick the
    # same source, and equal signals hide every select-bit test between them;
    # a source already selected by another mux of the tile is taken last.
    used: dict = defaultdict(set)
    for i, k in cfg.sel.items():
        used[i[0]].add(g.src(i, k))
    tries = 0
    while todo:
        i = todo.pop(0)
        fan = MM(i).fan_in
        u = used[i[0]]
        ks = sorted((k for k in range(fan) if not g.forbidden(i, k)),
                    key=lambda k: (not open_work.get((*i, k)), g.src(i, k) in u, (k + bias) % fan))
        ok = next((k for k in ks if cfg.assign(i, k)), None)
        if ok is not None:
            u.add(g.src(i, ok))
            continue
        if any(cfg.assign(i, k) for k in ks):
            continue
        tries += 1
        if tries > 5000:
            raise SystemExit(f"{i}: loop repair did not converge")
        k = ks[0]
        on_loop = [x for x in cfg.path_muxes(g.net_of[i], g.src(i, k)) if x != i
                   and MM(x).model != "direct"]
        if not on_loop:
            raise SystemExit(f"{i}: every input closes a loop")
        victim = on_loop[0]
        del cfg.sel[victim]
        if not cfg.assign(i, k):
            todo.insert(0, i)
        todo.append(victim)
    sels = sels_of(g, cfg.sel)
    for t in g.arr.io_tiles:
        sels[t]["__cbits"] = CBITS_PATTERN[bias % len(CBITS_PATTERN)]
    if g.arr.cycles(sels):
        moves = g.arr.break_loops(sels, max_moves=getattr(g.arr, "loop_max_moves", 2000))
        print(f"    (array check: {len(moves)} loop moves)", flush=True)
    return sels


def route_net(cfg: Cfg, net: str) -> bool:
    """Route a net (not a mux output) to an observed net."""
    if net in cfg.obs:
        return True
    banned: set = set()
    for _ in range(6):
        path = cfg.route(net, {}, banned)
        if path is None:
            return False
        done = []
        for i2, k2 in path:
            if cfg.assign(i2, k2):
                done.append(i2)
            else:
                banned.add((i2, k2))
                break
        else:
            cfg._mark(cfg.last_end)
            return True
        for i2 in done:
            del cfg.sel[i2]
    return False


def clocked_configs(arr: fabric.Array, n_cfg: int = 8):
    """Array version of configs/sm_clocked.py: in config c every tile has
    J_SR_BEG0 = J_EN_BEG0 = c (the jump wire each selects driven from a tile
    input), LUT SR / EN codes and reset values from sm_clocked.lut_plan(c),
    and every LUT output (the flop) routed to the array border. Returns
    [(sels, plan)]."""
    import sm_clocked as ck
    g = Graph(arr)
    out = []
    for c in range(n_cfg):
        cfg = Cfg(g)
        plan = ck.lut_plan(c)
        for t in arr.lut_tiles:
            used = set()
            for jm in ("J_SR_BEG0", "J_EN_BEG0"):
                if not cfg.assign((t, jm), c):
                    raise SystemExit(f"{arr.tn(t)} {jm}: code {c} closes a loop")
                src = M[jm].inputs[c].net
                o2 = next(q for q, m in M.items() if m.net == src)
                k2 = next((k for k, s in enumerate(M[o2].inputs) if s.kind == "tile_input" and s.net not in used),
                          None)
                if k2 is None or not cfg.assign((t, o2), k2):
                    raise SystemExit(f"{arr.tn(t)} {o2}: no tile-input code")
                used.add(M[o2].inputs[k2].net)
            for x, (sr, en, _rv) in plan.items():
                cfg.assign((t, f"L{x}_SR"), sr)
                cfg.assign((t, f"L{x}_EN"), en)
        missed = [f"{arr.tn(t)}.L{x}" for t in arr.lut_tiles for x in sc.LUTS
                  if not route_net(cfg, arr.canon(t, f"L{x}_O"))]
        sels = fill(g, cfg, {}, c)
        if g.arr.cycles(sels):
            g.arr.break_loops(sels)
        print(f"  clocked {c}: flops not routed to the border: {missed or 'none'}", flush=True)
        out.append((sels, {t: plan for t in arr.lut_tiles}))
    return out


def drive_net(cfg: Cfg, net: str, taken: set, used_in: set) -> bool:
    """Drive `net` from a border input of its own: backward Dijkstra through
    free muxes (cost 1 per mux), direct wires and pass-throughs (0); nets
    already on another drive path (`taken`) and used border inputs are not
    reused, so every driven net carries an independent signal."""
    g = cfg.g
    border = g.border_in
    dist, prev, pq = {net: 0.0}, {}, [(0.0, net)]
    goal = None
    while pq:
        d, n = heapq.heappop(pq)
        if d > dist.get(n, 1e18):
            continue
        if n in border and n not in used_in:
            goal = n
            break
        cand = []
        i = g.mux_of.get(n)
        if i is not None:
            m = MM(i)
            if m.model == "direct":
                cand.append((g.arr.canon(i[0], m.inputs[0].net), 0.0, None))
            elif i in cfg.sel:
                continue   # set already: it carries another signal
            else:
                for k, src in enumerate(m.inputs):
                    if src.kind != "const":
                        cand.append((g.arr.canon(i[0], src.net), 1.0, (i, k)))
        for p in g.pass_pred.get(n, ()):
            cand.append((p, 0.0, None))
        for t2, c, step in cand:
            if t2 in taken:
                continue
            nd = d + c
            if nd < dist.get(t2, 1e18):
                dist[t2] = nd
                prev[t2] = (n, step)
                heapq.heappush(pq, (nd, t2))
    if goal is None:
        return False
    steps, nets, n = [], [goal], goal
    while n != net:
        p, step = prev[n]
        if step:
            steps.append(step)
        nets.append(p)
        n = p
    done = []
    for i, k in steps:
        if cfg.assign(i, k):
            done.append(i)
        else:
            for i2 in done:
                del cfg.sel[i2]
            return False
    taken.update(nets)
    used_in.add(goal)
    return True


LUT_CONFIGS = {   # name: (truth table, I0 from Ci, FF, reset value), as configs/configs.py
    "BC1": (sc.cell.XNOR_TT, 1, 0, 1),
    "D0C": (sc.cell.XOR_TT, 0, 1, 0),
    "D1": (sc.cell.XOR_TT, 0, 1, 1),
}


def lut_configs(arr: fabric.Array, names=("BC1", "D0C", "D1")):
    """Array version of the LUT configs: every LUT gets the cell config, its
    inputs I0..I3 (and Ci when I0 comes from Ci), and every tile's J_SR /
    J_EN, are driven each from a border input of its own (drive_net), SR and
    EN muxes on J_SR / J_EN, every LUT output routed to the border. Tiles
    farthest from the border first. Returns [(name, sels, luts, report)]."""
    g = Graph(arr)
    g.border_in = set(arr.border_in)
    g.pass_pred = defaultdict(list)
    for t in arr.lut_tiles:
        for o, i in sc.PASS.items():
            g.pass_pred[arr.canon(t, o)].append(arr.canon(t, i))
    for t in arr.io_tiles:
        for p in "AB":
            g.pass_pred[arr.canon(t, f"{p}_O")].append(arr.canon(t, f"{p}_O_top"))
    depth = {t: min(t[0], t[1], arr.C - 1 - t[0], arr.R - 1 - t[1]) for t in arr.lut_tiles}
    order = sorted(arr.lut_tiles, key=lambda t: (-depth[t], t[1], t[0]))
    out = []
    for c, name in enumerate(names):
        lc = LUT_CONFIGS[name]
        cfg = Cfg(g)
        taken, used_in = set(), set()
        miss = []
        for t in order:
            # SR / EN on J_SR / J_EN when both are driven independently, else
            # SR = GND, EN = VCC (the flops load every clock; few pads in a fabric)
            ok = [drive_net(cfg, arr.canon(t, jn), taken, used_in) for jn in ("J_SR_BEG[0]", "J_EN_BEG[0]")]
            miss += [f"{arr.tn(t)}.{jn}" for jn, k in zip(("J_SR_BEG[0]", "J_EN_BEG[0]"), ok) if not k]
            for x in sc.LUTS:
                cfg.assign((t, f"L{x}_SR"), 1 if all(ok) else 0)
                cfg.assign((t, f"L{x}_EN"), 1 if all(ok) else 0)
            for x in sc.LUTS:
                # Ci: LB..LH take the previous LUT's carry, LA the south tile's Co
                # (a border input only in the south row): carry chain, not driven
                pins = (["Ci"] if lc[1] and x == "A" and t[1] == arr.R - 1 else [] if lc[1] else ["I0"]) + \
                    ["I1", "I2", "I3"]
                for pin in pins:
                    n = arr.canon(t, f"L{x}_{pin}")
                    if not drive_net(cfg, n, taken, used_in):
                        miss.append(f"{arr.tn(t)}.L{x}_{pin}")
        unrouted = [f"{arr.tn(t)}.L{x}" for t in order for x in sc.LUTS
                    if not route_net(cfg, arr.canon(t, f"L{x}_O"))]
        sels = fill(g, cfg, {}, c)
        luts = {t: {x: lc for x in sc.LUTS} for t in arr.lut_tiles}
        if arr.cycles(sels, luts):   # BC1: I0 from Ci adds Ci -> O
            moves = arr.break_loops(sels, luts=luts)
            print(f"    {name}: {len(moves)} loop moves", flush=True)
        rep = {"not driven": miss, "outputs not routed": unrouted, "border inputs used": len(used_in)}
        print(f"  {name}: {len(used_in)} border inputs used, {len(miss)} nets not driven independently "
              f"{miss[:6]}, LUT outputs not routed {unrouted[:6]}", flush=True)
        out.append((name, sels, luts, rep))
    return out


def route_to(cfg: Cfg, src: str, targets: set) -> bool:
    """Route net `src` to one of the nets in `targets` (point to point) through free muxes."""
    save = cfg.obs
    cfg.obs = set(targets)
    try:
        banned: set = set()
        for _ in range(6):
            path = cfg.route(src, {}, banned)
            if path is None:
                return False
            done = []
            for i2, k2 in path:
                if cfg.assign(i2, k2):
                    done.append(i2)
                else:
                    banned.add((i2, k2))
                    break
            else:
                return True
            for i2 in done:
                del cfg.sel[i2]
        return False
    finally:
        cfg.obs = save
        end = getattr(cfg, "last_end", None)
        if end in cfg.obs:
            cfg._mark(end)


CHAINS = 1   # --chains: chains per row


def backbone(cfg: Cfg, bias: int = 0) -> dict:
    """Observation backbone: per row, the LUTs of the row linked into one chain
    (each LUT output routed into an input of the next LUT, from the east tile to
    the west tile, LA..LH within a tile, rotated by bias), the chain end routed
    to the border (a W_IO pad in a fabric). With XOR LUTs every input of a chain
    LUT is then observable at the border. Returns a report."""
    arr = cfg.g.arr
    links = fails = 0
    for t in arr.lut_tiles:   # registered use (chain suite): EN = VCC, SR = GND
        for x in sc.LUTS:
            cfg.assign((t, f"L{x}_SR"), 0)
            cfg.assign((t, f"L{x}_EN"), 0)
    for y in sorted({t[1] for t in arr.lut_tiles}):
        row = sorted((t for t in arr.lut_tiles if t[1] == y), key=lambda t: -t[0])
        order = [(t, sc.LUTS[(k + bias) % 8]) for t in row for k in range(8)]
        n = len(order)
        segs = [order[i * n // CHAINS:(i + 1) * n // CHAINS] for i in range(CHAINS)]
        # chain ends: a pad output of the row's W_IO each (fabric), else any observed net
        pads = [arr.canon((-1, y), p) for p in ("A_I_top", "B_I_top", "A_T_top", "B_T_top")] \
            if arr.fabric else []
        for si, seg in enumerate(segs):
            for (ta, xa), (tb, xb) in zip(seg, seg[1:]):
                pins = {arr.canon(tb, f"L{xb}_I{j}") for j in range(4)}
                if route_to(cfg, arr.canon(ta, f"L{xa}_O"), pins):
                    links += 1
                else:
                    fails += 1
            end = arr.canon(seg[-1][0], f"L{seg[-1][1]}_O")
            ok = route_to(cfg, end, {pads[si % len(pads)]}) if pads and CHAINS > 1 else route_net(cfg, end)
            if not ok:
                fails += 1
    return {"links": links, "fails": fails}


def gain(g, sels, open_work, hist, only):
    return sum(len(open_work[key] & items) * (1 + hist.get(key[:2], 0))
               for key, items in fabric.grade(g.arr, sels, only).items() if key in open_work)


def polish(g: Graph, sels: dict, open_work: dict, hist: dict, max_cand: int = 400) -> None:
    """Move single muxes near the targets this config selects but does not
    fully show (drivers of sibling inputs, LUT input muxes, readers)."""
    arr = g.arr
    tgt = [(t, o) for t in arr.tiles for o in arr.MT(t)
           if open_work.get((t, o, sels[t].get(o, 0) if arr.MT(t)[o].model != "direct" else 0))]
    if not tgt:
        return
    cand = []
    for t, o in tgt:
        m = arr.MT(t)[o]
        k = sels[t].get(o, 0) if m.model != "direct" else 0
        nets = [arr.canon(t, s.net) for s in m.inputs if s.kind != "const"]
        nets.append(g.net_of[(t, o)])
        for n in nets:
            i = g.mux_of.get(n)
            if i is not None and MM(i).model != "direct":
                cand.append(i)
            for i2, _k in g.readers.get(n, ()):
                cand.append(i2)
        del k
    cand = list(dict.fromkeys(cand))[:max_cand]
    best = gain(g, sels, open_work, hist, tgt)
    # loop check per move: the move adds src -> net (and drops the old input):
    # a loop iff net already reaches src (DFS on the current dependency graph)
    succ: dict = defaultdict(set)
    for n, ins in arr.deps(sels).items():
        for p in ins:
            succ[p].add(n)

    def reaches(a, b, skip):
        seen, stack = {a}, [a]
        while stack:
            x = stack.pop()
            if x == b:
                return True
            for y in succ.get(x, ()):
                if (x, y) != skip and y not in seen:
                    seen.add(y)
                    stack.append(y)
        return False
    for t, o in cand:
        cur = sels[t][o]
        net = g.net_of[(t, o)]
        old = g.src(t_o := (t, o), cur)
        for k in range(arr.MT(t)[o].fan_in):
            if k == cur or g.forbidden(t_o, k):
                continue
            new = g.src(t_o, k)
            if new is not None and (new == net or reaches(net, new, (old, net))):
                continue
            sels[t][o] = k
            gg = gain(g, sels, open_work, hist, tgt)
            if gg > best:
                best, cur = gg, k
        sels[t][o] = cur
        now = g.src(t_o, cur)
        if now != old:
            if old is not None:
                succ[old].discard(net)
            if now is not None:
                succ[now].add(net)


def route_all(arr: fabric.Array, max_configs: int = 40, verbose: bool = True, do_polish: bool = True,
              hist0: dict | None = None, prior: list | None = None, seed=None):
    """prior: configs already chosen (selects per tile name): their shown items
    are removed first and the new configs only add to them (--extend)."""
    g = Graph(arr)
    reach = g.reachable()
    open_work = {(t, o, k): fabric.work_items_m(MM((t, o)), k) for t, o in g.ids if (t, o) in reach
                 for k in range(MM((t, o)).fan_in) if not g.forbidden((t, o), k)}
    total = sum(len(v) for v in open_work.values())
    hist: dict = defaultdict(int, hist0 or {})
    for cfg in prior or ():
        sels = {t: dict(cfg[arr.tn(t)]) for t in arr.tiles}
        for key, items in fabric.grade(arr, sels).items():
            if key in open_work:
                open_work[key] -= items
    if prior and verbose:
        print(f"  {len(prior)} prior configs: {sum(len(v) for v in open_work.values())} of {total} items open",
              flush=True)
    configs, log, stall = [], [], 0
    while len(configs) < max_configs and any(open_work.values()):
        t0 = time.time()
        sels = build_config(g, open_work, hist, len(configs), seed)
        if do_polish:
            polish(g, sels, open_work, hist)
        shown = fabric.grade(arr, sels)
        for t, o in g.ids:
            k = sels[t].get(o, 0) if arr.MT(t)[o].model != "direct" else 0
            key = (t, o, k)
            if open_work.get(key) and not (open_work[key] & shown.get(key, set())):
                hist[(t, o)] += 1
        gained = 0
        for key, items in shown.items():
            if key in open_work:
                b = len(open_work[key])
                open_work[key] -= items
                gained += b - len(open_work[key])
        left = sum(len(v) for v in open_work.values())
        if verbose:
            print(f"  config {len(configs):2d}: +{gained} items, {left} of {total} open "
                  f"({time.time() - t0:.0f} s)", flush=True)
        if gained == 0:
            stall += 1
            if stall >= 3:
                break
            continue
        stall = 0
        configs.append({arr.tn(t): s for t, s in sels.items()})
        log.append({"gain": gained, "open": left})
    return configs, open_work, total, log, hist


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("R", type=int)
    ap.add_argument("C", type=int)
    ap.add_argument("--max", type=int, default=40)
    ap.add_argument("--no-polish", action="store_true")
    ap.add_argument("--rand", type=int, default=0, help="random vectors per config (fabric.Array n_rand)")
    ap.add_argument("--fabric", action="store_true", help="W_IO west, N / S term, east open (fabric.Array)")
    ap.add_argument("--chains", type=int, default=1, help="backbone chains per row (each to its own pad)")
    ap.add_argument("--backbone", action="store_true",
                    help="seed every config with the observation backbone (LUT chains to the border)")
    ap.add_argument("--extend", metavar="TAG",
                    help="add configs to configs/fab_router_<R>x<C><TAG>.json (only the new ones are "
                         "written, to ..<TAG>_ext.json); with polish unless --no-polish")
    ap.add_argument("--passes", type=int, default=1,
                    help="PathFinder-style: each pass starts with the history of the previous one; "
                         "the pass with the fewest configs is kept")
    a = ap.parse_args()
    global CHAINS
    CHAINS = a.chains
    arr = fabric.Array(a.R, a.C, a.rand, fabric=a.fabric)
    t0 = time.time()
    best, hist = None, None
    for p in range(a.passes):
        prior = json.loads((ROOT / "configs" / f"fab_router_{'fabric_' if a.fabric else ''}{a.R}x{a.C}"
                                                f"{a.extend}.json").read_text())["configs"] if a.extend else None
        res = route_all(arr, a.max, do_polish=not a.no_polish, hist0=hist, prior=prior,
                        seed=(lambda cfg, b: backbone(cfg, b)) if a.backbone else None)
        hist = res[4]
        done_p = res[2] - sum(len(v) for v in res[1].values())
        print(f"pass {p}: {len(res[0])} configs, {done_p} of {res[2]} items", flush=True)
        if best is None or (done_p, -len(res[0])) > (best[2] - sum(len(v) for v in best[1].values()), -len(best[0])):
            best = res
    configs, open_work, total, log, _ = best
    done = total - sum(len(v) for v in open_work.values())
    left = {f"{arr.tn(t)}:{o}:{k}": sorted(map(str, v)) for (t, o, k), v in open_work.items() if v}
    tag = f"{a.extend}_ext" if a.extend else ("_nopolish" if a.no_polish else "") + (f"_r{a.rand}" if a.rand else "") \
        + ("_bb" if a.backbone else "") + (f"{a.chains}" if a.backbone and a.chains > 1 else "")
    out = ROOT / "configs" / f"fab_router_{'fabric_' if a.fabric else ''}{a.R}x{a.C}{tag}.json"
    out.write_text(json.dumps({"configs": configs, "log": log, "open": left, "n_rand": a.rand,
                               "polish": not a.no_polish,
                               "items": {"total": total, "shown": done}}, indent=0))
    print(f"{arr.name}: {len(configs)} configs, {done} of {total} items ({100 * done / total:.2f}%), "
          f"{time.time() - t0:.0f} s -> {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
