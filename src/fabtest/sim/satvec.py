"""SAT vectors for a fixed config (fabtest satvec; a prototype, opt-in).

Per config of a run: the config latches and the other config-port inputs as constants
(static values: pads and flops X), the pads free per clock. Scan-style targets: a fault is
the target of K clocks of the configured netlist from the states the kernel reaches after
the vectors so far (its net dump: lane 0 the good machine, lanes 1..63 faulty machines),
good and faulty copies sharing every net the fault cannot reach; the goal a difference in
the next state of a flop that reaches a border output (the XOR chains carry it to a pad)
or at a border output. A fault whose machine holds X where the good one does not (a flop
it never writes) is no target: the kernel keeps the X (a power-up question). Found vectors
are appended to the config's vectors (configs.json "extra_vectors"), then random flush
vectors that carry the differences along the chains; `fabtest faultsim` grades them as any
other vectors.

Classification of every (fault, config), one clock with the flop states free: UNSAT (or no
path to a flop or an output) proves the fault never makes a first difference in the
vector phase of that config (states are equal before it, and no state and no pad values
give one): a config problem, no vector helps. Faults that reach a config latch or a flop
clock are not decided (the load phase is not modelled).

The model is 2-valued (X sources free): a found vector can still fail where the fault
meets an X in the 3-valued kernel; the fault simulation decides.
"""
from __future__ import annotations

import csv
import json
import random
from collections import deque
from pathlib import Path

import numpy as np

AND, OR, NOT, BUF, LATCH, DFF = 0, 1, 2, 3, 4, 5
T = 1   # SAT variable 1: constant true


def _v3(t: int, a: int, b: int) -> int:
    """3-valued gate (0, 1, 2 = X)."""
    if t == AND:
        return 0 if a == 0 or b == 0 else (1 if a == 1 and b == 1 else 2)
    if t == OR:
        return 1 if a == 1 or b == 1 else (0 if a == 0 and b == 0 else 2)
    if t == NOT:
        return 2 if a == 2 else 1 - a
    return a


def dump_values(nl, st) -> np.ndarray:
    """Every net's fault-free value (0 / 1 / 2 = X) after the whole stimulus st (kernel dump)."""
    from fabtest.sim.fastsim import fastsim
    eng = fastsim.Engine(nl, [st], W=1, limit_factor=200)
    h = np.zeros(nl.n_nets, dtype=np.uint64)
    lo = np.zeros(nl.n_nets, dtype=np.uint64)
    eng.lib.set_dump(fastsim._p(h), fastsim._p(lo))
    try:
        eng.run_one([], 0)
    finally:
        eng.lib.set_dump(None, None)
    h1, l1 = (h & np.uint64(1)).astype(np.int8), (lo & np.uint64(1)).astype(np.int8)
    return np.where(h1 & l1, 2, np.where(h1, 1, 0)).astype(np.int8)


