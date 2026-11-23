"""Linear (GF(2)) model of a routed test config: reconvergence check, diagnostic, router hook.

In a test config every chain / route LUT is a registered XOR / XNOR and every switch-matrix
mux a fixed wire, so the fault-free value of a net is an affine function over GF(2) of the
pad inputs of the vectors so far and of the flops' power-up values. A value is an int: bit 0
the constant, bit 1 + t * P + j pad j at vector t, above them one bit per other variable:
power-up values and undriven nets (X), results of a non-linear op (a carry majority, a
MUX8LUT with a varying select, a BEL or memory read: fresh and defined, one per vector).
Two nets can take opposite values in some vector iff their functions differ (XOR not 0).

  Sim: the functions of a config per vector, before the clock edge (PRE) and at the sample
       after it (POST), clocked as fabric.seq_expected clocks the model.
  Sim.impulse: where a flip of a net at one vector shows at the border outputs (delays), for
       many nets at once (lanes): definite through linear ops, "maybe" through the others.
  MuxTrees: the gate-level tree of a switch-matrix mux in the flat netlist (its cone back to
       its config latches and data inputs, the data inputs matched to the model's codes by
       simulation) and the effect of a stuck-at fault on the mux output, as a truth table
       over the data inputs, for the code of a config.
  diagnose (fabtest linear): per fault a run leaves undetected, whether some config of the
       run could detect it at all: its effect at the mux output (the config's code), an
       observed output defined at the sample it reaches (the gate-level fault-free run) and
       data-input values the config's functions can take (with X inputs at both values, as
       the 3-valued simulation needs).
  Router hook (fab_router.LINEAR, fabtest route --linear): the bidirectional cover's second
       input and the codes the router tries are judged by these functions instead of the
       signal names (Funcs: functions of the config under construction).
"""
from __future__ import annotations

import csv
import json
import random
import re
from collections import Counter, defaultdict
from pathlib import Path

PRE, POST = 0, 1
LATCH, DFF = 4, 5


# ------------------------------------------------------------------ the config's functions

