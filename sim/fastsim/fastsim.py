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
from concurrent.futures import ProcessPoolExecutor
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
    return ctypes.CDLL(str(LIB))


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
        for r, comp in enumerate(reversed(comps)):
            for i in comp:
                rank[i] = r + 1
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


# ---------------------------------------------------------------- stimulus

class Stim:
    """Dense stimulus of one config: input port nets, 0/1 per step, sample flags, outputs."""

    def __init__(self, name, in_names, rows, sample, out_names, expected):
        self.name, self.in_names, self.rows, self.sample = name, in_names, rows, sample
        self.out_names, self.expected = out_names, expected


def sm_stims(sequence: bool) -> list[Stim]:
    import sm_configs as sc
    seqs = [sc.sequence(list(sc.BASE_SM_CONFIGS))] if sequence else [f() for f in sc.BASE_SM_CONFIGS.values()]
    out = []
    in_names = [f"FrameData[{k}]" for k in range(32)] + [f"FrameStrobe[{k}]" for k in range(20)] \
        + list(sc.INPUTS) + ["UserCLK"]
    for q in seqs:
        rows = np.zeros((len(q.steps), len(in_names)), dtype=np.uint8)
        for t, s in enumerate(q.steps):
            v = [(s.fd >> k) & 1 for k in range(32)] + [(s.fs >> k) & 1 for k in range(20)] \
                + [(s.inp >> j) & 1 for j in range(len(sc.INPUTS))] + [s.clk & 1]
            rows[t] = v
        sample = np.array([s.sample for s in q.steps], dtype=np.uint8)
        out.append(Stim(q.name, in_names, rows, sample, list(sc.OUTPUTS), sc.expected(q)))
    return out


def lut_stims(sequence: bool) -> list[Stim]:
    import tile_configs as tc
    r = tc.ROUTING
    seqs = [tc.sequence(list(tc.BASE_TILE_CONFIGS))] if sequence else [f() for f in tc.BASE_TILE_CONFIGS.values()]
    lut_in = [r["lut_inputs"][f"L{x}_I{j}"] for x in tc.LUTS for j in range(4)]
    in_names = [f"FrameData[{k}]" for k in range(32)] + [f"FrameStrobe[{k}]" for k in range(20)] \
        + lut_in + ["Ci[0]", r["lut_inputs"]["J_SR_BEG[0]"], r["lut_inputs"]["J_EN_BEG[0]"], "UserCLK"]
    held0 = sorted(set(tc.TILE.tile_inputs) - set(in_names))
    in_names += held0
    out_names = [r["lut_outputs"][f"L{x}_O"] for x in tc.LUTS] + ["Co[0]"]
    out = []
    for q in seqs:
        rows = np.zeros((len(q.steps), len(in_names)), dtype=np.uint8)
        for t, s in enumerate(q.steps):
            v = [(s.fd >> k) & 1 for k in range(32)] + [(s.fs >> k) & 1 for k in range(20)]
            v += [(s.i[k] >> j) & 1 for k in range(8) for j in range(4)]
            v += [s.ci & 1, s.sr & 1, s.en & 1, s.clk & 1] + [0] * len(held0)
            rows[t] = v
        sample = np.array([s.sample for s in q.steps], dtype=np.uint8)
        out.append(Stim(q.name, in_names, rows, sample, out_names, tc.tile_model(q)))
    return out


# ---------------------------------------------------------------- run

_G: dict = {}


def _p(a):
    return a.ctypes.data_as(ctypes.c_void_p)


def _init(netlist: str, stims: list[Stim], W: int, limit_factor: int):
    lib = build_lib()
    lib.run_config.restype = ctypes.c_int
    nl = Netlist(netlist)
    prepared = []
    for st in stims:
        in_nets = np.array([nl.port_bits[n] for n in st.in_names], dtype=np.int32)
        out_nets = np.array([nl.port_bits[n] for n in st.out_names], dtype=np.int32)
        prepared.append((st, in_nets, out_nets, np.ascontiguousarray(st.rows), st.sample))
    _G.update(lib=lib, nl=nl, prepared=prepared, W=W, limit=limit_factor * nl.n_cells)


