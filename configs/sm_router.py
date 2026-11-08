#!/usr/bin/env python3
"""PIP test router for the LUT4AB switch matrix (Phase 9 step 1).

Builds switch-matrix configs so that every mux code is selected while the
mux output is observed at a tile output, and the selected input takes both
values and differs from every input one select bit away (a stuck select
bit then picks a different value). Same frame of reference as the baseline
(configs/sm_configs.py): LUTs XOR, combinational, all tile inputs driven
with unique codes, every tile output observed, 2 W samples per config.

Targets: every mux code and every direct wire (code 0, no select bits).
Per config, greedy, over the targets still open:
  1. for each mux with open work (history first, PathFinder-style: how often
     an open code of the mux was selected but not shown in earlier configs;
     then the most open work): select an open code, then route the mux output
     forward to a net already observed (a tile output or a path routed
     earlier in this config), Dijkstra over the free muxes. A mux already
     set in this config is usable only on the input it selects (cost 0); a
     free mux costs 1, less when its code on the path is open itself. BEL
     edges: LUT input -> O (XOR, always sensitised), Ci / I1 / I2 -> Co and
     MUX8LUT inputs -> M_* (conditional, higher cost).
  2. every edge is added only if it closes no combinational loop.
  3. muxes left free get an open code, else any code that closes no loop.
The config is then graded exactly with the bit-parallel model (single-net
flip through the model, so reconvergent XOR masking counts as not
observed), after a hill-climb polish (single mux moves, gain weighted by
1 + history); what it really tested is removed from the open targets.
Repeat until nothing open is left or configs stop adding anything. Then
compaction: the config with the smallest unique contribution is dropped
when its items can be moved, code by code, into other configs.

Not reachable by combinational configs (reported, not targets): muxes
whose output only drives LUT SR / EN (flops not used) and the jump wires
that only feed those.

Run: python3 configs/sm_router.py [--max N] [--compact-only]   writes configs/sm_router.json
(about 8 min)
"""
from __future__ import annotations

import argparse
import heapq
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "configs"), str(ROOT / "sim")]
import sm_configs as sc  # noqa: E402

M = sc.MUXES
REAL = [o for o, m in M.items() if m.model != "direct"]
ALL = list(M)   # targets: real muxes and direct wires (code 0, no select bits)
NS = 2 * sc.W
FULL = (1 << NS) - 1
OUT_JSON = ROOT / "configs" / "sm_router.json"

MUX_OF = {m.net: o for o, m in M.items()}
BELDEPS: dict[str, list[str]] = {}
for _x in sc.LUTS:
    BELDEPS.update(sc.lut_deps(_x))
BELDEPS.update(sc.mux8_deps(0, 0))
DIRECT = {m.net: m.inputs[0].net for m in M.values() if m.model == "direct"}
OUT_NETS = set(sc.OUTPUTS)

# readers: net -> [(mux, code)] of the real muxes with that net as input
READERS: dict[str, list[tuple[str, int]]] = defaultdict(list)
for _o in REAL:
    for _k, _s in enumerate(M[_o].inputs):
        if _s.kind != "const":
            READERS[_s.net].append((_o, _k))
# fixed forward edges: net -> [(net, cost)]
FIXED: dict[str, list[tuple[str, float]]] = defaultdict(list)
for _n, _i in DIRECT.items():
    FIXED[_i].append((_n, 0.0))
for _out, _ins in BELDEPS.items():
    for _i in _ins:
        FIXED[_i].append((_out, 2.0 if _out.endswith("_O") else 4.0))


def _const(s) -> int:
    return 0 if s.port.startswith("GND") else FULL


def _input_vectors() -> dict[str, int]:
    seq = sc.SMSeq("v", "")
    seq.steps.append(sc.SStep())
    seq.selects.append({})
    seq.vectors()
    samples = [s for s in seq.steps if s.sample]
    return {p: sum(((s.inp >> j) & 1) << t for t, s in enumerate(samples))
            for j, p in enumerate(sc.INPUTS)}


INVEC = _input_vectors()


# ---------------------------------------------------------------- targets

def work_items(o: str, k: int) -> set:
    """What code k of mux o has to show: 't0', 't1' (observed with the
    input at 0 / 1) and select bit i (observed with A_k != A_(k ^ 2^i)).
    Items no config can show (constant inputs) are left out."""
    m = M[o]
    s = m.inputs[k]
    items = set()
    if s.kind != "const" or _const(s) == 0:
        items.add("t0")
    if s.kind != "const" or _const(s) == FULL:
        items.add("t1")
    for i in range(len(m.sel)):
        k2 = k ^ (1 << i)
        if k2 >= m.fan_in:
            continue
        s2 = m.inputs[k2]
        if s.kind == "const" and s2.kind == "const" and _const(s) == _const(s2):
            continue
        items.add(i)
    return items


