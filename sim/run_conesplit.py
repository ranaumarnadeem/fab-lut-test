#!/usr/bin/env python3
"""Cone-split power-up verdicts for cell and tile netlists (sim/conesplit.py).

U (the unwritten elements): `validate` uses "never possibly written", the
same U as the full enumerator; `run` uses "never definitely written" (enable
exactly 1 / clean 0 -> 1 edge with enable 1), which also catches elements
whose enable is X only because other state is X (see NOTES.md).

  python3 sim/run_conesplit.py validate <netlist> [--jobs N]
      Every fault the full enumerator handled (k <= 12, reports/<n>_exhaust.json):
      cone-split verdict must equal the full verdict. Escape witnesses are
      simulated as full assignments and must escape. Writes
      reports/<netlist>_conesplit_validate.csv.
  python3 sim/run_conesplit.py run <netlist> --only REGEX [--jobs N]
      Verdicts for the faults on matching nets. Writes
      reports/<netlist>_conesplit.json.

netlist: sky130, generic, sky130_br, generic_br (cell, 9 configs) or
tile_generic, tile_generic_br (tile, 3 configs). On sky130 netlists library
cells are never constant, so cones are structural.
"""
from __future__ import annotations

import argparse
import itertools
import csv
import json
import os
import re
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / d) for d in ("sim", "configs", "faults", "rtl")]
import conesplit as cs  # noqa: E402
import simcore  # noqa: E402
import state  # noqa: E402

REPORTS = ROOT / "reports"
SEQUENCE: list[str] | None = None   # --sequence: sequence-mode matrix and stimulus
TAG = ""                            # "_seq" in sequence mode (file names)


class Target:
    """Netlist-specific simulation glue."""

    def __init__(self, netlist: str, faults: list[dict], sequence: list[str] | None = None):
        self.netlist = netlist
        self.tile = netlist.startswith("tile_")
        self.sequence = sequence
        tag = "_seq" if sequence else ""
        js = ROOT / "synth" / f"netlist_{netlist}.json"
        self.elems = state.storage_elements(js)
        if self.tile:
            import run_tile as rt
            import tile_configs as tc
            if sequence:
                seqname = "SEQ_" + "_".join(sequence)
                tc.TILE_CONFIGS = {seqname: (lambda: tc.sequence(sequence))}
            self.configs = list(tc.TILE_CONFIGS)
            stims = rt.write_stimuli()
            # own build dir per process: concurrent invocations must not share a binary
            self.vvp, _ = rt.compile_netlist(netlist, faults, tag=f"_cone{os.getpid()}")
            self.golden = rt.golden_runs(self.vvp, stims)
            self.pa = {c: [f"+STIM={s}", f"+NSTEPS={n}"] for c, (s, n, _, _) in stims.items()}
            self.pa_early = rt.plusargs(stims, True)
            self.seqs = {c: f() for c, f in tc.TILE_CONFIGS.items()}
            r = tc.ROUTING
            self.obs = [r["lut_outputs"][f"L{x}_O"] for x in tc.LUTS] + ["Co[0]"]
            routed = set(r["lut_inputs"].values()) | {"Ci[0]"}
            self.held0 = [n for n in tc.TILE.tile_inputs if n not in routed]
        else:
            import run as rc
            import configs as cfg
            if sequence:
                cfg.register_sequence(sequence)
            m = list(csv.DictReader((REPORTS / f"{netlist}{tag}_matrix.csv").open()))
            self.configs = [c for c in m[0] if c not in ("id", "path", "polarity", "type", "any")]
            stims = rc.write_stimuli(self.configs)
            self.vvp = rc.compile_target(netlist, faults)
            self.pa = {c: rc.stim_args(s, n) for c, (s, n, _) in stims.items()}
            self.pa_early = dict(self.pa)
            self.golden = {c: simcore.run_vvp(self.vvp, self.pa[c]).samples for c in self.configs}
            self.seqs = {c: cfg.CONFIGS[c]() for c in self.configs}
            self.obs = ["O", "Co"]
            self.held0 = []
        # Segments: one per config write group (sequence mode: one per config).
        # Pruning a segment with its end-of-segment latch values is valid only
        # if no clock edge falls between its first and last frame strobe.
        self.segments, self.seg_prune = {}, {}
        for c, seq in self.seqs.items():
            ends = list(seq.seg_ends) if getattr(seq, "seg_ends", None) else [len(seq.steps) - 1]
            starts = [0] + [e + 1 for e in ends[:-1]]
            self.segments[c] = list(zip(starts, ends))
            ok = []
            for a, b in self.segments[c]:
                steps = seq.steps[a:b + 1]
                last_fs = max((i for i, s in enumerate(steps) if s.fs), default=-1)
                first_clk = min((i for i, s in enumerate(steps) if s.clk), default=10 ** 9)
                ok.append(first_clk > last_fs)
            self.seg_prune[c] = ok
            if len(ends) > 1:
                seg = [f"+SEGEND{j}={e}" for j, e in enumerate(ends)]
                self.pa[c] = self.pa[c] + seg
                self.pa_early[c] = self.pa_early[c] + seg
        self.prune_ok = {c: all(v) for c, v in self.seg_prune.items()}
        self.flops = {e.cell for e in self.elems if e.kind == "flop"}

    def sim(self, cfg: str, fid: int, dep: dict[int, int] | None = None,
            dump: bool = False) -> simcore.Result:
        pa = self.pa[cfg] + (["+DUMPSTATE"] if dump else [])
        return simcore.run_vvp(self.vvp, pa, fid, dep, timeout=30)


