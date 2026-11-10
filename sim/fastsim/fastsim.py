#!/usr/bin/env python3
"""Fast fault simulation: bit-parallel C kernel (fsim.c), many faults per pass.

  python3 sim/fastsim/fastsim.py faultsim <netlist> --suite sm|lut [--sequence] [--jobs N] [--W 4]
  python3 sim/fastsim/fastsim.py compare  <netlist> --suite sm|lut [--sequence]

Same semantics as the Icarus flow (sim/run_tile.py, sim/run_sm.py): strict
3-valued simulation, X-pessimistic latch / flop models (sim/seq_models.v),
faults forced from t = 1 ns, statuses D / P / U / T per (fault, config). Unlike
the Icarus flow every fault is simulated in every config (no stop at the first
D), so the matrix is complete. Generic-gate netlists only (tile_generic,
tile_generic_br).

Writes reports/<netlist><tag>_fast_matrix.csv; `compare` checks it against the
Icarus matrix reports/<netlist><tag>_matrix.csv (configs not run there, "-",
are skipped).
"""
from __future__ import annotations

import argparse
import csv
import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / d) for d in ("sim", "configs", "faults", "rtl")]
import gen_faults  # noqa: E402
from loop_check import tarjan  # noqa: E402

SRC = ROOT / "sim" / "fastsim" / "fsim.c"
LIB = ROOT / "build" / "fastsim" / "libfsim.so"
REPORTS = ROOT / "reports"
TYPES = {"$_AND_": 0, "$_OR_": 1, "$_NOT_": 2, "$_BUF_": 3, "$_DLATCH_P_": 4, "$_DFFE_PP_": 5}
PINS = {"$_AND_": ("A", "B", None), "$_OR_": ("A", "B", None), "$_NOT_": ("A", None, None),
        "$_BUF_": ("A", None, None), "$_DLATCH_P_": ("E", "D", None), "$_DFFE_PP_": ("C", "E", "D")}
OUTPIN = {"$_DLATCH_P_": "Q", "$_DFFE_PP_": "Q"}


def build_lib() -> ctypes.CDLL:
    if not LIB.exists() or LIB.stat().st_mtime < SRC.stat().st_mtime:
        LIB.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["gcc", "-O3", "-march=native", "-shared", "-fPIC", "-o", str(LIB), str(SRC)], check=True)
    lib = ctypes.CDLL(str(LIB))
    # FASTSIM_OSC=local: an oscillating lane keeps being simulated with only its
    # oscillating nets at X (default "kill": the whole lane goes X)
    lib.set_osc_local(1 if os.environ.get("FASTSIM_OSC", "kill") == "local" else 0)
    return lib


