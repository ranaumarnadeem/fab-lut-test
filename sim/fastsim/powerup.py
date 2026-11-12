#!/usr/bin/env python3
"""Power-up check (cone-split) on the fast kernel (Phase 11).

The same decision as sim/run_conesplit.py + sim/conesplit.py (Icarus), with
the fast kernel's lanes doing the runs:

- X-only fault: P in some config, D in none (strict matrix).
- U = storage elements the faulty run never definitely writes (latch enable
  exactly 1, or a clean 0 -> 1 clock edge with enable 1, after the faults
  and deposits settle), union over the P configs; their power-up value is
  the same for the whole test.
- Cones per config with the end-of-run latch values of the faulty run as
  constants (written latches not in U), the fault's net pinned
  (conesplit.groups_for): independent groups of U.
- A group is decided when every assignment of its elements (the rest of U
  at X) shows a definite mismatch on the group's own outputs in some P
  config; the fault is detected iff some group is decided. Groups larger
  than max_group: per output o, the elements whose cone reaches o (as
  conesplit.decide); still nothing: "group too large" (no case split here).
- A lane that oscillates (kernel timeout) gives no information, as an Icarus
  run that times out: the fault without deposits -> "timeout"; an
  assignment -> not detected by that config.

Every (fault, group, assignment) is one lane; lanes of many faults share a
kernel call (run_config_x: per-lane deposits, per-output mismatch masks,
definitely-written masks, end-of-run storage values).

  python3 sim/fastsim/powerup.py check <netlist> --suite sm|rt|ck   compare with reports/<n>_<tag>_conesplit.json
"""
from __future__ import annotations

import ctypes
import itertools
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path[:0] = [str(ROOT / "sim" / "fastsim"), str(ROOT / "sim"), str(ROOT / "faults")]
import fastsim  # noqa: E402

_p = fastsim._p
MAXW = 16
LANES = 64 * MAXW - 1


