"""fabtest command line (run, init, info, synth, route, faultsim, report, powerup, untestable,
compact, pack, clockcheck, export, cfgtest, bitgen-check).

  python3 -m fabtest run <FABulous project> [--suite default] [--run NAME] [--configs N]
                         [--sample N] [--jobs N] [--no-export] [--cfgtest] [--work DIR] [--force]
  python3 -m fabtest init <FABulous project> [--work DIR] [--force]
  python3 -m fabtest info <work dir> [--bels]
  python3 -m fabtest synth <work dir> [--jobs 4]
  python3 -m fabtest route <work dir> [--run NAME] [--configs 24] [--chains 2] [--extend]
  python3 -m fabtest faultsim <work dir> [--run NAME] [--configs-from JSON] [--sample 2000] [--jobs 4] [--force]
  python3 -m fabtest report <work dir> [--run NAME]
  python3 -m fabtest powerup <work dir> [--run NAME] [--sample 400] [--max-u 10]
  python3 -m fabtest untestable <work dir> [--run NAME] [--jobs 4]
  python3 -m fabtest clockcheck <work dir> [--run NAME] [--states 4] [--configs 8]
  python3 -m fabtest compact <work dir> [--run NAME] [--target 99 | --configs N] [--to NAME] [--by items]
  python3 -m fabtest pack <work dir> [--run NAME] [--configs 8] [--types LUT4AB,DSP_top] [--to NAME] [--solver cpsat|greedy]
      [--kinds mux,carry,ensr]
  python3 -m fabtest export <work dir> [--run NAME] [--configs-from JSON] [--only 0,1] [--canonical] [--port-test]
  python3 -m fabtest cfgtest <work dir> [--run NAME] [--configs-from JSON] [--only 0,1] [--jobs 4]
  python3 -m fabtest bitgen-check <work dir> [--run NAME | --configs-from JSON] [--only 0,1,2,3]

<work dir>: build/fabtest/<fabric> (fabtest init), or the FABulous project itself (its work
dir, made by init when needed); every run writes results/<fabric>/<run>/. Nothing a step made
is deleted without --force (run, faultsim): results whose inputs changed are moved aside.
"""
from __future__ import annotations

import argparse
import subprocess
from collections import Counter
from pathlib import Path


def _work(p: str) -> Path:
    """A work dir, or a FABulous project (its work dir, made by fabtest init when needed)."""
    w = Path(p)
    if (w / "spec.json").exists():
        return w
    if (w / "fabric.csv").exists():
        from fabtest import project
        return project.init(w)
    raise SystemExit(f"fabtest: {p} is neither a fabtest work dir (no spec.json) nor a FABulous project "
                     "(no fabric.csv); make the work dir with `fabtest init <FABulous project>`")


def _version() -> str:
    from fabtest import __version__
    from fabtest.paths import REPO
    try:
        sha = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"], capture_output=True,
                             text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        sha = ""
    return f"fabtest {__version__}" + (f" (git {sha})" if sha else "")


class _Version(argparse.Action):
    def __init__(self, option_strings, dest, **kw):
        super().__init__(option_strings, dest, nargs=0, help="show the version and exit")

    def __call__(self, parser, namespace, values, option_string=None):
        print(_version())
        parser.exit()


def cmd_run(a) -> None:
    from fabtest import pipeline
    pipeline.run_all(Path(a.project), a.suite, a.run, a.configs, a.sample, a.jobs, not a.no_export,
                     Path(a.work) if a.work else None, a.cfgtest, a.force)


def cmd_init(a) -> None:
    from fabtest import project
    project.init(Path(a.project), Path(a.work) if a.work else None, a.force)


def cmd_info(a) -> None:
    from fabtest import spec
    s = spec.load(_work(a.work))
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
            plug = None if m["module"] in s.d.get("opaque", ()) else std.macro_for(m["module"])
            print(f"  macro {m['module']} {m['inst']}: {plug.name if plug else 'opaque (X source)'}")