def reachable_muxes() -> set[str]:
    """Muxes whose output can reach a tile output for some selection."""
    seen = set(OUT_NETS)
    stack = list(OUT_NETS)
    while stack:
        n = stack.pop()
        preds = []
        if n in DIRECT:
            preds.append(DIRECT[n])
        if n in BELDEPS:
            preds += BELDEPS[n]
        if n in MUX_OF and M[MUX_OF[n]].model != "direct":
            preds += [s.net for s in M[MUX_OF[n]].inputs if s.kind != "const"]
        for p in preds:
            if p not in seen:
                seen.add(p)
                stack.append(p)
    return {o for o in ALL if M[o].net in seen}


# ---------------------------------------------------------------- one config

class Cfg:
    def __init__(self):
        self.sel: dict[str, int] = {}
        self.obs: set[str] = set(OUT_NETS)
        for n in list(OUT_NETS):
            self._mark(n)

    def succ(self, n: str):
        for t, _c in FIXED.get(n, ()):
            yield t
        for o, k in READERS.get(n, ()):
            if self.sel.get(o) == k:
                yield M[o].net

    def preds(self, n: str) -> list[str]:
        if n in DIRECT:
            return [DIRECT[n]]
        if n in BELDEPS:
            return BELDEPS[n]
        o = MUX_OF.get(n)
        if o is not None and o in self.sel:
            s = M[o].inputs[self.sel[o]]
            return [] if s.kind == "const" else [s.net]
        return []

    def reaches(self, a: str, b: str) -> bool:
        seen, stack = {a}, [a]
        while stack:
            n = stack.pop()
            if n == b:
                return True
            for t in self.succ(n):
                if t not in seen:
                    seen.add(t)
                    stack.append(t)
        return False

    def _mark(self, n: str) -> None:
        stack = [n]
        while stack:
            x = stack.pop()
            for p in self.preds(x):
                if p not in self.obs:
                    self.obs.add(p)
                    stack.append(p)

    def assign(self, o: str, k: int) -> bool:
        if o in self.sel:
            return self.sel[o] == k
        s = M[o].inputs[k]
        if s.kind != "const" and self.reaches(M[o].net, s.net):
            return False
        self.sel[o] = k
        if M[o].net in self.obs:
            self._mark(M[o].net)
        return True

    def path_muxes(self, a: str, b: str) -> list[str]:
        """Real muxes set on some path a -> b (empty if none)."""
        prev, stack = {a: None}, [a]
        while stack:
            n = stack.pop()
            if n == b:
                out = []
                while n is not None:
                    o = MUX_OF.get(n)
                    if o is not None and o in self.sel and M[o].model != "direct":
                        out.append(o)
                    n = prev[n]
                return out
            for t in self.succ(n):
                if t not in prev:
                    prev[t] = n
                    stack.append(t)
        return []

    def unassign(self, o: str) -> None:
        del self.sel[o]   # obs is only an estimate; rebuilt by the exact grading

    def route(self, start: str, open_work: dict, banned: set) -> list[tuple[str, int]] | None:
        """Cheapest path from net `start` to an observed net: the mux codes to set."""
        dist = {start: 0.0}
        prev: dict[str, tuple[str, tuple[str, int] | None]] = {}
        pq = [(0.0, start)]
        while pq:
            d, n = heapq.heappop(pq)
            if d > dist.get(n, 1e18):
                continue
            if n != start and n in self.obs:
                path = []
                while n != start:
                    p, step = prev[n]
                    if step:
                        path.append(step)
                    n = p
                return path[::-1]
            cand = [(t, c, None) for t, c in FIXED.get(n, ())]
            for o, k in READERS.get(n, ()):
                if (o, k) in banned:
                    continue
                if o in self.sel:
                    if self.sel[o] == k:
                        cand.append((M[o].net, 0.0, None))
                else:
                    if open_work.get((o, k)):
                        c = 0.4
                    elif self.mux_open(o, open_work):
                        c = 4.0   # would spend a select another open code of o needs
                    else:
                        c = 1.0
                    cand.append((M[o].net, c, (o, k)))
            for t, c, step in cand:
                nd = d + c
                if nd < dist.get(t, 1e18):
                    dist[t] = nd
                    prev[t] = (n, step)
                    heapq.heappush(pq, (nd, t))
        return None

    @staticmethod
    def mux_open(o: str, open_work: dict) -> bool:
        return any(open_work.get((o, k)) for k in range(M[o].fan_in))

    def try_cover(self, o: str, k: int, open_work: dict) -> bool:
        direct = M[o].model == "direct"
        if not direct and not self.assign(o, k):
            return False
        if M[o].net in self.obs:
            return True
        banned: set = set()
        for _ in range(6):
            path = self.route(M[o].net, open_work, banned)
            if path is None:
                break
            done = []
            for o2, k2 in path:
                if self.assign(o2, k2):
                    done.append(o2)
                else:
                    banned.add((o2, k2))
                    break
            else:
                self._mark_path(o)
                return True
            for o2 in done:
                self.unassign(o2)
        if not direct:
            self.unassign(o)
        return False

    def _mark_path(self, o: str) -> None:
        # the path ends on an observed net: re-mark from every observed net's
        # predecessors (cheap: the tile is small)
        for n in list(self.obs):
            self._mark(n)