class Netlist:
    """Flat arrays of one generic-gate netlist for the kernel."""

    def __init__(self, name: str):
        self.name = name
        _, mod = gen_faults.load_top(ROOT / "synth" / f"netlist_{name}.json", None)
        bits = gen_faults.analyse(mod)
        self.net_bit: dict[str, int] = {}
        for b, e in bits.items():
            self.net_bit[e["names"][0][0]] = b
            for a in e.get("aliases", []):
                self.net_bit.setdefault(a, b)
        self.port_bits: dict[str, int] = {}
        for pname, port in mod["ports"].items():
            for k, b in enumerate(port["bits"]):
                self.port_bits[f"{pname}[{k}]"] = b
                if len(port["bits"]) == 1:
                    self.port_bits[pname] = b
        maxbit = max([b for c in mod["cells"].values() for s in c["connections"].values()
                      for b in s if isinstance(b, int)] + list(self.port_bits.values()))
        self.consts: dict[str, int] = {}
        nxt = maxbit + 1

        def net(sig) -> int:
            nonlocal nxt
            if isinstance(sig, int):
                return sig
            if sig not in self.consts:
                self.consts[sig] = nxt
                nxt += 1
            return self.consts[sig]

        names = sorted(mod["cells"])
        cells = []
        for cn in names:
            c = mod["cells"][cn]
            t = c["type"]
            if t not in TYPES:
                raise SystemExit(f"fastsim: cell type {t} not supported (generic netlists only)")
            p = PINS[t]
            conn = c["connections"]
            ins = [net(conn[x][0]) if x else -1 for x in p]
            out = net(conn[OUTPIN.get(t, "Y")][0])
            cells.append((TYPES[t], *ins, out))
        self.n_nets = nxt
        self.n_cells = len(cells)
        arr = np.array(cells, dtype=np.int32)
        self.type, self.a, self.b, self.c, self.out = (np.ascontiguousarray(arr[:, k]) for k in range(5))
        # readers (CSR)
        rd: list[list[int]] = [[] for _ in range(self.n_nets)]
        for i, (t, a, b, c, o) in enumerate(cells):
            for x in (a, b, c):
                if x >= 0:
                    rd[x].append(i)
        self.rd_off = np.zeros(self.n_nets + 1, dtype=np.int32)
        self.rd_off[1:] = np.cumsum([len(r) for r in rd])
        self.rd_idx = np.array([i for r in rd for i in r], dtype=np.int32)
        # rank: topological order of the strongly connected components; flops first
        driver = {o: i for i, (*_, o) in enumerate(cells)}
        succ = lambda i: rd[cells[i][4]]  # noqa: E731
        comps = tarjan(list(range(self.n_cells)), succ)   # reverse topological order
        rank = np.zeros(self.n_cells, dtype=np.int32)
        r = 1
        for comp in reversed(comps):
            for i in (self._order_component(comp, succ) if len(comp) > 1 else comp):
                rank[i] = r
                r += 1
        for i, (t, *_) in enumerate(cells):
            if t == TYPES["$_DFFE_PP_"]:
                rank[i] = 0
        self.rank = rank
        self.dff_slot = np.full(self.n_cells, -1, dtype=np.int32)
        k = 0
        for i, (t, *_) in enumerate(cells):
            if t == TYPES["$_DFFE_PP_"]:
                self.dff_slot[i] = k
                k += 1
        self.n_dff = k
        cv = {"0": 0, "1": 1}
        self.const_net = np.array(list(self.consts.values()) or [0], dtype=np.int32)
        self.const_val = np.array([cv.get(s, 2) for s in self.consts] or [2], dtype=np.int32)
        self.n_const = len(self.consts)
        self.driver = driver
        # storage elements (latches, flops): slot order = cell name order
        self.cell_names = names
        self.store = [i for i, (t, *_) in enumerate(cells) if t in (TYPES["$_DLATCH_P_"], TYPES["$_DFFE_PP_"])]
        self.st_slot = np.full(self.n_cells, -1, dtype=np.int32)
        for k, i in enumerate(self.store):
            self.st_slot[i] = k
        self.st_net = np.array([cells[i][4] for i in self.store] or [0], dtype=np.int32)
        self.store_names = [names[i] for i in self.store]
        self.store_is_latch = [cells[i][0] == TYPES["$_DLATCH_P_"] for i in self.store]


    @staticmethod
    def _order_component(comp: list[int], succ) -> list[int]:
        """Cells of a cyclic component in reverse DFS post-order, started from
        the cells with predecessors outside the component: most edges then go
        forward, only feedback edges go back (fewer re-evaluations)."""
        inside = set(comp)
        preds = {i: 0 for i in comp}
        for i in comp:
            for j in succ(i):
                if j in inside:
                    preds[j] += 1
        roots = sorted(comp, key=lambda i: (preds[i], i))
        seen, post = set(), []
        for root in roots:
            if root in seen:
                continue
            seen.add(root)
            stack = [(root, iter(succ(root)))]
            while stack:
                v, it = stack[-1]
                for w in it:
                    if w in inside and w not in seen:
                        seen.add(w)
                        stack.append((w, iter(succ(w))))
                        break
                else:
                    stack.pop()
                    post.append(v)
        return post[::-1]


# ---------------------------------------------------------------- stimulus

class Stim:
    """Dense stimulus of one config: input port nets, 0/1 per step, sample flags, outputs."""

    def __init__(self, name, in_names, rows, sample, out_names, expected):
        self.name, self.in_names, self.rows, self.sample = name, in_names, rows, sample
        self.out_names, self.expected = out_names, expected