class Sim:
    """Affine functions of every net of a config (sels, luts as stimuli.config_setup) per
    vector and phase: vals[2 * t + PRE / POST][net]. xmask: the X variables."""

    def __init__(self, arr, sels: dict, luts: dict, n_vec: int | None = None):
        from fabtest.model import fabric
        self.arr = arr
        self.ops = arr.ops(sels, luts)
        self.luts = luts
        self.P = len(arr.border_in)
        self.nv = n_vec or len(arr.vecs)
        self.nvar = 1 + self.P * self.nv
        self.xmask = 0
        self._xfix: dict = {}
        self.unknown = set(getattr(arr, "rings", ())) | {"XU"}   # undriven: X in the gates
        # other unknown sources (W_IO registered pads, unmodelled BELs): fresh, defined
        self.tainted = set(getattr(arr, "tainted", ())) - self.unknown
        cn = arr.canon
        self.flop_of = {cn(t, f"L{x}_O"): (t, x) for t in arr.lut_tiles for x in "ABCDEFGH"}
        self.regs = [(t, x) for t in arr.lut_tiles for x in "ABCDEFGH" if luts[t][x][2]]
        self.fidx = {f: fabric.lut_index_nets(arr, f[0], f[1], luts[f[0]][f[1]][1]) for f in self.regs}
        self.fensr = {f: (cn(f[0], f"L{f[1]}_EN"), cn(f[0], f"L{f[1]}_SR")) for f in self.regs}
        self.lin: dict = {}     # net -> ("a", args) affine, coefficients 1 / ("n", deps) non-linear
        self.flin: dict = {}    # flop -> ("a", index nets) / ("h",) hold / ("c",) constant / ("n", nets)
        # memories (impulse): a read is "maybe" from the edge where any pin of its memory
        # carried a flip (write data / address / control), sticky
        self.mem_pins: dict = {}
        for k, p in getattr(arr, "bram_pins", {}).items():
            self.mem_pins[("B", k)] = [n for key in ("wr_data", "wr_addr", "rd_addr", "C") for n in p.get(key, ())]
        for t, p in getattr(arr, "rf_pins", {}).items():
            self.mem_pins[("R", t)] = [*p["D"], *p["W"], p["en"]]
        self.vals: list = []
        q = {f: self._var(True) for f in self.regs}
        for t in range(self.nv):
            pre = self._comb(t, q, record=(t == self.nv - 1))
            q = self._clock(pre, q, record=(t == self.nv - 1))
            self.vals.append(pre)
            self.vals.append(self._comb(t, q))

    def _var(self, x: bool = False) -> int:
        b = 1 << self.nvar
        self.nvar += 1
        if x:
            self.xmask |= b
        return b

    def _xconst(self, n) -> int:
        b = self._xfix.get(n)
        if b is None:
            b = self._xfix[n] = self._var(True)
        return b

    def _fresh(self, ins) -> int:
        return self._var(any(v & self.xmask for v in ins))

    def is_x(self, v: int) -> bool:
        return bool(v & self.xmask)

    def _lut(self, tt: int, ins: list[int]):
        """LUT on affine inputs: (value, linear inputs) when the table restricted to the
        non-constant inputs is affine, else (fresh variable, None)."""
        var = [j for j, x in enumerate(ins) if x not in (0, 1)]
        fix = sum(x << j for j, x in enumerate(ins) if x in (0, 1))

        def T(sub):
            e = fix
            for i, j in enumerate(var):
                if (sub >> i) & 1:
                    e |= 1 << j
            return (tt >> e) & 1
        c0 = T(0)
        coef = [T(1 << i) ^ c0 for i in range(len(var))]
        for sub in range(1 << len(var)):
            p = c0
            for i in range(len(var)):
                if (sub >> i) & 1:
                    p ^= coef[i]
            if T(sub) != p:
                return self._fresh(ins), None
        r = c0
        lin = []
        for i, j in enumerate(var):
            if coef[i]:
                r ^= ins[j]
                lin.append(j)
        return r, lin

    def _comb(self, t: int, q: dict, record: bool = False) -> dict:
        arr = self.arr
        v = {"GND": 0, "XU": self._xconst("XU")}
        base = 1 + t * self.P
        for j, n in enumerate(arr.border_in):
            v[n] = 1 << (base + j)
        lin = self.lin
        for n, op, a in self.ops:
            if op == "b":
                r, li = v[a], ("a", [a])
            elif op == "n":
                r, li = v[a] ^ 1, ("a", [a])
            elif op == "c":
                if n in self.unknown:
                    r, li = self._xconst(n), ("a", [])
                elif n in self.tainted:
                    r, li = self._var(False), ("n", [])
                else:
                    r, li = (1 if a else 0), ("a", [])
            elif op == "x":
                r = v[a[0]] ^ v[a[1]] ^ v[a[2]] ^ v[a[3]]
                li = ("a", list(a))
            elif op == "q":
                r, li = q[self.flop_of[n]], ("q",)
            elif op == "l":
                ins = [v[p] for p in a[0]]
                r, ll = self._lut(a[1], ins)
                li = ("a", [a[0][j] for j in ll]) if ll is not None else ("n", list(a[0]))
            elif op == "m":
                x, y, z = (v[p] for p in a)
                if x == y or x == z:
                    r, li = x, ("a", [a[0]])
                elif y == z:
                    r, li = y, ("a", [a[1]])
                elif {x, y} == {0, 1}:
                    r, li = z, ("a", [a[2]])
                elif {x, z} == {0, 1}:
                    r, li = y, ("a", [a[1]])
                elif {y, z} == {0, 1}:
                    r, li = x, ("a", [a[0]])
                else:
                    r, li = self._fresh((x, y, z)), ("n", list(a))
            elif op == "s":
                x, y, s = (v[p] for p in a)
                if s in (0, 1):
                    r, li = (y, ("a", [a[1]])) if s else (x, ("a", [a[0]]))
                elif x == y:
                    r, li = x, ("a", [a[0]])
                else:
                    r, li = self._fresh((x, y, s)), ("n", list(a))
            else:   # F / Q / M: a BEL or memory read: fresh, defined (optimistic)
                from fabtest.model.fabric import _deps
                d = _deps(op, a)
                r = self._var(False)
                li = ("M", ("B", a[1][0]), d) if op == "Q" else ("M", ("R", a[1][0]), d) if op == "F" else ("n", d)
            v[n] = r
            if record:
                lin[n] = li
        return v

    def _clock(self, v: dict, q: dict, record: bool = False) -> dict:
        """Flop update at the edge (fabric.seq_expected: EN ? (SR ? reset value : LUT) : hold)."""
        nq = {}
        for f in self.regs:
            tt, _i0, _ff, rv = self.luts[f[0]][f[1]]
            idx = self.fidx[f]
            d, ll = self._lut(tt, [v[n] for n in idx])
            en, sr = (v.get(n, 0) for n in self.fensr[f])
            if en == 1 and sr == 0:
                nq[f], li = d, ("a", [idx[j] for j in ll]) if ll is not None else ("n", list(idx))
            elif en == 0:
                nq[f], li = q[f], ("h",)
            elif en == 1 and sr == 1:
                nq[f], li = rv, ("c",)
            else:
                nq[f], li = self._fresh((d, en, sr, q[f])), ("n", list(idx) + list(self.fensr[f]))
            if record:
                self.flin[f] = li
        return nq

    def at(self, t: int, ph: int) -> dict:
        return self.vals[2 * t + ph]

    def impulse(self, lanes: list) -> dict:
        """ph -> [per border output: {delay: (definite, maybe) lane masks}] for a flip of
        each lane net (bit i: lanes[i]) at vector 0, phase ph (PRE: before the edge, caught
        by the flops; POST: at the sample, combinational only). Shift-invariant: the same
        delays from any vector."""
        inj = defaultdict(int)
        for i, n in enumerate(lanes):
            inj[n] |= 1 << i
        out_idx = list(self.arr.border_out)
        res = {}
        for ph in (PRE, POST):
            resp = [defaultdict(lambda: [0, 0]) for _ in out_idx]
            qd: dict = {}
            mem: dict = {}
            dv = self._dcomb(qd, inj, mem)
            if ph == POST:
                self._record(resp, dv, 0)
                res[ph] = resp
                continue
            self._write(dv, mem)
            qd = self._dclock(dv, qd)
            self._record(resp, self._dcomb(qd, None, mem), 0)
            for d in range(1, self.nv):
                if not any(a | b for a, b in qd.values()) and not any(mem.values()):
                    break
                dv = self._dcomb(qd, None, mem)
                self._write(dv, mem)
                qd = self._dclock(dv, qd)
                self._record(resp, self._dcomb(qd, None, mem), d)
            res[ph] = resp
        return res

    def _write(self, dv: dict, mem: dict) -> None:
        """The edge: a memory whose pins carry a flip may hold it from now on."""
        for key, pins in self.mem_pins.items():
            w = 0
            for p in pins:
                a, b = dv.get(p, (0, 0))
                w |= a | b
            if w:
                mem[key] = mem.get(key, 0) | w

    def _record(self, resp, dv, d):
        for i, n in enumerate(self.arr.border_out):
            a, b = dv.get(n, (0, 0))
            if a or b:
                resp[i][d][0] |= a
                resp[i][d][1] |= b

    def _dcomb(self, qd: dict, inj, mem=None) -> dict:
        dv: dict = {}
        z = (0, 0)
        for n, op, _a in self.ops:
            li = self.lin[n]
            if li[0] == "q":
                d, m = qd.get(self.flop_of[n], z)
            elif li[0] == "M":    # memory read: sticky state, and the address (RegFile)
                d, m = 0, (mem or {}).get(li[1], 0)
                for p in li[2]:
                    pd, pm = dv.get(p, z)
                    m |= pd | pm
            elif li[0] == "a":
                d = m = 0
                for p in li[1]:
                    pd, pm = dv.get(p, z)
                    d ^= pd
                    m |= pm
            else:
                m = 0
                for p in li[1]:
                    pd, pm = dv.get(p, z)
                    m |= pd | pm
                d = 0
            if inj is not None and n in inj:
                d ^= inj[n]
            if d or m:
                dv[n] = (d, m)
        return dv

    def _dclock(self, dv: dict, qd: dict) -> dict:
        z = (0, 0)
        nq = {}
        for f in self.regs:
            li = self.flin[f]
            if li[0] == "a":
                d = m = 0
                for p in li[1]:
                    pd, pm = dv.get(p, z)
                    d ^= pd
                    m |= pm
            elif li[0] == "h":
                d, m = qd.get(f, z)
            elif li[0] == "c":
                d = m = 0
            else:
                od, om = qd.get(f, z)
                m = od | om
                for p in li[1]:
                    pd, pm = dv.get(p, z)
                    m |= pd | pm
                d = 0
            if d or m:
                nq[f] = (d, m)
        return nq