_T: dict = {}


def _init(target: "Target", faults: list[dict], udef: bool):
    _T["udef"] = udef
    _T["t"] = target
    _T["g"] = cs.load_graph(ROOT / "synth" / f"netlist_{target.netlist}.json")
    _T["faults"] = {int(f["id"]): f for f in faults}


def load_faults(netlist: str) -> list[dict]:
    return list(csv.DictReader((ROOT / "faults" / f"{netlist}.csv").open()))


def verdict_for(job: tuple[int, list[str], list[str], int]) -> dict:
    fid, p_cfgs, d_cfgs, max_group = job
    t, g, f = _T["t"], _T["g"], _T["faults"][fid]
    if d_cfgs:
        return {"id": fid, "verdict": "detected", "reason": f"strict D in {d_cfgs}"}
    if not p_cfgs:
        return {"id": fid, "verdict": "escapes", "reason": "U in every config", "witness": {}}
    U, consts = set(), []
    branchable = None  # elements never possibly written in every P config
    out_bits = {g.port_bits[o]: k for k, o in enumerate(t.obs)}
    stuck = {g.net_bit[f["net"]]: int(f["polarity"])}
    for c in p_cfgs:
        r = t.sim(c, fid, dump=True)
        if r.timeout:
            return {"id": fid, "verdict": "timeout"}
        u_c = set(r.never_def_written if _T.get("udef") else r.never_written)
        U |= u_c
        branchable = set(r.never_written) if branchable is None else branchable & set(r.never_written)
        segs = t.segments[c]
        for j in range(len(segs)):
            const = dict(stuck)
            for n in t.held0:
                const[g.port_bits[n]] = 0
            if t.seg_prune[c][j]:
                st = r.seg_state.get(j) if len(segs) > 1 else r.state
                for e in t.elems:
                    v = st.get(e.index)
                    if e.kind == "latch" and v in ("0", "1") and e.index not in u_c:
                        const[g.cells[e.cell]["out"]] = int(v)
            consts.append(const)
    U = sorted(U)
    elem_cells = {e.index: e.cell for e in t.elems}

    def regroup(fixed: dict[int, int]):
        cs_ = []
        for const in consts:
            c2 = dict(const)
            for e, v in fixed.items():
                c2[g.cells[elem_cells[e]]["out"]] = v
            cs_.append(c2)
        rest = [e for e in U if e not in fixed]
        groups, cones = cs.groups_for(g, elem_cells, rest, cs_, set(out_bits),
                                      carry=t.flops if t.sequence else None)
        gpos = [{out_bits[b] for b in cs.group_outputs(g, set().union(*(cones[e] for e in grp)), set(out_bits))}
                for grp in groups]
        epos = {e: {out_bits[b] for b in cs.group_outputs(g, cones[e], set(out_bits))} for e in rest}
        return groups, gpos, epos

    def run(c, dep):
        r = simcore.run_vvp(t.vvp, t.pa_early[c], fid, dep, timeout=30)
        return r.samples

    rec = cs.decide(run, t.golden, p_cfgs, regroup, branchable, max_group)
    groups = regroup({})[0]
    if "detecting_output" in rec:
        rec["detecting_output"] = t.obs[rec["detecting_output"]]
        rec["detecting_elements"] = [t.elems[e].label for e in rec["detecting_elements"]]
    for key in ("fixed",):
        if key in rec:
            rec[key] = {t.elems[e].label: v for e, v in rec[key].items()}
    if "split_on" in rec:
        rec["split_on"] = t.elems[rec["split_on"]].label
    rec.update(id=fid, k=len(U), groups=[[t.elems[e].label for e in grp] for grp in groups])
    if rec["verdict"] == "escapes":
        full = {}
        for e in U:
            full[e] = rec["witness"].get(e, 0)
        det = [c for c in p_cfgs if simcore.status(t.golden[c], t.sim(c, fid, full)) == "D"]
        rec["witness_check"] = "escapes" if not det else f"DETECTED by {det}"
        rec["witness"] = {t.elems[e].label: v for e, v in rec["witness"].items()}
    return rec


