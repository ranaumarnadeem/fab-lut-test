#!/usr/bin/env python3
"""Validation fabrics: the FABulous demo project with another fabric layout.

  python3 tests/fabrics/make.py F1 [F2 ...] [--demo /home/user/ext/demo] [--out build/fabrics]

<name>.csv here holds the FabricBegin .. FabricEnd rows (tile names). The demo project is
copied to build/fabrics/<name> (tiles, fabric-level RTL, parameters unchanged), its
fabric.csv gets the new layout, and FABulous run_fab generates eFPGA.v, eFPGA_top.v and
the bitstream spec. The tile RTL is the demo's (same tile types).

  F1  4 x 5: W_IO, 3 x LUT4AB, RAM_IO with one BlockRAM (small: exhaustive fault simulation)
  F2  6 x 5: RegFile next to W_IO and at the east edge, no DSP, no BlockRAM
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SKIP = shutil.ignore_patterns(".git", "Test", "user_design", "__pycache__", "*.json", "bitStreamSpec.*", "*.vcd", "*.fst")


def fabulous() -> str:
    for c in (os.environ.get("FABULOUS"), "/home/user/ext/venv/bin/FABulous", shutil.which("FABulous")):
        if c and Path(c).exists():
            return c
    raise SystemExit("make.py: FABulous not found (set FABULOUS=...)")


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
    (dst / "fabric.csv").write_text("\n".join(csv[:a + 1] + rows + csv[b:]) + "\n")
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
    ap.add_argument("--demo", default="/home/user/ext/demo")
    ap.add_argument("--out", default=str(ROOT / "build" / "fabrics"))
    a = ap.parse_args()
    for n in a.names:
        make(n, Path(a.demo), Path(a.out))


if __name__ == "__main__":
    main()