# ------------------------------------------------------------------ feasibility over GF(2)

_PAT: dict = {}


def _patterns(L: int) -> list[int]:
    """Truth-table patterns of L inputs: bit a of P[j] = bit j of a."""
    p = _PAT.get(L)
    if p is None:
        n = 1 << L
        p = []
        for j in range(L):
            blk = ((1 << (1 << j)) - 1) << (1 << j)     # a run of 2^j ones after 2^j zeros
            per = 1 << (j + 1)
            w = 0
            for s in range(0, n, per):
                w |= blk << s
            p.append(w)
        _PAT[L] = p
    return p


def _forall(D: int, j: int, P: list) -> int:
    s = 1 << j
    both = (D & ~P[j]) & ((D & P[j]) >> s)
    return both | (both << s)


def _support(D: int, L: int, P: list) -> list[int]:
    out = []
    for j in range(L):
        s = 1 << j
        if (D & ~P[j]) != ((D & P[j]) >> s):
            out.append(j)
    return out


def feasible(D: int, L: int, funcs: list, xmask: int) -> bool:
    """Some assignment of the data inputs that the affine functions funcs (None: free) can
    take makes D true; X inputs (a function with X variables) must work for both values."""
    P = _patterns(L)
    for j in range(L):
        f = funcs[j]
        if f is not None and f & xmask:
            D = _forall(D, j, P)
            if not D:
                return False
    if not D:
        return False
    S = [j for j in _support(D, L, P) if funcs[j] is not None]
    free = [j for j in _support(D, L, P) if funcs[j] is None]
    # linear dependencies among the functions of S: each gives a parity constraint
    piv: dict = {}      # pivot bit -> (row, combination over S positions, constant parity)
    cons = []
    for i, j in enumerate(S):
        row, comb, par = funcs[j] >> 1, 1 << i, funcs[j] & 1
        while row:
            h = row.bit_length() - 1
            if h not in piv:
                piv[h] = (row, comb, par)
                break
            r2, c2, p2 = piv[h]
            row ^= r2
            comb ^= c2
            par ^= p2
        else:
            cons.append((comb, par))
    if not cons:
        return True     # every assignment of S can be taken; D (nonzero over S, rest free) holds somewhere
    n = len(S)
    for a in range(1 << n):
        if any((bin(a & c).count("1") & 1) != p for c, p in cons):
            continue
        idx = 0
        for i, j in enumerate(S):
            if (a >> i) & 1:
                idx |= 1 << j
        # free inputs (no model function): any value
        for fa in range(1 << len(free)):
            k = idx
            for i, j in enumerate(free):
                if (fa >> i) & 1:
                    k |= 1 << j
            if (D >> k) & 1:
                return True
    return False


# ------------------------------------------------------------------ gate-level mux trees

CB = re.compile(r"^Tile_X(\d+)Y(\d+)_\w+?\.(?:Tile_X(\d+)Y(\d+)_\w+?\.)?ConfigBits\[(\d+)\]$")
MAX_CELLS = 400


def _internal(name: str) -> bool:
    return bool(re.search(r"\.n\d+$", name)) or "cus_mux" in name or "$" in name


