"""Gate-level netlist and fault list of a FABulous fabric (fabtest P2).

From the spec (fabtest/spec.py) and the project copy:
  1. every module eFPGA.v instantiates (the tile types and the supertiles, all their
     .v sources from the tile directories, plus Fabric/models_pack.v) to generic gates
     with synth/synth_generic.ys.in, storage legalized to $_DFFE_PP_ / $_DLATCH_P_, XOR
     decomposed (as synth/build_fullfabric.sh did);
  2. when eFPGA_top.v has macros (e.g. BlockRAM_1KB): a wrapper fabtest_top = eFPGA plus
     the macro instances, wired as in eFPGA_top (the config logic is left out: the test
     drives FrameData / FrameStrobe); a macro's source from Fabric/<module>.v, blackboxes
     inside it with a known model (fabtest/models/) replaced by the model, which carries
     no faults; a macro without source is left out, its outputs X (undriven);
  3. flattened; xgen cells removed (their output undriven: X in the fast kernel, as the
     SRAM model's read during a write of the same address); every net bit with more than
     one name keeps one (the name inside the instance that drives it), as gen_faults needs;
  4. patterns/faults/gen_faults.py; faults inside models and on the X sources left out.
Outputs in <work>/synth/: netlist.json, faults.csv, tiles/*.json, logs.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fabtest.paths import REPO as ROOT  # noqa: E402
from fabtest.paths import SYNTH  # noqa: E402
TEMPLATE = SYNTH / "synth_generic.ys.in"
MODELS = {"sram_1rw1r_32_256_8_sky130": ROOT / "rtl" / "bram" / "sram_model.v"}
PATCH = ("techmap -map synth/mux_decompose.v",
         f"dfflegalize -cell $_DFFE_PP_ 01 -cell $_DLATCH_P_ x\ntechmap -map {SYNTH / 'xor_decompose.v'} t:$_XOR_\n"
         f"simplemap\ntechmap -map {SYNTH / 'mux_decompose.v'}")


def yosys() -> str:
    env = os.environ.get("YOSYS")
    if env:
        return env
    for cand in ("/home/user/ext/venv/bin/yowasp-yosys", shutil.which("yowasp-yosys"), shutil.which("yosys")):
        if cand and Path(cand).exists():
            return cand
    raise SystemExit("fabtest: no yosys found (set $YOSYS)")


def _script(top: str, sources: list[Path], out: Path) -> str:
    s = TEMPLATE.read_text()
    s = s.replace("@TOP@", top).replace("@SOURCES@", " ".join(str(p) for p in sources))
    s = s.replace("@TAGS@", "").replace("@CHECK@", "").replace("@OUT@", str(out))
    lines = [PATCH[1] if ln.startswith(PATCH[0]) else ln for ln in s.split("\n")]
    return "\n".join(lines)


def _run(top: str, sources: list[Path], outdir: Path) -> Path:
    out = outdir / top
    j = out.with_suffix(".json")
    if j.exists() and all(j.stat().st_mtime > p.stat().st_mtime for p in sources):
        return j   # up to date
    ys = outdir / f"{top}.ys"
    ys.write_text(_script(top, sources, out))
    r = subprocess.run([yosys(), "-q", "-l", str(outdir / f"{top}.log"), "-s", str(ys)],
                       cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        log = (outdir / f"{top}.log").read_text()[-3000:] if (outdir / f"{top}.log").exists() else r.stderr
        raise SystemExit(f"fabtest synth: yosys failed on {top}:\n{log}")
    return out.with_suffix(".json")


def _top_modules(spec) -> list[str]:
    """Module types eFPGA.v instantiates (tile types not inside a supertile, supertiles)."""
    text = re.sub(r"`ifdef EMULATION.*?`endif", "", (spec.proj / "Fabric" / "eFPGA.v").read_text(), flags=re.S)
    return sorted(set(re.findall(r"(?:\(\*\s*keep\s*\*\)\s*)?\b(\w+)\s+Tile_X\d+Y\d+_\w+\s*\(", text)) - {"module"})


def _sources(spec, mod: str) -> list[Path]:
    models = spec.proj / "Fabric" / "models_pack.v"
    src = [models] if models.exists() else []
    if mod in spec.supertiles:
        d = Path(spec.supertiles[mod]["dir"])
        src.append(d / f"{mod}.v")
        for row in spec.supertiles[mod]["map"]:
            for sub in row:
                if sub:
                    src += sorted(Path(spec.types[sub]["dir"]).glob("*.v"))
        return src
    return src + sorted(Path(spec.types[mod]["dir"]).glob("*.v"))


def _wrapper(spec, out: Path, have_src: set[str]) -> str | None:
    """fabtest_top: eFPGA + the macro instances of eFPGA_top (those with a source)."""
    top = spec.d["top"]
    if not top["macros"]:
        return None
    text = (spec.proj / "Fabric" / "eFPGA_top.v").read_text()
    efpga = spec.d["efpga"]["ports"]
    params = re.search(r"module\s+eFPGA\s*#\s*\((.*?)\)\s*\(", (spec.proj / "Fabric" / "eFPGA.v").read_text(), re.S)
    pmap = top["port_map"]
    # eFPGA ports on a wire bus that reaches a macro become internal (bits no macro reads
    # dangle, as in eFPGA_top); others (FrameData / FrameStrobe, chip pins) stay ports
    mbus = {b.split("[")[0] for mi in top["macros"] if mi["module"] in have_src
            for bits in mi["pins"].values() for b in bits}
    internal = {p.split("[")[0] for p, m in pmap.items() if "wire" in m and m["wire"].split("[")[0] in mbus}
    keep = [p for p in efpga if p not in internal]
    wires = {}
    for p, m in pmap.items():
        if "wire" in m and p.split("[")[0] in internal:
            b = m["wire"].split("[")[0]
            k = int(m["wire"].split("[")[1][:-1]) if "[" in m["wire"] else 0
            wires[b] = max(wires.get(b, 0), k + 1)
    inst = re.search(r"\neFPGA\s+(\w+)\s*\((.*?)\n\);", text, re.S)
    conns = re.findall(r"\.(\w+)\(([^)]*)\)", inst.group(2))
    L = ["// generated by fabtest/synth.py: eFPGA + the macros of eFPGA_top",
         f"module fabtest_top #({params.group(1).strip()}) (" if params else "module fabtest_top ("]
    L.append(",\n".join(f"    {efpga[p][0]} {'[%d:0] ' % (efpga[p][1] - 1) if efpga[p][1] > 1 else ''}{p}" for p in keep)
             + ",\n    input XSRC")
    L.append(");")
    L += [f"wire [{w - 1}:0] {b};" for b, w in sorted(wires.items())]
    L.append("wire CLK = UserCLK;")
    L.append("eFPGA eFPGA_inst (")
    L.append(",\n".join(f"    .{p}({e if p in internal else p})" for p, e in conns if p in efpga))
    L.append(");")
    for m in top["macros"]:
        if m["module"] not in have_src:
            continue
        blk = re.search(rf"\n{m['module']}\s+(?:#\s*\(.*?\)\s*)?{m['inst']}\s*\(.*?\n\);", text, re.S)
        L.append(blk.group(0).strip())
    L.append("endmodule")
    path = out / "fabtest_top.v"
    path.write_text("\n".join(L) + "\n")
    return str(path)


def _fix_names(m: dict, macros: list) -> tuple[int, int]:
    """xgen cells and undriven read nets -> input XSRC[] (X sources, no faults); one name
    per net bit."""
    x = [c for c, v in m["cells"].items() if v["type"] == "xgen"]
    xbits = [m["cells"][c]["connections"]["y"][0] for c in x]
    for c in x:
        del m["cells"][c]
    # undriven nets that are read (inputs FABulous leaves open at the fabric edge, e.g. the
    # west-going wires into the east column): X sources as well
    driven = {b for p in m["ports"].values() if p["direction"] == "input" for b in p["bits"] if isinstance(b, int)}
    read = set()
    for c in m["cells"].values():
        for p, d in c.get("port_directions", {}).items():
            bits = {b for b in c["connections"][p] if isinstance(b, int)}
            (driven if d == "output" else read).update(bits)
    xbits += sorted(read - driven - set(xbits))
    if xbits:
        old = m["ports"].get("XSRC", {}).get("bits", [])
        m["ports"]["XSRC"] = {"direction": "input", "bits": old + xbits}
        m["netnames"]["XSRC"] = {"hide_name": 0, "bits": old + xbits, "attributes": {}}
    m["netnames"] = {re.sub(r"^eFPGA_inst\.", "", n): v for n, v in m["netnames"].items()}
    m["cells"] = {re.sub(r"^eFPGA_inst\.", "", n): v for n, v in m["cells"].items()}
    drv = {}
    for cn, c in m["cells"].items():
        for p, d in c.get("port_directions", {}).items():
            if d == "output":
                for b in c["connections"][p]:
                    if isinstance(b, int):
                        drv[b] = cn.rsplit(".", 1)[0] + "." if "." in cn else ""
    ports = set(m["ports"])
    names: dict[int, list[str]] = {}
    for n, v in m["netnames"].items():
        for b in v["bits"]:
            if isinstance(b, int):
                names.setdefault(b, []).append(n)
    # only nets the wrapper adds names to (a name outside every tile instance): tile-to-tile
    # nets keep all their names, gen_faults resolves those itself
    macro_ports = {f"{mi['inst']}.{pin}" for mi in macros for pin in mi["pins"]}

    def is_port(n):
        base = n.split("[")[0]
        return base in macro_ports or ".memory_cell." in n and base.rsplit(".", 1)[-1][:-1] in (
            "addr", "din", "dout", "clk", "csb", "web", "wmask")
    keep: dict[int, str] = {}
    for b, ns in names.items():
        pre = drv.get(b)
        if len(ns) < 2 or any(n in ports for n in ns):
            continue
        if all(n.startswith("Tile_") for n in ns) and pre is not None:
            continue   # driven tile-to-tile net: gen_faults picks the driver's name
        # (undriven: a ring of wires through the tiles, e.g. 4-wires bouncing between the
        # N / S term tiles of a short fabric; no driver to pick a name: keep one here)
        # a name inside the driver's instance, but not inside a model (keep mem_dout over
        # memory_cell.dout0: model cells are left out of the fault list)
        inside = ([n for n in ns if pre and n.startswith(pre) and ".memory_cell." not in n]
                  or [n for n in ns if ".memory_cell." not in n] or ns)
        keep[b] = min(inside, key=lambda n: (is_port(n), n.count("."), len(n), n))
    # a name goes when none of its bits keeps it and every bit keeps another one; a bus
    # where only some bits go is split into one name per bit first
    gone = 0
    for n in list(m["netnames"]):
        v = m["netnames"][n]
        bits = v["bits"]
        drop = [isinstance(b, int) and b in keep and keep[b] != n for b in bits]
        if not any(drop):
            continue
        if all(drop):
            del m["netnames"][n]
            gone += 1
            continue
        off = v.get("offset", 0)
        del m["netnames"][n]
        for k, (b, d) in enumerate(zip(bits, drop)):
            if not d:
                m["netnames"][f"{n}[{k + off}]"] = {"hide_name": v.get("hide_name", 0), "bits": [b],
                                                    "attributes": v.get("attributes", {})}
        gone += 1
    return len(x), gone


def stamp_hash(work: Path) -> str:
    """What the netlist and fault list are built from: spec.json and the synthesis code."""
    import hashlib
    h = hashlib.sha1((work / "spec.json").read_bytes())
    here = Path(__file__).resolve().parent
    for f in sorted([*here.glob("*"), here.parent / "patterns" / "faults" / "gen_faults.py"]):
        if f.is_file() and f.suffix in (".py", ".in", ".v"):
            h.update(f.name.encode())
            h.update(f.read_bytes())
    return h.hexdigest()


def build(spec, work: Path, jobs: int = 4, force: bool = False) -> Path:
    from fabtest import project
    out = work / "synth"
    h = stamp_hash(work)
    if not force and project.stamp_ok(work, "synth", h) and (out / "netlist.json").exists() \
            and (out / "faults.csv").exists():
        print(f"fabtest synth: {out} is up to date")
        return out
    tiles = out / "tiles"
    tiles.mkdir(parents=True, exist_ok=True)
    mods = _top_modules(spec)
    jobs_l = [(m, _sources(spec, m)) for m in mods]
    macro_src = {}
    for m in spec.d["top"]["macros"]:
        src = spec.proj / "Fabric" / f"{m['module']}.v"
        if src.exists():
            macro_src[m["module"]] = src
    for mod, src in macro_src.items():
        jobs_l.append((mod, [src]))
    for name, model in MODELS.items():
        if any(name in p.read_text() for p in macro_src.values()):
            jobs_l.append((name, [model]))
    print(f"fabtest synth: {len(jobs_l)} modules ({', '.join(m for m, _ in jobs_l)})", flush=True)
    with ThreadPoolExecutor(max_workers=jobs) as ex:
        res = list(ex.map(lambda j: _run(j[0], j[1], tiles), jobs_l))
    # a macro's json also holds its blackboxes: the models replace them
    for mod in macro_src:
        p = tiles / f"{mod}.json"
        d = json.loads(p.read_text())
        d["modules"] = {k: v for k, v in d["modules"].items() if k == mod or k not in MODELS}
        p.write_text(json.dumps(d))
    wrap = _wrapper(spec, out, set(macro_src))
    top = "fabtest_top" if wrap else "eFPGA"
    ys = out / "top.ys"
    ys.write_text("\n".join([f"read_json {p}" for p in res] +
                            [f"read_verilog -defer {spec.proj / 'Fabric' / 'eFPGA.v'}" + (f" {wrap}" if wrap else ""),
                             f"hierarchy -top {top}", "flatten -noscopeinfo", "stat",
                             f"write_json {out / 'raw.json'}"]) + "\n")
    r = subprocess.run([yosys(), "-q", "-l", str(out / "top.log"), "-s", str(ys)], cwd=ROOT,
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"fabtest synth: yosys failed on the top:\n{(out / 'top.log').read_text()[-3000:]}")
    d = json.loads((out / "raw.json").read_text())
    m = d["modules"][top]
    nx, gone = _fix_names(m, spec.d["top"]["macros"])
    d["modules"] = {top: m}
    (out / "netlist.json").write_text(json.dumps(d))
    (out / "raw.json").unlink()
    ncell = len(m["cells"])
    print(f"fabtest synth: {ncell} cells, {nx} X sources, {gone} alias names removed", flush=True)
    faults(out)
    project.stamp(work, "synth", h)
    return out


def faults(out: Path) -> Path:
    allf = out / "faults_all.csv"
    r = subprocess.run([sys.executable, str(ROOT / "src" / "fabtest" / "patterns" / "faults" / "gen_faults.py"), str(out / "netlist.json"),
                        "--out", str(allf)], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"fabtest synth: gen_faults failed:\n{r.stdout[-2000:]}{r.stderr[-2000:]}")
    import csv
    rows = list(csv.DictReader(allf.open()))
    model_inst = re.compile(r"\.memory_cell\.")
    keep = [r for r in rows if not model_inst.search(r["path"]) and not r["path"].startswith("XSRC")]
    for k, r in enumerate(keep):
        r["id"] = k
    with (out / "faults.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(keep)
    allf.unlink()
    print(f"fabtest synth: {len(keep)} faults (of {len(rows)}; models and X sources left out)", flush=True)
    return out / "faults.csv"
