"""Shared locations of the demo tests (imported by every test: puts src/ on sys.path)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

# the tests check stimulus generation itself: never from the stimulus cache
os.environ.setdefault("FABTEST_STIM_CACHE", "0")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from fabtest.paths import BUILD, GOLDEN, RESULTS  # noqa: E402

DEMO_PROJECT = Path("/home/user/ext/demo")          # the FABulous demo project (generated RTL)
SETS = {s: GOLDEN / "configs" / f"{s}.json" for s in ("muladd", "regfile", "v2", "v3")}
REF_RUN = RESULTS / "demo_fullbram_96cfg_50k"       # local: the stored 50k-fault matrices (R1)
TEST_RUNS = BUILD / "tests"                          # scratch runs of the tests (not results/)


def demo_work() -> Path:
    """The demo's fabtest work dir (fabtest init, cached by project hash)."""
    from fabtest import project
    return project.init(DEMO_PROJECT)
