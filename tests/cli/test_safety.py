#!/usr/bin/env python3
"""Safety rules of work and run directories, without simulating (about 20 s on the demo):
nothing is deleted without --force, writes are atomic, compiled code is built once under
a lock, netlist keys survive an identical re-synth, and wrong arguments get a clear error.

  python3 tests/cli/test_safety.py
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import DEMO_PROJECT, ROOT, TEST_RUNS, demo_work  # noqa: E402

BASE = TEST_RUNS / "safety"


def refused(fn, *a, **k) -> str | None:
    """The SystemExit message of fn(*a, **k), or None when it returned."""
    try:
        fn(*a, **k)
    except SystemExit as e:
        return str(e)
    return None


def t_atomic(fails: list) -> None:
    from fabtest import util
    d = BASE / "atomic"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "x.json"
    util.atomic_write_text(f, "old")
    try:
        with util.atomic_open(f) as fh:
            fh.write("half")
            raise RuntimeError("crash")
    except RuntimeError:
        pass
    if f.read_text() != "old":
        fails.append("atomic_open: a failed write keeps the old file")
    util.atomic_write_json(f, {"a": 1})
    if json.loads(f.read_text()) != {"a": 1} or [p.name for p in d.iterdir()] != ["x.json"]:
        fails.append("atomic writes: new content, no temporary files left")


def t_built(fails: list) -> None:
    from fabtest import util
    d = BASE / "built"
    shutil.rmtree(d, ignore_errors=True)
    calls = []

    def build(tmp: Path) -> None:
        calls.append(1)
        time.sleep(0.2)
        tmp.write_text("lib")
    out = []
    th = [threading.Thread(target=lambda: out.append(util.built(d, "lib", ".so", ["src", "flags"], build)))
          for _ in range(8)]
    for t in th:
        t.start()
    for t in th:
        t.join()
    if len(calls) != 1 or len(set(out)) != 1 or out[0].read_text() != "lib":
        fails.append(f"built: 8 threads build once ({len(calls)} builds)")
    if util.built(d, "lib", ".so", ["src", "other flags"], build) == out[0]:
        fails.append("built: another key, another file")


def t_suite(fails: list) -> None:
    from fabtest import config
    d = BASE / "cwd"
    (d / "demo").mkdir(parents=True, exist_ok=True)   # a FABulous project called demo
    old = os.getcwd()
    os.chdir(d)
    try:
        s = config.suite("demo")
        if s["name"] != "demo" or not s["passes"]:
            fails.append("suite demo: from a directory that holds demo/")
        msg = refused(config.suite, "nosuch") or ""
        if "known: " not in msg:
            fails.append("suite nosuch: lists the known suites")
    finally:
        os.chdir(old)


def t_cli(fails: list) -> None:
    from fabtest import cli
    if cli._only("0-2,5") != [0, 1, 2, 5] or cli._only(None) is not None:
        fails.append("--only: ranges")
    if not refused(cli._only, "a,b"):
        fails.append("--only: a clear error for a bad list")
    r = subprocess.run([str(ROOT / "scripts" / "fabtest"), "--version"], capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.startswith("fabtest "):
        fails.append("--version")
    if cli._work(str(DEMO_PROJECT)) != demo_work():
        fails.append("a FABulous project where a work dir is taken: its work dir")
    msg = refused(cli._work, str(BASE / "nowhere")) or ""
    if "fabtest init" not in msg:
        fails.append("neither work dir nor project: says what to run")


def _run_files(run: Path, n_cfg: int = 2) -> None:
    (run / "faultsim").mkdir(parents=True, exist_ok=True)
    (run / "configs.json").write_text(json.dumps({"configs": [{} for _ in range(n_cfg)], "log": []}))
    names = [f"FF{c:02d}" for c in range(n_cfg)]
    with (run / "faultsim" / "chunk_0000.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "path", "polarity", *names])
        w.writerow([0, "a", 0, "D", "U"])
        w.writerow([1, "b", 1, "U", "D"])
        w.writerow([2, "c", 0, "D", "D"])
    (run / "faultsim" / "sample.json").write_text(json.dumps({"seed": 1, "n": 3, "of": 3, "chunk": 2000}))
    (run / "faultsim" / "configs.json").write_text(json.dumps({"kept": names, "left_out": []}))


def t_runs(fails: list, w: Path) -> None:
    from fabtest import paths, pipeline, spec as specmod
    paths.RESULTS = BASE / "results"
    shutil.rmtree(paths.RESULTS, ignore_errors=True)
    base = paths.RESULTS / w.name
    if not refused(pipeline.run_dir, w, "nosuch") or (base / "nosuch").exists():
        fails.append("run_dir: a missing run is an error and is not created")
    # fabtest run on configs it did not route: refused, left in place
    s = specmod.load(w)
    run = base / "routed_alone"
    _run_files(run)
    before = (run / "configs.json").read_bytes()
    if not refused(pipeline.route_suite, s, run, [{"configs": 2, "chains": 2}]) or \
            (run / "configs.json").read_bytes() != before:
        fails.append("route_suite: configs not made by `fabtest run` are refused and kept")
    # compact: never over another run or over itself
    src, other = base / "src", base / "other"
    _run_files(src)
    _run_files(other)
    if not refused(pipeline.compact, None, w, src, name="other") or not (other / "faultsim" / "chunk_0000.csv").exists():
        fails.append("compact --to an existing run: refused, the run kept")
    if not refused(pipeline.compact, None, w, src, name="src") or not (src / "faultsim" / "chunk_0000.csv").exists():
        fails.append("compact --to the run itself: refused")
    pipeline.compact(None, w, src)
    pipeline.compact(None, w, src)   # again: overwrites its own earlier compaction
    if not (base / "src_c2" / "configs.json").exists():
        fails.append("compact: the default run name, twice")


def t_stale(fails: list) -> None:
    from fabtest import pipeline, synth
    work = BASE / "work"
    out = work / "synth"
    shutil.rmtree(work, ignore_errors=True)
    out.mkdir(parents=True)
    nl = {"modules": {"top": {"ports": {}, "cells": {}, "netnames": {}}}}
    synth.write_netlist(out, nl)
    k1, legacy1 = synth.netlist_key(work), synth.legacy_key(out / "netlist.json")
    st = (out / "netlist.json").stat()
    os.utime(out / "netlist.json", (st.st_atime, st.st_mtime - 10))   # an older file, same content
    legacy0 = synth.legacy_key(out / "netlist.json")
    synth.write_netlist(out, nl)   # the same netlist again (a re-synth)
    if not (synth.netlist_ok(work, k1) and synth.netlist_ok(work, legacy1) and synth.netlist_ok(work, legacy0)):
        fails.append("netlist key: an identical re-synth keeps the old keys valid")
    synth.write_netlist(out, {"modules": {"top": {"ports": {}, "cells": {}, "netnames": {"n": {"bits": [2]}}}}})
    if synth.netlist_ok(work, k1) or synth.netlist_ok(work, legacy1):
        fails.append("netlist key: another netlist invalidates the old keys")
    meta = {"seed": 1, "n": 3, "of": 3, "chunk": 2000, "configs": "c", "netlist": synth.netlist_key(work),
            "vectors": 54, "kernel": "k", "osc": "local", "limit_factor": 200}
    cases = [({**meta}, None), ({**meta, "seed": 2}, "sample"), ({**meta, "n": 2}, "sample"),
             ({**meta, "configs": "d"}, "inputs"), ({**meta, "kernel": "j"}, "inputs"),
             ({k: v for k, v in meta.items() if k not in ("kernel", "osc", "limit_factor")}, None),   # older fabtest
             ({**meta, "netlist": k1}, "inputs")]
    for old, want in cases:
        got = pipeline._stale(old, meta, work)
        if (got and got[0]) != want:
            fails.append(f"stale chunks: {old} -> {got}, want {want}")


def main() -> None:
    fails: list = []
    shutil.rmtree(BASE, ignore_errors=True)
    BASE.mkdir(parents=True)
    w = demo_work()
    for t in (t_atomic, t_built, t_suite, t_cli, t_stale):
        t(fails)
    t_runs(fails, w)
    for f in fails:
        print("  FAIL " + f)
    print(f"test_safety: {'pass' if not fails else 'FAIL'} ({len(fails)} failures)")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