def _run_batch(batch: list[tuple[int, int]]):
    """batch: [(net bit, polarity)] -> per config: (status bytes per fault, golden ok)"""
    lib, nl, W = _G["lib"], _G["nl"], _G["W"]
    fnet = np.array([b for b, _ in batch] or [0], dtype=np.int32)
    fpol = np.array([p for _, p in batch] or [0], dtype=np.int32)
    out = []
    for st, in_nets, out_nets, rows, sample in _G["prepared"]:
        lanes = 64 * W
        res = np.zeros(lanes, dtype=np.uint8)
        ns = int(sample.sum())
        gold = np.zeros(ns * len(out_nets), dtype=np.uint8)
        rc = lib.run_config(
            nl.n_nets, nl.n_cells, W, _p(nl.type), _p(nl.a), _p(nl.b), _p(nl.c), _p(nl.out),
            _p(nl.rank), _p(nl.rd_off), _p(nl.rd_idx), _p(nl.dff_slot), nl.n_dff,
            _p(nl.const_net), _p(nl.const_val), nl.n_const,
            len(in_nets), _p(in_nets), rows.shape[0], _p(rows), _p(sample), len(out_nets), _p(out_nets),
            len(batch), _p(fnet), _p(fpol), _p(res), _p(gold), ctypes.c_long(_G["limit"]))
        if rc != 0:
            raise RuntimeError(f"{st.name}: the fault-free lane did not settle")
        g = ["".join("x" if v == 2 else str(v) for v in gold[k * len(out_nets):(k + 1) * len(out_nets)])
             for k in range(ns)]
        if g != st.expected:
            bad = next(i for i, (a, b) in enumerate(zip(g, st.expected)) if a != b)
            raise RuntimeError(f"{st.name}: fault-free lane differs from the model at sample {bad}")
        stat = []
        for k in range(len(batch)):
            v = res[k + 1]
            stat.append("D" if v & 1 else "T" if v & 4 else "P" if v & 2 else "U")
        out.append(stat)
    return out


def faultsim(netlist: str, suite: str, sequence: bool, jobs: int, W: int, limit_factor: int = 400):
    stims = (sm_stims if suite == "sm" else lut_stims)(sequence)
    nl = Netlist(netlist)
    faults = list(csv.DictReader((ROOT / "faults" / f"{netlist}.csv").open()))
    missing = [f["net"] for f in faults if f["net"] not in nl.net_bit]
    if missing:
        raise SystemExit(f"fault nets not in the netlist: {missing[:5]}")
    per = 64 * W - 1
    items = [(nl.net_bit[f["net"]], int(f["polarity"])) for f in faults]
    batches = [items[k:k + per] for k in range(0, len(items), per)]
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=jobs, initializer=_init,
                             initargs=(netlist, stims, W, limit_factor)) as ex:
        results = list(ex.map(_run_batch, batches))
    dt = time.time() - t0
    cfgs = [s.name for s in stims]
    rows = []
    k = 0
    for batch, res in zip(batches, results):
        for j in range(len(batch)):
            f = faults[k]
            st = {c: res[ci][j] for ci, c in enumerate(cfgs)}
            v = set(st.values())
            anyd = "D" if "D" in v else "P" if "P" in v else "T" if "T" in v else "U"
            rows.append({"id": f["id"], "path": f["path"], "polarity": f["polarity"], "type": f["type"],
                         **st, "any": anyd})
            k += 1
    tag = ("_sm" if suite == "sm" else "") + ("_seq" if sequence else "")
    path = REPORTS / f"{netlist}{tag}_fast_matrix.csv"
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["id", "path", "polarity", "type", *cfgs, "any"])
        w.writeheader()
        w.writerows(rows)
    print(f"{netlist} {suite}{' sequence' if sequence else ''}: {len(faults)} faults x {len(cfgs)} configs "
          f"in {dt:.1f} s on {jobs} workers ({len(batches)} batches of {per}); {path.name}")
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
    ap.add_argument("--W", type=int, default=4, help="64-lane words per batch (<= 16)")
    a = ap.parse_args()
    if a.cmd == "faultsim":
        faultsim(a.netlist, a.suite, a.sequence, a.jobs, a.W)
    else:
        compare(a.netlist, a.suite, a.sequence)


if __name__ == "__main__":
    main()
