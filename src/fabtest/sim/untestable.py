"""Untestable stuck-at faults: a SAT proof per fault on its tile (pysat, CaDiCaL).

A fault is UNTESTABLE when no test exists in any configuration. The check works on a
window of the flat netlist: the tile instance(s) the fault net touches (its driver's and its
readers', so a wire between two tiles takes both). Everything entering the window is free:

  - nets driven outside the window (the neighbours, the pads, the config frame registers)
  - the outputs of every latch and flop in the window (config bits, LUT flops, memories)

and the fault is detectable in the window when some assignment of those makes the fault-free
and the faulty circuit differ on a window output (a net read outside the window, or a top
output port) or on the next state of a latch / flop (E ? D : Q). UNSAT is a proof: a real
test of the fabric has a first clock where the two circuits differ while their stored state
is still equal, and that clock satisfies the window problem (the window only frees what the
fabric constrains). SAT proves nothing about the fabric (the free inputs may be unreachable).

Not decided: faults whose cone reaches a flop clock (a stuck clock is not a next-state
difference), windows over MAX_CELLS cells and solves over BUDGET conflicts. Combinational loops need nothing special: the
CNF allows every fixed point of both circuits, a superset of what the gates can hold.

Statuses: U untestable (proof), T testable in its window, C not decided. why (after a
check): dead (the cone reaches no window output and no storage element) or redundant (SAT
UNSAT: observable, but no input makes a difference).
"""
from __future__ import annotations

import numpy as np

AND, OR, NOT, BUF, LATCH, DFF = 0, 1, 2, 3, 4, 5
MAX_CELLS = 200_000
BUDGET = 20_000   # SAT conflicts per solve; over it: C (not decided)


def instances(nl) -> np.ndarray:
    """Instance index of every cell: the first component of its flat name."""
    ids: dict[str, int] = {}
    return np.array([ids.setdefault(n.split(".", 1)[0], len(ids)) for n in nl.cell_names], dtype=np.int32)


