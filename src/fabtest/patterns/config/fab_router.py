#!/usr/bin/env python3
"""Test config router on the fabric graph (fabric.Array, e.g. FullFabric / GenericFabric).

A target is (tile, mux, code) with its model-level items (fabric.work_items_m);
observation only at the fabric border, stimulus only at the border inputs. Per config:
  - the observation backbone (backbone): the LUTs of every row linked into chains that
    end at the row's W_IO pads; RegFile and BlockRAM pinned to defined values;
  - then greedy over the open targets: (tile, mux) ordered by history (PathFinder-style:
    an open code was selected but not shown in an earlier config), then open work;
    select an open code and route the mux output forward to an observed net: Dijkstra
    over free muxes of every tile, inter-tile wires and pass-throughs are fixed edges
    (cost 0), LUT I -> O 2, Ci / I1 / I2 -> Co and MUX8LUT 4; a free mux costs 1, 0.4 when
    its code on the path is open, 4 when the mux still has other open codes;
  - every edge added only if it closes no loop; free muxes filled with an open loop-free
    code (loop repair by freeing a mux on the loop);
  - exact grading of the whole fabric (fabric.grade).
Configs are added until nothing open is left or configs stop adding items.
"""
from __future__ import annotations

import heapq
import time
from collections import defaultdict

from fabtest.model import fabric  # noqa: E402
from fabtest.model import sm_configs as sc  # noqa: E402

M = fabric.M
# BEL edges of the LUT4AB tile (LUT I -> O / Co, MUX8LUT)
BELDEPS: dict[str, list[str]] = {}
for _x in sc.LUTS:
    BELDEPS.update(sc.lut_deps(_x))
BELDEPS.update(sc.mux8_deps(0, 0))
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
            for out, ins in BELDEPS.items():
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
    """One config under construction (pure Python; NativeCfg keeps the same state in C++).
    sel: (tile, mux) -> code, in the order the router chose them; obs: the observed nets
    (push_obs / pop_obs: a point-to-point route observes only its targets); cut: the
    constant LUT outputs (no input -> output edge)."""

    def __init__(self, g: Graph):
        self.g = g
        self.sel: dict = {}
        self.obs = set(g.out_nets)
        self.cut: set = set()
        self._obs_saved: list = []
        for n in list(g.out_nets):
            self._mark(n)

    def obs_has(self, n) -> bool:
        return n in self.obs

    def push_obs(self, targets) -> None:
        self._obs_saved.append(self.obs)
        self.obs = set(targets)

    def pop_obs(self) -> None:
        self.obs = self._obs_saved.pop()

    def set_cut(self, nets) -> None:
        self.cut = set(nets)

    def add_cut(self, n) -> None:
        self.cut.add(n)

    def del_cut(self, n) -> None:
        self.cut.discard(n)

    def _open_muxes(self, open_work: dict) -> set:
        """Muxes with open work (cached per open_work dict; it does not change during a config)."""
        if getattr(self, "_om_src", None) is not open_work:
            self._om_src = open_work
            self._om = {(t, o) for (t, o, _k), v in open_work.items() if v}
        return self._om

    def succ(self, n):
        cut = self.cut
        for t, _c in self.g.fixed.get(n, ()):
            if t not in cut:   # constant LUT outputs: no input -> output edge
                yield t
        for i, k in self.g.readers.get(n, ()):
            if self.sel.get(i) == k:
                yield self.g.net_of[i]

    def preds(self, n):
        out = [] if n in self.cut else list(self.g.fixed_pred.get(n, ()))
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
        if self.obs_has(self.g.net_of[i]):
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
            # wires_only (routes that carry a value: constants, addresses, data): no BEL
            # edges (cost > 0, LUT input -> output), whose output is a different signal
            cand = [(t, c, None) for t, c in self.g.fixed.get(n, ())
                    if not (getattr(self, "wires_only", False) and c > 0)]
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
        if self.obs_has(start):
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


