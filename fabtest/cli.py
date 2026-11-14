"""fabtest command line (fabtest P1: init, info; further commands follow the plan).

  python3 -m fabtest init <FABulous project> [--work DIR] [--force]
  python3 -m fabtest info <work dir> [--bels]
"""
from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path


def cmd_init(a) -> None:
    from fabtest import project
    project.init(Path(a.project), Path(a.work) if a.work else None, a.force)


def cmd_info(a) -> None:
    from fabtest import spec
    s = spec.load(Path(a.work))
    print(s.summary())
    for y, row in enumerate(s.grid):
        print(f"  Y{y:2d} " + " ".join(f"{(t or '.'):>14s}" for t in row))
    if a.bels:
        mods = Counter()
        for _, t in s.tiles():
            for b in s.bels(t):
                mods[(b["module"], t)] += 1
        for (m, t), n in sorted(mods.items()):
            print(f"  {m:40s} in {t:16s} x{n}")
        for m in s.d["top"]["macros"]:
            print(f"  macro {m['module']} {m['inst']}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="fabtest")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init", help="copy and parse a FABulous project")
    p.add_argument("project")
    p.add_argument("--work")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("info", help="show what fabtest knows about a fabric")
    p.add_argument("work")
    p.add_argument("--bels", action="store_true")
    a = ap.parse_args(argv)
    {"init": cmd_init, "info": cmd_info}[a.cmd](a)
