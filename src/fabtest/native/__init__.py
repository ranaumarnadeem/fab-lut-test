"""Native (C++, pybind11) routines, built on first use into build/native.

load() returns the extension module, or None when it cannot be built (no compiler or no
pybind11) or FABTEST_NATIVE=0; callers then use their pure-Python path, which gives the
same results (tests compare both).
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import sysconfig
from pathlib import Path

from fabtest.paths import BUILD

SRC = sorted(Path(__file__).resolve().parent.glob("*.cpp"))
_MOD = None
_TRIED = False


def _build(out: Path) -> None:
    import pybind11
    inc = [f"-I{sysconfig.get_paths()['include']}", f"-I{pybind11.get_include()}"]
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    subprocess.run([os.environ.get("CXX", "c++"), "-O3", "-march=native", "-std=c++17", "-shared", "-fPIC",
                    *inc, *map(str, SRC), "-o", str(tmp)], check=True, capture_output=True, text=True)
    tmp.rename(out)


def load():
    global _MOD, _TRIED
    if _TRIED:
        return _MOD
    _TRIED = True
    if os.environ.get("FABTEST_NATIVE", "1") == "0":
        return None
    out = BUILD / "native" / f"_native{sysconfig.get_config_var('EXT_SUFFIX')}"
    try:
        if not out.exists() or out.stat().st_mtime < max(p.stat().st_mtime for p in SRC):
            _build(out)
        spec = importlib.util.spec_from_file_location("_native", out)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _MOD = mod
    except (ImportError, OSError, subprocess.CalledProcessError) as e:
        msg = getattr(e, "stderr", "") or str(e)
        print(f"fabtest: native module not available, using Python ({msg.strip()[-300:]})", file=sys.stderr)
        _MOD = None
    return _MOD