# ---------------------------------------------------------------- exact grading

def compile_ops(sel: dict[str, int]):
    """Topological list of (net, op, args) for the model with these selects."""
    d = sc.deps(sel)
    order, seen = [], set(INVEC)

    def visit(n):
        stack = [(n, iter(d.get(n, ())))]
        seen.add(n)
        while stack:
            x, it = stack[-1]
            for p in it:
                if p not in seen:
                    seen.add(p)
                    stack.append((p, iter(d.get(p, ()))))
                    break
            else:
                stack.pop()
                order.append(x)

    for n in sorted(d):
        if n not in seen:
            visit(n)
    ops = []
    for n in order:
        if n in INVEC:
            continue
        o = MUX_OF.get(n)
        if o is not None:
            s = M[o].inputs[sel.get(o, 0)] if M[o].model != "direct" else M[o].inputs[0]
            ops.append((n, "c", _const(s)) if s.kind == "const" else (n, "b", s.net))
        elif n in sc.PASS:
            ops.append((n, "b", sc.PASS[n]))
        elif n.endswith("_O"):
            ops.append((n, "x", BELDEPS[n]))
        elif n.endswith("_Co"):
            ops.append((n, "m", BELDEPS[n]))
        elif n.startswith("M_"):
            ops.append((n, "s", BELDEPS[n]))
        else:
            raise KeyError(n)
    return ops


def run_ops(ops, forced: str | None = None, fval: int = 0) -> dict[str, int]:
    v = dict(INVEC)
    if forced is not None:
        v[forced] = fval
    for n, op, a in ops:
        if n == forced:
            continue
        if op == "b":
            v[n] = v[a]
        elif op == "c":
            v[n] = a
        elif op == "x":
            v[n] = v[a[0]] ^ v[a[1]] ^ v[a[2]] ^ v[a[3]]
        elif op == "m":
            x, y, z = v[a[0]], v[a[1]], v[a[2]]
            v[n] = (x & y) | (x & z) | (y & z)
        else:
            x, y, s = v[a[0]], v[a[1]], v[a[2]]
            v[n] = (s & y) | (~s & FULL & x)
    return v


def grade(sel: dict[str, int], only=None) -> dict[tuple[str, int], set]:
    """(mux, code) -> work items this config shows (exact, model level);
    `only`: the muxes to grade (default all)."""
    ops = compile_ops(sel)
    base = run_ops(ops)
    shown = {}
    for o in (ALL if only is None else only):
        k = sel.get(o, 0)
        m = M[o]
        if m.net not in base:
            continue
        flip = run_ops(ops, m.net, base[m.net] ^ FULL)
        ob = 0
        for n in sc.OUTPUTS:
            ob |= base[n] ^ flip[n]
        if not ob:
            continue
        s = m.inputs[k]
        a = _const(s) if s.kind == "const" else base[s.net]
        items = set()
        if ob & ~a & FULL:
            items.add("t0")
        if ob & a:
            items.add("t1")
        for i in range(len(m.sel)):
            k2 = k ^ (1 << i)
            if k2 >= m.fan_in:
                continue
            s2 = m.inputs[k2]
            b = _const(s2) if s2.kind == "const" else base.get(s2.net, 0)
            if ob & (a ^ b):
                items.add(i)
        shown[(o, k)] = items
    return shown


