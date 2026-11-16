"""fabtest work directory (fabtest P1).

build/fabtest/<project name>-<hash8>/ (or --work DIR):
  project/    copy of the FABulous project (FABulous's parser writes <bel>.json files there)
  spec.json   fabtest/spec.py
  stamps/     <step>.json: the input hash a step ran with; `fabtest run` skips a step whose
              stamp matches
The project must already contain the generated RTL (Fabric/eFPGA.v, eFPGA_top.v,
Tile/<T>/<T>.v ...): fabtest does not run FABulous.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from fabtest.paths import REPO as ROOT  # noqa: E402
IGNORE = shutil.ignore_patterns(".git", "*.json", "__pycache__", "*.vcd", "*.fst")


def fabulous_python() -> str:
    """A Python that can import FABulous: $FABTEST_FABULOUS_PY, the repo's ext venv, or this one."""
    env = os.environ.get("FABTEST_FABULOUS_PY")
    if env:
        return env
    for cand in (Path("/home/user/ext/venv/bin/python"), ROOT / "venv" / "bin" / "python"):
        if cand.exists():
            return str(cand)
    return sys.executable


def project_hash(proj: Path) -> str:
    """Hash of the files fabtest reads: fabric.csv, Tile/ and Fabric/ sources."""
    h = hashlib.sha1()
    files = [proj / "fabric.csv"] + sorted(p for d in ("Tile", "Fabric") for p in (proj / d).rglob("*")
                                           if p.is_file() and p.suffix in (".csv", ".v", ".list", ".sv"))
    for f in files:
        if f.exists():
            h.update(str(f.relative_to(proj)).encode())
            h.update(f.read_bytes())
    return h.hexdigest()


def work_dir(proj: Path, work: Path | None) -> Path:
    if work is not None:
        return work
    return ROOT / "build" / "fabtest" / f"{proj.name}-{project_hash(proj)[:8]}"


def init(proj: Path, work: Path | None = None, force: bool = False) -> Path:
    from fabtest import spec as specmod
    proj = proj.resolve()
    if not (proj / "fabric.csv").exists():
        raise SystemExit(f"fabtest: {proj} is not a FABulous project (no fabric.csv)")
    if not (proj / "Fabric" / "eFPGA.v").exists():
        raise SystemExit(f"fabtest: {proj}/Fabric/eFPGA.v missing: generate the fabric first "
                         f"(FABulous -p {proj} run \"load_fabric; run_fab\")")
    from fabtest import config
    h = project_hash(proj) + config.file_hash(proj)   # fabtest.toml: not in the work dir name
    w = work_dir(proj, work)
    if stamp_ok(w, "init", h) and not force:
        print(f"fabtest init: {w} is up to date")
        return w
    copy = w / "project"
    if copy.exists():
        shutil.rmtree(copy)
    w.mkdir(parents=True, exist_ok=True)
    shutil.copytree(proj, copy, ignore=IGNORE)
    raw = w / "spec_fabulous.json"
    r = subprocess.run([fabulous_python(), str(Path(__file__).resolve().parent / "fabulous_loader.py"), str(copy), str(raw)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stdout[-2000:] + r.stderr[-4000:])
        raise SystemExit("fabtest: FABulous could not parse the project (see above)")
    spec = specmod.build(json.loads(raw.read_text()), copy)
    (w / "spec.json").write_text(json.dumps(spec, indent=1))
    stamp(w, "init", h)
    print(f"fabtest init: {proj.name} -> {w}")
    print("  " + specmod.Spec(spec, w).summary())
    return w


def stamp(w: Path, step: str, h: str) -> None:
    (w / "stamps").mkdir(parents=True, exist_ok=True)
    (w / "stamps" / f"{step}.json").write_text(json.dumps({"hash": h}))


def stamp_ok(w: Path, step: str, h: str) -> bool:
    f = w / "stamps" / f"{step}.json"
    return f.exists() and json.loads(f.read_text()).get("hash") == h