class Config:
    """One config of the netlist in its vector phase: static values (pads and flops X),
    a fault-free clock step, and the SAT encodings."""

    def __init__(self, nl, st, pad_nets: list[int], clk_net: int, dump: np.ndarray):
        self.nl, self.st, self.pads, self.clk = nl, st, pad_nets, clk_net
        self.pad_ix = {n: j for j, n in enumerate(pad_nets)}
        self.dump = dump
        typ, a, b, c, out = (x.tolist() for x in (nl.type, nl.a, nl.b, nl.c, nl.out))
        self.typ, self.a, self.b, self.c, self.out = typ, a, b, c, out
        self.drv = nl.driver
        self.dffs = [i for i, t in enumerate(typ) if t == DFF]
        self.q_of = {out[i]: i for i in self.dffs}
        rd_off, rd_idx = nl.rd_off.tolist(), nl.rd_idx.tolist()
        self.readers = lambda n: rd_idx[rd_off[n]:rd_off[n + 1]]   # noqa: E731
        # constant inputs: the config port as after the load (held in every vector row)
        row = st.rows[-1]
        sv = [2] * nl.n_nets
        for k, nm in enumerate(st.in_names):
            n = nl.port_bits[nm]
            if n not in self.pad_ix and n != clk_net:
                sv[n] = int(row[k])
        sv[clk_net] = 0
        for n, v in zip(nl.const_net.tolist(), nl.const_val.tolist()):
            if nl.n_const:
                sv[n] = v
        self.rank = nl.rank.tolist()
        order = sorted((i for i, t in enumerate(typ) if t != DFF), key=lambda i: self.rank[i])
        self.latch_hold = {out[i]: int(dump[out[i]]) for i, t in enumerate(typ) if t == LATCH}
        self._settle(sv, order, bytearray(1 if t != DFF else 0 for t in typ))
        self.sv = sv
        self.active = [i for i in order if sv[out[i]] == 2 and typ[i] != LATCH]
        self.isact = bytearray(nl.n_cells)
        for i in self.active:
            self.isact[i] = 1
        # clock nets: forward from UserCLK through combinational cells
        clocks, todo = {clk_net}, [clk_net]
        while todo:
            n = todo.pop()
            for i in self.readers(n):
                if typ[i] in (AND, OR, NOT, BUF) and out[i] not in clocks:
                    clocks.add(out[i])
                    todo.append(out[i])
        self.clocks = clocks
        self.clocked = {i for i in self.dffs if a[i] in clocks}
        # config-load logic: nets whose forward cone reaches a latch pin or a flop clock
        self.load_pins = {a[i] for i, t in enumerate(typ) if t == LATCH} | \
                         {b[i] for i, t in enumerate(typ) if t == LATCH} | {a[i] for i in self.dffs}
        self.outs = [nl.port_bits[nm] for nm in st.out_names]
        self._observable()

    # -------------------------------------------------------------- simulation

    def state(self) -> dict[int, int]:
        """Flop states (cell -> 0 / 1 / 2) of the dump."""
        return {i: int(self.dump[self.out[i]]) for i in self.dffs}

    def _settle(self, v: list, cells: list[int], live: bytearray) -> None:
        """Event-driven evaluation as the kernel (rank order, a cell again when an input
        changes; structural loops iterate): cells first, then the live readers."""
        import heapq
        typ, a, b, out, rank = self.typ, self.a, self.b, self.out, self.rank
        heap = [(rank[i], i) for i in cells]
        heapq.heapify(heap)
        inq = set(cells)
        limit = 50 * len(cells) + 1000
        while heap and limit:
            limit -= 1
            _, i = heapq.heappop(heap)
            inq.discard(i)
            t, o = typ[i], out[i]
            if t == LATCH:
                e, d, q = v[a[i]], v[b[i]], self.latch_hold[o]
                x = d if e == 1 else (q if e == 0 else (q if q == d and q != 2 else 2))
            else:
                x = _v3(t, v[a[i]], v[b[i]] if t in (AND, OR) else 0)
            if x != v[o]:
                v[o] = x
                for r in self.readers(o):
                    if live[r] and r not in inq:
                        inq.add(r)
                        heapq.heappush(heap, (rank[r], r))

    def step(self, q: dict[int, int], vec: int) -> dict[int, int]:
        """One vector from flop states q: pads, rising clock edge; the new flop states."""
        v = list(self.sv)
        for n, j in self.pad_ix.items():
            v[n] = (vec >> j) & 1
        for i in self.dffs:
            v[self.out[i]] = q[i]
        self._settle(v, self.active, self.isact)
        nq = dict(q)
        for i in self.clocked:
            e, d, qq = v[self.b[i]], v[self.c[i]], q[i]
            nq[i] = d if e == 1 else (qq if e == 0 else (qq if qq == d and qq != 2 else 2))
        return nq

    # -------------------------------------------------------------- structure

    def _blocked(self, i: int, n: int, aff) -> bool:
        """Cell i passes no change on its input n (the other input a static controlling
        value that the change does not reach)."""
        t = self.typ[i]
        if t not in (AND, OR):
            return False
        o = self.b[i] if self.a[i] == n else self.a[i]
        if o in aff:
            return False
        return self.sv[o] == (0 if t == AND else 1)

    def _observable(self) -> None:
        """Flops whose change reaches a border output (0-1 BFS: clocks to the output),
        through flops with a defined state in the dump (an X chain masks the change)."""
        dist = {n: 0 for n in self.outs}
        dq = deque(self.outs)
        while dq:
            n = dq.popleft()
            d = dist[n]
            i = self.drv.get(n)
            if i is None:
                continue
            t = self.typ[i]
            if t == DFF:
                if i not in self.clocked or self.dump[n] == 2:
                    continue
                for m in (self.b[i], self.c[i]):
                    if m not in dist or dist[m] > d + 1:
                        dist[m] = d + 1
                        dq.append(m)
            elif t in (AND, OR, NOT, BUF):
                for m in ((self.a[i], self.b[i]) if t in (AND, OR) else (self.a[i],)):
                    if self.sv[m] != 2 or (t in (AND, OR) and self._blocked(i, m, ())):
                        continue
                    if m not in dist or dist[m] > d:
                        dist[m] = d
                        dq.appendleft(m)
        self.obs_dist = dist
        self.obs_dff = {i: dist[self.out[i]] + 1 for i in self.dffs if self.out[i] in dist}

    def affected(self, fnet: int, K: int, seeds=()):
        """Per clock the nets the fault can change (combinational closure from the fault
        net, the flops it changed one clock before and, at the first clock, seeds: outputs
        of flops whose faulty state differs already) and the flops it reaches; None when it
        reaches config-load logic (a latch pin or a flop clock)."""
        affs, hits = [], []
        prev: set[int] = set()
        for f in range(K):
            aff = {fnet} | {self.out[i] for i in prev} | (set(seeds) if f == 0 else set())
            todo = list(aff)
            hit = set()
            waiting: dict[int, list[int]] = {}   # blocking input -> cells it blocks
            while todo:
                n = todo.pop()
                if n in self.load_pins:
                    return None
                for i in waiting.pop(n, ()):      # its blocking input changes too
                    if self.out[i] not in aff:
                        aff.add(self.out[i])
                        todo.append(self.out[i])
                for i in self.readers(n):
                    t = self.typ[i]
                    if t == DFF:
                        if i in self.clocked and n in (self.b[i], self.c[i]):
                            hit.add(i)
                        continue
                    if t == LATCH:
                        continue
                    if self._blocked(i, n, aff):
                        waiting.setdefault(self.b[i] if self.a[i] == n else self.a[i], []).append(i)
                        continue
                    o = self.out[i]
                    if o not in aff:
                        aff.add(o)
                        todo.append(o)
            affs.append(aff)
            hits.append(hit)
            prev = hit | {self.q_of[n] for n in aff if n in self.q_of}   # a changed state stays
        return affs, hits