class MuxTrees:
    """Gate-level trees of the switch-matrix muxes of a flat netlist (fastsim.Netlist)."""

    def __init__(self, nl, arr):
        from fabtest.patterns.config import pack as pk
        self.nl, self.arr = nl, arr
        self.mux_of = pk.mux_nets(nl)
        self.bit_of = {v: b for b, v in self.mux_of.items()}
        names = defaultdict(list)
        for n, b in nl.net_bit.items():
            names[int(b)].append(n)
        # boundary nets (a tree's data inputs): a name outside the mux trees' own names
        self.boundary = {b for b, ns in names.items() if not all(_internal(n) for n in ns)}
        self.latch_key = {}
        for n, b in nl.net_bit.items():
            m = CB.match(n)
            if m:
                X, Y, dx, dy, k = m.groups()
                t = (int(X) + int(dx) - 1, int(Y) + int(dy)) if dx else (int(X) - 1, int(Y))
                self.latch_key[int(b)] = (t, int(k))
        self.const = {int(b): int(v) for b, v in zip(nl.const_net, nl.const_val)}
        self._trees: dict = {}
        self._good: dict = {}
        self._find_roots()

    def _find_roots(self) -> None:
        """Muxes whose output net has no switch-matrix name (jump wires, BEL inputs: named by
        their readers only): forward from the nets named inside the mux instance to the first
        boundary net (the tree's code check in _build confirms it)."""
        nl, arr = self.nl, self.arr
        inst = defaultdict(list)
        pat = re.compile(r"^Tile_X(\d+)Y(\d+)_\w+?\.Inst_\w+_switch_matrix\.inst_cus_mux\d+(?:_buf)?_(\w+)\.")
        for n, b in nl.net_bit.items():
            m = pat.match(n)
            if m:
                inst[(int(m.group(1)) - 1, int(m.group(2)), m.group(3))].append(int(b))
        have = set(self.bit_of)
        for t in arr.tiles:
            try:
                muxes = arr.MT(t)
            except Exception:
                continue
            for o, m in muxes.items():
                if m.model == "direct" or (t, o) in have:
                    continue
                seeds = inst.get((t[0], t[1], o))
                if not seeds:
                    continue
                seen, front, root = set(seeds), list(seeds), None
                for _ in range(64):
                    nxt = []
                    for b in front:
                        for c in nl.rd_idx[nl.rd_off[b]:nl.rd_off[b + 1]]:
                            if int(nl.type[c]) in (LATCH, DFF):
                                continue
                            o2 = int(nl.out[c])
                            if o2 not in seen:
                                seen.add(o2)
                                nxt.append(o2)
                    hits = [b for b in nxt if b in self.boundary or b in self.mux_of]
                    if hits:
                        root = hits[0] if len(hits) == 1 else None
                        break
                    front = nxt
                    if not front:
                        break
                if root is not None and root not in self.mux_of:
                    self.mux_of[root] = (t, o)
                    self.bit_of[(t, o)] = root

    def tree(self, i):
        """(tile, mux) -> dict(cells, data, latch, nets, leaf_net, L) or None (no tree found,
        a direct mux or inputs not matched)."""
        if i in self._trees:
            return self._trees[i]
        self._trees[i] = tr = self._build(i)
        return tr

    def _build(self, i):
        nl, arr = self.nl, self.arr
        root = self.bit_of.get(i)
        m = arr.MT(i[0]).get(i[1]) if root is not None else None
        if m is None or m.model == "direct":
            return None
        cells, data, latch = [], [], []
        seen, stack = {root}, [root]
        while stack:
            b = stack.pop()
            if b in self.const:
                continue
            c = nl.driver.get(b)
            if c is None:
                data.append(b)
                continue
            ty = int(nl.type[c])
            if ty == LATCH:
                latch.append(b)
                continue
            if ty == DFF or (b != root and (b in self.mux_of or b in self.boundary)):
                data.append(b)
                continue
            cells.append(c)
            if len(cells) > MAX_CELLS:
                return None
            for p in (int(nl.a[c]), int(nl.b[c])) if ty in (0, 1) else (int(nl.a[c]),):
                if p >= 0 and p not in seen:
                    seen.add(p)
                    stack.append(p)
        cells = self._topo(cells)
        try:
            code_bits = [m.code_for(k) for k in range(m.fan_in)]
        except Exception:
            return None
        lk = []
        for b in latch:
            key = self.latch_key.get(b)
            if key is None or key[0] != i[0]:
                return None
            lk.append(key[1])
        tr = {"root": root, "cells": cells, "data": data, "latch": list(zip(latch, lk)),
              "L": len(data), "code_bits": code_bits, "m": m}
        if tr["L"] > 16:
            return None
        # data inputs -> model nets: code k with data input j one-hot
        L = tr["L"]
        full = (1 << L) - 1
        leaf_net: list = [None] * L
        for k in range(m.fan_in):
            s = m.inputs[k]
            out = self._eval(tr, k, [1 << j for j in range(L)], full)
            if s.kind == "const":
                want = 0 if s.port.startswith("GND") else full
                if out != want:
                    return None
                continue
            if out == 0 or out & (out - 1):
                return None
            j = out.bit_length() - 1
            net = arr.canon(i[0], s.net)
            if leaf_net[j] is not None and leaf_net[j] != net:
                return None
            leaf_net[j] = net
        tr["leaf_net"] = leaf_net
        tr["nets"] = {root, *data, *(b for b, _ in tr["latch"])} | {int(nl.out[c]) for c in cells}
        return tr

    def _topo(self, cells: list) -> list:
        """The cone's cells, each after the cells driving its inputs."""
        nl = self.nl
        by_out = {int(nl.out[c]): c for c in cells}
        done, order = set(), []
        for c0 in cells:
            stack = [(c0, False)]
            while stack:
                c, ready = stack.pop()
                if c in done:
                    continue
                if ready:
                    done.add(c)
                    order.append(c)
                    continue
                stack.append((c, True))
                ty = int(nl.type[c])
                for p in (int(nl.a[c]), int(nl.b[c])) if ty in (0, 1) else (int(nl.a[c]),):
                    d = by_out.get(p)
                    if d is not None and d not in done:
                        stack.append((d, False))
        return order

    def _eval(self, tr, k, leafvals, full, forced=None) -> int:
        nl = self.nl
        cb = tr["code_bits"][k]
        v = {}
        for j, b in enumerate(tr["data"]):
            v[b] = leafvals[j]
        for b, idx in tr["latch"]:
            v[b] = full if cb.get(idx, 0) else 0
        for b, cv in self.const.items():
            v[b] = full if cv else 0
        if forced is not None and forced[0] in v:
            v[forced[0]] = full if forced[1] else 0
        for c in tr["cells"]:
            ty = int(nl.type[c])
            a = v.get(int(nl.a[c]), 0)
            if ty == 0:
                r = a & v.get(int(nl.b[c]), 0)
            elif ty == 1:
                r = a | v.get(int(nl.b[c]), 0)
            elif ty == 2:
                r = ~a & full
            else:
                r = a
            o = int(nl.out[c])
            if forced is not None and o == forced[0]:
                r = full if forced[1] else 0
            v[o] = r
        return v[tr["root"]]

    def effect(self, i, k: int, bit: int, pol: int) -> int:
        """Truth table over the data inputs: the stuck-at fault (bit, pol) changes the mux
        output under code k."""
        tr = self.tree(i)
        L = tr["L"]
        P = _patterns(L)
        full = (1 << (1 << L)) - 1
        g = self._good.get((i, k))
        if g is None:
            g = self._good[(i, k)] = self._eval(tr, k, P, full)
        return g ^ self._eval(tr, k, P, full, (bit, pol))


