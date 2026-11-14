"""fabtest command line (init, info, synth; further commands follow the plan).

  python3 -m fabtest init <FABulous project> [--work DIR] [--force]
  python3 -m fabtest info <work dir> [--bels]
  python3 -m fabtest synth <work dir> [--jobs 4]
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
        from fabtest import bels as belreg
        from fabtest.bels import std
        kinds = belreg.classify(s)
        count = Counter(t for _, t in s.tiles())
        for typ in sorted(count):
            k = kinds[typ]
            print(f"  {typ:16s} x{count[typ]:<3d} kind {k['kind']}" + (f" ({k['why']})" if k["why"] else ""))
            for (m, plug), n in sorted(Counter((m, pl) for _, m, pl in k["plugins"]).items()):
                print(f"      {m:40s} x{n}  {plug}")
        for m in s.d["top"]["macros"]:
            plug = std.macro_for(m["module"])
            print(f"  macro {m['module']} {m['inst']}: {plug.name if plug else 'opaque (X source)'}")


def cmd_synth(a) -> None:
    from fabtest import spec, synth
    w = Path(a.work)
    synth.build(spec.load(w), w, a.jobs)


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
    p = sub.add_parser("synth", help="gate-level netlist and fault list")
    p.add_argument("work")
    p.add_argument("--jobs", type=int, default=4)
    a = ap.parse_args(argv)
    {"init": cmd_init, "info": cmd_info, "synth": cmd_synth}[a.cmd](a)