class Enc:
    """CNF of K clocks of a Config from a state (fixed values, or free), with constant
    folding; good copy shared, faulty copies per target behind an activation literal."""

    def __init__(self, cfg: Config, K: int, q: dict[int, int] | None, budget: int = 2000):
        from pysat.solvers import Cadical195
        self.cfg, self.K, self.q = cfg, K, q
        self.s = Cadical195()
        self.s.add_clause([T])
        self.nv = 1
        self.budget = budget
        self.pv = [[self.new() for _ in cfg.pads] for _ in range(K)]
        self.free: dict = {}
        self.g: dict = {}

    def new(self) -> int:
        self.nv += 1
        return self.nv

    def and2(self, x: int, y: int) -> int:
        if x == -T or y == -T or x == -y:
            return -T
        if x == T or x == y:
            return y
        if y == T:
            return x
        z = self.new()
        self.s.add_clause([-z, x])
        self.s.add_clause([-z, y])
        self.s.add_clause([z, -x, -y])
        return z

    def or2(self, x: int, y: int) -> int:
        return -self.and2(-x, -y)

    def xor2(self, x: int, y: int) -> int:
        if x == y:
            return -T
        if x == -y:
            return T
        if abs(x) == T:
            return -y if x == T else y
        if abs(y) == T:
            return -x if y == T else x
        z = self.new()
        for c in ([-z, x, y], [-z, -x, -y], [z, -x, y], [z, x, -y]):
            self.s.add_clause(c)
        return z

    def mux(self, e: int, d: int, q: int) -> int:
        if e == T:
            return d
        if e == -T or d == q:
            return q
        return self.or2(self.and2(e, d), self.and2(-e, q))

    def _free(self, key) -> int:
        if key not in self.free:
            self.free[key] = self.new()
        return self.free[key]

    def good(self, f: int, n: int) -> int:
        """Literal of net n at clock f in the fault-free copy."""
        return self._lit(f, n, None)

    def _lit(self, f: int, n: int, fx) -> int:
        """fx: None (good) or (fault net, polarity, affected per clock, memo)."""
        cfg = self.cfg
        memo = self.g if fx is None else fx[3]
        if (f, n) in memo:
            return memo[(f, n)]
        # depth-first, one missing input at a time: the stack is the chain of ancestors, so
        # an input already on it closes a structural loop, cut with a free variable
        stack, onstack = [(f, n)], {(f, n)}

        def need(d) -> bool:
            if d in memo:
                return False
            if d in onstack:
                memo[d] = self._free(("cyc", fx is None, d))
                return False
            stack.append(d)
            onstack.add(d)
            return True

        while stack:
            key = stack[-1]
            f0, n0 = key
            r = None
            if key in memo:
                stack.pop()
                onstack.discard(key)
                continue
            if fx is not None and n0 not in fx[2][f0]:   # the fault cannot reach it
                r = self.good(f0, n0)
            elif fx is not None and n0 == fx[0]:
                r = T if fx[1] else -T
            elif cfg.sv[n0] != 2:
                r = T if cfg.sv[n0] else -T
            elif n0 in cfg.pad_ix:
                r = self.pv[f0][cfg.pad_ix[n0]]
            else:
                i = cfg.drv.get(n0)
                if i is None or cfg.typ[i] == LATCH:
                    r = self._free(("u", n0))
                elif cfg.typ[i] == DFF:
                    if f0 == 0 and fx is not None and i in fx[4]:   # the faulty machine's own state
                        v = fx[4][i]
                        r = self._free(("qf", i)) if v == 2 else (T if v else -T)
                    elif f0 == 0:
                        v = 2 if self.q is None else self.q[i]
                        r = self._free(("q", i)) if v == 2 else (T if v else -T)
                    elif i not in cfg.clocked:
                        r = self._lit(0, n0, fx)
                    else:   # next state of clock f0 - 1: E ? D : Q
                        de, dd, dq = (f0 - 1, cfg.b[i]), (f0 - 1, cfg.c[i]), (f0 - 1, n0)
                        if need(de):
                            continue
                        e = memo[de]
                        if e != -T and need(dd):
                            continue
                        if e != T and need(dq):
                            continue
                        r = memo[dd] if e == T else (memo[dq] if e == -T else self.mux(e, memo[dd], memo[dq]))
                else:
                    t = cfg.typ[i]
                    da = (f0, cfg.a[i])
                    if need(da):
                        continue
                    x = memo[da]
                    if t == NOT:
                        r = -x
                    elif t == BUF:
                        r = x
                    elif (t == AND and x == -T) or (t == OR and x == T):   # controlling
                        r = x
                    else:
                        db = (f0, cfg.b[i])
                        if need(db):
                            continue
                        r = self.and2(x, memo[db]) if t == AND else self.or2(x, memo[db])
            memo[key] = r
            stack.pop()
            onstack.discard(key)
        return memo[(f, n)]

    def target(self, fnet: int, pol: int, dffs=None, outputs: bool = True, qf: dict | None = None):
        """Activation literal of the fault's goal (a next-state difference of a flop in
        dffs, None: any, or at a border output), or "load" / "blocked". qf: flops whose
        state in the faulty machine differs from the good one (default: equal states)."""
        cfg = self.cfg
        qf = qf or {}
        a = cfg.affected(fnet, self.K, [cfg.out[i] for i in qf])
        if a is None:
            return "load"
        affs, hits = a
        fx = (fnet, pol, affs, {}, qf)
        diffs = []
        for f in range(self.K):
            for i in sorted(hits[f]):
                if dffs is not None and i not in dffs:
                    continue
                g = self.mux(self.good(f, cfg.b[i]), self.good(f, cfg.c[i]), self.good(f, cfg.out[i]))
                x = self.mux(self._lit(f, cfg.b[i], fx), self._lit(f, cfg.c[i], fx), self._lit(f, cfg.out[i], fx))
                d = self.xor2(g, x)
                if d != -T:
                    diffs.append(d)
            if outputs:
                for o in cfg.outs:
                    if o in affs[f]:
                        d = self.xor2(self.good(f, o), self._lit(f, o, fx))
                        if d != -T:
                            diffs.append(d)
        if T in diffs:
            return T
        if not diffs:
            return "blocked"
        act = self.new()
        self.s.add_clause([-act] + diffs)
        return act

    def solve(self, assumptions: list[int]):
        self.s.conf_budget(self.budget)
        return self.s.solve_limited(assumptions=[x for x in assumptions if x != T])

    def vectors(self, rnd: random.Random) -> list[int]:
        """Pad values per clock of the last model (pads the model leaves open: random)."""
        m = self.s.get_model() or []
        pos = {abs(x): x > 0 for x in m}
        vs = []
        for f in range(self.K):
            v = 0
            for j, x in enumerate(self.pv[f]):
                bit = pos[x] if x in pos else rnd.getrandbits(1)
                v |= int(bit) << j
            vs.append(v)
        return vs

    def close(self):
        self.s.delete()