def gain(sel: dict[str, int], open_work: dict, hist: dict | None = None) -> int:
    """Open items shown; with `hist`, each item of mux o weighs 1 + hist[o]."""
    only = [o for o in ALL if open_work.get((o, sel.get(o, 0)))]
    return sum(len(open_work[key] & items) * (1 + (hist or {}).get(key[0], 0))
               for key, items in grade(sel, only).items())


def polish(sel: dict[str, int], open_work: dict, rounds: int = 2, hist: dict | None = None) -> int:
    """Hill climb: move single muxes to another code while the exact gain
    on the open targets (weighted by `hist`) grows and no loop closes.
    Returns the gain."""
    best = gain(sel, open_work, hist)
    for _ in range(rounds):
        better = False
        for o in REAL:
            cur = sel[o]
            for k in range(M[o].fan_in):
                if k == cur:
                    continue
                sel[o] = k
                if not is_loop_free(sel):
                    continue
                g = gain(sel, open_work, hist)
                if g > best:
                    best, cur, better = g, k, True
            sel[o] = cur
        if not better:
            break
    return best


def is_loop_free(sel: dict[str, int]) -> bool:
    return not sc.cycles(sel)


# ---------------------------------------------------------------- router

ONE_GROUP = True
POLISH = True


def build_config(open_work: dict, order_bias: int, hist: dict | None = None) -> dict[str, int]:
    cfg = Cfg()
    internal = [o for o in ALL if M[o].sink != "tile_output"]
    outs = [o for o in ALL if M[o].sink == "tile_output"]

    def work(o):
        return sum(len(open_work.get((o, k), ())) for k in range(M[o].fan_in))

    # most open work first; rotate ties by config so different muxes win exits
    # history first (PathFinder-style: muxes whose open code was selected
    # but not shown in earlier configs), then most open work
    hist = hist or {}
    key = lambda o: (-hist.get(o, 0), -work(o), (ALL.index(o) + order_bias * 7) % len(ALL))  # noqa: E731
    for group in ((sorted(internal + outs, key=key),) if ONE_GROUP else (sorted(internal, key=key), sorted(outs, key=key))):
        for o in group:
            if o in cfg.sel or not work(o):
                continue
            ks = sorted((k for k in range(M[o].fan_in) if open_work.get((o, k))),
                        key=lambda k: (-len(open_work[(o, k)]), (k + order_bias) % M[o].fan_in))
            for k in ks:
                if cfg.try_cover(o, k, open_work):
                    break
    # fill: open code if loop free, else any loop-free code; if every code
    # closes a loop, free a mux on the loop (it is filled again later)
    todo = [o for o in REAL if o not in cfg.sel]
    tries = 0
    while todo:
        o = todo.pop(0)
        ks = sorted(range(M[o].fan_in), key=lambda k: (not open_work.get((o, k)), (k + order_bias) % M[o].fan_in))
        if any(cfg.assign(o, k) for k in ks):
            continue
        tries += 1
        if tries > 500:
            raise SystemExit(f"{o}: loop repair did not converge")
        k = ks[0]
        on_loop = [x for x in cfg.path_muxes(M[o].net, M[o].inputs[k].net) if x != o]
        if not on_loop:
            raise SystemExit(f"{o}: every input closes a loop")
        victim = on_loop[0]
        cfg.unassign(victim)
        if not cfg.assign(o, k):
            todo.insert(0, o)
        todo.append(victim)
    if not is_loop_free(cfg.sel):
        raise SystemExit("router produced a loop (incremental check missed it)")
    return cfg.sel


def route_all(max_configs: int = 40, verbose: bool = True):
    reach = reachable_muxes()
    open_work = {(o, k): work_items(o, k) for o in ALL if o in reach for k in range(M[o].fan_in)}
    total = sum(len(v) for v in open_work.values())
    unreachable = sorted(set(ALL) - reach)
    configs, log = [], []
    stall = 0
    hist: dict[str, int] = defaultdict(int)
    while len(configs) < max_configs and any(open_work.values()):
        sel = build_config(open_work, len(configs), hist)
        if POLISH:
            polish(sel, open_work, hist=hist)
        shown = grade(sel)
        for o in ALL:
            key = (o, sel.get(o, 0))
            if open_work.get(key) and not (open_work[key] & shown.get(key, set())):
                hist[o] += 1
        gain = 0
        for key, items in shown.items():
            if key in open_work:
                before = len(open_work[key])
                open_work[key] -= items
                gain += before - len(open_work[key])
        left = sum(len(v) for v in open_work.values())
        if verbose:
            print(f"  config {len(configs):2d}: +{gain} items, {left} of {total} open", flush=True)
        if gain == 0:
            stall += 1
            if stall >= 3:
                break
            continue
        stall = 0
        configs.append(sel)
        log.append({"gain": gain, "open": left})
    return configs, open_work, unreachable, total, log


