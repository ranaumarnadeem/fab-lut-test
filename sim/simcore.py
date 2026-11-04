"""Netlist-independent simulation core: compile, run, classify, exhaust.

A testbench (cell or tile) must:
  - `include "monitor.vh"` at module scope,
  - in its main initial block, after applying initial input values and
    `#1`, `include "faults.vh"`, then `#1` and `include "deposit.vh"`,
  - in the step loop after sampling, `include "segdump.vh"` (needs `k`),
  - before `$display("END")`, `include "report.vh"`,
  - declare `integer fault_id, pu_v, k;` and print samples as `S <bits>`.
"""
from __future__ import annotations

import itertools
import subprocess
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import state


def faults_vh(faults: list[dict] | None) -> str:
    if not faults:
        return "\n"
    lines = ["        case (fault_id)"]
    for f in faults:
        lines.append(f"            {f['id']}: force dut.{f['force_ref']} = 1'b{f['polarity']};")
    lines += ["            default: ;", "        endcase"]
    return "\n".join(lines) + "\n"


def compile_tb(build_dir: Path, tb_text: str, sources: list[Path],
               faults: list[dict] | None, elems: list[state.Element],
               replace_models: bool = True, defines: list[str] | None = None) -> Path:
    build_dir.mkdir(parents=True, exist_ok=True)
    (build_dir / "tb.v").write_text(tb_text)
    (build_dir / "faults.vh").write_text(faults_vh(faults))
    (build_dir / "monitor.vh").write_text(state.monitor_vh(elems))
    # The original UDP state is not reachable, so no deposits with those models.
    (build_dir / "deposit.vh").write_text(state.deposit_vh(elems) if replace_models else "\n")
    (build_dir / "report.vh").write_text(state.report_vh(elems, dump=replace_models))
    (build_dir / "segdump.vh").write_text(state.segdump_vh(elems, dump=replace_models))
    srcs = state.model_sources(sources, build_dir / "models") if replace_models else sources
    vvp = build_dir / "sim.vvp"
    cmd = ["iverilog", "-g2005", "-DUNIT_DELAY=", *(defines or []), "-o", str(vvp),
           "-I", str(build_dir), str(build_dir / "tb.v"), *map(str, srcs)]
    subprocess.run(cmd, check=True)
    return vvp


@dataclass
class Result:
    samples: list[str]
    never_written: frozenset[int] = field(default_factory=frozenset)       # never possibly written
    never_def_written: frozenset[int] = field(default_factory=frozenset)   # never definitely written
    state: dict[int, str] = field(default_factory=dict)  # +DUMPSTATE: element -> value
    seg_state: dict[int, dict[int, str]] = field(default_factory=dict)  # +SEGEND<j>: j -> element -> value
    early_detect: bool = False   # testbench stopped at a definite mismatch (+EARLY)
    timeout: bool = False        # no END within the time limit (e.g. an oscillating loop)


def run_vvp(vvp: Path, plusargs: list[str], fault_id: int | None = None,
            deposits: dict[int, int] | None = None, timeout: float | None = None) -> Result:
    cmd = ["vvp", "-n", str(vvp), *plusargs]
    if fault_id is not None:
        cmd.append(f"+FAULT={fault_id}")
    for i, v in (deposits or {}).items():
        cmd.append(f"+PU{i}={v}")
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return Result(samples=[], timeout=True)
    lines = res.stdout.splitlines()
    if "DET" in lines:
        return Result(samples=[ln[2:] for ln in lines if ln.startswith("S ")], early_detect=True)
    if "END" not in lines:
        raise RuntimeError(f"simulation did not finish: {' '.join(cmd)}\n{res.stdout}{res.stderr}")
    return Result(
        samples=[ln[2:] for ln in lines if ln.startswith("S ")],
        never_written=frozenset(int(ln[3:]) for ln in lines if ln.startswith("NW ")),
        never_def_written=frozenset(int(ln[3:]) for ln in lines if ln.startswith("ND ")),
        state={int(ln.split()[1]): ln.split()[2] for ln in lines if ln.startswith("ST ")},
        seg_state=_seg(lines),
    )


def _seg(lines: list[str]) -> dict[int, dict[int, str]]:
    out: dict[int, dict[int, str]] = {}
    for ln in lines:
        if ln.startswith("SG "):
            _, j, i, v = ln.split()
            out.setdefault(int(j), {})[int(i)] = v
    return out


def status(golden: list[str], r: Result) -> str:
    """D / P / U, plus T for a run that timed out (counted as not detected)."""
    if r.early_detect:
        return "D"
    if r.timeout:
        return "T"
    return classify(golden, r.samples)


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


# ---------------------------------------------------------------- exhaust

_X: dict = {}


def _init_exhaust(vvp: str, plusargs: dict, golden: dict, kmax: int,
                  timeout: float | None = None) -> None:
    _X.update(vvp=Path(vvp), plusargs=plusargs, golden=golden, kmax=kmax, timeout=timeout)


def exhaust_fault(job: tuple[int, list[str], list[str]]) -> dict:
    """Enumerate the never-written elements of one fault.

    job = (fault_id, configs with status P, configs with status D)
    Strict D stays D for every assignment and U stays U (the simulation is
    monotone in X), so only the P configs are rerun.
    """
    fid, p_cfgs, d_cfgs = job
    vvp, pa, golden = _X["vvp"], _X["plusargs"], _X["golden"]
    tmo = _X.get("timeout")
    nw_per_cfg = {}
    for c in p_cfgs:
        r = run_vvp(vvp, [a for a in pa[c] if a != "+EARLY"], fid, timeout=tmo)
        if r.timeout:
            return {"id": fid, "elements": [], "k": None, "consistent": None,
                    "p_configs": p_cfgs, "assignments": None, "timeout": True}
        nw_per_cfg[c] = r.never_written
    elems = sorted(set().union(*nw_per_cfg.values())) if nw_per_cfg else []
    consistent = len({v for v in nw_per_cfg.values()}) <= 1
    out = {"id": fid, "elements": elems, "k": len(elems), "consistent": consistent,
           "p_configs": p_cfgs, "assignments": None, "timeout": False}
    if len(elems) > _X["kmax"]:
        return out
    det = []
    for bits in itertools.product((0, 1), repeat=len(elems)):
        dep = dict(zip(elems, bits))
        found = set(d_cfgs)
        for c in p_cfgs:
            r = run_vvp(vvp, pa[c], fid, dep, timeout=tmo)
            if status(golden[c], r) == "D":
                found.add(c)
        det.append(("".join(map(str, bits)), sorted(found)))
    out["assignments"] = det
    return out


def exhaust(vvp: Path, plusargs: dict[str, list[str]], golden: dict[str, list[str]],
            jobs: list[tuple[int, list[str], list[str]]], kmax: int, workers: int,
            timeout: float | None = None) -> list[dict]:
    with ProcessPoolExecutor(max_workers=workers, initializer=_init_exhaust,
                             initargs=(str(vvp), plusargs, golden, kmax, timeout)) as ex:
        return list(ex.map(exhaust_fault, jobs))


def covered_exhaustive(rec: dict, subset: set[str]) -> bool | None:
    """True/False for an enumerated fault, None if k exceeded kmax."""
    if rec["assignments"] is None:
        return None
    return all(subset & set(found) for _, found in rec["assignments"])
