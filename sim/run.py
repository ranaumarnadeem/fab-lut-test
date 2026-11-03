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

Detection: D if some sample has 0/1 in both runs and they differ; P
(potential) if the only differences involve X/Z; U otherwise.
"""
from __future__ import annotations

import argparse
import csv
import os
import re
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


SIMCELL_REG_Q = re.compile(r"^(\s*)(output\s+reg\s+Q\s*;)\s*$", re.M)
UDP_BLOCK = re.compile(r"^primitive\s+(\S+)\s*\(.*?^endprimitive", re.M | re.S)

# Behavioral stand-ins for the sky130 sequential UDPs, power-up pass only.
# The UDP tables turn a held 1 into x while GATE is still x at t = 0 and D has
# already settled to 0, so an `initial` value would not survive. These models
# act only on a defined enable / clock edge, like yosys simcells.
SKY130_SEQ_UDP = {
    "sky130_fd_sc_hd__udp_dlatch$P": (
        "module sky130_fd_sc_hd__udp_dlatch$P (Q, D, GATE);\n"
        "    output reg Q;\n    input D, GATE;\n"
        "    initial Q = `PWRUP_INIT;\n"
        "    always @* if (GATE === 1'b1) Q = D;\n"
        "endmodule"
    ),
    "sky130_fd_sc_hd__udp_dff$P": (
        "module sky130_fd_sc_hd__udp_dff$P (Q, D, CLK);\n"
        "    output reg Q;\n    input D, CLK;\n"
        "    initial Q = `PWRUP_INIT;\n"
        "    always @(posedge CLK) Q <= D;\n"
        "endmodule"
    ),
}


def _pwrup_sky130(text: str) -> str:
    def repl(m: re.Match) -> str:
        name, body = m.group(1), m.group(0)
        if name in SKY130_SEQ_UDP:
            return SKY130_SEQ_UDP[name]
        if re.search(r"^\s*reg\s+Q\s*;", body, re.M):
            raise SystemExit(f"sequential UDP {name} has no power-up stand-in")
        return body

    new = UDP_BLOCK.sub(repl, text)
    if new == text:
        raise SystemExit("sky130_cells.v: no sequential UDP replaced")
    return new


def pwrup_sources(target: str, d: Path) -> list[Path]:
    """Copies of the netlist's cell models where every latch and flop starts
    at `PWRUP_INIT` instead of x (sky130: sequential UDPs replaced, see
    SKY130_SEQ_UDP; generic: `initial Q` added to every `output reg Q`)."""
    out = []
    for s in TARGETS[target]:
        src = ROOT / s
        text = src.read_text()
        if s.endswith("sky130_cells.v"):
            text = _pwrup_sky130(text)
        elif s.endswith("simcells.v"):
            text, n = SIMCELL_REG_Q.subn(r"\1\2\n\1initial Q = `PWRUP_INIT;", text)
            if n == 0:
                raise SystemExit(f"{s}: no state element found to initialise")
        else:
            out.append(src)
            continue
        dst = d / src.name
        dst.write_text(text)
        out.append(dst)
    return out


def compile_target(target: str, faults: list[dict] | None, pwrup: int | None = None) -> Path:
    d = BUILD / "sim" / (target if pwrup is None else f"{target}_pwrup{pwrup}")
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
    if pwrup is None:
        cmd += [str(ROOT / s) for s in TARGETS[target]]
    else:
        cmd += [f"-DPWRUP_INIT=1'b{pwrup}"] + [str(p) for p in pwrup_sources(target, d)]
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
        f.write(f"# {netlist}: faults not detected by any of {names}\n")
        f.write("# id\tnet\tSA\tstatus (P = only X differences)\n")
        for flt in faults:
            r = results[int(flt["id"])]
            if "D" not in r.values():
                st = "P" if "P" in r.values() else "U"
                f.write(f"{flt['id']}\t{flt['net']}\tSA{flt['polarity']}\t{st}\n")
    return 0


# ---------------------------------------------------------------- xresolve

def cmd_xresolve(args: argparse.Namespace) -> int:
    """Rerun faults not detected (D) under all-0 and all-1 power-up states.

    A real chip powers up with every latch and flop at 0 or 1, never X. A
    fault counts as resolved when, for both all-0 and all-1, at least one
    config of the set detects it. This is evidence for two states only, not
    a proof over all 2^20 power-up states (see summary.md for the argument).
    """
    netlist = args.netlist
    matrix = list(csv.DictReader((REPORTS / f"{netlist}_matrix.csv").open()))
    names = [c for c in matrix[0] if c not in ("id", "path", "polarity", "type", "any")]
    todo = [r for r in matrix if r["any"] != "D"]
    faults = {int(f["id"]): f for f in load_faults(netlist)}
    stims = write_stimuli(names)
    stims_s = {k: (str(v[0]), v[1], v[2]) for k, v in stims.items()}

    per_init: dict[int, dict[int, dict[str, str]]] = {}
    for init in (0, 1):
        vvp = compile_target(netlist, list(faults.values()), pwrup=init)
        golden = {}
        for name, (stim, n, exp) in stims.items():
            g = run_vvp(vvp, stim, n)
            if g != exp:
                raise SystemExit(f"{netlist} pwrup{init} {name}: golden differs from model")
            golden[name] = g
        with ProcessPoolExecutor(
            max_workers=args.jobs, initializer=_init_worker, initargs=(str(vvp), stims_s, golden)
        ) as ex:
            per_init[init] = dict(ex.map(_sim_fault, [int(r["id"]) for r in todo]))

    out = REPORTS / f"{netlist}_xresolve.csv"
    resolved = 0
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "path", "polarity", "status_3valued",
                    "detecting_configs_pwrup0", "detecting_configs_pwrup1", "resolved"])
        for r in todo:
            fid = int(r["id"])
            d0 = [n for n in names if per_init[0][fid][n] == "D"]
            d1 = [n for n in names if per_init[1][fid][n] == "D"]
            ok = bool(d0) and bool(d1)
            resolved += ok
            w.writerow([fid, r["path"], r["polarity"], r["any"], " ".join(d0), " ".join(d1),
                        "yes" if ok else "no"])
            print(f"  {r['path']:18s} SA{r['polarity']}  pwrup0: {' '.join(d0) or '-':14s} "
                  f"pwrup1: {' '.join(d1) or '-':14s} {'resolved' if ok else 'NOT resolved'}")
    total = len(matrix)
    strict = total - len(todo)
    print(f"{netlist}: strict {strict}/{total} = {100 * strict / total:.2f}%, "
          f"with power-up resolution {strict + resolved}/{total} = "
          f"{100 * (strict + resolved) / total:.2f}%")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    for cmd in ("faultsim", "xresolve"):
        p = sub.add_parser(cmd)
        p.add_argument("netlist", choices=NETLISTS)
        p.add_argument("--jobs", type=int, default=os.cpu_count())
        if cmd == "faultsim":
            p.add_argument("--configs", nargs="*")
    args = ap.parse_args()
    return {"check": cmd_check, "faultsim": cmd_faultsim, "xresolve": cmd_xresolve}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