# ------------------------------------------------------------------ one config

def kernel_states(nl, st, cfg: Config, col0: int, extra: list[int], faults: list[tuple[int, int]]):
    """Flop states after st and the vectors extra: the good machine's (cell -> 0 / 1 / 2) and
    per fault the flops where its machine differs (the kernel's net dump: lane 0 good, lane
    k + 1 the k-th fault of a batch of 63)."""
    from fabtest.sim.fastsim import fastsim
    add = []
    for v in extra:
        r = st.rows[-1].copy()
        for j in range(len(cfg.pads)):
            r[col0 + j] = (v >> j) & 1
        r[-1] = 0
        r1 = r.copy()
        r1[-1] = 1
        add += [r, r1, r.copy()]
    rows = np.vstack([st.rows] + ([np.array(add, dtype=st.rows.dtype)] if add else []))
    sample = np.concatenate([st.sample, np.zeros(len(add), dtype=st.sample.dtype)])
    eng = fastsim.Engine(nl, [fastsim.Stim(st.name, st.in_names, rows, sample, st.out_names, None)],
                         W=1, limit_factor=200)
    qn = np.array([cfg.out[i] for i in cfg.dffs], dtype=np.int64)
    good, faulty = None, []
    for b in range(0, max(len(faults), 1), 63):
        batch = faults[b:b + 63]
        h = np.zeros(nl.n_nets, dtype=np.uint64)
        lo = np.zeros(nl.n_nets, dtype=np.uint64)
        eng.lib.set_dump(fastsim._p(h), fastsim._p(lo))
        try:
            eng.run_one(batch, 0)
        finally:
            eng.lib.set_dump(None, None)
        hq, lq = h[qn], lo[qn]

        def lane(k):
            hb = ((hq >> np.uint64(k)) & np.uint64(1)).astype(np.int8)
            lb = ((lq >> np.uint64(k)) & np.uint64(1)).astype(np.int8)
            return np.where(hb & lb, 2, np.where(hb, 1, 0))
        g = lane(0)
        if good is None:
            good = {i: int(x) for i, x in zip(cfg.dffs, g.tolist())}
        for k in range(len(batch)):
            f = lane(k + 1)
            idx = np.nonzero(f != g)[0]
            faulty.append({cfg.dffs[j]: int(f[j]) for j in idx.tolist()})
    return good, faulty