class _Sel(dict):
    """cfg.sel of a NativeCfg: a dict (the router's order) that writes through to C++."""
    __slots__ = ("_r", "_pos")

    def __init__(self, r, pos):
        super().__init__()
        self._r, self._pos = r, pos

    def __setitem__(self, i, k):
        dict.__setitem__(self, i, k)
        self._r.sel_set(self._pos[i], k)

    def __delitem__(self, i):
        dict.__delitem__(self, i)
        self._r.sel_del(self._pos[i])

    def _no(self, *a, **k):
        raise NotImplementedError("cfg.sel: only [] =, del and reads")
    pop = popitem = update = setdefault = clear = _no


def _native_router(g: Graph):
    """The C++ Router of a Graph (built once; NativeCfg resets it per config), or None."""
    if getattr(g, "_native", False) is not False:
        return g._native
    from fabtest import native
    mod = native.load()
    g._native = None
    if mod is None:
        return None
    nets = set(g.fixed) | {b for lst in g.fixed.values() for b, _c in lst} | set(g.fixed_pred)
    nets |= set(g.readers) | set(g.net_of.values()) | set(g.out_nets)
    src = []
    for i in g.ids:
        row = [g.src(i, k) for k in range(MM(i).fan_in)]
        nets |= {s for s in row if s is not None}
        src.append(row)
    names = sorted(nets)
    nid = {n: j for j, n in enumerate(names)}
    mux_of = [-1] * len(names)
    for n, i in g.mux_of.items():
        mux_of[nid[n]] = g.pos[i]
    g._native = mod.Router(
        len(names),
        [[(nid[b], c) for b, c in g.fixed.get(n, ())] for n in names],
        [[nid[a] for a in g.fixed_pred.get(n, ())] for n in names],
        [[(g.pos[i], k) for i, k in g.readers.get(n, ())] for n in names],
        [nid[g.net_of[i]] for i in g.ids], mux_of,
        [[-1 if s is None else nid[s] for s in row] for row in src])
    g._nnames, g._nid = names, nid
    return g._native


class NativeCfg(Cfg):
    """Cfg with its search state and searches in C++ (native/router.cpp); same results."""

    def __init__(self, g: Graph, r):
        self.g, self._r = g, r
        r.reset()
        self._nid, self._names = g._nid, g._nnames
        self.sel = _Sel(r, g.pos)
        self._open_src = None
        for n in g.out_nets:
            r.obs_add(self._nid[n])
        for n in list(g.out_nets):
            self._mark(n)

    def _id(self, n) -> int:
        return self._nid.get(n, -1)

    def obs_has(self, n) -> bool:
        return self._r.obs_has(self._id(n))

    def push_obs(self, targets) -> None:
        self._r.push_obs([j for j in map(self._id, targets) if j >= 0])

    def pop_obs(self) -> None:
        self._r.pop_obs()

    def set_cut(self, nets) -> None:
        self._r.set_cut([j for j in map(self._id, nets) if j >= 0])

    def add_cut(self, n) -> None:
        self._r.add_cut(self._id(n))

    def del_cut(self, n) -> None:
        self._r.del_cut(self._id(n))

    def _mark(self, n):
        self._r.mark(self._id(n))

    def reaches(self, a, b) -> bool:
        return self._r.reaches(self._id(a), self._id(b), self.g.max_reach or 0)

    def path_muxes(self, a, b) -> list:
        return [self.g.ids[m] for m in self._r.path_muxes(self._id(a), self._id(b))]

    def route(self, start, open_work, banned):
        if open_work and self._open_src is not open_work:
            self._open_src = open_work
            pos = self.g.pos
            self._r.set_open([(pos[(t, o)], k) for (t, o, k), v in open_work.items() if v],
                             [pos[i] for i in self._open_muxes(open_work)])
        pos = self.g.pos
        res = self._r.route(self._id(start), [(pos[i], k) for i, k in banned], self.g.max_pop or 0,
                            bool(getattr(self, "wires_only", False)), bool(open_work))
        if res is None:
            return None
        self.last_end = self._names[self._r.last_end()]
        return [(self.g.ids[m], k) for m, k in res]


def make_cfg(g: Graph) -> Cfg:
    """NativeCfg when the native module builds (FABTEST_NATIVE=0: always Python), else Cfg."""
    r = _native_router(g)
    return Cfg(g) if r is None else NativeCfg(g, r)