def cmd_synth(a) -> None:
    from fabtest import spec, synth
    w = _work(a.work)
    synth.build(spec.load(w), w, a.jobs, a.force)


def _run(a, w: Path, create: bool = False) -> Path:
    from fabtest import pipeline
    run = pipeline.run_dir(w, a.run, create)
    if getattr(a, "configs_from", None):
        pipeline.use_configs(run, Path(a.configs_from))
    print(f"fabtest: run {run}")
    return run


def _jobs(a, s) -> int:
    """--jobs, else fabtest.toml [faultsim] jobs."""
    return a.jobs or s.cfg["faultsim"]["jobs"]


def cmd_route(a) -> None:
    from fabtest import pipeline, spec
    w = _work(a.work)
    pipeline.route(spec.load(w), _run(a, w, create=True), a.configs, a.chains, a.extend)


def cmd_faultsim(a) -> None:
    from fabtest import pipeline, spec
    w = _work(a.work)
    s = spec.load(w)
    run = _run(a, w, create=bool(a.configs_from))
    pipeline.use_configs(run, None)
    fs = s.cfg["faultsim"]
    pick = lambda v, k: fs[k] if v is None else v   # noqa: E731
    pipeline.faultsim(s, w, run, pick(a.sample, "sample"), pick(a.seed, "seed"), pick(a.chunk, "chunk"),
                      pick(a.jobs, "jobs"), a.force)
    print(pipeline.report(s, run), end="")


def cmd_report(a) -> None:
    from fabtest import pipeline, spec
    w = _work(a.work)
    print(pipeline.report(spec.load(w), _run(a, w)), end="")


def _only(x):
    """--only: config indices, e.g. 0,1 or 0-3,8."""
    if not x:
        return None
    out = []
    try:
        for part in x.split(","):
            lo, _, hi = part.strip().partition("-")
            out += list(range(int(lo), int(hi) + 1)) if hi else [int(lo)]
    except ValueError:
        raise SystemExit(f"fabtest: --only {x}: config indices, e.g. 0,1 or 0-3,8") from None
    return out


def cmd_powerup(a) -> None:
    from fabtest import pipeline, spec
    w = _work(a.work)
    s = spec.load(w)
    print(pipeline.powerup(s, w, _run(a, w), a.sample, a.seed, a.max_u, _jobs(a, s)), end="")


def cmd_untestable(a) -> None:
    from fabtest import pipeline, spec
    w = _work(a.work)
    s = spec.load(w)
    print(pipeline.untestable(s, w, _run(a, w), _jobs(a, s)), end="")


def cmd_clockcheck(a) -> None:
    from fabtest import pipeline, spec
    w = _work(a.work)
    print(pipeline.clockcheck(spec.load(w), w, _run(a, w), a.states, a.configs), end="")


def cmd_compact(a) -> None:
    from fabtest import pipeline, spec
    w = _work(a.work)
    s = spec.load(w)
    pipeline.compact(s, w, _run(a, w), a.target, a.configs, a.to, a.by, _jobs(a, s))


def cmd_pack(a) -> None:
    from fabtest import pipeline, spec
    w = _work(a.work)
    s = spec.load(w)
    pipeline.pack(s, w, _run(a, w), a.configs, a.to, a.time, a.types.split(",") if a.types else None, _jobs(a, s),
                  a.solver, tuple(k for k in a.kinds.split(",") if k))


def cmd_export(a) -> None:
    from fabtest import export, pipeline, spec
    w = _work(a.work)
    run = _run(a, w, create=bool(a.configs_from))
    pipeline.use_configs(run, None)
    export.export(spec.load(w), w, run, _only(a.only), a.canonical, a.port_test)


