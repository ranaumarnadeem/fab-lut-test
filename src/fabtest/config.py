"""fabtest.toml (per-fabric settings) and suite files (routing recipes).

<FABulous project>/fabtest.toml, optional; copied by `fabtest init` into the work dir:

  [route]
  max_pop = 3000          # Dijkstra exploration per route (default: scaled by tile count)
  max_reach = 10000       # incremental loop check (exact check once per config)
  loop_max_moves = 20000  # mux moves to break the loops of a config
  bram_max_pop = 60000    # routes from the pads to a BlockRAM column
  const_radius = 2        # a pinned pin takes an existing constant this many tiles away
  [faultsim]
  sample = 2000           # 0: every fault
  seed = 1
  chunk = 2000
  jobs = 4
  [bels]
  opaque = []             # BEL or macro modules modelled as X sources (not tested)
  [vectors]
  random = 54             # random vectors per config, after the 2 x log2(pads) counting ones
                          # (vectors cost ~1 clock each, a config load thousands)

The route caps default to the demo's values, scaled up by tile count (a fabric with
more tiles than the demo's 158 gets proportionally larger caps; smaller ones keep the
demo values).

Suite files (src/fabtest/suites/<name>.toml, or a path): routing passes, each routed on
top of the configs of the passes before it:

  name = "demo"
  [[pass]]
  configs = 96
  chains = 2
"""
from __future__ import annotations

import copy
import hashlib
import json
import tomllib
from pathlib import Path

DEMO_TILES = 158
DEFAULTS = {
    "route": {"max_pop": 3000, "max_reach": 10000, "loop_max_moves": 20000,
              "bram_max_pop": 60000, "const_radius": 2},
    "faultsim": {"sample": 2000, "seed": 1, "chunk": 2000, "jobs": 4},
    "bels": {"opaque": []},
    "vectors": {"random": 54},
}
SCALED = ("max_pop", "max_reach", "loop_max_moves", "bram_max_pop")
SUITES = Path(__file__).resolve().parent / "suites"
NAME = "fabtest.toml"


def _read(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(f"fabtest: {path}: {e}")


def _merge(base: dict, over: dict, where: str) -> dict:
    out = copy.deepcopy(base)
    for sec, vals in over.items():
        if sec not in out or not isinstance(vals, dict):
            raise SystemExit(f"fabtest: {where}: unknown section [{sec}] (known: {', '.join(out)})")
        for k, v in vals.items():
            if k not in out[sec]:
                raise SystemExit(f"fabtest: {where}: unknown key {sec}.{k} (known: {', '.join(out[sec])})")
            if type(v) is not type(out[sec][k]):
                raise SystemExit(f"fabtest: {where}: {sec}.{k} must be {type(out[sec][k]).__name__}")
            out[sec][k] = v
    return out


def load(work: Path, n_tiles: int | None = None) -> dict:
    """Settings of a work dir: defaults (route caps scaled by n_tiles), then
    <work>/project/fabtest.toml."""
    d = copy.deepcopy(DEFAULTS)
    if n_tiles and n_tiles > DEMO_TILES:
        for k in SCALED:
            d["route"][k] = int(round(d["route"][k] * n_tiles / DEMO_TILES))
    f = Path(work) / "project" / NAME
    return _merge(d, _read(f), str(f)) if f.exists() else d


def file_hash(proj: Path) -> str:
    """Hash of the project's fabtest.toml ("" when there is none)."""
    f = Path(proj) / NAME
    return hashlib.sha1(f.read_bytes()).hexdigest() if f.exists() else ""


def suite(name: str) -> dict:
    """A suite by name (src/fabtest/suites/<name>.toml) or path: {"name", "passes"}."""
    p = Path(name)
    if not p.exists():
        p = SUITES / f"{name}.toml"
    if not p.exists():
        known = ", ".join(sorted(q.stem for q in SUITES.glob("*.toml")))
        raise SystemExit(f"fabtest: no suite {name} (known: {known}, or a path)")
    d = _read(p)
    passes = d.get("pass") or []
    if not passes:
        raise SystemExit(f"fabtest: {p}: no [[pass]]")
    out = []
    for i, ps in enumerate(passes):
        extra = set(ps) - {"configs", "chains"}
        if extra or not isinstance(ps.get("configs"), int) or ps["configs"] < 1:
            raise SystemExit(f"fabtest: {p}: pass {i}: configs (int >= 1) and chains only")
        out.append({"configs": ps["configs"], "chains": int(ps.get("chains", 2))})
    return {"name": d.get("name", p.stem), "passes": out}


def sha(obj) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True).encode()).hexdigest()
