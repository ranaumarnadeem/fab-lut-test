"""Clocked chain stimuli of a routed config set (the fault-simulated form of every config).

config_setup: per config the selects, the frame order and the LUT functions (all LUTs
registered XOR / XNOR by config parity, the RegFile / BRAM pinning LUTs combinational);
build_stims: fastsim.Stim per config (fabric.stim_clocked, partial expected values),
cached in build/cache/stims.
"""
from __future__ import annotations

import hashlib
import json
import os
import pickle
from pathlib import Path

from fabtest.model import fabric
from fabtest.model import sm_configs as sc


def config_setup(ff, data: dict, c: int) -> tuple[dict, list, dict]:
    """Config c of a config set as fault-simulated: (sels per tile, frame order, LUTs: tile ->
    LUT -> (table, i0_from_ci, ff, reset value)). All LUTs registered XOR (even c) / XNOR (odd
    c), I0 from Ci for the carry-mode LUTs ("__ci", fabtest pack); the constant LUTs (RegFile /
    BRAM pinning) combinational."""
    inv = {ff.tn(t): t for t in ff.tiles}
    sels = {inv[n]: s for n, s in data["configs"][c].items()}
    for t in ff.tiles:
        sels.setdefault(t, {})
    tt = sc.cell.XNOR_TT if c % 2 else sc.cell.XOR_TT
    luts = {t: {x: (tt, 0, 1, 0) for x in sc.LUTS} for t in ff.lut_tiles}
    for t in ff.lut_tiles:
        for x in sels[t].get("__ci", ()):
            luts[t][x] = (tt, 1, 1, 0)
        for x, val in sels[t].get("__lut", {}).items():
            luts[t][x] = (val["tt"] if isinstance(val, dict) else 0xFFFF if val else 0, 0, 0, 0)
        if sels[t].get("__srp"):   # SR pulse (route --sr-pulse): reset values 0 / 1 by config and LUT
            for k, x in enumerate(sc.LUTS):
                a, i0, f, _rv = luts[t][x]
                if f:
                    luts[t][x] = (a, i0, f, (c + k) % 2)
    return sels, sc.frame_order(c % sc.F), luts


def reset_pad(ff, sels: dict) -> int | None:
    """Index in ff.border_in of the config's reset pad ("__rst" on its W_IO tile: fabtest
    route --sr-pulse), None in an ordinary config."""
    for t in getattr(ff, "io_tiles", ()):
        p = sels.get(t, {}).get("__rst")
        if p is not None:
            return ff.border_in.index(f"PAD/{ff.out_or_pad(t, p)}")
    return None


def config_vectors(ff, data: dict, c: int, sels: dict) -> list[int]:
    """The vectors of config c: the fabric's own; with a reset pad, that pad 1 in the first
    vector and 0 in every other (the SR pulse)."""
    vecs = list(ff.vecs)
    j = reset_pad(ff, sels)
    if j is not None:
        vecs = [v | (1 << j) if k == 0 else v & ~(1 << j) for k, v in enumerate(vecs)]
    return vecs


def lut_bits(luts: dict) -> dict:
    """config_setup LUTs -> tile -> LUT -> config bits (as fabric.stim_clocked)."""
    return {t: {x: sc.cell.make_bits(tt, ff=f, i0_from_ci=i0, reset_value=rv) for x, (tt, i0, f, rv) in c.items()}
            for t, c in luts.items()}


_CODE_HASH = None


def _code_hash() -> str:
    """Hash of the code a stimulus depends on (model, BEL plugins, this file)."""
    global _CODE_HASH
    if _CODE_HASH is None:
        pkg = Path(__file__).resolve().parent.parent
        h = hashlib.sha1()
        for f in sorted([*(pkg / "model").glob("*.py"), *(pkg / "bels").glob("*.py"), Path(__file__).resolve()]):
            h.update(f.read_bytes())
        from fabtest.paths import RTL
        for f in sorted(RTL.rglob("*")):   # the repo's tile copies (the demo default model)
            if f.is_file():
                st = f.stat()
                h.update(f"{f}:{st.st_size}:{st.st_mtime_ns}".encode())
        _CODE_HASH = h.hexdigest()
    return _CODE_HASH


def _fabric_key(ff) -> str:
    """What the model of ff is built from: the spec (or the demo default), its options."""
    spec = getattr(ff, "spec", None)
    if spec is None and getattr(ff, "type_dir", None) is not None:
        src = json.dumps(sorted((k, str(v)) for k, v in ff.type_dir.items()))
    else:
        src = json.dumps(spec.d, sort_keys=True) if spec is not None else "array"
    return hashlib.sha1(f"{type(ff).__name__}:{ff.name}:{ff.n_rand}:{getattr(ff, 'bram', None)}:{src}".encode()).hexdigest()


def _stim_cache(ff, data: dict, c: int) -> Path | None:
    if os.environ.get("FABTEST_STIM_CACHE", "1") == "0":
        return None
    from fabtest.paths import BUILD
    key = hashlib.sha1(f"{_code_hash()}:{_fabric_key(ff)}:{c}:".encode()
                       + json.dumps(data["configs"][c], sort_keys=True).encode()).hexdigest()
    return BUILD / "cache" / "stims" / f"{key}.pkl"


def build_stim(ff, data: dict, c: int):
    """fastsim.Stim of config c (cached in build/cache/stims: keyed by the code, the fabric
    model and the config; FABTEST_STIM_CACHE=0: always built)."""
    cache = _stim_cache(ff, data, c)
    if cache is not None and cache.exists():
        try:
            with cache.open("rb") as fh:
                return pickle.load(fh)
        except Exception:   # unreadable (another numpy, a half-written file): built again
            pass
    sels, order, luts = config_setup(ff, data, c)
    st = fabric.stim_clocked(ff, f"FF{c:02d}", sels, order, None, config_vectors(ff, data, c, sels),
                             luts, partial=True)
    if cache is not None:
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(f".tmp{os.getpid()}")
        with tmp.open("wb") as fh:
            pickle.dump(st, fh, protocol=pickle.HIGHEST_PROTOCOL)
        tmp.replace(cache)
    return st


def build_stims(ff, cfg_path: Path, only: list[int] | None = None, quiet: bool = False) -> list:
    """Clocked chain stimuli of a config set; only: these config indices."""
    data = json.loads(Path(cfg_path).read_text())
    stims = []
    for c in range(len(data["configs"])):
        if only is not None and c not in only:
            continue
        stims.append(build_stim(ff, data, c))
        if not quiet:
            print(f"  FF{c:02d}: {int(stims[-1].sample.sum())} samples", flush=True)
    return stims
