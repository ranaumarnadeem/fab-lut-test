#!/usr/bin/env python3
"""Combinational loop check of the configured LUT4AB tile.

The configured netlist: every config latch output becomes the constant its
config word gives it, tile inputs the testbench holds at 0 are 0, constants
are propagated in 3-valued logic (sim/conesplit.py). Cells whose output
stays non-constant are live; a combinational loop is a strongly connected
component of live cells (Tarjan) with more than one cell or a self edge.
Flops break loops (they are left out of the graph); latches are constants.

  python3 sim/loop_check.py [--netlist tile_generic] [--jobs N]
      Checks the all-zero background config, BC1, D0C and D1 fault free, and
      every fault that can create a loop in each config, writes
      reports/loops.md and reports/<netlist>_loops.json.

Which faults can create a loop: pinning a net that is already non-constant
only makes more of the netlist constant, so it can never close a loop. Only
a fault that forces a net that is constant in the configured tile to the
opposite value can enable a path (e.g. a mux select). Those faults are
checked, which includes every config-bit (latch output) SA0/SA1 fault.

`check_config(words)` is also used by sim/run_tile.py as a gate: it refuses
to simulate a fault-free config that contains a loop.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / d) for d in ("sim", "configs", "faults", "rtl")]
import conesplit as cs  # noqa: E402
import state  # noqa: E402

REPORTS = ROOT / "reports"
V = cs.V


def tarjan(nodes: list[str], succ) -> list[list[str]]:
    """Iterative Tarjan; returns SCCs."""
    index, low, on, stack, out = {}, {}, set(), [], []
    counter = [0]
    for root in nodes:
        if root in index:
            continue
        work = [(root, iter(succ(root)))]
        index[root] = low[root] = counter[0]
        counter[0] += 1
        stack.append(root)
        on.add(root)
        while work:
            v, it = work[-1]
            advanced = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter[0]
                    counter[0] += 1
                    stack.append(w)
                    on.add(w)
                    work.append((w, iter(succ(w))))
                    advanced = True
                    break
                if w in on:
                    low[v] = min(low[v], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                out.append(comp)
    return out


class Checker:
    def __init__(self, netlist: str = "tile_generic"):
        import tile_configs as tc
        self.tc = tc
        self.netlist = netlist
        js = ROOT / "synth" / f"netlist_{netlist}.json"
        self.g = cs.load_graph(js)
        self.elems = state.storage_elements(js)
        import routing_bits as rb
        self.latch_bit = rb.latch_bits(netlist)          # element index -> config bit
        r = tc.ROUTING
        self.obs = [r["lut_outputs"][f"L{x}_O"] for x in tc.LUTS] + ["Co[0]"]
        self.obs_bits = {self.g.port_bits[o] for o in self.obs}
        routed = set(r["lut_inputs"].values()) | {"Ci[0]"}
        self.held0 = [n for n in tc.TILE.tile_inputs if n not in routed]
        self.flops = {e.cell for e in self.elems if e.kind == "flop"}
        self.bit_name = {}
        for name, b in self.g.net_bit.items():
            self.bit_name.setdefault(b, name)

    def consts(self, cfg: dict[int, int]) -> dict[int, int]:
        c = {}
        for e in self.elems:
            if e.kind == "latch":
                c[self.g.cells[e.cell]["out"]] = cfg[self.latch_bit[e.index]]
        for n in self.held0:
            c[self.g.port_bits[n]] = 0
        return c

    def loops(self, const: dict[int, int]) -> tuple[list[list[str]], dict[int, int]]:
        g = self.g
        val = cs.propagate(g, const)
        live = [n for n, c in g.cells.items()
                if n not in self.flops and val.get(c["out"], V) == V]
        live_set = set(live)

        def succ(n):
            return [r for r in g.readers.get(g.cells[n]["out"], ()) if r in live_set]

        comps = [c for c in tarjan(live, succ)
                 if len(c) > 1 or c[0] in succ(c[0])]
        return comps, val

    def reaches_output(self, comp: list[str], val: dict[int, int]) -> list[str]:
        """Observed outputs reachable from the loop through live cells (and flops)."""
        g = self.g
        seen, q = set(comp), list(comp)
        hit = set()
        while q:
            n = q.pop()
            o = g.cells[n]["out"]
            if o in self.obs_bits:
                hit.add(o)
            for r in g.readers.get(o, ()):
                if r not in seen and val.get(g.cells[r]["out"], V) == V:
                    seen.add(r)
                    q.append(r)
        return sorted(self.obs[[self.g.port_bits[x] for x in self.obs].index(b)] for b in hit)

    def describe(self, comp: list[str]) -> list[str]:
        return sorted({self.bit_name.get(self.g.cells[n]["out"], f"bit{self.g.cells[n]['out']}") for n in comp})

    def check_config(self, cfg: dict[int, int], extra: dict[int, int] | None = None):
        const = self.consts(cfg)
        const.update(extra or {})
        comps, val = self.loops(const)
        return [{"nets": self.describe(c), "cells": len(c), "outputs": self.reaches_output(c, val)}
                for c in comps], val


def config_words() -> dict[str, dict[int, int]]:
    import configs as cell
    import tile_configs as tc
    out = {"all-zero": {b: 0 for b in tc.TILE.configmem}}
    for name, f in tc.TILE_CONFIGS.items():
        seq = f()
        out[name] = seq.configs_written[-1]
    return out


_C: dict = {}


def _init(netlist):
    _C["k"] = Checker(netlist)
    _C["cfgs"] = config_words()


def _fault_job(job):
    cfg_name, fid, bit, pol = job
    k = _C["k"]
    loops, _ = k.check_config(_C["cfgs"][cfg_name], {bit: pol})
    return cfg_name, fid, loops


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--netlist", default="tile_generic")
    ap.add_argument("--jobs", type=int, default=os.cpu_count())
    args = ap.parse_args()
    k = Checker(args.netlist)
    cfgs = config_words()
    base = {}
    for name, cfg in cfgs.items():
        loops, val = k.check_config(cfg)
        base[name] = (loops, val)
        print(f"{name}: {len(loops)} loops")
    faults = list(csv.DictReader((ROOT / "faults" / f"{args.netlist}.csv").open()))
    jobs = []
    for name in ("BC1", "D0C", "D1"):
        val = base[name][1]
        for f in faults:
            b = k.g.net_bit[f["net"]]
            v = val.get(b, V)
            if v != V and v != int(f["polarity"]):
                jobs.append((name, int(f["id"]), b, int(f["polarity"])))
    print(f"{len(jobs)} (config, fault) pairs can create a loop; checking", flush=True)
    res = []
    with ProcessPoolExecutor(max_workers=args.jobs, initializer=_init, initargs=(args.netlist,)) as ex:
        for r in ex.map(_fault_job, jobs, chunksize=32):
            res.append(r)
    base_sets = {n: {tuple(l["nets"]) for l in base[n][0]} for n in base}
    by_fault: dict[int, dict] = {}
    fmap = {int(f["id"]): f for f in faults}
    for cfg_name, fid, loops in res:
        new = [l for l in loops if tuple(l["nets"]) not in base_sets[cfg_name]]
        if new:
            by_fault.setdefault(fid, {"id": fid, "path": fmap[fid]["path"], "polarity": fmap[fid]["polarity"],
                                      "configs": {}})["configs"][cfg_name] = new
    data = {"netlist": args.netlist,
            "fault_free": {n: base[n][0] for n in base},
            "checked_pairs": len(jobs),
            "faults_creating_loops": list(by_fault.values())}
    (REPORTS / f"{args.netlist}_loops.json").write_text(json.dumps(data, indent=1))
    n_out = sum(1 for f in by_fault.values() if any(l["outputs"] for ls in f["configs"].values() for l in ls))
    print(f"{len(by_fault)} faults create a loop in some config, {n_out} reach an observed output")
    return 0


if __name__ == "__main__":
    sys.exit(main())
