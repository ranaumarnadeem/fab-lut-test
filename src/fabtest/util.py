"""Small shared helpers: atomic writes, a lock for builds, compiled-code cache names.

atomic_write_*: through a temporary file in the same directory and os.replace, so a
crash leaves the old file or the new one, never a truncated one (the up-to-date checks
of the steps trust what is on disk).
file_lock: exclusive between the threads of this process and, where fcntl exists,
between processes (two `fabtest` runs building the same kernel at once).
built: a compiled artifact named by a key of everything it is built from (sources,
compiler, flags, host CPU for -march=native), built once under the lock.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import platform
import subprocess
import threading
from pathlib import Path

try:
    import fcntl
except ImportError:   # Windows: thread lock only
    fcntl = None

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _tmp(path: Path) -> Path:
    return path.with_name(f".{path.name}.tmp{os.getpid()}-{threading.get_ident()}")


def atomic_write_bytes(path: Path | str, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp(path)
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def atomic_write_text(path: Path | str, text: str) -> None:
    atomic_write_bytes(path, text.encode())


def atomic_write_json(path: Path | str, obj, **kw) -> None:
    """json.dump through a temporary file (streamed: no second copy of a large netlist)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp(path)
    try:
        with open(tmp, "w") as fh:
            json.dump(obj, fh, **kw)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


@contextlib.contextmanager
def atomic_open(path: Path | str, mode: str = "w", **kw):
    """open() for writing through a temporary file, moved in place when the block ends
    without an exception (a CSV writer, np.savez)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp(path)
    try:
        with open(tmp, mode, **kw) as fh:
            yield fh
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


@contextlib.contextmanager
def file_lock(path: Path | str):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCKS_GUARD:
        tl = _LOCKS.setdefault(str(path.resolve()), threading.Lock())
    with tl:
        if fcntl is None:
            yield
            return
        with open(path, "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)


def sha1_file(path: Path | str) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for blk in iter(lambda: fh.read(1 << 24), b""):
            h.update(blk)
    return h.hexdigest()


_MACHINE = None


def machine_key() -> str:
    """The host CPU, for code built with -march=native (it may not run on another CPU)."""
    global _MACHINE
    if _MACHINE is None:
        parts = [platform.system(), platform.machine()]
        try:
            seen = set()
            for ln in Path("/proc/cpuinfo").read_text().splitlines():
                k = ln.split(":", 1)[0].strip()
                if k in ("vendor_id", "model name", "flags", "Features", "CPU part") and k not in seen:
                    seen.add(k)
                    parts.append(ln)
        except OSError:
            parts.append(platform.processor())
        _MACHINE = hashlib.sha1("\n".join(parts).encode()).hexdigest()
    return _MACHINE


_CC_VERSION: dict[str, str] = {}


def compiler_version(cc: str) -> str:
    if cc not in _CC_VERSION:
        try:
            r = subprocess.run([cc, "--version"], capture_output=True, text=True, timeout=30)
            _CC_VERSION[cc] = (r.stdout or r.stderr).splitlines()[0] if (r.stdout or r.stderr) else cc
        except (OSError, subprocess.TimeoutExpired):
            _CC_VERSION[cc] = cc
    return _CC_VERSION[cc]


def built(out_dir: Path, stem: str, suffix: str, key_parts: list, build) -> Path:
    """out_dir/<stem>-<key><suffix>, the key a hash of key_parts (bytes or str). Built once:
    under file_lock, build(tmp) writes a temporary file that is then moved in place, so
    concurrent callers neither build twice nor load a half-written file."""
    h = hashlib.sha1()
    for p in key_parts:
        h.update(p if isinstance(p, bytes) else str(p).encode())
        h.update(b"\0")
    out = Path(out_dir) / f"{stem}-{h.hexdigest()[:16]}{suffix}"
    if out.exists():
        return out
    with file_lock(Path(out_dir) / f".{stem}.lock"):
        if out.exists():
            return out
        tmp = out.with_name(f".{out.name}.tmp{os.getpid()}")
        try:
            build(tmp)
            os.replace(tmp, out)
        finally:
            tmp.unlink(missing_ok=True)
    return out
