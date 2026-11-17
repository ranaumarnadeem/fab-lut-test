"""SAT-targeted test configs (fabtest target) for undetected switch-matrix faults.

Per undetected fault in a switch-matrix mux (MuxTrees: the gate tree of each mux in the
flat netlist): a SAT model on the trees the fault acts in (sim/untestable.Checker, window =
their cells, leaves and select bits free), lifted to the values that force a difference at
one mux output. That becomes a target for the router:

  sels    mux -> code for every mux one of whose select bits is in the lifted model
  inputs  the switch-matrix inputs whose value matters (model nets): each driven by its own
          pad signal (the stimulus has every pad code and its complement, so two different
          pads take both value pairs; more inputs rely on the random vectors)
  obs     a switch-matrix output that differs (a mux output): routed to an observed net

A config starts from the observation backbone (fab_router.backbone), places as many
targets as fit (assign the codes, route the output to observation, the inputs from pads;
undone when one step fails) and is completed by fab_router.fill (loop-free codes for the
other muxes, the exact loop check).
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

RX_SM = re.compile(r"^(.+)\.Inst_(\w+?)_switch_matrix\.(\w+(?:\[\d+\])?)$")
RX_CB = re.compile(r"^(.+)\.ConfigBits\[(\d+)\]$")
RX_TILE = re.compile(r"^Tile_X(\d+)Y(\d+)_(\w+?)(?:\.Tile_X(\d+)Y(\d+)_(\w+))?$")


def model_tile(ff, inst: str):
    """Model tile of a netlist tile instance (Tile_X<x>Y<y>_<type>[.Tile_X<dx>Y<dy>_<sub>]):
    W_IO at (-1, y), every other tile at (x - 1 + dx, y + dy); None when the type differs."""
    m = RX_TILE.match(inst)
    if not m:
        return None
    x, y, typ = int(m.group(1)), int(m.group(2)), m.group(3)
    if m.group(4) is not None:
        x, y, typ = x + int(m.group(4)), y + int(m.group(5)), m.group(6)
    t = (-1, y) if typ == "W_IO" else (x - 1, y)
    return t if ff.types.get(t) == typ else None


class MuxTrees:
    """The gate tree of every switch-matrix mux in the flat netlist.

    A mux's select bits are the latch outputs <inst>.ConfigBits[tile bit] (model SelBit).
    Its tree: the forward cone of its select bits (every gate of a cus_mux tree, or of the
    behavioural mux of MultiplexerStyle generic, reads a select bit or a gate that does),
    stopped at gates that read another config bit (the next mux, a BEL), cut back to the
    paths into its output: the net named <inst>.Inst_<type>_switch_matrix.<mux>, else the
    one net of the cone read outside it. Its leaves are the other inputs of those gates: the
    mux inputs, whatever the netlist calls them. Trees of different muxes are disjoint, so a window of trees has no loop.
    Which leaf is input k: the tree evaluated with code k and one leaf at 1."""
    MAX_CELLS = 4000

    def __init__(self, nl, ff):
        self.nl, self.ff = nl, ff
        outs_of: dict = defaultdict(dict)
        cfg: dict = defaultdict(dict)
        for name, bit in nl.net_bit.items():
            m = RX_SM.match(name)
            if m:
                outs_of[m.group(1)].setdefault(m.group(3), bit)
                continue
            m = RX_CB.match(name)
            if m:
                cfg[m.group(1)][int(m.group(2))] = bit
        top_of = [n.split(".", 1)[0] for n in nl.cell_names]
        self.ports = set(nl.port_bits.values())
        self.trees, self.cell_tree = [], {}
        self.leaf_trees: dict = defaultdict(list)
        self.sel_tree: dict = {}
        insts = set(outs_of) | set(cfg)
        for inst in insts:
            t = model_tile(ff, inst)
            if t is None:
                continue
            ob = outs_of.get(inst, {})
            top = inst.split(".", 1)[0]
            cfgbits = set(cfg[inst].values())
            # config nets of the tile: latch outputs and their inverters / buffers
            cfgnet = set(cfgbits)
            for b in cfgbits:
                for c in self._readers(b):
                    if nl.type[c] in (2, 3):
                        cfgnet.add(int(nl.out[c]))
            for o, m in ff.MT(t).items():
                if m.model == "direct":
                    continue
                sels = [cfg[inst].get(s.tile_bit) for s in m.sel]
                if None in sels:
                    continue
                mine = set(sels)
                for b in sels:
                    for c in self._readers(b):
                        if nl.type[c] in (2, 3):
                            mine.add(int(nl.out[c]))
                foreign = cfgnet - mine
                cells, stack = set(), list(sels)
                while stack and len(cells) <= self.MAX_CELLS:
                    n = stack.pop()
                    for c in self._readers(n):
                        if c in cells or top_of[c] != top or nl.type[c] in (4, 5):
                            continue
                        if any(int(x) in foreign for x in (nl.a[c], nl.b[c], nl.c[c])):
                            continue   # another mux (or a BEL) reading its own config
                        cells.add(c)
                        stack.append(int(nl.out[c]))
                if not cells or len(cells) > self.MAX_CELLS:
                    continue
                exits = [int(nl.out[c]) for c in cells
                         if int(nl.out[c]) in self.ports or
                         any(r not in cells for r in self._readers(int(nl.out[c])))]
                exits = [y for y in exits if y not in mine]
                if ob.get(o) in exits:
                    out = ob[o]
                elif len(exits) == 1:
                    out = exits[0]
                else:
                    continue
                # the tree: the cells on paths from the select bits to the output
                back, stack = set(), [out]
                while stack:
                    n = stack.pop()
                    c = nl.driver.get(n)
                    if c is None or c not in cells or c in back:
                        continue
                    back.add(c)
                    stack += [int(x) for x in (nl.a[c], nl.b[c], nl.c[c]) if x >= 0]
                cells = back
                leaves = sorted({int(x) for c in cells for x in (nl.a[c], nl.b[c], nl.c[c])
                                 if x >= 0 and nl.driver.get(int(x)) not in cells and int(x) not in mine})
                k = len(self.trees)
                self.trees.append({"t": t, "o": o, "out": out, "cells": cells, "sels": sels,
                                   "leaves": leaves, "map": None})
                for c in cells:
                    self.cell_tree[c] = k
                for x in leaves:
                    self.leaf_trees[x].append(k)
                for x in sels:
                    self.sel_tree[x] = k

    def _readers(self, n):
        nl = self.nl
        return [int(c) for c in nl.rd_idx[nl.rd_off[n]:nl.rd_off[n + 1]]]

    def trees_of(self, bit: int) -> list[int]:
        """Trees a fault net acts in: its driver's, the trees reading it as a leaf, the
        tree it selects."""
        out = []
        c = self.nl.driver.get(bit)
        if c is not None and c in self.cell_tree:
            out.append(self.cell_tree[c])
        out += self.leaf_trees.get(bit, [])
        if bit in self.sel_tree:
            out.append(self.sel_tree[bit])
        for c in self._readers(bit):
            if c in self.cell_tree:
                out.append(self.cell_tree[c])
        return sorted(set(out))

    def leaf_map(self, k: int) -> dict:
        """leaf net -> mux input index of tree k (evaluated: code k, one leaf at 1)."""
        tr = self.trees[k]
        if tr["map"] is not None:
            return tr["map"]
        nl = self.nl
        order, done, cells = [], set(), tr["cells"]
        pending = sorted(cells)
        while pending:   # topological order of the tree
            rest = []
            for c in pending:
                if all(int(x) < 0 or nl.driver.get(int(x)) not in cells or nl.driver.get(int(x)) in done
                       for x in (nl.a[c], nl.b[c], nl.c[c])):
                    order.append(c)
                    done.add(c)
                else:
                    rest.append(c)
            if len(rest) == len(pending):
                tr["map"] = {}
                return tr["map"]
            pending = rest
        n_in = len(self.ff.MT(tr["t"])[tr["o"]].inputs)

        def ev(val):
            v = dict(val)
            for c in order:
                ty = nl.type[c]
                a = v.get(int(nl.a[c]), 0)
                if ty == 0:
                    y = a & v.get(int(nl.b[c]), 0)
                elif ty == 1:
                    y = a | v.get(int(nl.b[c]), 0)
                elif ty == 2:
                    y = 1 - a
                else:
                    y = a
                v[int(nl.out[c])] = y
            return v.get(tr["out"], 0)
        mp = {}
        for code in range(n_in):
            base = {s: (code >> i) & 1 for i, s in enumerate(tr["sels"])}
            for leaf in tr["leaves"]:
                if ev({**base, **{l: int(l == leaf) for l in tr["leaves"]}}) == 1 and \
                        ev({**base, **{l: int(l != leaf) for l in tr["leaves"]}}) == 0:
                    mp.setdefault(leaf, code)
        tr["map"] = mp
        return mp


def target_of(ck, mt: MuxTrees, bit: int, pol: int):
    """The router target of one fault (dict), or None (in no mux tree, not testable there,
    or a select code outside the mux)."""
    ks = mt.trees_of(bit)
    if not ks:
        return None
    window = set().union(*(mt.trees[k]["cells"] for k in ks))
    ck.cfg_nets = {s for k in ks for s in mt.trees[k]["sels"]}
    if ck.check(bit, pol, window=window, witness=True) != "T":
        return None
    care, val = ck.care, ck.val
    ff = mt.ff
    t = mt.trees[ks[0]]["t"]
    sels, inputs, obs = {}, [], []
    for k in ks:
        tr = mt.trees[k]
        m = ff.MT(tr["t"])[tr["o"]]
        if any(s in care for s in tr["sels"]):
            code = sum(val.get(s, 0) << i for i, s in enumerate(tr["sels"]))
            if code >= len(m.inputs) or ff.forbidden_src(tr["t"], tr["o"], code):
                return None
            sels[(tr["t"], tr["o"])] = code
        mp = None
        for leaf in tr["leaves"]:
            if leaf in care:
                mp = mp if mp is not None else mt.leaf_map(k)
                if leaf not in mp:
                    return None
                s = m.inputs[mp[leaf]]
                if s.kind != "const":
                    inputs.append(ff.canon(tr["t"], s.net))
        if tr["out"] in ck.obs_hit:
            obs.append((tr["t"], tr["o"]))
    if not obs:
        return None
    return {"t": t, "sels": sels, "inputs": sorted(set(inputs)), "obs": obs}


# ---------------------------------------------------------------- router


FAILS: Counter = Counter()   # why targets were not placed (build_configs prints it)


def _wire_pred(g) -> dict:
    out = {}
    for a, lst in g.fixed.items():
        for b, c in lst:
            if c == 0:
                out.setdefault(b, a)
    return out


def _root(cfg, wp: dict, n: str):
    """The pad-side net a model net carries through selected muxes and wires, else None."""
    g = cfg.g
    seen = set()
    while n not in seen:
        seen.add(n)
        if n.startswith("PAD/") or n.startswith("IOY") and n.endswith(("/A_O", "/B_O")):
            return n
        i = g.mux_of.get(n)
        if i is not None and i in cfg.sel:
            s = g.src(i, cfg.sel[i])
            if s is None:
                return None
            n = s
            continue
        p = wp.get(n)
        if p is None:
            return None
        n = p
    return None


def place(cfg, tg: dict, wp: dict, bias: int) -> bool:
    """Place one target in cfg (undone on failure)."""
    from fabtest.patterns.config import fab_router as fr
    g, arr = cfg.g, cfg.g.arr
    t = tg["t"]
    before = set(cfg.sel)

    def fail(why):
        FAILS[why] += 1
        fr._undo(cfg, before)
        return False
    for i, k in tg["sels"].items():
        if not cfg.assign(i, k):
            return fail("select" if i not in cfg.sel else "select taken")
    if not any(i in g.net_of and fr.route_net(cfg, g.net_of[i]) for i in tg["obs"]):
        return fail("observation")
    used = set()
    pads = fr._rot(fr.pads_near(arr, t[1], ("A_O", "B_O")), bias)
    for n in tg["inputs"]:
        r = _root(cfg, wp, n)
        if r is not None and r not in used:
            used.add(r)
            continue
        if r is not None or g.mux_of.get(n) in cfg.sel:
            continue   # driven already (by another target's route or the backbone)
        ok = False
        for p in pads:
            if p in used:
                continue
            if fr.route_to(cfg, p, {n}, True):
                used.add(p)
                ok = True
                break
        if not ok:
            return fail("input")
    return True


def build_configs(ff, targets: list[dict], max_configs: int, prior: list | None = None,
                  idx0: int = 0, verbose: bool = True, first: bool = False):
    """Configs for the targets. Each config: the observation backbone, the router's own
    greedy cover of the items the prior configs leave open, then as many targets as fit in
    the muxes left free, then its fill (fab_router.route_all with post=). Returns (configs as
    {tile name: selects}, per config the target indices placed)."""
    from fabtest.patterns.config import fab_router as fr
    g = fr.Graph(ff)
    wp = _wire_pred(g)
    state = {"left": list(range(len(targets))), "placed": []}

    def post(cfg, bias):
        placed, rest = [], []
        for j in state["left"]:
            (placed if place(cfg, targets[j], wp, bias) else rest).append(j)
        state["left"] = rest
        state["placed"].append(placed)
        if verbose:
            print(f"  target config {len(state['placed']) - 1}: {len(placed)} targets, {len(rest)} left; "
                  f"not placed: {dict(FAILS)}", flush=True)
        FAILS.clear()
        return len(placed)
    if first:   # targets right after the backbone, before the cover
        def seed(cfg, b):
            rep = fr.backbone(cfg, b)
            post(cfg, b)
            return rep
        configs, _, _, log, _ = fr.route_all(ff, max_configs, verbose=verbose, prior=prior, idx0=idx0, seed=seed)
    else:
        configs, _, _, log, _ = fr.route_all(ff, max_configs, verbose=verbose, prior=prior, idx0=idx0,
                                             seed=lambda cfg, b: fr.backbone(cfg, b), post=post)
    return configs, state["placed"][:len(configs)]
