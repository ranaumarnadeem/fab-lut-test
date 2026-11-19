"""Repository locations (fabtest is run from a checkout; see package/ for packaging)."""
from __future__ import annotations

import os
from pathlib import Path

PKG = Path(__file__).resolve().parent              # src/fabtest
REPO = PKG.parent.parent                           # the checkout
RTL = REPO / "rtl"                                 # input RTL: LUT4AB / W_IO / term copies, BRAM model
TB = REPO / "tb"                                   # testbench templates
SYNTH = PKG / "synth"                              # yosys templates and techmaps
BUILD = REPO / "build"                             # scratch: work dirs, compiled kernel, logs
RESULTS = Path(os.environ.get("FABTEST_RESULTS") or REPO / "results")   # one directory per run (local)
GOLDEN = REPO / "tests" / "golden"                 # committed reference data of the tests
# the FABulous demo project (with its generated RTL) the tests and the demo script use
DEMO = Path(os.environ.get("FABTEST_DEMO") or REPO.parent / "ext" / "demo")