def matrix_jobs(netlist: str, ids: set[int] | None, configs: list[str], max_group: int):
    jobs = []
    for row in csv.DictReader((REPORTS / f"{netlist}{TAG}_matrix.csv").open()):
        fid = int(row["id"])
        if ids is not None and fid not in ids:
            continue
        p = [c for c in configs if row.get(c) == "P"]
        d = [c for c in configs if row.get(c) == "D"]
        jobs.append((fid, p, d, max_group))
    return jobs


def configs_of(netlist: str) -> list[str]:
    m = next(csv.DictReader((REPORTS / f"{netlist}{TAG}_matrix.csv").open()))
    return [c for c in m if c not in ("id", "path", "polarity", "type", "any")]


def run_jobs(netlist, jobs, workers, udef: bool):
    ids = {j[0] for j in jobs}
    faults = [f for f in load_faults(netlist) if int(f["id"]) in ids]
    target = Target(netlist, faults, SEQUENCE)   # compiles once, here
    with ProcessPoolExecutor(max_workers=workers, initializer=_init, initargs=(target, faults, udef)) as ex:
        return list(ex.map(verdict_for, jobs))


def full_and_cone(job) -> dict:
    """New-rule validation: U = never definitely written for both methods.

    Full enumeration of all 2^k assignments of U (k <= kmax) over the X-only
    configs (stop at the first escaping assignment), then cone-split with the
    same U; the record carries both verdicts.
    """
    fid, p_cfgs, d_cfgs, max_group, kmax = job
    t = _T["t"]
    if d_cfgs or not p_cfgs:
        return {"id": fid, "trivial": True, **verdict_for((fid, p_cfgs, d_cfgs, max_group))}
    U = set()
    for c in p_cfgs:
        r = t.sim(c, fid)
        if r.timeout:
            return {"id": fid, "skip": "timeout"}
        U |= set(r.never_def_written)
    U = sorted(U)
    if len(U) > kmax:
        return {"id": fid, "skip": f"k = {len(U)} > {kmax}", "k": len(U)}
    full_det, runs = True, 0
    for bits in itertools.product((0, 1), repeat=len(U)):
        dep = dict(zip(U, bits))
        hit = False
        for c in p_cfgs:
            runs += 1
            r = simcore.run_vvp(t.vvp, t.pa_early[c], fid, dep, timeout=30)
            if simcore.status(t.golden[c], r) == "D":
                hit = True
                break
        if not hit:
            full_det = False
            break
    cone = verdict_for((fid, p_cfgs, d_cfgs, max_group))
    return {"id": fid, "k": len(U), "full_detected": full_det, "full_runs": runs,
            "cone": cone["verdict"], "cone_k": cone.get("k"), "group_sizes": cone.get("group_sizes"),
            "witness_check": cone.get("witness_check", ""), "method": cone.get("method")}


def cmd_validate_definite(args) -> int:
    n = args.netlist
    cfgs = configs_of(n)
    jobs = [(fid, p, d, mg, args.kmax) for fid, p, d, mg in matrix_jobs(n, None, cfgs, args.max_group)
            if p]   # every fault with an X-only result in some config
    ids = {j[0] for j in jobs}
    faults = [f for f in load_faults(n) if int(f["id"]) in ids]
    target = Target(n, faults, SEQUENCE)
    with ProcessPoolExecutor(max_workers=args.jobs, initializer=_init,
                             initargs=(target, faults, True)) as ex:
        recs = list(ex.map(full_and_cone, jobs))
    trivial = [r for r in recs if r.get("trivial")]
    skipped = [r for r in recs if r.get("skip")]
    cmp_ = [r for r in recs if not r.get("trivial") and not r.get("skip")]
    agree = [r for r in cmp_ if (r["cone"] == "detected") == r["full_detected"] and r["cone_k"] == r["k"]]
    bad_w = [r for r in cmp_ if str(r["witness_check"]).startswith("DETECTED")]
    esc = [r for r in cmp_ if not r["full_detected"]]
    byid = {int(f["id"]): f for f in faults}
    with (REPORTS / f"{n}{TAG}_conesplit_validate_definite.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "path", "polarity", "k", "full_detected", "conesplit", "agree",
                    "group_sizes", "witness_check", "note"])
        for r in recs:
            fl = byid[r["id"]]
            if r.get("trivial"):
                w.writerow([r["id"], fl["path"], fl["polarity"], "", "", r["verdict"], "", "", "",
                            r.get("reason", "strict")])
            elif r.get("skip"):
                w.writerow([r["id"], fl["path"], fl["polarity"], r.get("k", ""), "", "", "", "", "", r["skip"]])
            else:
                ok = (r["cone"] == "detected") == r["full_detected"] and r["cone_k"] == r["k"]
                w.writerow([r["id"], fl["path"], fl["polarity"], r["k"], r["full_detected"], r["cone"],
                            ok, r["group_sizes"], r["witness_check"], ""])
    big = sorted({r.get("k") for r in skipped if r.get("k")})
    print(f"{n}: {len(recs)} X-only faults; {len(trivial)} decided by strict D; "
          f"{len(skipped)} skipped (k > {args.kmax}: k in {big}; timeouts "
          f"{sum(1 for r in skipped if r['skip'] == 'timeout')}); {len(cmp_)} compared, "
          f"{len(agree)} agree, {len(cmp_) - len(agree)} disagree; {len(esc)} escapes, "
          f"{len(bad_w)} witnesses that do not escape")
    return 1 if len(agree) != len(cmp_) or bad_w else 0