def run_config(nl, ff, st, faults: list[tuple[int, int]], opts: dict) -> dict:
    """Classify the faults in this config and generate SAT vectors for the possible ones.
    faults: (net bit, polarity). Returns the extra vectors and the counts."""
    col0 = st.in_names.index(ff.port(ff.border_in[0]))
    pads = [nl.port_bits[st.in_names[col0 + j]] for j in range(len(ff.border_in))]
    cfg = Config(nl, st, pads, nl.port_bits["UserCLK"], dump_values(nl, st))
    rnd = random.Random(opts.get("seed", 1) * 1000 + int(st.name[2:]))
    out = {"classes": {"possible": 0, "unsat": 0, "timeout": 0, "blocked": 0, "load": 0}}
    # classification: one clock, states free
    enc = Enc(cfg, 1, None, opts.get("budget", 2000))
    possible, undecided = [], []
    for k, (n, p) in enumerate(faults):
        act = enc.target(n, p)
        if act in ("load", "blocked"):
            out["classes"][act] += 1
            if act == "load":
                undecided.append(k)
            continue
        r = enc.solve([act])
        key = "possible" if r else ("unsat" if r is False else "timeout")
        out["classes"][key] += 1
        if r:
            possible.append(k)
        elif r is None:
            undecided.append(k)
    enc.close()
    out["possible_ids"], out["undecided_ids"] = possible, undecided
    # generation: K clocks from the reached states (good and per fault, from the kernel),
    # targets packed per round
    K = opts.get("frames", 2)
    obs = cfg.obs_dff
    flush = (max(obs.values()) + 2) if obs else 0
    room = opts.get("max_vectors", 256) - opts["n_base"] - flush
    pend = possible[:opts.get("targets", 400)]
    tries = {k: 0 for k in pend}
    extra: list[int] = []
    gen = {"found": 0, "unsat_from_state": 0, "timeout": 0, "unobservable": 0, "x_state": 0, "rounds": 0,
           "idle": 0}
    idle, seen_x, found_ids = 0, set(), []
    while pend and len(extra) + K <= room:
        q, qfs = kernel_states(nl, st, cfg, col0, extra, [faults[k] for k in pend])
        enc = Enc(cfg, K, q, opts.get("budget", 2000))
        acc, left = [], []
        for j, k in enumerate(pend):
            n, p = faults[k]
            if any(v == 2 for v in qfs[j].values()):
                # its faulty machine holds X where the good one does not (a flop it never
                # writes: a power-up question, fabtest powerup); the kernel keeps that X, so
                # no vector gives a definite difference through it: not a target
                if k not in seen_x:
                    seen_x.add(k)
                    gen["x_state"] += 1
                if opts.get("strict_x", True):
                    tries[k] = 99
                    continue
            act = enc.target(n, p, dffs=obs, outputs=True, qf=qfs[j])
            if act in ("load", "blocked"):   # no observable flop or output reached
                gen["unobservable"] += 1
                tries[k] = 99
                continue
            r = enc.solve([x for _, x in acc] + [act])
            if r:
                acc.append((k, act))
                if len(acc) >= opts.get("per_round", 64):
                    left.extend(pend[j + 1:])
                    break
            else:
                tries[k] += 1
                if not acc:
                    gen["unsat_from_state" if r is False else "timeout"] += 1
                left.append(k)
        gen["rounds"] += 1
        if acc:
            vs = enc.vectors(rnd)
            gen["found"] += len(acc)
            found_ids += [k for k, _ in acc]
            idle = 0
        else:   # nothing from this state: one random vector, then again (a few times)
            idle += 1
            gen["idle"] += 1
            vs = [rnd.getrandbits(len(pads))]
        enc.close()
        extra += vs
        if idle >= opts.get("max_idle", 3):
            break
        pend = [k for k in left if tries[k] < opts.get("max_tries", 3)]
    n_sat = len(extra)
    extra += [rnd.getrandbits(len(pads)) for _ in range(flush if extra else 0)]
    out.update(gen=gen, sat_vectors=n_sat, flush=flush if n_sat else 0, extra=extra, x_ids=sorted(seen_x),
               found_ids=found_ids,
               possible=len(possible), targets=len(possible[:opts.get("targets", 400)]))
    return out


