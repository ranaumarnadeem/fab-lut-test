#!/usr/bin/env python3
"""Fast fault simulation: bit-parallel C kernel (fsim.c), many faults per pass.

Strict 3-valued simulation, X-pessimistic latch / flop models, faults forced from the
first step, statuses D (detected: a definite 0 / 1 difference at an observed output),
P (possibly: X against a definite value), U / T (undetected / timeout) per (fault, config).
Every fault is simulated in every config, so the matrix is complete.
Netlist: the generic-gate JSON of fabtest synth; Stim: one config's stimulus (inputs per
step, sample flags, expected outputs); run_faults: the fault list over all stimuli,
threaded (ctypes releases the GIL during the kernel call).
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import pickle
import subprocess
import threading
import time
from pathlib import Path

import numpy as np

from fabtest import util  # noqa: E402
from fabtest.paths import REPO as ROOT  # noqa: E402
from fabtest.patterns.faults import gen_faults  # noqa: E402
from fabtest.sim import graph  # noqa: E402
from fabtest.sim.graph import tarjan  # noqa: E402

SRC = Path(__file__).resolve().parent / "fsim.c"
LIBDIR = ROOT / "build" / "fastsim"
CFLAGS = ["-O3", "-march=native", "-shared", "-fPIC"]
TYPES = {"$_AND_": 0, "$_OR_": 1, "$_NOT_": 2, "$_BUF_": 3, "$_DLATCH_P_": 4, "$_DFFE_PP_": 5}
PINS = {"$_AND_": ("A", "B", None), "$_OR_": ("A", "B", None), "$_NOT_": ("A", None, None),
        "$_BUF_": ("A", None, None), "$_DLATCH_P_": ("E", "D", None), "$_DFFE_PP_": ("C", "E", "D")}
OUTPIN = {"$_DLATCH_P_": "Q", "$_DFFE_PP_": "Q"}
MAXW = 16   # lane words per kernel call (fixed arrays in fsim.c)
_LIB = None
_LIB_LOCK = threading.Lock()


def kernel_key() -> str:
    """What the statuses of a fault simulation depend on in the code: the kernel and this
    driver (stored with every run's fault simulation, faultsim/sample.json)."""
    h = hashlib.sha1(SRC.read_bytes())
    h.update(Path(__file__).read_bytes())
    return h.hexdigest()[:16]


def lib_path() -> Path:
    """build/fastsim/libfsim-<key>.so, the key: the source, compiler, flags and host CPU
    (-march=native code may not run on another one). Built once, under a lock."""
    cc = os.environ.get("CC", "gcc")

    def build(tmp: Path) -> None:
        subprocess.run([cc, *CFLAGS, "-o", str(tmp), str(SRC)], check=True)
    return util.built(LIBDIR, "libfsim", ".so", [SRC.read_bytes(), cc, util.compiler_version(cc),
                                                 " ".join(CFLAGS), util.machine_key()], build)


def build_lib() -> ctypes.CDLL:
    global _LIB
    with _LIB_LOCK:   # every worker thread makes an Engine: one build, one load
        if _LIB is None:
            _LIB = ctypes.CDLL(str(lib_path()))
    # FASTSIM_OSC=local: an oscillating lane keeps being simulated with only its
    # oscillating nets at X (default "kill": the whole lane goes X)
    _LIB.set_osc_local(1 if os.environ.get("FASTSIM_OSC", "kill") == "local" else 0)
    return _LIB


def _netlist_cache(path: Path) -> Path | None:
    """Cache file of a netlist JSON: keyed by its path, size and mtime and by the code that
    builds the arrays (this file, gen_faults.py, graph.py). FASTSIM_NETLIST_CACHE=0: no cache."""
    if os.environ.get("FASTSIM_NETLIST_CACHE", "1") == "0" or not path.exists():
        return None
    st = path.stat()
    h = hashlib.sha1(f"{path.resolve()}:{st.st_size}:{st.st_mtime_ns}".encode())
    for src in (Path(__file__), Path(gen_faults.__file__), Path(graph.__file__)):
        h.update(src.read_bytes())
    return ROOT / "build" / "cache" / "netlist" / f"{path.stem}-{h.hexdigest()[:16]}.pkl"


class Netlist:
    """Flat arrays of one generic-gate netlist for the kernel."""

    def __init__(self, name: str, path: Path | str | None = None):
        """name: build/netlists/netlist_<name>.json, or path: any netlist JSON (fabtest synth).
        The built arrays are cached (build/cache/netlist, keyed by the JSON and this code):
        a later load of the same netlist takes seconds instead of a minute."""
        path = Path(path) if path else ROOT / "build" / "netlists" / f"netlist_{name}.json"
        cache = _netlist_cache(path)
        if cache is not None and cache.exists():
            try:
                with cache.open("rb") as fh:
                    self.__dict__.update(pickle.load(fh))
                self.name = name
                return
            except Exception:   # unreadable (another numpy, a half-written file): built again
                pass
        self._build(name, path)
        if cache is not None:
            cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache.with_suffix(f".tmp{os.getpid()}")
            with tmp.open("wb") as fh:
                pickle.dump(self.__dict__, fh, protocol=pickle.HIGHEST_PROTOCOL)
            tmp.replace(cache)

    def _build(self, name: str, path: Path) -> None:
        self.name = name
        _, mod = gen_faults.load_top(path, None)
        bits = gen_faults.analyse(mod)
        maxbit = max([b for c in mod["cells"].values() for s in c["connections"].values()
                      for b in s if isinstance(b, int)] +
                     [b for p in mod["ports"].values() for b in p["bits"] if isinstance(b, int)] + [0])
        # names of bits no cell and no port touches (dangling netnames) are left out: their
        # index would point past the nets (or onto a constant net)
        self.net_bit: dict[str, int] = {}
        for b, e in bits.items():
            if not isinstance(b, int) or b > maxbit:
                continue
            self.net_bit[e["names"][0][0]] = b
            for a in e.get("aliases", []):
                self.net_bit.setdefault(a, b)
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
        self.port_bits: dict[str, int] = {}
        for pname, port in mod["ports"].items():
            for k, b in enumerate(port["bits"]):   # a port bit tied to "0" / "1": a constant net
                self.port_bits[f"{pname}[{k}]"] = net(b)
                if len(port["bits"]) == 1:
                    self.port_bits[pname] = net(b)

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


# ---------------------------------------------------------------- run

_G: dict = {}


def _p(a):
    return a.ctypes.data_as(ctypes.c_void_p)


class Engine:
    """One netlist + one set of stimuli, shared by all worker threads (ctypes
    releases the GIL during the kernel call, so threads run in parallel)."""

    def __init__(self, nl: Netlist, stims: list[Stim], W: int = 8, limit_factor: int = 20):
        if not 1 <= W <= MAXW:
            raise ValueError(f"fastsim: W={W} lane words, the kernel takes 1..{MAXW}")
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
        return [self.run_one(batch, ci) for ci in range(len(self.prepared))]

    def run_one(self, batch: list[tuple[int, int]], ci: int) -> list[str]:
        """The batch under config ci only (run_faults runs (batch, config) pairs in parallel)."""
        lib, nl, W = self.lib, self.nl, self.W
        fnet = np.array([b for b, _ in batch] or [0], dtype=np.int32)
        fpol = np.array([p for _, p in batch] or [0], dtype=np.int32)
        for st, in_nets, out_nets, rows, sample in self.prepared[ci:ci + 1]:
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
            self.last_gold = (gold, len(out_nets))   # fault-free outputs per sample (2 = X)
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
            return np.where(v & 1, "D", np.where(v & 4, "T", np.where(v & 2, "P", "U"))).tolist()


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


def outputs(nl: Netlist, st: Stim, limit_factor: int = 200) -> list[str]:
    """The fault-free outputs of st at its samples: per sample a string over st.out_names
    (0 / 1 / x)."""
    eng = Engine(nl, [st], W=1, limit_factor=limit_factor)
    eng.run_one([], 0)
    gold, n = eng.last_gold
    return ["".join("x" if v == 2 else str(v) for v in gold[k * n:(k + 1) * n]) for k in range(len(gold) // n)]


def fault_free(nl: Netlist, stims: list, jobs: int = os.cpu_count() or 1, limit_factor: int = 200) -> dict:
    """Fault-free run of every stimulus, in parallel threads: name -> None (the netlist
    gives the model's expected outputs) or the error message (left out)."""
    from concurrent.futures import ThreadPoolExecutor

    def one(st):
        try:
            Engine(nl, [st], W=1, limit_factor=limit_factor).run_batch([])
            return st.name, None
        except RuntimeError as e:
            return st.name, str(e)
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
        return dict(ex.map(one, stims))


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
    first_const = nl.n_nets - nl.n_const
    bad = [f["net"] for f, (b, _) in zip(faults, items) if not 0 <= b < first_const]
    if bad:
        raise SystemExit(f"fault nets outside the netlist's nets (or on a constant): {bad[:5]}")
    observed = {nl.port_bits[n] for st in stims for n in st.out_names}
    reps = collapse(nl, items, observed) if do_collapse else list(range(len(items)))
    todo = sorted(set(reps))
    per = 64 * W - 1
    batches = [todo[k:k + per] for k in range(0, len(todo), per)]
    eng = Engine(nl, stims, W, limit_factor=limit_factor)
    t0 = time.time()
    from concurrent.futures import ThreadPoolExecutor
    pairs = [(bi, ci) for bi in range(len(batches)) for ci in range(len(stims))]   # all in parallel
    with ThreadPoolExecutor(max_workers=jobs) as ex:
        results = list(ex.map(lambda p: eng.run_one([items[k] for k in batches[p[0]]], p[1]), pairs))
    status: dict[int, dict[str, str]] = {}
    cfgs = [s.name for s in stims]
    for (bi, ci), res in zip(pairs, results):
        for j, k in enumerate(batches[bi]):
            status.setdefault(k, {})[cfgs[ci]] = res[j]
    dt = time.time() - t0
    if not quiet:
        print(f"{netlist}: {len(items)} faults ({len(todo)} after collapsing) x {len(cfgs)} configs "
              f"in {dt:.2f} s on {jobs} threads")
    return cfgs, [status[r] for r in reps]
