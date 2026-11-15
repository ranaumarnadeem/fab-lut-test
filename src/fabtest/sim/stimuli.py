"""Clocked chain stimuli of a routed config set (the fault-simulated form of every config).

config_setup: per config the selects, the frame order and the LUT functions (all LUTs
registered XOR / XNOR by config parity, the RegFile / BRAM pinning LUTs combinational);
build_stims: fastsim.Stim per config (fabric.stim_clocked, partial expected values).
"""
from __future__ import annotations

import json
from pathlib import Path

from fabtest.model import fabric
from fabtest.model import sm_configs as sc


def config_setup(ff, data: dict, c: int) -> tuple[dict, list, dict]:
    """Config c of a config set as fault-simulated: (sels per tile, frame order, LUTs: tile ->
    LUT -> (table, i0_from_ci, ff, reset value)). All LUTs registered XOR (even c) / XNOR (odd
    c); the constant LUTs (RegFile / BRAM pinning) combinational."""
    inv = {ff.tn(t): t for t in ff.tiles}
    sels = {inv[n]: s for n, s in data["configs"][c].items()}
    for t in ff.tiles:
        sels.setdefault(t, {})
    tt = sc.cell.XNOR_TT if c % 2 else sc.cell.XOR_TT
    luts = {t: {x: (tt, 0, 1, 0) for x in sc.LUTS} for t in ff.lut_tiles}
    for t in ff.lut_tiles:
        for x, val in sels[t].get("__lut", {}).items():
            luts[t][x] = (val["tt"] if isinstance(val, dict) else 0xFFFF if val else 0, 0, 0, 0)
    return sels, sc.frame_order(c % sc.F), luts


def lut_bits(luts: dict) -> dict:
    """config_setup LUTs -> tile -> LUT -> config bits (as fabric.stim_clocked)."""
    return {t: {x: sc.cell.make_bits(tt, ff=f, i0_from_ci=i0, reset_value=rv) for x, (tt, i0, f, rv) in c.items()}
            for t, c in luts.items()}


def build_stims(ff, cfg_path: Path, only: list[int] | None = None, quiet: bool = False) -> list:
    """Clocked chain stimuli of a config set; only: these config indices."""
    data = json.loads(Path(cfg_path).read_text())
    stims = []
    for c in range(len(data["configs"])):
        if only is not None and c not in only:
            continue
        sels, order, luts = config_setup(ff, data, c)
        stims.append(fabric.stim_clocked(ff, f"FF{c:02d}", sels, order, None, list(ff.vecs), luts, partial=True))
        if not quiet:
            print(f"  FF{c:02d}: {int(stims[-1].sample.sum())} samples", flush=True)
    return stims