# ------------------------------------------------------------------ a run

_W: dict = {}


def _work(c: int) -> tuple[int, dict]:
    w = _W
    st = w["stims"][c]
    return c, run_config(w["nl"], w["ff"], st, w["items"], w["opts"])


def satvec(spec, work: Path, run: Path, out: Path, opts: dict, jobs: int = 3) -> dict:
    """SAT vectors for every config of run, targets: the faults its fault simulation leaves
    undetected (every config); writes out/configs.json (run's configs + extra_vectors) and
    out/satvec.json."""
    from fabtest import pipeline
    from fabtest.sim.fastsim import fastsim
    data = json.loads((run / "configs.json").read_text())
    if data.get("extra_vectors"):
        raise SystemExit(f"fabtest satvec: {run} already has extra vectors")
    rows = [r for p in sorted((run / "faultsim").glob("chunk_*.csv")) for r in csv.DictReader(p.open())]
    if not rows:
        raise SystemExit(f"fabtest satvec: {run} has no fault simulation")
    cfgs = [k for k in rows[0] if k not in ("id", "path", "polarity")]
    und = {r["id"] for r in rows if "D" not in {r[c] for c in cfgs}}
    with (work / "synth" / "faults.csv").open() as fh:
        net = {r["id"]: (r["net"], int(r["polarity"])) for r in csv.DictReader(fh) if r["id"] in und}
    ids = sorted(und, key=int)
    stims = pipeline._stims(spec, run / "configs.json")
    nl = fastsim.Netlist("fabtest", work / "synth" / "netlist.json")
    ff = pipeline.model(spec)
    items = [(int(nl.net_bit[net[i][0]]), net[i][1]) for i in ids]
    opts = dict(opts, n_base=len(ff.vecs))
    _W.update(nl=nl, ff=ff, stims=stims, items=items, opts=opts)
    res: dict[int, dict] = {}
    if jobs > 1:
        import multiprocessing as mp
        with mp.get_context("fork").Pool(jobs) as pool:
            for c, r in pool.imap_unordered(_work, range(len(stims))):
                res[c] = r
                print(f"  {stims[c].name}: {r['classes']} vectors {r['sat_vectors']} + {r['flush']} flush, "
                      f"found {r['gen']['found']}", flush=True)
    else:
        for c in range(len(stims)):
            res[c] = _work(c)[1]
            print(f"  {stims[c].name}: {res[c]['classes']}", flush=True)
    out.mkdir(parents=True, exist_ok=True)
    data["extra_vectors"] = [res[c]["extra"] for c in range(len(stims))]
    (out / "configs.json").write_text(json.dumps(data))
    tot = {k: sum(res[c]["classes"][k] for c in res) for k in res[0]["classes"]}
    gen = {k: sum(res[c]["gen"][k] for c in res) for k in res[0]["gen"]}
    poss = {k for c in res for k in res[c]["possible_ids"]}
    open_ = {k for c in res for k in res[c]["undecided_ids"]} - poss
    never = [ids[k] for k in range(len(ids)) if k not in poss and k not in open_]
    clean = {k for c in res for k in res[c]["possible_ids"] if k not in res[c]["x_ids"]}
    found = sorted({k for c in res for k in res[c]["found_ids"]})
    info = {"run": str(run), "targets": len(ids), "opts": opts, "classes": tot, "gen": gen,
            "possible_in_some_config": len(poss), "possible_without_x_state": len(clean),
            "faults_with_a_vector": len(found), "found_fault_ids": [ids[k] for k in found],
            "undecided": len(open_),
            "impossible_in_every_config": len(never), "impossible_ids": never,
            "vectors": {stims[c].name: [res[c]["sat_vectors"], res[c]["flush"]] for c in res},
            "per_config": {stims[c].name: {k: v for k, v in res[c].items()
                                           if k not in ("extra", "possible_ids", "undecided_ids", "x_ids",
                                                        "found_ids")}
                           for c in res}}
    (out / "satvec.json").write_text(json.dumps(info, indent=1))
    return info
