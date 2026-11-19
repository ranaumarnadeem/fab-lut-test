#!/usr/bin/env python3
"""Validation fabrics: the FABulous demo project with another fabric layout.

  python3 tests/fabrics/make.py F1 [F2 ...] [--demo $FABTEST_DEMO] [--out build/fabrics]

<name>.csv here holds the FabricBegin .. FabricEnd rows (tile names). The demo project is
copied to build/fabrics/<name> (tiles, fabric-level RTL, parameters unchanged), its
fabric.csv gets the new layout, and FABulous run_fab generates eFPGA.v, eFPGA_top.v and
the bitstream spec. The tile RTL is the demo's (same tile types).

  F1  4 x 5: W_IO, 3 x LUT4AB, RAM_IO with one BlockRAM (small: exhaustive fault simulation)
  F2  6 x 5: RegFile next to W_IO and at the east edge, no DSP, no BlockRAM
  F3  5 x 5, MultiplexerStyle generic (behavioural switch-matrix muxes), a CUST column
      whose BEL (F3/ALU4.v) no plugin knows: it must come out opaque
  F4  7 x 8, uneven: a column alternating RegFile / LUT4AB, a RegFile column, DSP and
      RAM_IO (3 BlockRAMs) at the east edge; the DSP is 2 tiles from the nearest LUT,
      so its pinned MULADD inputs take constants at the const_radius limit
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))
from fabtest.paths import DEMO  # noqa: E402
ROOT = HERE.parents[1]
SKIP = shutil.ignore_patterns(".git", "Test", "user_design", "__pycache__", "*.json", "bitStreamSpec.*", "*.vcd", "*.fst")


def fabulous() -> str:
    for c in (os.environ.get("FABULOUS"), "/home/user/ext/venv/bin/FABulous", shutil.which("FABulous")):
        if c and Path(c).exists():
            return c
    raise SystemExit("make.py: FABulous not found (set FABULOUS=...)")


def _param(csv: list[str], key: str, value: str) -> list[str]:
    i = next(i for i, l in enumerate(csv) if l.split(",")[0] == key)
    f = csv[i].split(",")
    f[1] = value
    return csv[:i] + [",".join(f)] + csv[i + 1:]


def _f3(dst: Path, csv: list[str]) -> list[str]:
    """Generic mux style; tile CUST = RegFile with the ALU4 BEL."""
    src, t = dst / "Tile" / "RegFile", dst / "Tile" / "CUST"
    t.mkdir()
    shutil.copy(HERE / "F3" / "ALU4.v", t / "ALU4.v")
    lst = (src / "RegFile_switch_matrix.list").read_text().replace("# RegFile", "# CUST (RegFile wiring)", 1)
    (t / "CUST_switch_matrix.list").write_text(lst)
    (t / "CUST.csv").write_text((src / "RegFile.csv").read_text().replace("TILE,RegFile", "TILE,CUST")
                                .replace("./RegFile_32x4.v", "./ALU4.v").replace("RegFile_", "CUST_"))
    csv = _param(csv, "MultiplexerStyle", "generic")
    i = next(i for i, l in enumerate(csv) if l.startswith("Tile,./Tile/RegFile/"))
    return csv[:i + 1] + ["Tile,./Tile/CUST/CUST.csv" + "," * (csv[i].count(","))] + csv[i + 1:]


EXTRA = {"F3": _f3}


def make(name: str, demo: Path, out: Path) -> Path:
    layout = (HERE / f"{name}.csv").read_text().strip().splitlines()
    dst = out / name
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(demo, dst, ignore=SKIP)
    (dst / "user_design").mkdir(exist_ok=True)
    csv = (dst / "fabric.csv").read_text().splitlines()
    a = next(i for i, l in enumerate(csv) if l.startswith("FabricBegin"))
    b = next(i for i, l in enumerate(csv) if l.startswith("FabricEnd"))
    width = max(len(l.split(",")) for l in layout)
    rows = [l + "," * (width - len(l.split(","))) for l in layout]
    csv = csv[:a + 1] + rows + csv[b:]
    if name in EXTRA:
        csv = EXTRA[name](dst, csv)
    (dst / "fabric.csv").write_text("\n".join(csv) + "\n")
    for f in (dst / "Fabric").glob("eFPGA*.v"):
        if f.name in ("eFPGA.v", "eFPGA_top.v"):
            f.unlink()
    r = subprocess.run([fabulous(), "-p", str(dst), "run", "load_fabric; run_fab"], capture_output=True, text=True)
    log = dst / "run_fab.log"
    log.write_text(r.stdout + r.stderr)
    if r.returncode != 0 or not (dst / "Fabric" / "eFPGA.v").exists() or not (dst / ".FABulous" / "bitStreamSpec.bin").exists():
        sys.stderr.write((r.stdout + r.stderr)[-3000:])
        raise SystemExit(f"make.py: FABulous failed for {name} (see {log})")
    print(f"make.py: {name} -> {dst}")
    return dst


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="+")
    ap.add_argument("--demo", default=str(DEMO))
    ap.add_argument("--out", default=str(ROOT / "build" / "fabrics"))
    a = ap.parse_args()
    for n in a.names:
        make(n, Path(a.demo), Path(a.out))


if __name__ == "__main__":
    main()