def all_targets() -> dict:
    reach = reachable_muxes()
    return {(o, k): work_items(o, k) for o in ALL if o in reach for k in range(M[o].fan_in)}


def shown_items(sel: dict[str, int], targets: dict) -> set:
    return {(key, it) for key, items in grade(sel).items() if key in targets for it in items & targets[key]}


def compact(configs: list[dict], targets: dict, verbose: bool = True) -> list[dict]:
    """Drop the config with the smallest unique contribution when its unique
    items can be moved, one (mux, code) at a time, into other configs: a
    config takes a key when, re-made (polished, or routed afresh and
    polished), it shows its own unique items plus that key's. Repeat."""
    configs = [dict(c) for c in configs]
    sets = [shown_items(c, targets) for c in configs]

    def uniq(i, sets_, skip=()):
        rest = set().union(*(sets_[j] for j in range(len(sets_)) if j != i and j not in skip))
        return sets_[i] - rest

    while len(configs) > 1:
        v = min(range(len(configs)), key=lambda i: len(uniq(i, sets)))
        by_key = defaultdict(set)
        for key, it in uniq(v, sets):
            by_key[key].add(it)
        cf, st = [dict(c) for c in configs], list(sets)
        ok = True
        for key, items in sorted(by_key.items()):
            placed = False
            for c in sorted((i for i in range(len(cf)) if i != v), key=lambda i: len(uniq(i, st, (v,)))):
                need = uniq(c, st, (v,)) | {(key, it) for it in items}
                work = defaultdict(set)
                for k2, it in need:
                    work[k2].add(it)
                for start in ("polish", "route"):
                    sel = dict(cf[c]) if start == "polish" else build_config(work, c)
                    if polish(sel, work, rounds=3) == len(need):
                        cf[c], st[c] = sel, shown_items(sel, targets)
                        placed = True
                        break
                if placed:
                    break
            if not placed:
                ok = False
                break
        if not ok:
            if verbose:
                print(f"  compact: config {v} ({len(uniq(v, sets))} unique items) cannot be folded", flush=True)
            break
        if verbose:
            print(f"  compact: config {v} ({len(uniq(v, sets))} unique items, {len(by_key)} codes) "
                  f"moved into other configs", flush=True)
        del cf[v], st[v]
        configs, sets = cf, st
    return configs


def baseline_items() -> tuple[int, int]:
    """Model-level items the 16 baseline configs show, for comparison."""
    reach = reachable_muxes()
    open_work = {(o, k): work_items(o, k) for o in ALL if o in reach for k in range(M[o].fan_in)}
    total = sum(len(v) for v in open_work.values())
    for c in range(sc.F):
        for key, items in grade(sc.BASE_SELECTS[c]).items():
            if key in open_work:
                open_work[key] -= items
    return total - sum(len(v) for v in open_work.values()), total


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=40)
    ap.add_argument("--compact-only", action="store_true", help="only compact the configs in sm_router.json")
    a = ap.parse_args()
    b, total = baseline_items()
    print(f"baseline (16 configs): {b} of {total} model-level items shown ({100 * b / total:.2f}%)")
    if a.compact_only:
        configs = json.loads(OUT_JSON.read_text())["configs"]
        unreach, log = sorted(set(ALL) - reachable_muxes()), []
    else:
        configs, open_work, unreach, total, log = route_all(a.max)
    configs = compact(configs, all_targets())
    open_work = all_targets()
    total = sum(len(v) for v in open_work.values())
    for sel in configs:
        for key, items in grade(sel).items():
            if key in open_work:
                open_work[key] -= items
    left = {k: sorted(map(str, v)) for k, v in open_work.items() if v}
    done = total - sum(len(v) for v in open_work.values())
    print(f"router: {len(configs)} configs, {done} of {total} items shown ({100 * done / total:.2f}%); "
          f"{len(left)} codes with open items; not reachable combinationally: {unreach}")
    OUT_JSON.write_text(json.dumps({
        "configs": configs, "log": log, "unreachable": unreach,
        "open": {f"{o}:{k}": v for (o, k), v in sorted(left.items())},
        "items": {"total": total, "shown": done, "baseline_shown": b}}, indent=1))
    print(f"-> {OUT_JSON.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
