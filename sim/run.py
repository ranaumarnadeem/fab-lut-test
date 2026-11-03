#!/usr/bin/env python3
"""Simulation runner: equivalence checks and stuck-at fault simulation.

  python3 sim/run.py check
      RTL, netlist S and netlist G, fault free, every config: all samples
      must match the Python cell model (and therefore each other).

  python3 sim/run.py faultsim <netlist> [--jobs N] [--configs A B ...]
      netlist: sky130, generic, or sky130_br / generic_br (stem + branch)
      One vvp run per (fault, config). A fault is forced from t = 1 ns (before
      the first step, see tb_template.v) for the
      whole config. Writes reports/<netlist>_matrix.csv, _coverage.csv,
      _undetected.txt.

  python3 sim/run.py exhaust <netlist> [--configs ...] [--kmax 12]
      For every fault with status P in some config: find the storage
      elements never written under the fault, enumerate all 2^k initial
      values of exactly those (everything else stays X), rerun the P configs.
      Writes reports/<netlist>_exhaust[--suffix].csv / .json.

Detection: D if some sample has 0/1 in both runs and they differ; P
(potential) if the only differences involve X/Z; U otherwise.
All runs use the reg-based latch/flop models of sim/seq_models.v (see
sim/state.py); `check` also runs the original models for comparison.
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
sys.path.insert(0, str(ROOT / "configs"))
sys.path.insert(0, str(ROOT / "sim"))
import configs as cfgmod  # noqa: E402
import simcore  # noqa: E402
import state  # noqa: E402

BUILD = ROOT / "build"
STIM_DIR = BUILD / "stim"
REPORTS = ROOT / "reports"

TARGETS = {
    "rtl": ["rtl/models_pack.v", "rtl/LUT4c_frame_config_dffesr.v", "rtl/lut_cell_top.v"],
    "sky130": ["synth/netlist_sky130.v", "synth/sky130_cells.v"],
    "generic": ["synth/netlist_generic.v", "synth/simcells.v"],
    # Branch-fault variants (synth/insbuf_branches.py)
    "sky130_br": ["synth/netlist_sky130_br.v", "synth/sky130_cells.v", "synth/branch_buf.v"],
    "generic_br": ["synth/netlist_generic_br.v", "synth/simcells.v"],
}
NETLISTS = ["sky130", "generic", "sky130_br", "generic_br"]

# One fault per config that the config is designed to catch.
SANITY = {
    "A": ("ConfigBits[1]", 0),   # XOR table bit 1 is 1
    "B": ("ConfigBits[0]", 0),   # XNOR table bit 0 is 1
    "A2": ("ConfigBits[1]", 0),
    "C": ("Ci", 0),              # I0 taken from Ci
    "D0": ("SR", 0),             # rv never loaded
    "D1": ("EN", 1),             # EN=0 hold fails
    "BC": ("ConfigBits[0]", 0),  # XNOR bit 0 read at I=0000, Ci=0
    "BC1": ("ConfigBits[0]", 0),
    "D0C": ("SR", 0),
}


def write_stimuli(names: list[str]) -> dict[str, tuple[Path, int, list[str]]]:
    STIM_DIR.mkdir(parents=True, exist_ok=True)
    out = {}
    for name in names:
        seq = cfgmod.CONFIGS[name]()
        stim = STIM_DIR / f"{name}.stim"
        seq.write_stim(stim)
        exp = [
            ("x" if o is None else str(o)) + ("x" if c is None else str(c))
            for o, c in cfgmod.cell_model(seq)
        ]
        out[name] = (stim, len(seq.steps), exp)
    return out


def load_faults(netlist: str) -> list[dict]:
    path = ROOT / "faults" / f"{netlist}.csv"
    return list(csv.DictReader(path.open()))


def netlist_json(target: str) -> Path | None:
    return None if target == "rtl" else ROOT / "synth" / f"netlist_{target}.json"


def compile_target(target: str, faults: list[dict] | None, original_models: bool = False) -> Path:
    fd = [m.frame_data_bit for m in cfgmod.BITMAP]
    fs = [m.frame_strobe for m in cfgmod.BITMAP]
    tb = (ROOT / "sim" / "tb_template.v").read_text()
    for k, v in {"FD_HI": max(fd), "FD_LO": min(fd), "FS_HI": max(fs), "FS_LO": min(fs)}.items():
        tb = tb.replace(f"@{k}@", str(v))
    js = netlist_json(target)
    elems = state.storage_elements(js) if js else []
    d = BUILD / "sim" / (f"{target}_orig" if original_models else target)
    return simcore.compile_tb(d, tb, [ROOT / s for s in TARGETS[target]], faults, elems,
                              replace_models=not (original_models or target == "rtl"))


def stim_args(stim: Path, nsteps: int) -> list[str]:
    return [f"+STIM={stim}", f"+NSTEPS={nsteps}"]


def run_vvp(vvp: Path, stim: Path, nsteps: int, fault_id: int | None = None) -> list[str]:
    return simcore.run_vvp(vvp, stim_args(stim, nsteps), fault_id).samples


classify = simcore.classify


# ---------------------------------------------------------------- check

def cmd_check(args: argparse.Namespace) -> int:
    stims = write_stimuli(list(cfgmod.CONFIGS))
    ok = True
    variants = [(t, False) for t in TARGETS] + [(t, True) for t in TARGETS if t != "rtl"]
    for target, orig in variants:
        vvp = compile_target(target, None, original_models=orig)
        label = target + (" (orig models)" if orig else "")
        for name, (stim, n, exp) in stims.items():
            got = run_vvp(vvp, stim, n)
            status = "PASS" if got == exp else "FAIL"
            ok &= got == exp
            print(f"{label:24s} {name:4s} {status} ({len(got)} samples)")
            if got != exp:
                for k, (g, e) in enumerate(zip(got, exp)):
                    if g != e:
                        print(f"    sample {k}: got {g} expected {e}")
    print("ALL PASS" if ok else "FAILURES")
    return 0 if ok else 1


# ---------------------------------------------------------------- faultsim

_W: dict = {}


def _init_worker(vvp: str, stims: dict, golden: dict) -> None:
    _W.update(vvp=Path(vvp), stims=stims, golden=golden)


def _sim_fault(fault_id: int) -> tuple[int, dict[str, str]]:
    res = {}
    for name, (stim, n, _) in _W["stims"].items():
        faulty = run_vvp(_W["vvp"], Path(stim), n, fault_id)
        res[name] = classify(_W["golden"][name], faulty)
    return fault_id, res


def cmd_faultsim(args: argparse.Namespace) -> int:
    netlist = args.netlist
    names = args.configs or cfgmod.TEST_CONFIGS
    stims = write_stimuli(names)
    faults = load_faults(netlist)
    vvp = compile_target(netlist, faults)

    golden = {}
    for name, (stim, n, exp) in stims.items():
        g = run_vvp(vvp, stim, n)
        if g != exp:
            raise SystemExit(f"{netlist} {name}: fault-free run differs from model")
        if any(c not in "01" for s in g for c in s):
            raise SystemExit(f"{netlist} {name}: golden run has X/Z samples")
        if run_vvp(vvp, stim, n, -1) != g:
            raise SystemExit(f"{netlist} {name}: FAULT=-1 run differs from golden")
        golden[name] = g
    print(f"{netlist}: golden OK for {names}; {len(faults)} faults x {len(names)} configs")

    stims_s = {k: (str(v[0]), v[1], v[2]) for k, v in stims.items()}
    results: dict[int, dict[str, str]] = {}
    with ProcessPoolExecutor(
        max_workers=args.jobs, initializer=_init_worker, initargs=(str(vvp), stims_s, golden)
    ) as ex:
        for k, (fid, res) in enumerate(ex.map(_sim_fault, [int(f["id"]) for f in faults], chunksize=4)):
            results[fid] = res
            if (k + 1) % 50 == 0:
                print(f"  {k + 1}/{len(faults)}")

    # Sanity: hand-picked fault per config must be detected.
    by_key = {(f["net"], int(f["polarity"])): int(f["id"]) for f in faults}
    sanity_ok = True
    for name in names:
        if name not in SANITY:
            continue
        key = SANITY[name]
        if key not in by_key:
            raise SystemExit(f"sanity fault {key} not in {netlist} fault list")
        st = results[by_key[key]][name]
        print(f"  sanity {name}: {key[0]} SA{key[1]} -> {st}")
        sanity_ok &= st == "D"
    if not sanity_ok:
        raise SystemExit("sanity check failed")

    REPORTS.mkdir(exist_ok=True)
    with (REPORTS / f"{netlist}_matrix.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "path", "polarity", "type", *names, "any"])
        for flt in faults:
            r = results[int(flt["id"])]
            anyd = "D" if "D" in r.values() else ("P" if "P" in r.values() else "U")
            w.writerow([flt["id"], flt["path"], flt["polarity"], flt["type"],
                        *[r[n] for n in names], anyd])

    total = len(faults)
    cum: set[int] = set()
    with (REPORTS / f"{netlist}_coverage.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["config", "detected", "potential", "total", "coverage_pct",
                    "cumulative_detected", "cumulative_pct"])
        for n in names:
            det = {fid for fid, r in results.items() if r[n] == "D"}
            pot = sum(1 for r in results.values() if r[n] == "P")
            cum |= det
            w.writerow([n, len(det), pot, total, f"{100 * len(det) / total:.2f}",
                        len(cum), f"{100 * len(cum) / total:.2f}"])
            print(f"  {n:3s} {len(det):4d}/{total} = {100 * len(det) / total:6.2f}%   "
                  f"cumulative {len(cum):4d} = {100 * len(cum) / total:6.2f}%")

    with (REPORTS / f"{netlist}_undetected.txt").open("w") as f:
        f.write(f"# {netlist}: faults not detected by any of {names}\n")
        f.write("# id\tnet\tSA\tstatus (P = only X differences)\n")
        for flt in faults:
            r = results[int(flt["id"])]
            if "D" not in r.values():
                st = "P" if "P" in r.values() else "U"
                f.write(f"{flt['id']}\t{flt['net']}\tSA{flt['polarity']}\t{st}\n")
    return 0


# ---------------------------------------------------------------- exhaust

def cmd_exhaust(args: argparse.Namespace) -> int:
    netlist = args.netlist
    matrix = list(csv.DictReader((REPORTS / f"{netlist}_matrix.csv").open()))
    all_cfgs = [c for c in matrix[0] if c not in ("id", "path", "polarity", "type", "any")]
    names = args.configs or all_cfgs
    stims = write_stimuli(names)
    faults = load_faults(netlist)
    vvp = compile_target(netlist, faults)
    golden, pa = {}, {}
    for name, (stim, n, exp) in stims.items():
        pa[name] = stim_args(stim, n)
        r = simcore.run_vvp(vvp, pa[name])
        if r.samples != exp:
            raise SystemExit(f"{netlist} {name}: golden differs from model")
        if r.never_written:
            raise SystemExit(f"{netlist} {name}: fault-free run leaves elements {sorted(r.never_written)} unwritten")
        golden[name] = r.samples
    jobs = []
    for row in matrix:
        p = [c for c in names if row[c] == "P"]
        if p:
            jobs.append((int(row["id"]), p, [c for c in names if row[c] == "D"]))
    print(f"{netlist}: {len(jobs)} faults with P status in {names}")
    elems = state.storage_elements(netlist_json(netlist))
    recs = simcore.exhaust(vvp, pa, golden, jobs, args.kmax, args.jobs)
    by_id = {r["id"]: r for r in matrix}
    for rec in recs:
        row = by_id[str(rec["id"])]
        rec.update(path=row["path"], polarity=row["polarity"], type=row["type"],
                   element_labels=[elems[i].label for i in rec["elements"]])
    sfx = args.suffix
    (REPORTS / f"{netlist}_exhaust{sfx}.json").write_text(json.dumps(
        {"configs": names, "kmax": args.kmax, "faults": recs}, indent=1))
    with (REPORTS / f"{netlist}_exhaust{sfx}.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "path", "polarity", "type", "k", "elements", "consistent_across_configs",
                    "assignments", "detected_for_all_assignments", "failing_assignments"])
        for rec in recs:
            if rec["assignments"] is None:
                status, fails = f"not enumerated (k > {args.kmax})", ""
            else:
                bad = [a for a, found in rec["assignments"] if not set(found) & set(names)]
                status, fails = ("yes" if not bad else "no"), " ".join(bad)
            w.writerow([rec["id"], rec["path"], rec["polarity"], rec["type"], rec["k"],
                        " ".join(rec["element_labels"]), rec["consistent"],
                        2 ** rec["k"] if rec["assignments"] is not None else "", status, fails])
            print(f"  {rec['path']:34s} SA{rec['polarity']} k={rec['k']:2d} -> {status}"
                  + (f" fails {fails}" if fails else ""))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    for cmd in ("faultsim", "exhaust"):
        p = sub.add_parser(cmd)
        p.add_argument("netlist", choices=NETLISTS)
        p.add_argument("--jobs", type=int, default=os.cpu_count())
        p.add_argument("--configs", nargs="*")
        if cmd == "exhaust":
            p.add_argument("--kmax", type=int, default=12)
            p.add_argument("--suffix", default="", help="output name suffix, e.g. _rec")
    args = ap.parse_args()
    return {"check": cmd_check, "faultsim": cmd_faultsim, "exhaust": cmd_exhaust}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
