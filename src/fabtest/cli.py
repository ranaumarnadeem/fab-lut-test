"""fabtest command line (init, info, synth, route, faultsim, report, export).

  python3 -m fabtest init <FABulous project> [--work DIR] [--force]
  python3 -m fabtest info <work dir> [--bels]
  python3 -m fabtest synth <work dir> [--jobs 4]
  python3 -m fabtest route <work dir> [--run NAME] [--configs 24] [--chains 2] [--extend]
  python3 -m fabtest faultsim <work dir> [--run NAME] [--configs-from JSON] [--sample 2000] [--jobs 4]
  python3 -m fabtest report <work dir> [--run NAME]
  python3 -m fabtest export <work dir> [--run NAME] [--configs-from JSON] [--only 0,1] [--canonical]
  python3 -m fabtest bitgen-check <work dir> [--run NAME | --configs-from JSON] [--only 0,1,2,3]

<work dir>: build/fabtest/<fabric> (fabtest init); every run writes results/<fabric>/<run>/.
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


def _run(a, create: bool = False) -> Path:
    from fabtest import pipeline
    run = pipeline.run_dir(Path(a.work), a.run, create)
    if getattr(a, "configs_from", None):
        pipeline.use_configs(run, Path(a.configs_from))
    print(f"fabtest: run {run}")
    return run


def cmd_route(a) -> None:
    from fabtest import pipeline, spec
    w = Path(a.work)
    pipeline.route(spec.load(w), _run(a, create=True), a.configs, a.chains, a.extend)


def cmd_faultsim(a) -> None:
    from fabtest import pipeline, spec
    w = Path(a.work)
    s = spec.load(w)
    run = _run(a, create=bool(a.configs_from))
    pipeline.use_configs(run, None)
    pipeline.faultsim(s, w, run, a.sample, a.seed, a.chunk, a.jobs)
    print(pipeline.report(s, run), end="")


def cmd_report(a) -> None:
    from fabtest import pipeline, spec
    w = Path(a.work)
    print(pipeline.report(spec.load(w), _run(a)), end="")


def _only(x):
    return [int(v) for v in x.split(",")] if x else None


def cmd_export(a) -> None:
    from fabtest import export, pipeline, spec
    w = Path(a.work)
    run = _run(a, create=bool(a.configs_from))
    pipeline.use_configs(run, None)
    export.export(spec.load(w), w, run, _only(a.only), a.canonical)


def cmd_bitgen_check(a) -> None:
    import sys
    from fabtest import export, spec
    w = Path(a.work)
    from fabtest import pipeline
    cfg = Path(a.configs_from) if a.configs_from else pipeline.run_dir(w, a.run) / "configs.json"
    ok = export.bitgen_check(spec.load(w), w, cfg, _only(a.only) or [0, 1])
    print(f"fabtest bitgen-check: {'pass' if ok else 'FAIL'}")
    sys.exit(0 if ok else 1)


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
    p = sub.add_parser("route", help="routed test configs (LUT chain backbone)")
    p.add_argument("work")
    p.add_argument("--configs", type=int, default=24)
    p.add_argument("--chains", type=int, default=2)
    p.add_argument("--extend", action="store_true", help="add configs to the run's configs.json")
    p.add_argument("--run", help="run name (default: a new timestamped run)")
    p = sub.add_parser("faultsim", help="sampled fault simulation, resumable in chunks")
    p.add_argument("work")
    p.add_argument("--sample", type=int, default=2000, help="0: every fault")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--chunk", type=int, default=2000)
    p.add_argument("--jobs", type=int, default=4)
    p.add_argument("--configs-from", help="a stored config set: copied into the run")
    p.add_argument("--run", help="run name (default: the latest run)")
    p = sub.add_parser("report", help="coverage from the fault simulation results")
    p.add_argument("work")
    p.add_argument("--run", help="run name (default: the latest run)")
    p = sub.add_parser("export", help="bitstreams, vectors, Icarus testbench, plan, manifest")
    p.add_argument("work")
    p.add_argument("--configs-from", help="a stored config set: copied into the run")
    p.add_argument("--run", help="run name (default: the latest run)")
    p.add_argument("--only", help="config indices, e.g. 0,1")
    p.add_argument("--canonical", action="store_true", help="frames 0..F-1 (bit_gen order)")
    p = sub.add_parser("bitgen-check", help="canonical .bin = FABulous bit_gen of the FASM")
    p.add_argument("work")
    p.add_argument("--configs-from", help="a config JSON (default: the run's configs)")
    p.add_argument("--run", help="run name (default: the latest run)")
    p.add_argument("--only", help="config indices (default 0,1)")
    a = ap.parse_args(argv)
    {"init": cmd_init, "info": cmd_info, "synth": cmd_synth, "route": cmd_route,
     "faultsim": cmd_faultsim, "report": cmd_report, "export": cmd_export,
     "bitgen-check": cmd_bitgen_check}[a.cmd](a)
