"""Dedicated RegFile test configs.

The general configs pin every RegFile to defined values (fab_router.backbone) but never
observe its read data: routed through the observation chains it would carry X (an entry
not yet written) and loop through its read address (a read is combinational in it). A
RegFile config adds, per RegFile whose pins all got defined values (rf_mode), two of its
eight read bits (AD0..3, BD0..3, rotated by the config index) routed point to point,
through switch-matrix muxes only, to output pads of its row's W_IO tile (A_T / B_T first,
then A_I / B_I): no BEL on the way, so no X into a chain and no loop. In rf_mode read port
B also takes the write address bits 0 / 1 from the pad where that routes (A their
complement), so the two read ports read different entries. The rest of the config is the
router's own cover (fab_router.route_all with this as the seed).
"""
from __future__ import annotations

BITS = [("A", k) for k in range(4)] + [("B", k) for k in range(4)]
PADS = ("A_T", "B_T", "A_I", "B_I")


def observe(cfg, bias: int) -> int:
    """Route two read bits of every fully pinned RegFile to pads of its row; returns the
    number routed."""
    from fabtest.patterns.config import fab_router as fr
    arr = cfg.g.arr
    io_rows = {t[1] for t in arr.io_tiles}
    used: set = set()
    n = 0
    for r in getattr(cfg, "rf_ok", []):
        if r[1] not in io_rows:
            continue
        for j in (2 * bias, 2 * bias + 1):
            port, k = BITS[j % len(BITS)]
            src = arr.canon(r, f"{port}D{k}")
            pads = [arr.canon((-1, r[1]), p) for p in PADS]
            pads = [p for p in pads if p not in used and cfg.g.mux_of.get(p) not in cfg.sel]
            for p in pads:
                if fr.route_to(cfg, src, {p}, True):
                    used.add(p)
                    n += 1
                    break
    return n


def seed(cfg, bias: int) -> dict:
    from fabtest.patterns.config import fab_router as fr
    cfg.rf_mode = True
    cfg.rf_ok = []
    rep = fr.backbone(cfg, bias)
    return {**rep, "rf_pinned": len(cfg.rf_ok), "rf_observed": observe(cfg, bias)}


def build_configs(ff, n: int, prior: list | None = None, idx0: int = 0, verbose: bool = True):
    """n RegFile configs on top of prior (the router's cover of what prior leaves open)."""
    from fabtest.patterns.config import fab_router as fr
    configs, _, _, log, _ = fr.route_all(ff, n, verbose=verbose, prior=prior, idx0=idx0, seed=seed)
    return configs, log
