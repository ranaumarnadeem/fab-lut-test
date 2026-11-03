#!/usr/bin/env python3
"""Simulation runner: equivalence checks and stuck-at fault simulation.

  python3 sim/run.py check
      RTL, netlist S and netlist G, fault free, every config: all samples
      must match the Python cell model (and therefore each other).

  python3 sim/run.py faultsim sky130|generic [--jobs N] [--configs A B ...]
      One vvp run per (fault, config). A fault is forced from time 0 for the
      whole config. Writes reports/<netlist>_matrix.csv, _coverage.csv,
      _undetected.txt.

Detection: D if some sample has 0/1 in both runs and they differ; P
(potential) if the only differences involve X/Z; U otherwise.
"""
from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "configs"))
import configs as cfgmod  # noqa: E402

BUILD = ROOT / "build"
STIM_DIR = BUILD / "stim"
REPORTS = ROOT / "reports"

TARGETS = {
    "rtl": ["rtl/models_pack.v", "rtl/LUT4c_frame_config_dffesr.v", "rtl/lut_cell_top.v"],
    "sky130": ["synth/netlist_sky130.v", "synth/sky130_cells.v"],
    "generic": ["synth/netlist_generic.v", "synth/simcells.v"],
}

# One fault per config that the config is designed to catch.
SANITY = {
    "A": ("ConfigBits[1]", 0),   # XOR table bit 1 is 1
    "B": ("ConfigBits[0]", 0),   # XNOR table bit 0 is 1
    "A2": ("ConfigBits[1]", 0),
    "C": ("Ci", 0),              # I0 taken from Ci
    "D0": ("SR", 0),             # rv never loaded
    "D1": ("EN", 1),             # EN=0 hold fails
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
    return [r for r in csv.DictReader(path.open()) if r["type"] == "stem"]


def compile_target(target: str, faults: list[dict] | None) -> Path:
    d = BUILD / "sim" / target
    d.mkdir(parents=True, exist_ok=True)
    fd = [m.frame_data_bit for m in cfgmod.BITMAP]
    fs = [m.frame_strobe for m in cfgmod.BITMAP]
    tb = (ROOT / "sim" / "tb_template.v").read_text()
    for k, v in {"FD_HI": max(fd), "FD_LO": min(fd), "FS_HI": max(fs), "FS_LO": min(fs)}.items():
        tb = tb.replace(f"@{k}@", str(v))
    (d / "tb.v").write_text(tb)
    lines = []
    if faults:
        lines.append("        case (fault_id)")
        for f in faults:
            lines.append(f"            {f['id']}: force dut.{f['force_ref']} = 1'b{f['polarity']};")
        lines.append("            default: ;")
        lines.append("        endcase")
    (d / "faults.vh").write_text("\n".join(lines) + "\n")
    vvp = d / "sim.vvp"
    cmd = ["iverilog", "-g2005", "-DUNIT_DELAY=", "-o", str(vvp), "-I", str(d), str(d / "tb.v")]
    cmd += [str(ROOT / s) for s in TARGETS[target]]
    subprocess.run(cmd, check=True, cwd=ROOT)
    return vvp


def run_vvp(vvp: Path, stim: Path, nsteps: int, fault_id: int | None = None) -> list[str]:
    cmd = ["vvp", "-n", str(vvp), f"+STIM={stim}", f"+NSTEPS={nsteps}"]
    if fault_id is not None:
        cmd.append(f"+FAULT={fault_id}")
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    lines = res.stdout.splitlines()
    if "END" not in lines:
        raise RuntimeError(f"simulation did not finish: {' '.join(cmd)}\n{res.stdout}{res.stderr}")
    return [ln[2:] for ln in lines if ln.startswith("S ")]


def classify(golden: list[str], faulty: list[str]) -> str:
    if len(golden) != len(faulty):
        raise RuntimeError("sample count mismatch")
    potential = False
    for g, f in zip(golden, faulty):
        for gb, fb in zip(g, f):
            if gb in "01" and fb in "01" and gb != fb:
                return "D"
            if gb != fb:
                potential = True
    return "P" if potential else "U"


# ---------------------------------------------------------------- check

def cmd_check(args: argparse.Namespace) -> int:
    stims = write_stimuli(list(cfgmod.CONFIGS))
    ok = True
    for target in TARGETS:
        vvp = compile_target(target, None)
        for name, (stim, n, exp) in stims.items():
            got = run_vvp(vvp, stim, n)
            status = "PASS" if got == exp else "FAIL"
            ok &= got == exp
            print(f"{target:8s} {name:4s} {status} ({len(got)} samples)")
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
        f.write(f"# {netlist}: stem faults not detected by any of {names}\n")
        f.write("# id\tnet\tSA\tstatus (P = only X differences)\n")
        for flt in faults:
            r = results[int(flt["id"])]
            if "D" not in r.values():
                st = "P" if "P" in r.values() else "U"
                f.write(f"{flt['id']}\t{flt['net']}\tSA{flt['polarity']}\t{st}\n")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    fs = sub.add_parser("faultsim")
    fs.add_argument("netlist", choices=["sky130", "generic"])
    fs.add_argument("--jobs", type=int, default=os.cpu_count())
    fs.add_argument("--configs", nargs="*")
    args = ap.parse_args()
    return {"check": cmd_check, "faultsim": cmd_faultsim}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