class Checker:
    def __init__(self, nl, thru_inv: bool = False):
        """thru_inv: an inverter / buffer outside the window on a latch output is encoded
        too (a window of a switch matrix then keeps ConfigBits_N = NOT ConfigBits)."""
        self.thru_inv = thru_inv
        from pysat.solvers import Cadical195   # noqa: F401  (fail early when pysat is missing)
        self.nl = nl
        self.inst = instances(nl)
        order = np.argsort(self.inst, kind="stable")
        bounds = np.searchsorted(self.inst[order], np.arange(self.inst.max() + 2))
        self.cells_of = [order[bounds[i]:bounds[i + 1]] for i in range(len(bounds) - 1)]
        self.drv = np.full(nl.n_nets, -1, dtype=np.int64)
        for net, cell in nl.driver.items():
            self.drv[net] = cell
        self.ins = np.stack([nl.a, nl.b, nl.c], axis=1)
        self.out_ports = {b for b in nl.port_bits.values() if self.drv[b] >= 0}
        self.const = {int(n): int(v) for n, v in zip(nl.const_net, nl.const_val) if v in (0, 1)} if nl.n_const else {}
        # X sources: undriven nets that are not top inputs, and x constants (a floating wire:
        # its value is neither chosen by the tester nor known)
        top_in = {b for b in nl.port_bits.values() if self.drv[b] < 0}
        self.xsrc = {int(n) for n, v in zip(nl.const_net, nl.const_val) if v not in (0, 1)} if nl.n_const else set()
        used = np.unique(np.concatenate([nl.a, nl.b, nl.c]))
        self.xsrc |= {int(n) for n in used if n >= 0 and self.drv[n] < 0 and n not in top_in and n not in self.const}
        self.last_x = 0
        self.why = ""
        self.budget = BUDGET
        self.cfg_nets: set[int] = set()   # free nets lifted last (config bits: keep data free first)

    def readers(self, net: int) -> np.ndarray:
        return self.nl.rd_idx[self.nl.rd_off[net]:self.nl.rd_off[net + 1]]

    def check(self, net: int, pol: int, fix=None, fixes=None, window=None, witness: bool = False,
              cut=frozenset()) -> str:
        """fix: optional per-net values (int8: 0 / 1, other: free) for the free nets of the
        window, e.g. the config latches of one loaded config: then T / U answer whether the
        fault is detectable in the tile under that config. fixes: a list of such arrays,
        solved incrementally on one CNF (assumptions); self.hits is then the list of their
        indices that are T (empty when the fault is U or C without fixes), self.undecided
        those over BUDGET / 10 conflicts. window: the cells of the window (a set), instead of
        the tile instances of the fault net. witness: on T, self.care = the free-net values
        that force a difference whatever the other free nets are (a lifted SAT model), and
        self.obs_hit = the observed nets that differ under it."""
        self.hits, self.undecided = [], []
        from pysat.solvers import Cadical195
        nl, typ, out, ins = self.nl, self.nl.type, self.nl.out, self.ins
        self.care, self.obs_hit, self.val = {}, [], {}
        # cut: nets driven in the window but read as free inputs (a wire that leaves the
        # window and comes back: no loop through it); a cone net in cut is observed
        self.why = "dead"
        if window is not None:
            inwin = window.__contains__
        else:
            rd = self.readers(net)
            win = {int(self.inst[c]) for c in rd}
            if self.drv[net] >= 0:
                win.add(int(self.inst[self.drv[net]]))
            if not win:
                return "U"   # nothing reads or drives it: no effect
            if sum(len(self.cells_of[i]) for i in win) > MAX_CELLS:
                return "C"
            inwin = lambda c: int(self.inst[c]) in win   # noqa: E731
        thru = self.thru_inv

        def isfree(m: int) -> bool:
            if m in cut:
                return True
            c = self.drv[m]
            if c < 0 or typ[c] in (LATCH, DFF):
                return True
            if inwin(int(c)):
                return False
            # thru_inv: an inverter / buffer on a latch output (ConfigBits_N) is encoded
            if thru and typ[c] in (NOT, BUF):
                d = self.drv[ins[c, 0]]
                return not (d >= 0 and typ[d] == LATCH)
            return True
        # forward cone from the fault net: combinational cells; storage cells are sinks
        cone_nets, cone_cells, sinks, stack = {net}, [], set(), [net]
        while stack:
            n = stack.pop()
            for c in self.readers(n):
                c = int(c)
                if not inwin(c):
                    continue
                t = typ[c]
                if t == DFF and ins[c, 0] in cone_nets:
                    return "C"   # a clock in the cone
                if t in (LATCH, DFF):
                    sinks.add(c)
                    continue
                o = int(out[c])
                if o not in cone_nets:
                    cone_nets.add(o)
                    cone_cells.append(c)
                    if o not in cut:
                        stack.append(o)
        # observed nets of the cone: read outside the window or a top output port
        obs = [n for n in cone_nets if n in self.out_ports or n in cut and n != net
               or any(not inwin(int(c)) for c in self.readers(n))]
        if not obs and not sinks:
            return "U"
        # fault-free values needed: the fanin of the cone cells and of the sinks' pins
        V: dict[int, int] = {}
        clauses: list[list[int]] = []
        nv = [0]

        def var() -> int:
            nv[0] += 1
            return nv[0]

        def good(n: int) -> int:
            """Variable of fault-free net n; encodes its fanin in the window (worklist: every
            net gets its variable before its gate, so loops close by themselves)."""
            if n in V:
                return V[n]
            V[n] = var()
            work = [n]
            while work:
                m = work.pop()
                c = self.drv[m]
                if isfree(m):
                    if m in self.const:
                        clauses.append([V[m] if self.const[m] else -V[m]])
                    elif fix is not None and fix[m] in (0, 1):
                        clauses.append([V[m] if fix[m] else -V[m]])
                    continue
                for x in ins[c]:
                    x = int(x)
                    if x >= 0 and x not in V:
                        V[x] = var()
                        work.append(x)
                gate(int(c), V, V[m])
            return V[n]

        def gate(c: int, src: dict, y: int, alt=None) -> None:
            """Clauses of cell c's output y; inputs from alt (faulty) first, else src (good)."""
            def lit(x):
                x = int(x)
                if alt is not None and x in alt and (x not in cut or x == net):
                    return alt[x]
                return src[x] if x in src else good(x)   # good: during good() every input has one
            t = typ[c]
            a = lit(ins[c, 0])
            if t == AND or t == OR:
                b = lit(ins[c, 1])
                if t == AND:
                    clauses.extend([[-y, a], [-y, b], [y, -a, -b]])
                else:
                    clauses.extend([[y, -a], [y, -b], [-y, a, b]])
            elif t == NOT:
                clauses.extend([[y, a], [-y, -a]])
            else:   # BUF
                clauses.extend([[y, -a], [-y, a]])

        F: dict[int, int] = {}
        F[net] = var()
        clauses.append([F[net] if pol else -F[net]])
        for c in cone_cells:
            F[int(out[c])] = var()
        for c in cone_cells:
            gate(c, V, F[int(out[c])], alt=F)
        diffs = []

        dnet = {}

        def differ(g: int, f: int) -> int:
            d = var()
            clauses.extend([[-d, g, f], [-d, -g, -f], [d, -g, f], [d, g, -f]])   # d = g xor f
            diffs.append(d)
            return d
        for n in obs:
            dnet[differ(good(n), F[n])] = n

        def nxt(c: int, src) -> int:
            """E ? D : Q of storage cell c (latch: E, D; flop: C, E, D)."""
            e, d = (ins[c, 0], ins[c, 1]) if typ[c] == LATCH else (ins[c, 1], ins[c, 2])
            q = int(out[c])
            lit = lambda x: src.get(int(x)) or good(int(x))   # noqa: E731
            E, D, Q = lit(e), lit(d), lit(q)
            y = var()
            clauses.extend([[-y, -E, D], [-y, E, Q], [y, -E, -D], [y, E, -Q]])
            return y
        for c in sinks:
            differ(nxt(c, {}), nxt(c, F))
        if not diffs:
            return "U"
        self.last_x = sum(1 for n in V if n in self.xsrc)
        clauses.append(diffs)
        self.why = "redundant"
        with Cadical195(bootstrap_with=clauses) as s:
            s.conf_budget(self.budget)
            r = s.solve_limited()
            if r is None:
                return "C"   # over the conflict budget (e.g. a multiplier miter)
            if not r:
                return "U"
            self.free_nets = free = [m for m in V if m not in F and isfree(m)]
            if witness:
                model = set(s.get_model())
                self.val = val = {m: int(V[m] in model) for m in free}
                hit = [d for d in dnet if d in model]
                # lifting towards one observed net: drop a free net when the rest still forces
                # that net to differ; a few candidate nets, the smallest result is kept
                best = None
                for d in hit[:4]:
                    with Cadical195(bootstrap_with=clauses[:-1] + [[-d]]) as s2:
                        care = dict(val)
                        lit = lambda m: V[m] if care[m] else -V[m]   # noqa: E731
                        for m in sorted(free, key=lambda m: m in self.cfg_nets):   # data first
                            keep = care.pop(m)
                            s2.conf_budget(max(self.budget // 10, 100))
                            if s2.solve_limited(assumptions=[lit(x) for x in care]) is not False:
                                care[m] = keep   # needed (or undecided)
                    if best is None or len(care) < len(best[1]):
                        best = (dnet[d], care)
                self.obs_hit = [best[0]]
                self.care = best[1]
            if fixes is not None:
                fa = np.array(free, dtype=np.int64)
                va = np.array([V[m] for m in free], dtype=np.int64)
                for k, fx in enumerate(fixes):
                    vals = fx[fa]
                    sel = (vals == 0) | (vals == 1)
                    assume = np.where(vals[sel] == 1, va[sel], -va[sel]).tolist()
                    s.conf_budget(max(self.budget // 10, 100))
                    r = s.solve_limited(assumptions=assume)
                    if r:
                        self.hits.append(k)
                    elif r is None:
                        self.undecided.append(k)   # over the budget
            return "T"