def cmd_cfgtest(a) -> None:
    from fabtest import cfgtest, spec
    w = _work(a.work)
    s = spec.load(w)
    if a.build_only:
        cfgtest.build(s, w, a.force)
        return
    if a.force:
        cfgtest.build(s, w, True)
    from fabtest import pipeline
    run = _run(a, w, create=bool(a.configs_from))
    pipeline.use_configs(run, None)
    print(cfgtest.run(s, w, run, _only(a.only), _jobs(a, s)), end="")


def cmd_bitgen_check(a) -> None:
    import sys
    from fabtest import export, spec
    w = _work(a.work)
    from fabtest import pipeline
    cfg = Path(a.configs_from) if a.configs_from else pipeline.run_dir(w, a.run) / "configs.json"
    ok = export.bitgen_check(spec.load(w), w, cfg, _only(a.only) or [0, 1])
    print(f"fabtest bitgen-check: {'pass' if ok else 'FAIL'}")
    sys.exit(0 if ok else 1)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="fabtest")
    ap.add_argument("--version", action=_Version)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="everything: init, synth, route, faultsim, report, export")
    p.add_argument("project", help="FABulous project (with the generated fabric RTL)")
    p.add_argument("--suite", default="default", help="suite name (src/fabtest/suites) or file")
    p.add_argument("--run", help="run name (default: the suite name)")
    p.add_argument("--configs", type=int, help="configs in all (default: the suite's)")
    p.add_argument("--sample", type=int, help="sampled faults, 0: every fault (default: fabtest.toml)")
    p.add_argument("--jobs", type=int, help="threads (default: fabtest.toml)")
    p.add_argument("--no-export", action="store_true")
    p.add_argument("--cfgtest", action="store_true", help="also the configuration-logic test")
    p.add_argument("--work", help="work dir (default: build/fabtest/<project>-<hash>)")
    p.add_argument("--force", action="store_true",
                   help="a run holding other configs or another fault sample: move them aside and redo")
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
    p.add_argument("--force", action="store_true", help="also when up to date")
    p = sub.add_parser("route", help="routed test configs (LUT chain backbone)")
    p.add_argument("work")
    p.add_argument("--configs", type=int, default=24)
    p.add_argument("--chains", type=int, default=2)
    p.add_argument("--extend", action="store_true", help="add configs to the run's configs.json")
    p.add_argument("--run", help="run name (default: a new timestamped run)")
    p = sub.add_parser("faultsim", help="sampled fault simulation, resumable in chunks")
    p.add_argument("work")
    p.add_argument("--sample", type=int, help="0: every fault (default: fabtest.toml, 2000)")
    p.add_argument("--seed", type=int)
    p.add_argument("--chunk", type=int)
    p.add_argument("--jobs", type=int)
    p.add_argument("--configs-from", help="a stored config set: copied into the run")
    p.add_argument("--run", help="run name (default: the latest run)")
    p.add_argument("--force", action="store_true",
                   help="the run holds another sample: move it to faultsim/stale/ and simulate this one")
    p = sub.add_parser("report", help="coverage from the fault simulation results")
    p.add_argument("work")
    p.add_argument("--run", help="run name (default: the latest run)")
    p = sub.add_parser("powerup", help="power-up check of the X-only faults of a run")
    p.add_argument("work")
    p.add_argument("--run", help="run name (default: the latest run)")
    p.add_argument("--sample", type=int, default=400)
    p.add_argument("--seed", type=int, default=2)
    p.add_argument("--max-u", type=int, default=10, help="largest undecided set enumerated")
    p.add_argument("--jobs", type=int, help="threads (default: fabtest.toml)")
    p = sub.add_parser("untestable", help="SAT proof of the undetected faults that no test can detect")
    p.add_argument("work")
    p.add_argument("--run", help="run name (default: the latest run)")
    p.add_argument("--jobs", type=int, help="processes (default: fabtest.toml)")
    p = sub.add_parser("clockcheck", help="clock-network faults under random power-up states")
    p.add_argument("work")
    p.add_argument("--run", help="run name (default: the latest run)")
    p.add_argument("--states", type=int, default=4, help="random power-up states per fault")
    p.add_argument("--configs", type=int, default=8, help="the run's first N configs")
    p = sub.add_parser("compact", help="fewer configs: greedy set cover of the fault simulation")
    p.add_argument("work")
    p.add_argument("--run", help="run name (default: the latest run)")
    p.add_argument("--target", type=float, help="percent of what all configs detect (default 100)")
    p.add_argument("--configs", type=int, help="keep the N best configs instead")
    p.add_argument("--to", help="name of the new run (default: <run>_c<N>)")
    p.add_argument("--by", choices=("faults", "items"), default="faults",
                   help="cover the run's fault simulation, or the router items of every config")
    p.add_argument("--jobs", type=int, help="processes (default: fabtest.toml)")
    p = sub.add_parser("pack", help="more configs for the undetected faults: fault-targeted bin packing")
    p.add_argument("work")
    p.add_argument("--run", help="run name (default: the latest run)")
    p.add_argument("--configs", type=int, default=8, help="configs to add")
    p.add_argument("--to", help="name of the new run (default: <run>_p<N>)")
    p.add_argument("--types", help="only faults of these tile types, e.g. LUT4AB,DSP_top,DSP_bot")
    p.add_argument("--solver", choices=("cpsat", "greedy"), default="cpsat",
                   help="bin packing: CP-SAT (OR-Tools; greedy when not installed), or greedy first fit")
    p.add_argument("--time", type=float, default=300, help="CP-SAT time limit, seconds")
    p.add_argument("--kinds", default="mux",
                   help="target kinds: mux (switch-matrix codes), carry (LUTs with I0 from Ci), "
                        "ensr (non-constant EN / SR of the LUT flops), e.g. mux,carry,ensr")
    p.add_argument("--jobs", type=int, help="processes (default: fabtest.toml)")
    p = sub.add_parser("export", help="bitstreams, vectors, Icarus testbench, plan, manifest")
    p.add_argument("work")
    p.add_argument("--configs-from", help="a stored config set: copied into the run")
    p.add_argument("--run", help="run name (default: the latest run)")
    p.add_argument("--only", help="config indices, e.g. 0,1 or 0-3")
    p.add_argument("--canonical", action="store_true", help="frames 0..F-1 (bit_gen order)")
    p.add_argument("--port-test", action="store_true", help="also the configuration-port test (bitbang, UART)")
    p = sub.add_parser("cfgtest", help="configuration logic (ConfigFSM, frame registers, UART, bitbang)")
    p.add_argument("work")
    p.add_argument("--configs-from", help="a stored config set: copied into the run")
    p.add_argument("--run", help="run name (default: the latest run)")
    p.add_argument("--only", help="the configs loaded (default: the first two kept)")
    p.add_argument("--jobs", type=int)
    p.add_argument("--build-only", action="store_true", help="the netlist and fault list only")
    p.add_argument("--force", action="store_true", help="rebuild the netlist")
    p = sub.add_parser("bitgen-check", help="canonical .bin = FABulous bit_gen of the FASM")
    p.add_argument("work")
    p.add_argument("--configs-from", help="a config JSON (default: the run's configs)")
    p.add_argument("--run", help="run name (default: the latest run)")
    p.add_argument("--only", help="config indices (default 0,1)")
    a = ap.parse_args(argv)
    {"run": cmd_run, "init": cmd_init, "info": cmd_info, "synth": cmd_synth, "route": cmd_route,
     "faultsim": cmd_faultsim, "report": cmd_report, "powerup": cmd_powerup, "untestable": cmd_untestable,
     "compact": cmd_compact, "pack": cmd_pack, "clockcheck": cmd_clockcheck, "export": cmd_export,
     "cfgtest": cmd_cfgtest, "bitgen-check": cmd_bitgen_check}[a.cmd](a)