def sm_stim(q) -> Stim:
    """Stimulus of one switch-matrix sequence (configs/sm_configs.SMSeq)."""
    import sm_configs as sc
    in_names = [f"FrameData[{k}]" for k in range(32)] + [f"FrameStrobe[{k}]" for k in range(20)] \
        + list(sc.INPUTS) + ["UserCLK"]
    rows = np.zeros((len(q.steps), len(in_names)), dtype=np.uint8)
    for t, s in enumerate(q.steps):
        rows[t] = [(s.fd >> k) & 1 for k in range(32)] + [(s.fs >> k) & 1 for k in range(20)] \
            + [(s.inp >> j) & 1 for j in range(len(sc.INPUTS))] + [s.clk & 1]
    sample = np.array([s.sample for s in q.steps], dtype=np.uint8)
    return Stim(q.name, in_names, rows, sample, list(sc.OUTPUTS), sc.expected(q))


def lut_stim(q) -> Stim:
    """Stimulus of one LUT-config sequence (configs/tile_configs.TileSeq)."""
    import tile_configs as tc
    r = tc.ROUTING
    lut_in = [r["lut_inputs"][f"L{x}_I{j}"] for x in tc.LUTS for j in range(4)]
    in_names = [f"FrameData[{k}]" for k in range(32)] + [f"FrameStrobe[{k}]" for k in range(20)] \
        + lut_in + ["Ci[0]", r["lut_inputs"]["J_SR_BEG[0]"], r["lut_inputs"]["J_EN_BEG[0]"], "UserCLK"]
    held0 = sorted(set(tc.TILE.tile_inputs) - set(in_names))
    in_names += held0
    out_names = [r["lut_outputs"][f"L{x}_O"] for x in tc.LUTS] + ["Co[0]"]
    rows = np.zeros((len(q.steps), len(in_names)), dtype=np.uint8)
    for t, s in enumerate(q.steps):
        v = [(s.fd >> k) & 1 for k in range(32)] + [(s.fs >> k) & 1 for k in range(20)]
        v += [(s.i[k] >> j) & 1 for k in range(8) for j in range(4)]
        v += [s.ci & 1, s.sr & 1, s.en & 1, s.clk & 1] + [0] * len(held0)
        rows[t] = v
    sample = np.array([s.sample for s in q.steps], dtype=np.uint8)
    return Stim(q.name, in_names, rows, sample, out_names, tc.tile_model(q))


def stim_from_seq(q) -> Stim:
    return sm_stim(q) if hasattr(q.steps[0], "inp") else lut_stim(q)


def sm_stims(sequence: bool) -> list[Stim]:
    import sm_configs as sc
    seqs = [sc.sequence(list(sc.BASE_SM_CONFIGS))] if sequence else [f() for f in sc.BASE_SM_CONFIGS.values()]
    return [sm_stim(q) for q in seqs]


def lut_stims(sequence: bool) -> list[Stim]:
    import tile_configs as tc
    seqs = [tc.sequence(list(tc.BASE_TILE_CONFIGS))] if sequence else [f() for f in tc.BASE_TILE_CONFIGS.values()]
    return [lut_stim(q) for q in seqs]


# ---------------------------------------------------------------- run

_G: dict = {}


def _p(a):
    return a.ctypes.data_as(ctypes.c_void_p)


