"""Native (C++, pybind11) routines, built on first use into build/native.

load() returns the extension module, or None when it cannot be built (no compiler or no
pybind11) or FABTEST_NATIVE=0; callers then use their pure-Python path, which gives the
same results (tests compare both). The module file is named by a key of the sources,
compiler, flags, pybind11, Python and host CPU (-march=native), built once under a lock.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import sysconfig
from pathlib import Path

from fabtest import util
from fabtest.paths import BUILD

SRC = sorted(Path(__file__).resolve().parent.glob("*.cpp"))
FLAGS = ["-O3", "-march=native", "-std=c++17", "-shared", "-fPIC"]
_MOD = None
_TRIED = False


def _path() -> Path:
    import pybind11
    cxx = os.environ.get("CXX", "c++")
    inc = [f"-I{sysconfig.get_paths()['include']}", f"-I{pybind11.get_include()}"]
    suffix = sysconfig.get_config_var("EXT_SUFFIX")

    def build(tmp: Path) -> None:
        subprocess.run([cxx, *FLAGS, *inc, *map(str, SRC), "-o", str(tmp)], check=True, capture_output=True,
                       text=True)
    key = [p.read_bytes() for p in SRC] + [cxx, util.compiler_version(cxx), " ".join(FLAGS), pybind11.__version__,
                                           suffix, sys.version, util.machine_key()]
    return util.built(BUILD / "native", "_native", suffix, key, build)


def load():
    global _MOD, _TRIED
    if _TRIED:
        return _MOD
    _TRIED = True
    if os.environ.get("FABTEST_NATIVE", "1") == "0":
        return None
    try:
        if not SRC:
            raise ImportError("no C++ sources")
        out = _path()
        spec = importlib.util.spec_from_file_location("_native", out)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _MOD = mod
    except (ImportError, OSError, subprocess.CalledProcessError) as e:
        msg = getattr(e, "stderr", "") or str(e)
        print(f"fabtest: native module not available, using Python ({msg.strip()[-300:]})", file=sys.stderr)
        _MOD = None
    return _MOD