def cmd_validate(args) -> int:
    if args.u_definite:
        return cmd_validate_definite(args)
    n = args.netlist
    ex = json.loads((REPORTS / f"{n}_exhaust.json").read_text())
    cfgs = ex["configs"]
    full = {r["id"]: all(set(f) & set(cfgs) for _, f in r["assignments"])
            for r in ex["faults"] if r["assignments"] is not None}
    jobs = [j for j in matrix_jobs(n, set(full), cfgs, args.max_group)]
    # The full enumerator only reran P configs and counted strict D; drop D
    # here so that cone-split decides from the P configs alone, like it.
    jobs = [(fid, p, [], mg) if not d else (fid, p, d, mg) for fid, p, d, mg in jobs]
    # Same U as the full enumerator (never possibly written), for a like-for-like check.
    recs = run_jobs(n, jobs, args.jobs, udef=False)
    agree = mismatch = bad_witness = 0
    rows = []
    for r in recs:
        cone_det = r["verdict"] == "detected"
        ok = cone_det == full[r["id"]]
        agree += ok
        mismatch += not ok
        wc = r.get("witness_check", "")
        bad_witness += wc.startswith("DETECTED")
        rows.append([r["id"], full[r["id"]], r["verdict"], ok, r.get("k"), r.get("group_sizes"), wc])
    with (REPORTS / f"{n}_conesplit_validate.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "full_detected", "conesplit", "agree", "k", "group_sizes", "witness_check"])
        w.writerows(rows)
    nontriv = sum(1 for j in jobs if not j[2])
    print(f"{n}: {len(recs)} faults compared ({nontriv} with no strict D), {agree} agree, "
          f"{mismatch} disagree, {bad_witness} witnesses that do not escape")
    return 1 if mismatch or bad_witness else 0


def cmd_run(args) -> int:
    n = args.netlist
    pat = re.compile(args.only)
    ids = {int(f["id"]) for f in load_faults(n) if pat.search(f["net"]) and
           (args.pol is None or f["polarity"] == str(args.pol)) and
           (not args.stem or f["type"] == "stem")}
    jobs = matrix_jobs(n, ids, configs_of(n), args.max_group)
    recs = run_jobs(n, jobs, args.jobs, udef=not args.u_possible)
    byid = {int(f["id"]): f for f in load_faults(n)}
    for r in recs:
        r["path"] = byid[r["id"]]["path"]
        r["polarity"] = byid[r["id"]]["polarity"]
    (REPORTS / f"{n}{TAG}_conesplit.json").write_text(json.dumps(recs, indent=1))
    for r in recs:
        print(f"  {r['path']:20s} SA{r['polarity']} k={r.get('k')} -> {r['verdict']}"
              f" groups {r.get('group_sizes')} {r.get('witness_check', '')}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("validate", "run"):
        p = sub.add_parser(c)
        p.add_argument("netlist")
        p.add_argument("--jobs", type=int, default=os.cpu_count())
        p.add_argument("--max-group", type=int, default=16)
        p.add_argument("--sequence", nargs="+", metavar="CONFIG",
                       help="sequence mode (reports/<netlist>_seq_matrix.csv)")
        if c == "validate":
            p.add_argument("--u-definite", action="store_true",
                           help="validate the new rule: full vs cone-split, both with "
                                "U = never definitely written, on all X-only faults with k <= --kmax")
            p.add_argument("--kmax", type=int, default=12)
        if c == "run":
            p.add_argument("--only", required=True)
            p.add_argument("--pol", type=int)
            p.add_argument("--stem", action="store_true")
            p.add_argument("--u-possible", action="store_true",
                           help="U = never possibly written (default: never definitely written)")
    args = ap.parse_args()
    global SEQUENCE, TAG
    if args.sequence:
        SEQUENCE, TAG = args.sequence, "_seq"
    return {"validate": cmd_validate, "run": cmd_run}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