class Engine:
    """One netlist + one set of stimuli, shared by all worker threads (ctypes
    releases the GIL during the kernel call, so threads run in parallel)."""

    def __init__(self, nl: Netlist, stims: list[Stim], W: int = 8, limit_factor: int = 20):
        self.lib = build_lib()
        self.lib.run_config.restype = ctypes.c_int
        self.nl, self.W, self.limit = nl, W, limit_factor * nl.n_cells
        self.stims = stims
        self.checked: set[str] = set()
        self.prepared = []
        for st in stims:
            in_nets = np.array([nl.port_bits[n] for n in st.in_names], dtype=np.int32)
            out_nets = np.array([nl.port_bits[n] for n in st.out_names], dtype=np.int32)
            self.prepared.append((st, in_nets, out_nets, np.ascontiguousarray(st.rows), st.sample))

    def run_batch(self, batch: list[tuple[int, int]]) -> list[list[str]]:
        """batch: [(net bit, polarity)] (at most 64 W - 1) -> per config, a status per fault"""
        lib, nl, W = self.lib, self.nl, self.W
        fnet = np.array([b for b, _ in batch] or [0], dtype=np.int32)
        fpol = np.array([p for _, p in batch] or [0], dtype=np.int32)
        out = []
        for st, in_nets, out_nets, rows, sample in self.prepared:
            res = np.zeros(64 * W, dtype=np.uint8)
            ns = int(sample.sum())
            gold = np.zeros(ns * len(out_nets), dtype=np.uint8)
            rc = lib.run_config(
                nl.n_nets, nl.n_cells, W, _p(nl.type), _p(nl.a), _p(nl.b), _p(nl.c), _p(nl.out),
                _p(nl.rank), _p(nl.rd_off), _p(nl.rd_idx), _p(nl.dff_slot), nl.n_dff,
                _p(nl.const_net), _p(nl.const_val), nl.n_const,
                len(in_nets), _p(in_nets), rows.shape[0], _p(rows), _p(sample), len(out_nets), _p(out_nets),
                len(batch), _p(fnet), _p(fpol), _p(res), _p(gold), ctypes.c_long(self.limit))
            if rc != 0:
                raise RuntimeError(f"{st.name}: the fault-free lane did not settle")
            if st.expected is not None and st.name not in self.checked:   # the same in every batch
                n_out = len(out_nets)
                g = ["".join("x" if v == 2 else str(v) for v in gold[k * n_out:(k + 1) * n_out])
                     for k in range(ns)]
                # "x" in the model: not compared (the gates may be defined there)
                ok = [all(e == "x" or a == e for a, e in zip(x, y)) for x, y in zip(g, st.expected)]
                if len(g) != len(st.expected) or not all(ok):
                    bad = ok.index(False) if not all(ok) else min(len(g), len(st.expected))
                    raise RuntimeError(f"{st.name}: fault-free lane differs from the model at sample {bad}")
                self.checked.add(st.name)
            v = res[1:len(batch) + 1]
            out.append(np.where(v & 1, "D", np.where(v & 4, "T", np.where(v & 2, "P", "U"))).tolist())
        return out


def collapse(nl: Netlist, items: list[tuple[int, int]], observed: set[int]) -> list[int]:
    """Exact structural equivalence: for a gate whose input net has no other
    reader and is not observed, input SA0 of AND = output SA0, input SA1 of
    OR = output SA1, NOT input SAv = output SA(1-v), BUF input SAv = output
    SAv. Forcing such an input forces the output to the same value, so the
    whole simulation (D / P / U / T) is identical. Returns, per item, the
    index of its representative item."""
    parent: dict = {}

    def find(x):
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[ry] = rx

    fanout = np.diff(nl.rd_off)
    consts = set(nl.consts.values())
    for t, a, b, c, o in zip(nl.type.tolist(), nl.a.tolist(), nl.b.tolist(), nl.c.tolist(), nl.out.tolist()):
        if t > 3:
            continue   # latch / flop: sequential, no collapsing through it
        for x in (a, b):
            if x < 0 or x in consts or fanout[x] != 1 or x in observed:
                continue
            if t == 0:
                union((o, 0), (x, 0))
            elif t == 1:
                union((o, 1), (x, 1))
            elif t == 2:
                union((o, 1), (x, 0)); union((o, 0), (x, 1))
            else:
                union((o, 0), (x, 0)); union((o, 1), (x, 1))
    rep_of_class: dict = {}
    reps = []
    for k, it in enumerate(items):
        r = find(it)
        reps.append(rep_of_class.setdefault(r, k))
    return reps