def sels_of(g: Graph, sel: dict) -> dict:
    out = {t: {} for t in g.arr.tiles}
    for (t, o), k in sel.items():
        out[t][o] = k
    return out


def build_config(g: Graph, open_work: dict, hist: dict, bias: int, seed=None) -> dict:
    cfg = make_cfg(g)
    g.last_seed = seed(cfg, bias) if seed is not None else None

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
        if tries > 20000:
            raise SystemExit(f"{i}: loop repair did not converge")
        k = ks[0] if tries <= 5000 else ks[(tries // 2) % len(ks)]
        on_loop = [x for x in cfg.path_muxes(g.net_of[i], g.src(i, k)) if x != i
                   and MM(x).model != "direct" and x not in getattr(cfg, "pinned", ())]
        if not on_loop:
            raise SystemExit(f"{i}: every input closes a loop")
        # past 5000 tries two muxes keep freeing each other (every input of one loops
        # through the other): take the other inputs and the other muxes of the loop in turn
        victim = on_loop[0] if tries <= 5000 else on_loop[(tries // 50) % len(on_loop)]
        del cfg.sel[victim]
        if not cfg.assign(i, k):
            todo.insert(0, i)
        todo.append(victim)
    sels = sels_of(g, cfg.sel)
    for t in g.arr.io_tiles:
        sels[t]["__cbits"] = CBITS_PATTERN[bias % len(CBITS_PATTERN)]
    for t, c in getattr(cfg, "lut_const", {}).items():   # before the loop check: constants
        sels[t]["__lut"] = dict(c)
    if g.arr.cycles(sels):
        moves = g.arr.break_loops(sels, max_moves=getattr(g.arr, "loop_max_moves", 2000),
                                  pinned=getattr(cfg, "pinned", frozenset()))
        print(f"    (array check: {len(moves)} loop moves)", flush=True)
    return sels


def route_net(cfg: Cfg, net: str) -> bool:
    """Route a net (not a mux output) to an observed net."""
    if cfg.obs_has(net):
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


def route_to(cfg: Cfg, src: str, targets: set, wires_only: bool = False) -> bool:
    """Route net `src` to one of the nets in `targets` (point to point) through free muxes;
    wires_only: never through a BEL (the target gets exactly the value of src)."""
    cfg.push_obs(targets)
    cfg.wires_only = wires_only
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
        cfg.pop_obs()
        cfg.wires_only = False
        end = getattr(cfg, "last_end", None)
        if end is not None and cfg.obs_has(end):
            cfg._mark(end)


CHAINS = 1   # --chains: chains per row


def lut_rank(arr, t, side: int, n: int = 3, avoid=()) -> list:
    """The n LUT4AB tiles nearest to a RegFile / BRAM at t: same row on the side (-1 west,
    +1 east) first by distance, then any by distance (hops, then y, x)."""
    luts = [u for u in arr.lut_tiles if u not in avoid]
    same = sorted((u for u in luts if u[1] == t[1] and (u[0] - t[0]) * side > 0), key=lambda u: abs(u[0] - t[0]))
    rest = sorted((u for u in luts if u not in same), key=lambda u: (abs(u[0] - t[0]) + abs(u[1] - t[1]), u[1], u[0]))
    return (same + rest)[:n]


def lut_near(arr, t, side: int, avoid=()) -> tuple | None:
    """The LUT4AB tile that serves a RegFile / BRAM at t (lut_rank's first)."""
    r = lut_rank(arr, t, side, 1, avoid)
    return r[0] if r else None


def pads_near(arr, y: int, names) -> list:
    """Pad nets of the W_IO tiles, rows nearest to y first."""
    rows = sorted({t[1] for t in arr.io_tiles}, key=lambda yy: (abs(yy - y), yy))
    return [arr.canon((-1, yy), q) for yy in rows for q in names]


def _rot(seq, k: int) -> list:
    seq = list(seq)
    return seq[k % len(seq):] + seq[:k % len(seq)] if seq else seq


CONST_RADIUS = 2   # tiles: a pinned pin takes an existing constant this close before a new one
BRAM_MAX_POP = 60000   # route cap for the BlockRAM pins (pads are far from the BRAM column)


def letters(bias: int) -> list:
    """LUT letters for constant / helper LUTs in config `bias` (rotated: different LUTs leave
    the chains in different configs)."""
    return _rot("HGFEDCBA", 3 * bias)


def _undo(cfg, before: set) -> None:
    for q in set(cfg.sel) - before:
        del cfg.sel[q]


def _free_luts(cfg, tiles, bias):
    """(tile, letter) not yet used as a constant or helper LUT, in rotation order."""
    for t in tiles:
        for x in letters(bias):
            if x not in cfg.lut_const.get(t, {}):
                yield t, x


def _drop_consts(cfg, n_before: dict) -> None:
    """Forget the constant LUTs created since n_before (their routes were undone): the LUTs
    are free again (no constant, not cut)."""
    for v, n in n_before.items():
        for t, out in cfg.consts[v][n:]:
            x = out.rsplit("/", 1)[1][1]
            cfg.lut_const.get(t, {}).pop(x, None)
            cfg.del_cut(out)
        del cfg.consts[v][n:]


def _tile_of(arr, net: str):
    """The tile of a fabric net (<tile name>/<local>), or None (a pad)."""
    m = arr.__dict__.get("_tile_by_name")
    if m is None:
        m = arr._tile_by_name = {arr.tn(t): t for t in arr.tiles}
    return m.get(net.split("/", 1)[0])


def route_const(cfg, val: int, targets: list, near, bias: int) -> bool:
    """Route the constant val (0 / 1) to every net of targets (wires only, one source for
    all): an existing constant of the config pool (cfg.consts) within CONST_RADIUS of the
    targets' tile (else `near`), nearest first, else a new constant LUT in one of the LUT
    tiles nearest to it (tile and letter rotated by bias). A constant 0 is a LUT with table 0 (X inputs are fine: 0 & X
    = 0); a 1 needs its four inputs from a 0 (with X inputs an all-ones table gives X in the
    gates)."""
    arr = cfg.g.arr
    near = _tile_of(arr, targets[0]) or near   # the pins' tile (a macro spans several)

    def dist(t):
        return abs(t[0] - near[0]) + abs(t[1] - near[1])

    def to_all(src):
        before = set(cfg.sel)
        if all(route_to(cfg, src, {n}, True) for n in targets):
            return True
        _undo(cfg, before)
        return False
    for t, net in sorted(cfg.consts[val], key=lambda e: (dist(e[0]), e[1])):
        if dist(t) <= CONST_RADIUS and to_all(net):
            return True
    ranked = _rot(lut_rank(arr, near, -1, 3), bias)
    for t, x in _free_luts(cfg, ranked, bias):
        out = arr.canon(t, f"L{x}_O")
        before = set(cfg.sel)
        n_before = {v: len(cfg.consts[v]) for v in (0, 1)}
        if not to_all(out):   # the targets first, before the LUT's own inputs take the wires
            continue
        if val == 1 and not route_const(cfg, 0, [arr.canon(t, f"L{x}_I{j}") for j in range(4)], t, bias):
            _undo(cfg, before)
            _drop_consts(cfg, n_before)
            continue
        cfg.lut_const.setdefault(t, {})[x] = val
        cfg.add_cut(out)
        cfg.consts[val].append((t, out))
        return True
    return False


def backbone(cfg: Cfg, bias: int = 0) -> dict:
    """Observation backbone: per row, the LUTs of the row linked into one chain
    (each LUT output routed into an input of the next LUT, from the east tile to
    the west tile, LA..LH within a tile, rotated by bias), the chain end routed
    to the border (a W_IO pad in a fabric). With XOR LUTs every input of a chain
    LUT is then observable at the border. RegFiles and BlockRAMs are pinned to defined
    values first, from constants shared through the config (route_const) and helper LUTs
    in nearby tiles, both rotated per config. Returns a report."""
    arr = cfg.g.arr
    links = fails = 0
    cfg.lut_const = {}
    cfg.consts = {0: [], 1: []}
    cfg.set_cut(())
    rf = getattr(arr, "rf_pins", {})
    br = getattr(arr, "bram_pins", {})
    for t in arr.lut_tiles:   # registered use (chain suite): EN = VCC, SR = GND
        for x in sc.LUTS:
            cfg.assign((t, f"L{x}_SR"), 0)
            cfg.assign((t, f"L{x}_EN"), 0)
    sel0 = set(cfg.sel)
    # RegFile: W_en = 1, address bit 0 / 1 = pad on W, NOT pad on A and B (a NAND helper LUT:
    # 4 words per config, a read never returns a word before it is written once), bits 2..4
    # the config index on all three ports, data from pads: the RegFile is a 4-bit register
    # on a few words (AD = BD = D one clock later)
    for ri, (r, p) in enumerate(rf.items()):
        if not arr.lut_tiles:
            break
        if route_const(cfg, 1, [p["en"]], r, bias + ri):
            links += 1
        else:
            fails += 1
        pads = _rot(pads_near(arr, r[1], ("A_O", "B_O")), bias + ri)
        helpers_t = _rot(lut_rank(arr, r, -1, 3), bias + ri)
        for i in range(2):
            got = False
            for src in pads[2 * i:2 * i + 6]:
                for t, x in _free_luts(cfg, helpers_t, bias + ri):
                    before = set(cfg.sel)
                    n_before = {v: len(cfg.consts[v]) for v in (0, 1)}
                    if route_to(cfg, src, {p["W"][i]}, True):
                        kin = next((kk for kk in range(4)
                                    if route_to(cfg, src, {arr.canon(t, f"L{x}_I{kk}")}, True)), None)
                        if kin is not None and \
                                all(route_const(cfg, 1, [arr.canon(t, f"L{x}_I{j}")], t, bias + ri)
                                    for j in range(4) if j != kin) and \
                                all(route_to(cfg, arr.canon(t, f"L{x}_O"), {p[port][i]}, True) for port in "AB"):
                            cfg.lut_const.setdefault(t, {})[x] = {"tt": 0x7FFF}   # NAND: NOT pad
                            got = True
                            break
                    _undo(cfg, before)
                    _drop_consts(cfg, n_before)
                if got:
                    break
            if got:
                links += 7
            elif route_const(cfg, 0, [p[port][i] for port in "WAB"], r, bias + ri):
                links += 3
            else:
                fails += 1
        adr = bias % 8
        for i in range(2, 5):
            want = (adr >> (i - 2)) & 1
            if any(route_const(cfg, v, [p[port][i] for port in "WAB"], r, bias + ri) for v in (want, 1 - want)):
                links += 3
            else:
                fails += 1
        for k in range(4):   # write data from pad inputs (defined every vector)
            if any(route_to(cfg, src, {p["D"][k]}, True) for src in _rot(pads, k)):
                links += 1
            else:
                fails += 1
    # BRAM: C by the config index (write width C0 C1: 32 / 16 / 8 bit, read width C2 C3,
    # output register C5; C4 = 1: write every cycle, else wr_data[20] from a pad), the 0s
    # from the muxes' GND inputs; read address = write address (a constant, config index
    # mod 256) except bit 0: write p, read NOT p (a 0x5555 helper LUT): the read never meets
    # the write of the same cycle (undefined for the macro: X); write data from two pads
    def gnd(net):   # select the GND input of the mux driving net (RAM_IO OutPass inputs A, C)
        i = cfg.g.mux_of.get(net)
        if i is None:
            return False
        ks = [k for k, s_ in enumerate(MM(i).inputs) if s_.kind == "const" and s_.port.startswith("GND")]
        return bool(ks) and cfg.assign(i, ks[0])

    for k, bp in br.items():
        if not arr.lut_tiles:
            break
        (xr, y1) = bp["tiles"][0]
        near = (xr, y1)
        wrc, rdc, c5 = bias % 3, (bias // 3) % 3, (bias // 9) % 2
        want = [wrc >> 1, wrc & 1, rdc >> 1, rdc & 1, 1, c5]
        cval = [int(bool(want[j_]) and route_const(cfg, 1, [bp["C"][j_]], near, bias + k)) for j_ in range(6)]
        if not all(gnd(n) for j_, n in enumerate(bp["C"]) if not cval[j_]):
            fails += 1
            continue
        links += 6
        we_pad = not cval[4]
        adr = bias % 128
        for i in range(1, 8):   # address bits 1..7: the same constant on both ports
            ok = (adr >> (i - 1)) & 1 and route_const(cfg, 1, [bp[port][i] for port in ("wr_addr", "rd_addr")],
                                                      near, bias + k)
            if ok or (gnd(bp["wr_addr"][i]) and gnd(bp["rd_addr"][i])):
                links += 2
            else:
                fails += 1
        pads = _rot(pads_near(arr, y1, ("A_O", "B_O")), bias + k)
        helpers_t = _rot(lut_rank(arr, near, -1, 3), bias + k)
        cap, cfg.g.max_pop = cfg.g.max_pop, BRAM_MAX_POP
        pa = None
        for src in pads:
            for t, x in _free_luts(cfg, helpers_t, bias + k):
                before = set(cfg.sel)
                n_before = {v: len(cfg.consts[v]) for v in (0, 1)}
                if route_to(cfg, src, {bp["wr_addr"][0]}, True) and \
                        route_to(cfg, src, {arr.canon(t, f"L{x}_I0")}, True) and \
                        route_const(cfg, 0, [arr.canon(t, f"L{x}_I{j_}") for j_ in (1, 2, 3)], t, bias + k) and \
                        route_to(cfg, arr.canon(t, f"L{x}_O"), {bp["rd_addr"][0]}, True):
                    cfg.lut_const.setdefault(t, {})[x] = {"tt": 0x5555}
                    pa = src
                    break
                _undo(cfg, before)
                _drop_consts(cfg, n_before)
            if pa is not None:
                break
        if pa is None:
            fails += 1
            cfg.g.max_pop = cap
            continue
        links += 6
        # data: even bits from one pad, odd bits from another (the half / byte selects
        # wr_data[17:16] and [25:24] then take all four values)
        used = {pa}
        for par in (0, 1):
            bits = [i for i in range(par, 32, 2) if not (we_pad and i == 20)]
            for src in [x for x in pads if x not in used]:
                before = set(cfg.sel)
                got = [route_to(cfg, src, {bp["wr_data"][i]}, True) for i in bits]
                if sum(got) >= len(bits) - 4:
                    links += sum(got)
                    fails += len(got) - sum(got)
                    used.add(src)
                    break
                _undo(cfg, before)
            else:
                fails += len(bits)
        if we_pad:
            if any(route_to(cfg, src, {bp["wr_data"][20]}, True) for src in pads if src not in used):
                links += 1
            else:
                fails += 1
        cfg.g.max_pop = cap
    cfg.pinned = set(cfg.sel) - sel0 if (rf or br) else set()   # RegFile / BRAM routes: never moved
    for y in sorted({t[1] for t in arr.lut_tiles}):
        row = sorted((t for t in arr.lut_tiles if t[1] == y), key=lambda t: -t[0])
        order = [(t, sc.LUTS[(k + bias) % 8]) for t in row for k in range(8)
                 if sc.LUTS[(k + bias) % 8] not in cfg.lut_const.get(t, {})]
        n = len(order)
        segs = [order[i * n // CHAINS:(i + 1) * n // CHAINS] for i in range(CHAINS)]
        # chain ends: a pad output of the row's W_IO each (fabric), else any observed net
        pads = pads_near(arr, y, ("A_I_top", "B_I_top", "A_T_top", "B_T_top"))[:4] \
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
    n_const = sum(len(c) for c in cfg.lut_const.values())
    return {"links": links, "fails": fails, "const_luts": n_const}


def route_all(arr: fabric.Array, max_configs: int = 40, verbose: bool = True,
              hist0: dict | None = None, prior: list | None = None, seed=None, idx0: int = 0,
              checkpoint=None):
    """prior: configs already chosen (selects per tile name): their shown items
    are removed first and the new configs only add to them (--extend). idx0: index
    of the first new config (fill decorrelation, frame order)."""
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
        sels = build_config(g, open_work, hist, idx0 + len(configs), seed)
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
        log.append({"gain": gained, "open": left, **({"backbone": g.last_seed} if getattr(g, "last_seed", None) else {})})
        if checkpoint is not None:
            checkpoint(configs, log, total, left)
    return configs, open_work, total, log, hist