class PowerUp:
    def __init__(self, nl: fastsim.Netlist, stims: list, netlist_json: Path, limit_factor: int = 20):
        import conesplit as cs
        self.cs = cs
        self.nl = nl
        self.eng = fastsim.Engine(nl, stims, W=1, limit_factor=limit_factor)
        self.lib = self.eng.lib
        self.lib.run_config_x.restype = ctypes.c_int
        self._json, self._g = netlist_json, None
        self.cfg_index = {st.name: k for k, st in enumerate(stims)}
        outs = stims[0].out_names
        if any(st.out_names != outs for st in stims):
            raise SystemExit("powerup: every config must observe the same outputs")
        self.out_bit_pos = {nl.port_bits[o]: k for k, o in enumerate(outs)}
        self.elem_cells = {k: nl.store_names[k] for k in range(len(nl.store))}
        self.q_bit = {k: int(nl.st_net[k]) for k in range(len(nl.store))}

    @property
    def g(self):   # loaded on first use (cones; not needed when every U is small)
        if self._g is None:
            self._g = self.cs.load_graph(self._json)
        return self._g

    def run(self, cfg: str, lanes: list[tuple[int, int, dict]]):
        """lanes: [(fault net bit, polarity, deposits {slot: 0/1})] (at most LANES).
        Returns (res, detmap [n_out, W], wr [n_store, W], stq_h, stq_l) as numpy."""
        nl = self.nl
        st, in_nets, out_nets, rows, sample = self.eng.prepared[self.cfg_index[cfg]]
        W = max(1, (len(lanes) + 1 + 63) // 64)
        fnet = np.array([n for n, _, _ in lanes] or [0], dtype=np.int32)
        fpol = np.array([p for _, p, _ in lanes] or [0], dtype=np.int32)
        slots = sorted({sl for _, _, dep in lanes for sl in dep})
        si = {sl: k for k, sl in enumerate(slots)}
        ih = np.zeros((max(1, len(slots)), W), dtype=np.uint64)
        il = np.zeros_like(ih)
        im = np.zeros_like(ih)
        for j, (_, _, dep) in enumerate(lanes):
            lane = j + 1
            w, b = lane >> 6, np.uint64(1 << (lane & 63))
            for sl, v in dep.items():
                k = si[sl]
                im[k, w] |= b
                if v:
                    ih[k, w] |= b
                else:
                    il[k, w] |= b
        init_net = np.array([self.q_bit[sl] for sl in slots] or [0], dtype=np.int32)
        n_store = len(nl.store)
        res = np.zeros(64 * W, dtype=np.uint8)
        ns = int(sample.sum())
        gold = np.zeros(max(1, ns * len(out_nets)), dtype=np.uint8)
        wr = np.zeros((max(1, n_store), W), dtype=np.uint64)
        sh, sl_ = np.zeros_like(wr), np.zeros_like(wr)
        det = np.zeros((len(out_nets), W), dtype=np.uint64)
        rc = self.lib.run_config_x(
            nl.n_nets, nl.n_cells, W, _p(nl.type), _p(nl.a), _p(nl.b), _p(nl.c), _p(nl.out),
            _p(nl.rank), _p(nl.rd_off), _p(nl.rd_idx), _p(nl.dff_slot), nl.n_dff,
            _p(nl.const_net), _p(nl.const_val), nl.n_const,
            len(in_nets), _p(in_nets), rows.shape[0], _p(rows), _p(sample), len(out_nets), _p(out_nets),
            len(lanes), _p(fnet), _p(fpol), _p(res), _p(gold), ctypes.c_long(self.eng.limit),
            len(slots), _p(init_net), _p(ih), _p(il), _p(im),
            _p(nl.st_slot), n_store, _p(nl.st_net), _p(wr), _p(sh), _p(sl_), _p(det))
        if rc != 0:
            raise RuntimeError(f"{cfg}: the fault-free lane did not settle")
        return res, det, wr, sh, sl_

    @staticmethod
    def lane_bit(arr2d, lane):
        return (arr2d[:, lane >> 6] >> np.uint64(lane & 63)) & np.uint64(1)

    def decide(self, faults: list[dict], p_cfgs: dict[int, list[str]], max_group: int = 12,
               verbose: bool = True, full_k: int = 10, jobs: int = 4,
               max_u: int | None = None) -> dict[int, dict]:
        """faults: X-only fault rows (net, polarity, id); p_cfgs: fault index -> P configs.
        max_u: faults with a larger U are not decided ("U too large", no cones).
        Returns fault index -> {verdict, k, groups}."""
        cs, nl = self.cs, self.nl
        t0 = time.time()
        bit = {i: nl.net_bit[f["net"]] for i, f in enumerate(faults)}
        pol = {i: int(f["polarity"]) for i, f in enumerate(faults)}
        need_cones = max_u is None or max_u > full_k
        # 1. U and end-of-run latch values per (fault, config)
        U = defaultdict(set)
        consts = defaultdict(list)
        timeout = set()
        by_cfg = defaultdict(list)
        for i, cl in p_cfgs.items():
            for c in cl:
                by_cfg[c].append(i)
        for c, idx in by_cfg.items():
            for b0 in range(0, len(idx), LANES):
                part = idx[b0:b0 + LANES]
                res, _, wr, sh, sl = self.run(c, [(bit[i], pol[i], {}) for i in part])
                for j, i in enumerate(part):
                    lane = j + 1
                    if res[lane] & 4:
                        timeout.add(i)   # oscillates: no verdict (Icarus: run timed out)
                    w_ = self.lane_bit(wr, lane)
                    h_ = self.lane_bit(sh, lane)
                    l_ = self.lane_bit(sl, lane)
                    u_c = set(np.nonzero(w_ == 0)[0].tolist())
                    U[i] |= u_c
                    if need_cones:   # end-of-run latch values: only the cones use them
                        const = {bit[i]: pol[i]}
                        for k in np.nonzero((w_ == 1) & (h_ != l_))[0].tolist():
                            if nl.store_is_latch[k]:
                                const[self.q_bit[k]] = int(h_[k])
                        consts[i].append(const)
        if verbose:
            print(f"  U computed for {len(p_cfgs)} faults ({time.time() - t0:.1f} s)", flush=True)
        # 2. groups and their outputs
        plan = {}
        out_bits = set(self.out_bit_pos)
        verdict = {i: {"verdict": "timeout", "k": len(U[i]), "group_sizes": []} for i in timeout}
        all_pos = set(self.out_bit_pos.values())
        n_cone = 0
        for i in p_cfgs:
            if i in timeout:
                continue
            Ul = sorted(U[i])
            if max_u is not None and len(Ul) > max_u:
                verdict[i] = {"verdict": "U too large", "k": len(Ul), "group_sizes": []}
                continue
            if len(Ul) <= full_k:
                # small U: every joint assignment of U on every output (exact, no cones needed)
                plan[i] = (Ul, [Ul], [all_pos], {e: all_pos for e in Ul})
                continue
            g = self.g
            n_cone += 1
            groups, cones = cs.groups_for(g, self.elem_cells, Ul, consts[i], out_bits, carry=None)
            gpos = [{self.out_bit_pos[b] for b in cs.group_outputs(g, set().union(*(cones[e] for e in grp)),
                                                                     out_bits)} for grp in groups]
            epos = {e: {self.out_bit_pos[b] for b in cs.group_outputs(g, cones[e], out_bits)} for e in Ul}
            plan[i] = (Ul, groups, gpos, epos)
        if verbose:
            print(f"  cones / groups for {n_cone} faults with |U| > {full_k}, the rest enumerated "
                  f"({time.time() - t0:.1f} s)", flush=True)

        def try_sets(cands: dict[int, list[tuple[list[int], set[int]]]]) -> dict[int, bool]:
            """cands: fault -> [(elements, positions)]; True for a fault when some set has
            every assignment detected on its positions in some P config."""
            jobs = []   # (fault, set index, assignment index, deposit, positions)
            n_assign = {}
            for i, sets in cands.items():
                for si, (els, pos) in enumerate(sets):
                    if not pos:
                        continue
                    assigns = list(itertools.product((0, 1), repeat=len(els)))
                    n_assign[(i, si)] = len(assigns)
                    for ai, bits in enumerate(assigns):
                        jobs.append((i, si, ai, dict(zip(els, bits)), pos))
            hit = set()
            from concurrent.futures import ThreadPoolExecutor

            def one(args):
                c, part = args
                res, det, _, _, _ = self.run(c, [(bit[j[0]], pol[j[0]], j[3]) for j in part])
                out = []
                for k, j in enumerate(part):
                    lane = k + 1
                    if res[lane] & 4:
                        continue   # oscillates under this assignment: no information (as Icarus)
                    if any((int(det[o, lane >> 6]) >> (lane & 63)) & 1 for o in j[4]):
                        out.append((j[0], j[1], j[2]))
                return out
            with ThreadPoolExecutor(max_workers=jobs_n) as ex:
                for c in sorted({c for i in cands for c in p_cfgs[i]}):
                    todo = [j for j in jobs if c in p_cfgs[j[0]] and (j[0], j[1], j[2]) not in hit]
                    parts = [(c, todo[b0:b0 + LANES]) for b0 in range(0, len(todo), LANES)]
                    for got in ex.map(one, parts):
                        hit.update(got)
            done = defaultdict(bool)
            for (i, si), n in n_assign.items():
                if all((i, si, a) in hit for a in range(n)):
                    done[i] = True
            return done

        jobs_n = jobs
        # rounds by group rank (smallest first): a fault one group decides is not simulated further
        d1: dict = {}
        ranked = {i: sorted([(grp, pos) for grp, pos in zip(pl[1], pl[2]) if len(grp) <= max_group],
                            key=lambda gp: len(gp[0])) for i, pl in plan.items()}
        for r in range(max((len(v) for v in ranked.values()), default=0)):
            cand = {i: [v[r]] for i, v in ranked.items() if len(v) > r and not d1.get(i)}
            if not cand:
                break
            for i, ok in try_sets(cand).items():
                if ok:
                    d1[i] = True
        rest = {}
        for i, (Ul, groups, gpos, epos) in plan.items():
            if d1.get(i):
                verdict[i] = {"verdict": "detected", "method": "group", "k": len(Ul)}
            elif all(len(grp) <= max_group for grp in groups):
                verdict[i] = {"verdict": "escapes", "k": len(Ul)}
            else:
                outs = sorted({p for ps in epos.values() for p in ps})
                rest[i] = [(sorted(e for e, ps in epos.items() if o in ps), {o}) for o in outs]
                rest[i] = [(s_o, pos) for s_o, pos in rest[i] if len(s_o) <= max_group]
        d2 = try_sets(rest) if rest else {}
        for i in rest:
            verdict[i] = {"verdict": "detected", "method": "output", "k": len(plan[i][0])} if d2.get(i) else \
                {"verdict": "group too large", "k": len(plan[i][0])}
        for i in plan:
            verdict[i]["group_sizes"] = sorted(len(grp) for grp in plan[i][1])
        if verbose:
            print(f"  decided {len(verdict)} faults ({time.time() - t0:.1f} s)", flush=True)
        return verdict


def x_only(rows: list[dict], cfgs: list[str]) -> dict[int, list[str]]:
    out = {}
    for j, r in enumerate(rows):
        st = [r[c] for c in cfgs]
        if "D" not in st and "P" in st:
            out[j] = [c for c in cfgs if r[c] == "P"]
    return out


def main() -> None:
    import argparse
    import csv
    import json
    import os
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["check"])
    ap.add_argument("netlist")
    ap.add_argument("--suite", choices=["sm", "rt", "ck"], default="sm")
    ap.add_argument("--max-group", type=int, default=12)
    a = ap.parse_args()
    os.environ["SM_SUITE"] = {"sm": "baseline", "rt": "router", "ck": "clocked"}[a.suite]
    sys.path.insert(0, str(ROOT / "configs"))
    import sm_configs as sc
    stims = [fastsim.sm_stim(f()) for f in sc.SM_CONFIGS.values()]
    tag = {"sm": "_sm", "rt": "_rt", "ck": "_ck"}[a.suite]
    reports = ROOT / "reports"
    rows = list(csv.DictReader((reports / f"{a.netlist}{tag}_matrix.csv").open()))
    cfgs = [st.name for st in stims]
    xo = x_only(rows, cfgs)
    stems = {j for j in xo if rows[j]["path"].startswith("FrameStrobe[") and "__br" not in rows[j]["path"]}
    xo = {j: v for j, v in xo.items() if j not in stems}   # the Icarus runs left the strobe stems out
    faults = list(csv.DictReader((ROOT / "faults" / f"{a.netlist}.csv").open()))
    fl = [faults[j] for j in xo]
    nl = fastsim.Netlist(a.netlist)
    pu = PowerUp(nl, stims, ROOT / "synth" / f"netlist_{a.netlist}.json")
    t0 = time.time()
    v = pu.decide(fl, {k: xo[j] for k, j in enumerate(xo)}, a.max_group)
    print(f"{a.netlist} {a.suite}: {len(fl)} X-only faults in {time.time() - t0:.1f} s: "
          f"{sum(r['verdict'] == 'detected' for r in v.values())} detected")
    ref_path = reports / f"{a.netlist}{tag}_conesplit.json"
    if ref_path.exists():
        ref = {str(r["id"]): r["verdict"] for r in json.loads(ref_path.read_text())}
        diff = [(fl[k]["path"], fl[k]["polarity"], ref.get(fl[k]["id"]), r["verdict"])
                for k, r in v.items() if ref.get(fl[k]["id"]) is not None
                and (ref[fl[k]["id"]] == "detected") != (r["verdict"] == "detected")]
        both = sum(1 for k in v if fl[k]["id"] in ref)
        print(f"  vs Icarus cone-split ({ref_path.name}): {both} faults in both, {len(diff)} differ")
        for d in diff[:10]:
            print("   ", d)


if __name__ == "__main__":
    main()