def run_faults(netlist: str, stims: list[Stim], faults: list[dict], jobs: int = os.cpu_count() or 1,
               W: int = 8, do_collapse: bool = True, nl: Netlist | None = None, quiet: bool = False,
               limit_factor: int = 20):
    """Fault-simulate `faults` (dicts with net, polarity) under every stimulus.
    Returns (config names, per fault {config: status})."""
    nl = nl or Netlist(netlist)
    missing = [f["net"] for f in faults if f["net"] not in nl.net_bit]
    if missing:
        raise SystemExit(f"fault nets not in the netlist: {missing[:5]}")
    items = [(nl.net_bit[f["net"]], int(f["polarity"])) for f in faults]
    observed = {nl.port_bits[n] for st in stims for n in st.out_names}
    reps = collapse(nl, items, observed) if do_collapse else list(range(len(items)))
    todo = sorted(set(reps))
    per = 64 * W - 1
    batches = [todo[k:k + per] for k in range(0, len(todo), per)]
    eng = Engine(nl, stims, W, limit_factor=limit_factor)
    t0 = time.time()
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=jobs) as ex:
        results = list(ex.map(lambda bt: eng.run_batch([items[k] for k in bt]), batches))
    status: dict[int, dict[str, str]] = {}
    cfgs = [s.name for s in stims]
    for bt, res in zip(batches, results):
        for j, k in enumerate(bt):
            status[k] = {c: res[ci][j] for ci, c in enumerate(cfgs)}
    dt = time.time() - t0
    if not quiet:
        print(f"{netlist}: {len(items)} faults ({len(todo)} after collapsing) x {len(cfgs)} configs "
              f"in {dt:.2f} s on {jobs} threads")
    return cfgs, [status[r] for r in reps]


def faultsim(netlist: str, suite: str, sequence: bool, jobs: int, W: int, do_collapse: bool = True):
    stims = (sm_stims if suite == "sm" else lut_stims)(sequence)
    faults = list(csv.DictReader((ROOT / "faults" / f"{netlist}.csv").open()))
    cfgs, stat = run_faults(netlist, stims, faults, jobs, W, do_collapse)
    rows = []
    for f, st in zip(faults, stat):
        v = set(st.values())
        anyd = "D" if "D" in v else "P" if "P" in v else "T" if "T" in v else "U"
        rows.append({"id": f["id"], "path": f["path"], "polarity": f["polarity"], "type": f["type"],
                     **st, "any": anyd})
    tag = ("_sm" if suite == "sm" else "") + ("_seq" if sequence else "")
    path = REPORTS / f"{netlist}{tag}_fast_matrix.csv"
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["id", "path", "polarity", "type", *cfgs, "any"])
        w.writeheader()
        w.writerows(rows)
    print(f"   -> {path.name}")
    return path


def compare(netlist: str, suite: str, sequence: bool):
    tag = ("_sm" if suite == "sm" else "") + ("_seq" if sequence else "")
    fast = {r["id"]: r for r in csv.DictReader((REPORTS / f"{netlist}{tag}_fast_matrix.csv").open())}
    ref = {r["id"]: r for r in csv.DictReader((REPORTS / f"{netlist}{tag}_matrix.csv").open())}
    cfgs = [c for c in next(iter(ref.values())) if c not in ("id", "path", "polarity", "type", "any")]
    from collections import Counter
    diff = Counter()
    ex = []
    cells = 0
    for i, r in ref.items():
        for c in cfgs:
            if r[c] == "-":
                continue
            cells += 1
            if r[c] != fast[i][c]:
                diff[(r[c], fast[i][c])] += 1
                if len(ex) < 8:
                    ex.append((r["path"], r["polarity"], c, r[c], fast[i][c]))
    anyd = Counter((r["any"], fast[i]["any"]) for i, r in ref.items() if r["any"] != fast[i]["any"])
    print(f"{netlist} {suite}{' sequence' if sequence else ''}: {cells} (fault, config) results compared, "
          f"{sum(diff.values())} differ {dict(diff)}; 'any' differs for {sum(anyd.values())} {dict(anyd)}")
    for e in ex:
        print("   ", e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["faultsim", "compare"])
    ap.add_argument("netlist")
    ap.add_argument("--suite", choices=["sm", "lut"], required=True)
    ap.add_argument("--sequence", action="store_true")
    ap.add_argument("--jobs", type=int, default=os.cpu_count())
    ap.add_argument("--W", type=int, default=8, help="64-lane words per batch (<= 16)")
    ap.add_argument("--no-collapse", action="store_true", help="simulate every fault (no equivalence collapsing)")
    a = ap.parse_args()
    if a.cmd == "faultsim":
        faultsim(a.netlist, a.suite, a.sequence, a.jobs, a.W, not a.no_collapse)
    else:
        compare(a.netlist, a.suite, a.sequence)


if __name__ == "__main__":
    main()