# ------------------------------------------------------------------ diagnostic (fabtest linear)

OBS_TRY = 16    # time points tried per (fault, config)
ORDER = ["possible", "act", "obs_x", "obs_path", "code"]
WHY = {"possible": "possible in some config (vectors / observation timing / X at the inputs)",
       "act": "impossible: observable, but the data inputs cannot take the values (equal / constant)",
       "obs_x": "impossible: the outputs it reaches are X at those samples",
       "obs_path": "impossible: no path to an observed output",
       "code": "impossible: no effect at the mux output under any config's code"}


class _Cfg:
    """One config of a run for the diagnostic: Sim, impulse responses, gate-level output
    definedness per vector."""

    def __init__(self, ff, nl, data, c, lanes, gate_obs=frozenset()):
        from fabtest.sim import stimuli
        from fabtest.sim.fastsim import fastsim
        sels, _order, luts = stimuli.config_setup(ff, data, c)
        self.sels = sels
        self.gate_obs = gate_obs    # lane nets whose stuck-at the gate-level run detects
        self.sim = Sim(ff, sels, luts)
        self.lane = {n: i for i, n in enumerate(lanes)}
        self.resp = self.sim.impulse(lanes)
        st = stimuli.build_stim(ff, data, c)
        ports = [ff.port(n) for n in ff.border_out]
        if list(st.out_names) != ports:
            raise SystemExit("fabtest linear: stimulus outputs not in border_out order")
        nv = self.sim.nv
        n_cfg = len(st.rows) - 3 * nv
        smp = [t for t in range(nv) if st.sample[n_cfg + 3 * t + 2]]
        gold = fastsim.outputs(nl, st)
        self.defined = [[False] * nv for _ in ff.border_out]
        dmask = [0] * len(ff.border_out)     # bit t: output defined at the sample of vector t
        for row, t in zip(gold, smp):
            for o, ch in enumerate(row):
                if ch != "x":
                    self.defined[o][t] = True
                    dmask[o] |= 1 << t
        # per lane and phase: bit t set when a flip at vector t reaches a defined output
        self.hit = defaultdict(lambda: [0, 0])
        self.path = set()
        for ph in (PRE, POST):
            for o, dd in enumerate(self.resp[ph]):
                for d, (a, b) in dd.items():
                    w = a | b
                    sh = dmask[o] >> d
                    while w:
                        low = w & -w
                        i = low.bit_length() - 1
                        self.path.add(i)
                        self.hit[i][ph] |= sh
                        w ^= low
        self._obs: dict = {}

    def obs_times(self, net) -> tuple[list, bool]:
        """(t, phase), latest first, where a flip of net reaches an output defined at that
        sample (at most OBS_TRY of them: the latest, and some spread over the rest; the
        functions repeat once the chains are defined); and whether it reaches any output."""
        r = self._obs.get(net)
        if r is not None:
            return r
        i = self.lane[net]
        h = self.hit.get(i, (0, 0))
        nv = self.sim.nv
        if net in self.gate_obs and not (h[0] or h[1]):   # observable in the gates, no model path
            h = ((1 << nv) - 1, (1 << nv) - 1)
        times = [(t, ph) for t in range(nv - 1, -1, -1) for ph in (PRE, POST) if (h[ph] >> t) & 1]
        if len(times) > OBS_TRY:
            k = OBS_TRY // 2
            rest = times[k:]
            step = max(1, len(rest) // (OBS_TRY - k))
            times = times[:k] + rest[::step][:OBS_TRY - k]
        self._obs[net] = r = (times, i in self.path or net in self.gate_obs)
        return r


def classify(trees: MuxTrees, cfg: _Cfg, cands: list, bit: int, pol: int, net=None) -> str:
    """Best status of a fault in one config: ok / act / obs_x / obs_path / code. cands: the
    muxes whose tree holds the fault net; net: its model net when it has one (a mux output
    or a tree's data input): the stuck value itself, wherever the net is read."""
    best = "code"
    if net is not None and net in cfg.lane:
        times, path = cfg.obs_times(net)
        if not times:
            best = _better(best, "obs_x" if path else "obs_path")
        for tt, ph in times:
            f = cfg.sim.at(tt, ph).get(net)
            if f is None or f & cfg.sim.xmask:
                continue
            if f >> 1 or (f & 1) != pol:
                return "ok"
        if times:
            best = _better(best, "act")
    for i in cands:
        tr = trees.tree(i)
        t, o = i
        k = cfg.sels[t].get(o, 0)
        D = trees.effect(i, k, bit, pol)
        if not D:
            continue
        mnet = cfg.sim.arr.canon(t, tr["m"].net)
        if mnet not in cfg.lane:
            continue
        times, path = cfg.obs_times(mnet)
        if not times:
            best = _better(best, "obs_x" if path else "obs_path")
            continue
        for tt, ph in times:
            vals = cfg.sim.at(tt, ph)
            funcs = [vals.get(n) if n is not None else None for n in tr["leaf_net"]]
            if feasible(D, tr["L"], funcs, cfg.sim.xmask):
                return "ok"
        best = _better(best, "act")
    return best


def _better(a: str, b: str) -> str:
    rank = {"ok": 0, "act": 1, "obs_x": 2, "obs_path": 3, "code": 4}
    return a if rank[a] <= rank[b] else b


def diagnose(spec, work: Path, run: Path, n_sound: int = 2000, seed: int = 5, jobs: int = 3) -> str:
    """fabtest linear: the diagnostic of a run's undetected faults (and the soundness check
    on detected (fault, config) pairs); writes <run>/linear.json, returns the report."""
    import time
    from fabtest import pipeline
    from fabtest.sim.fastsim import fastsim
    t0 = time.time()
    ff = pipeline.model(spec)
    data = json.loads((run / "configs.json").read_text())
    fdir = run / "faultsim"
    rows = [r for p in sorted(fdir.glob("chunk_*.csv")) for r in csv.reader(p.open())]
    head = rows[0]
    rows = [r for r in rows if r[0] != "id"]
    cfg_names = head[3:]
    cidx = [int(n[2:]) for n in cfg_names]
    nets = {}
    with (work / "synth" / "faults.csv").open() as fh:
        for r in csv.DictReader(fh):
            nets[r["id"]] = r["net"]
    nl = fastsim.Netlist("fabtest", work / "synth" / "netlist.json")
    trees = MuxTrees(nl, ff)
    # every mux tree: the muxes whose tree holds a net, the model net of a net (mux outputs,
    # data inputs)
    index: dict = defaultdict(list)
    model_net: dict = {}
    for i in sorted(trees.bit_of):
        m = ff.MT(i[0]).get(i[1])
        tr = trees.tree(i) if m is not None else None
        if tr is None:
            continue
        for b in tr["nets"]:
            index[b].append(i)
        model_net[tr["root"]] = ff.canon(i[0], m.net)
        for b, n in zip(tr["data"], tr["leaf_net"]):
            if n is not None:
                model_net.setdefault(b, n)
    cand, fnet, fbit = {}, {}, {}
    for r in rows:
        b = fbit[r[0]] = int(nl.net_bit[nets[r[0]]])
        cand[r[0]] = index.get(b, [])
        fnet[r[0]] = model_net.get(b)
    mapped = {k for k in cand if cand[k] or fnet[k]}
    und = [r for r in rows if "D" not in r[3:]]
    det_pairs = [(r, j) for r in rows if r[0] in mapped for j, s in enumerate(r[3:]) if s == "D"]
    random.Random(seed).shuffle(det_pairs)
    det_pairs = det_pairs[:n_sound]
    lanes = sorted({ff.canon(i[0], trees.tree(i)["m"].net) for c in cand.values() for i in c}
                   | {n for n in fnet.values() if n is not None})
    print(f"fabtest linear: {len(rows)} faults, {len(und)} undetected ({sum(r[0] in mapped for r in und)} "
          f"in mux trees), {len(lanes)} nets; {len(cidx)} configs", flush=True)
    # gate level: stuck-at 0 / 1 on every lane net per config. A net whose stuck-at is
    # detected is observable in that config, whatever the model's paths say (the model has
    # no path through a memory's state, a term tile's X wires, ...)
    from fabtest.sim import stimuli
    bit_of_net: dict = {}
    for b, n in model_net.items():
        bit_of_net.setdefault(n, b)
    name_of: dict = {}
    for nm, b in nl.net_bit.items():
        name_of.setdefault(int(b), nm)
    gl = [n for n in lanes if n in bit_of_net]
    gfaults = [{"net": name_of[bit_of_net[n]], "polarity": p} for n in gl for p in (0, 1)]
    stims = stimuli.build_stims(ff, run / "configs.json", only=cidx, quiet=True)
    gnames, gstat = fastsim.run_faults("fabtest", stims, gfaults, jobs, nl=nl, quiet=True,
                                       limit_factor=pipeline.LIMIT_FACTOR)
    gobs = {(gl[k // 2], c) for k, st in enumerate(gstat) for c, s in st.items() if s == "D"}
    print(f"  gate-level observability of {len(gl)} nets: {time.time() - t0:.0f} s", flush=True)
    per_fault: dict = defaultdict(dict)
    sound_bad = []
    xslots = [0, 0]
    for j, c in enumerate(cidx):
        cfg = _Cfg(ff, nl, data, c, lanes, {n for n, cn in gobs if cn == cfg_names[j]})
        for o, n in enumerate(ff.border_out):
            if n.split("/", 1)[1] in ("A_I_top", "B_I_top"):
                xslots[0] += sum(not x for x in cfg.defined[o])
                xslots[1] += len(cfg.defined[o])
        for r in und:
            if r[0] in mapped:
                per_fault[r[0]][j] = classify(trees, cfg, cand[r[0]], fbit[r[0]], int(r[2]), fnet[r[0]])
        for r, jj in det_pairs:
            if jj == j:
                st = classify(trees, cfg, cand[r[0]], fbit[r[0]], int(r[2]), fnet[r[0]])
                if st != "ok":
                    sound_bad.append((r[1], r[2], cfg_names[j], st))
        print(f"  {cfg_names[j]}: {time.time() - t0:.0f} s", flush=True)
    summary = Counter()
    nposs = []
    for r in und:
        if r[0] not in mapped:
            summary["unmapped"] += 1
            continue
        sts = per_fault[r[0]].values()
        if "ok" in sts:
            summary["possible"] += 1
            nposs.append(sum(s == "ok" for s in sts))
        else:
            summary[min(sts, key=lambda s: ORDER.index(s))] += 1
    out = {"faults": len(rows), "undetected": len(und), "summary": dict(summary),
           "sound": {"pairs": len(det_pairs), "bad": sound_bad[:50], "n_bad": len(sound_bad)},
           "x_slots": xslots, "per_fault": {k: v for k, v in per_fault.items()}}
    (run / "linear.json").write_text(json.dumps(out, indent=0))
    lines = [f"fabtest linear: {run.name}, {len(und)} undetected of {len(rows)} sampled faults "
             f"({time.time() - t0:.0f} s)"]
    for k in ORDER:
        if summary[k]:
            lines.append(f"  {summary[k]:6d}  {WHY[k]}")
    lines.append(f"  {summary['unmapped']:6d}  not in a switch-matrix mux tree (LUT, flop, config logic, ...)")
    if nposs:
        nposs.sort()
        lines.append(f"  possible faults: in a median of {nposs[len(nposs) // 2]} of {len(cidx)} configs")
    lines.append(f"  soundness: {len(sound_bad)} of {len(det_pairs)} detected (fault, config) pairs "
                 f"called impossible in the config that detects them")
    if xslots[1]:
        lines.append(f"  chain-end pads (A_I_top / B_I_top): {100 * xslots[0] / xslots[1]:.1f}% of the "
                     f"(pad, vector) samples X in the fault-free gate run")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ router hook (fab_router.LINEAR)

DMAX = 128   # delays kept per unknown term (a registered LUT stage: one more)


class Funcs:
    """Functions of nets in a config under construction (fab_router Cfg / NativeCfg): a value
    is (constant, pad terms, unknown terms); pad j at delay d: bit d * P + j; an unknown term
    (a mux not selected yet, a non-linear BEL, an undriven net) u at delay d: bit u * DMAX +
    d. Registered XOR / XNOR LUTs (by the config's parity) add one delay; constant LUTs are
    constants; the NOT helper LUT (table 0x5555) is combinational."""

    def __init__(self, cfg):
        from fabtest.patterns.config import fab_router as fr
        self.cfg, self.g, self.fr = cfg, cfg.g, fr
        arr = cfg.g.arr
        self.P = len(arr.border_in)
        self.pad = {n: j for j, n in enumerate(arr.border_in)}
        self.xnor = getattr(cfg, "bias", 0) % 2
        self.cval = {out: v for v in (0, 1) for _t, out in getattr(cfg, "consts", {}).get(v, ())}
        self.helper = {}
        for t, d in getattr(cfg, "lut_const", {}).items():
            for x, val in d.items():
                if isinstance(val, dict):
                    self.helper[arr.canon(t, f"L{x}_O")] = (t, x, val["tt"])
        self.lut_out = {arr.canon(t, f"L{x}_O") for t in arr.lut_tiles for x in "ABCDEFGH"}
        self.fpc = fr._fixed_pred_cost(cfg.g)
        self.lutcut = cfg.__dict__.get("_lutcut", ())
        self.uid: dict = {}
        self.memo: dict = {}

    def _u(self, n):
        u = self.uid.get(n)
        if u is None:
            u = self.uid[n] = len(self.uid)
        return (0, 0, 1 << (u * DMAX))

    def _shift(self, v):
        c, pm, um = v
        um2 = 0
        while um:
            low = um & -um
            if (low.bit_length() - 1) % DMAX != DMAX - 1:
                um2 |= low << 1
            um ^= low
        return (c, pm << self.P, um2)

    def f(self, n):
        """The function of net n (iterative: chains are long)."""
        memo = self.memo
        if n in memo:
            return memo[n]
        stack = [(n, False)]
        onstack = set()
        while stack:
            x, ready = stack.pop()
            if x in memo:
                continue
            deps, how = self._deps(x)
            if not ready:
                todo = [d for d in deps if d not in memo]
                if todo:
                    if x in onstack:      # a loop (not in a loop-free config): unknown
                        memo[x] = self._u(x)
                        continue
                    onstack.add(x)
                    stack.append((x, True))
                    stack.extend((d, False) for d in todo if d not in onstack)
                    continue
            memo[x] = self._combine(x, deps, how)
        return memo[n]

    def _deps(self, n):
        """(inputs, how): how is "pad", ("const", v), "u", "copy", "not" or "lut"."""
        g = self.g
        if n in self.pad:
            return [], "pad"
        if n == "GND":
            return [], ("const", 0)
        if n in self.cval:
            return [], ("const", self.cval[n])
        h = self.helper.get(n)
        if h is not None:
            t, x, tt = h
            return ([g.arr.canon(t, f"L{x}_I0")], "not") if tt == 0x5555 else ([], "u")
        i = g.mux_of.get(n)
        if i is not None and self.fr.MM(i).model != "direct":
            k = self.cfg.sel.get(i)
            if k is None:
                return [], "u"
            s = g.src(i, k)
            if s is None:
                return [], ("const", 0 if self.fr.MM(i).inputs[k].port.startswith("GND") else 1)
            return [s], "copy"
        if n in self.lutcut:
            return [], "u"
        preds = self.fpc.get(n, ())
        if not preds:
            return [], "u"
        if n in self.lut_out and all(c == 2.0 for _a, c in preds):
            return [a for a, _c in preds], "lut"
        if len(preds) == 1 and preds[0][1] == 0.0:
            return [preds[0][0]], "copy"
        return [], "u"

    def _combine(self, n, deps, how):
        if how == "pad":
            return (0, 1 << self.pad[n], 0)
        if how == "u":
            return self._u(n)
        if isinstance(how, tuple):
            return (how[1], 0, 0)
        if how == "copy":
            return self.memo.get(deps[0]) or self._u(n)
        if how == "not":
            c, pm, um = self.memo[deps[0]]
            return (c ^ 1, pm, um)
        c, pm, um = self.xnor, 0, 0
        for d in deps:
            c2, p2, u2 = self.memo[d]
            c, pm, um = c ^ c2, pm ^ p2, um ^ u2
        return self._shift((c, pm, um))


def differs(a, b) -> bool:
    """Can a and b take opposite values in some vector (unknown terms: assume so)?"""
    return bool((a[0] ^ b[0]) or (a[1] ^ b[1]) or (a[2] ^ b[2]))


def known(a) -> bool:
    return not a[2]


def constant(a) -> bool:
    return not a[1] and not a[2]


def useful(cfg, i, k, open_work) -> bool:
    """Code k of mux i can show one of its open items in this config (functions so far;
    unknown parts count as varying): t0 / t1 a selected input that can take that value, select
    bit b an input of code k ^ 2^b whose function differs from the selected one's."""
    from fabtest.patterns.config import fab_router as fr
    items = open_work.get((*i, k)) or ()
    g = cfg.g
    m = fr.MM(i)
    F = Funcs(cfg)

    def fn(j):
        s = g.src(i, j)
        if s is None:
            return (0 if m.inputs[j].port.startswith("GND") else 1, 0, 0)
        return F.f(s)
    fs = fn(k)
    for it in items:
        if it in ("t0", "t1"):
            if not constant(fs) or fs[0] == (0 if it == "t0" else 1):
                return True
        else:
            k2 = k ^ (1 << it)
            if k2 < m.fan_in and not g.forbidden(i, k2) and differs(fs, fn(k2)):
                return True
    fr.BIDIR_STATS["lin code skipped"] += 1
    return False


def drive_pair(cfg, i, k, s) -> None:
    """fab_router._drive_pair with functions instead of signal names: input k (source s)
    driven by a pad signal, then one other input (rotated by the config) whose function
    differs from s's: one already there, else routed back to a pad signal; when its function
    then equals s's, the next input is tried too (routes are not undone: the router's
    observation marks would go stale)."""
    from fabtest.patterns.config import fab_router as fr
    g = cfg.g
    ctrl = fr._ctrl(cfg)
    root = cfg.__dict__["_root"]
    fan = fr.MM(i).fan_in
    others = [g.src(i, j) for j in range(fan) if j != k and not g.forbidden(i, j)]
    others = [x for x in others if x is not None and x != s]
    if not fr.drive_input(cfg, s, avoid={root[x] for x in others if x in ctrl}):
        return
    fs = Funcs(cfg).f(s)
    order = [(k + 1 + getattr(cfg, "bias", 0) + j) % fan for j in range(fan)]
    for j in order:
        if j == k or g.forbidden(i, j):
            continue
        x = g.src(i, j)
        if x is None or x == s:
            continue
        fx = Funcs(cfg).f(x)
        if known(fx) and differs(fs, fx) and not (constant(fs) and constant(fx)):
            fr.BIDIR_STATS["lin pair already"] += 1
            return
        if x in ctrl:
            continue    # driven, but the same function (or not known yet): another input
        if fr.drive_input(cfg, x, avoid={root.get(s)}):
            F = Funcs(cfg)
            fx, fs = F.f(x), F.f(s)
            if differs(fs, fx) and not (constant(fs) and constant(fx)):
                fr.BIDIR_STATS["lin pair routed"] += 1
                return
            fr.BIDIR_STATS["lin pair same"] += 1
    fr.BIDIR_STATS["lin pair failed"] += 1
