#!/usr/bin/env python3
"""FABulous project -> spec JSON (fabtest P1). The only module that imports FABulous.

  <FABulous venv python> fabtest/fabulous_loader.py <project copy> <out.json>

Runs FABulous's own CSV parser (parseFabricCSV) on a COPY of the project: the parser runs
yosys on every BEL and writes <bel>.json next to it, so the user's project is never
parsed in place (fabtest/project.py makes the copy). Writes plain JSON, no FABulous
objects: fabric parameters, the tile grid (FABulous coordinates, grid[y][x]), the tile
types (directory, BELs with module name, prefix, ports, external ports, config bits,
feature names) and the supertiles. Stops with a clear message when the fabric is outside
what fabtest supports (frame based, 32 bits per frame row, 20 frames per column).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _bel(b) -> dict:
    return {"prefix": b.prefix, "module": b.module_name, "name": b.name, "src": str(b.src),
            "inputs": list(b.inputs), "outputs": list(b.outputs),
            "ext_in": list(b.externalInput), "ext_out": list(b.externalOutput),
            "shared": [str(p) for p in b.sharedPort], "config_bits": int(b.configBit),
            "features": list(b.belFeatureMap), "user_clk": bool(b.withUserCLK)}


def main() -> None:
    proj, out = Path(sys.argv[1]).resolve(), Path(sys.argv[2])
    try:
        from loguru import logger
        logger.remove()
    except ImportError:
        pass
    from fabulous.fabulous_settings import init_context
    from fabulous.fabric_generator.parser.parse_csv import parseFabricCSV
    init_context(project_dir=proj)
    fab = parseFabricCSV(str(proj / "fabric.csv"))
    mode = str(fab.configBitMode).split(".")[-1].lower()
    errs = []
    if mode != "frame_based":
        errs.append(f"ConfigBitMode {mode}: only frame_based is supported")
    if fab.frameBitsPerRow != 32 or fab.maxFramesPerCol != 20:
        errs.append(f"FrameBitsPerRow {fab.frameBitsPerRow} / MaxFramesPerCol {fab.maxFramesPerCol}: 32 / 20 expected")
    if errs:
        raise SystemExit("fabtest: unsupported fabric: " + "; ".join(errs))
    grid = [[(fab.tile[y][x].name if fab.tile[y][x] is not None else None) for x in range(fab.numberOfColumns)]
            for y in range(fab.numberOfRows)]
    types = {}
    for name, t in fab.tileDic.items():
        types[name] = {"dir": str(Path(t.tileDir).parent), "bels": [_bel(b) for b in t.bels],
                       "config_bits": int(t.globalConfigBits), "supertile_part": bool(t.partOfSuperTile)}
    supertiles = {}
    for name, st in fab.superTileDic.items():
        supertiles[name] = {"dir": str(Path(st.tileDir).parent),
                            "map": [[(t.name if t is not None else None) for t in row] for row in st.tileMap]}
    # border rows: FABulous's top wrapper loads only rows 1 .. rows-2 through ConfigFSM
    border_cfg = sorted({grid[y][x] for y in (0, fab.numberOfRows - 1) for x in range(fab.numberOfColumns)
                         if grid[y][x] and types[grid[y][x]]["config_bits"]})
    if border_cfg:
        raise SystemExit(f"fabtest: unsupported fabric: border-row tiles with config bits {border_cfg}")
    try:
        import importlib.metadata as md
        version = md.version("FABulous-FPGA")
    except Exception:
        version = "unknown"
    out.write_text(json.dumps({
        "project": str(proj), "fabulous_version": version,
        "params": {"rows": fab.numberOfRows, "cols": fab.numberOfColumns,
                   "frame_bits": fab.frameBitsPerRow, "frames": fab.maxFramesPerCol,
                   "mux_style": str(getattr(fab, "multiplexerStyle", "")).split(".")[-1].lower()},
        "grid": grid, "types": types, "supertiles": supertiles}, indent=1))
    print(f"fabulous_loader: {fab.numberOfRows} x {fab.numberOfColumns}, {len(types)} tile types, "
          f"{len(supertiles)} supertiles -> {out}")


if __name__ == "__main__":
    main()
