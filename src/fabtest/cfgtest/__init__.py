"""Configuration-logic test (fabtest P8): the fabric's config path through every port.

The fabric test configs exercise the fabric, not the logic that loads them: eFPGA_top's
eFPGA_Config (ConfigFSM, config_UART, bitbang), the frame data registers and the frame
selects. Here that logic is simulated on its own:

  <work>/cfgtest/
    cfg_top.v       eFPGA_top.v without the fabric and the macros, unchanged otherwise, and
                    cfg_mem in place of the fabric: one latch per config bit the fabric's
                    ConfigMem modules use (enable FrameStrobe[MaxFramesPerCol * X + frame],
                    data FrameData[FrameBitsPerRow * y + bit]), brought out as CM[]
    cfg_bits.json   (column X, frame, row y, frame-data bit) of every CM bit
    netlist.json    generic gates (fabtest synth's flow; async resets made synchronous: the
                    test holds resetn low over clock edges), faults.csv (cfg_mem left out)
  results/<fabric>/<run>/cfgtest/
    matrix.csv      every fault: status in CFG (CM, ComActive, ReceiveLED after each
                    phase) and PINS (the two status pins every 16 clocks, as a tester sees
                    them), any
    report.txt      coverage, per source module, the phases and what the fault-free
                    netlist did with them

The test: reset, two test configs of the run through SelfWriteData (one word per clock,
3 idle clocks after a frame), then frames of the second config inverted through bitbang
(two frames), UART binary (one) and UART hex (one): every phase writes data different
from what the memory holds, so a write that does not happen is seen. A fault is detected
when a CM bit or a status pin differs from the fault-free run at a sample. A config bit
seen wrong here means a fabric configured wrong on silicon (the fabric test then fails).
A phase the fault-free netlist does not deliver (FABulous's config_UART hex mode never
completes a byte) is expected as not written, and reported.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import subprocess
import time
from collections import Counter
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve()
SYNC, PINS_EVERY = 0xFAB0FAB1, 16
ASYNC2SYNC = "async2sync t:$_DFF_* t:$_DFFE_* t:$_DFFSR_* t:$_DFFSRE_*"


# ------------------------------------------------------------------ the netlist

def used_bits(ff) -> list[tuple[int, int, int, int]]:
    """(column X, frame, row y, frame-data bit) of every ConfigMem bit of the fabric."""
    from fabtest.model import tile_model
    cm = {}
    for t in ff.tiles:
        typ = ff.types[t]
        if typ not in cm:
            cm[typ] = ff.gen[typ].configmem if typ in ff.gen else \
                tile_model.TileType(ff.spec.type_dir(typ), typ).configmem
    return sorted({(t[0] + 1, fr, t[1], fd) for t in ff.tiles for fr, fd in cm[ff.types[t]].values()})


def _blocks(text: str, module: str, inst: str) -> str:
    """text without the instance `module [#(...)] inst (...);`."""
    m = re.search(rf"\n[ \t]*{re.escape(module)}\b[^;]*?\b{re.escape(inst)}\s*\(", text, flags=re.S)
    if not m:
        raise SystemExit(f"fabtest cfgtest: eFPGA_top.v: instance {module} {inst} not found")
    end = text.index(");", m.end())
    return text[:m.start()] + "\n" + text[end + 2:]


def gen_top(spec, bits: list) -> str:
    """cfg_top.v: eFPGA_top without the fabric and the macros, with cfg_mem."""
    p = spec.params
    rows, cols = p["rows"], p["cols"]
    fbits, frames = p["frame_bits"], p["frames"]
    v = (spec.proj / "Fabric" / "eFPGA_top.v").read_text()
    m = re.search(r"\n[ \t]*eFPGA\b[^;]*?\b(\w+)\s*\(\s*\n?\s*\.", v, flags=re.S)
    if not m:
        raise SystemExit("fabtest cfgtest: eFPGA_top.v: no eFPGA instance")
    v = _blocks(v, "eFPGA", m.group(1))
    for mi in spec.d["top"]["macros"]:
        v = _blocks(v, mi["module"], mi["inst"])
    v = v.replace("module eFPGA_top", "module cfg_top", 1)
    hm = re.search(r"module cfg_top\s*(?:#\s*\((?:[^()]|\([^()]*\))*\)\s*)?\(", v, flags=re.S)
    head, rest = v[:hm.end()], v[hm.end():]
    ports, body = rest.split(");", 1)
    keep = []
    for ln in ports.split("\n"):
        name = re.search(r"(\w+)\s*,?\s*(?://.*)?$", ln.strip())
        if re.match(r"\s*(input|output)\b", ln) and name and not re.search(rf"\b{name.group(1)}\b", body):
            continue   # a pad port of the fabric: nothing drives or reads it any more
        keep.append(ln)
    ports = "\n".join(keep).rstrip().rstrip(",") + f",\n        output  [{len(bits) - 1}:0] CM"
    inst = "cfg_mem Inst_cfg_mem (.FrameData(FrameData), .FrameStrobe(FrameSelect), .CM(CM));"
    body = body.replace("endmodule", inst + "\nendmodule", 1)
    lines = [f"module cfg_mem (input wire [{fbits * rows - 1}:0] FrameData, "
             f"input wire [{frames * cols - 1}:0] FrameStrobe,",
             f"                output reg [{len(bits) - 1}:0] CM);"]
    by: dict = {}
    for k, (x, fr, y, fd) in enumerate(bits):
        by.setdefault(frames * x + fr, []).append((k, fbits * y + fd))
    for s_, lst in sorted(by.items()):   # one latch group per frame strobe
        lines.append(f"    always @(*) if (FrameStrobe[{s_}]) begin")
        lines += [f"        CM[{k}] = FrameData[{d}];" for k, d in lst]
        lines.append("    end")
    lines += ["endmodule", ""]
    return head + ports + "\n);" + body + "\n\n" + "\n".join(lines)


def sources(spec, top: Path) -> list[Path]:
    """cfg_top.v and the Fabric/ files of the modules it reaches."""
    defs = {}
    for f in sorted((spec.proj / "Fabric").glob("*.v")):
        for mod in re.findall(r"^\s*module\s+(\w+)", f.read_text(), flags=re.M):
            defs.setdefault(mod, f)
    out, todo, seen = [top], [top], {"cfg_top", "cfg_mem"}
    while todo:
        text = re.sub(r"//.*", "", todo.pop().read_text())
        for mod in sorted(defs):
            if mod not in seen and re.search(rf"^\s*{re.escape(mod)}\b\s*(#|\w)", text, flags=re.M):
                seen.add(mod)
                if defs[mod] not in out:
                    out.append(defs[mod])
                    todo.append(defs[mod])
    return out


def stamp_hash(work: Path) -> str:
    from fabtest import synth
    h = hashlib.sha1((work / "spec.json").read_bytes())
    for f in (HERE, synth.TEMPLATE, Path(synth.__file__), HERE.parents[1] / "patterns" / "faults" / "gen_faults.py"):
        h.update(f.read_bytes())
    return h.hexdigest()


def build(spec, work: Path, force: bool = False) -> Path:
    """<work>/cfgtest: cfg_top.v, cfg_bits.json, netlist.json, faults.csv."""
    from fabtest import project, synth
    from fabtest.model.generic import GenericFabric
    out = work / "cfgtest"
    h = stamp_hash(work)
    if not force and project.stamp_ok(work, "cfgtest", h) and (out / "faults.csv").exists():
        print(f"fabtest cfgtest: {out} is up to date")
        return out
    out.mkdir(parents=True, exist_ok=True)
    ff = GenericFabric(spec)
    bits = used_bits(ff)
    top = out / "cfg_top.v"
    top.write_text(gen_top(spec, bits))
    (out / "cfg_bits.json").write_text(json.dumps(bits))
    srcs = sources(spec, top)
    print(f"fabtest cfgtest: {len(bits)} config bits; {', '.join(p.name for p in srcs)}", flush=True)
    s = synth.TEMPLATE.read_text().replace("@TOP@", "cfg_top").replace("@SOURCES@", " ".join(map(str, srcs)))
    s = s.replace("@TAGS@", "").replace("@CHECK@", "").replace("@OUT@", str(out / "netlist"))
    s = "\n".join(ASYNC2SYNC + "\n" + synth.PATCH[1] if ln.startswith(synth.PATCH[0]) else ln
                  for ln in s.split("\n"))
    (out / "cfg_top.ys").write_text(s)
    r = subprocess.run([synth.yosys(), "-q", "-l", str(out / "cfg_top.log"), "-s", str(out / "cfg_top.ys")],
                       cwd=synth.ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"fabtest cfgtest: yosys failed:\n{(out / 'cfg_top.log').read_text()[-3000:]}")
    (out / "netlist.v").unlink(missing_ok=True)
    allf = out / "faults_all.csv"
    r = subprocess.run(["python3", str(HERE.parents[1] / "patterns" / "faults" / "gen_faults.py"),
                        str(out / "netlist.json"), "--out", str(allf)], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"fabtest cfgtest: gen_faults failed:\n{r.stdout[-2000:]}{r.stderr[-2000:]}")
    rows = list(csv.DictReader(allf.open()))
    # the cfg_mem latches (the cells driving CM) are the model: their faults are left out;
    # by driver, not by name (FrameData and FrameSelect nets also carry cfg_mem port names)
    m = json.loads((out / "netlist.json").read_text())["modules"]["cfg_top"]
    cm = set(m["ports"]["CM"]["bits"])
    model = {n for n, c in m["cells"].items() for pin, sig in c["connections"].items()
             if c["port_directions"][pin] == "output" and cm & set(sig)}
    keep = [x for x in rows if x["driver"].rsplit(".", 1)[0] not in model and not x["driver"].startswith("port:CM")]
    for k, x in enumerate(keep):
        x["id"] = k
    with (out / "faults.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(keep)
    allf.unlink()
    print(f"fabtest cfgtest: {len(m['cells']) - len(model)} cells and {len(model)} cfg_mem latches, "
          f"{len(keep)} faults (of {len(rows)}; cfg_mem left out)", flush=True)
    project.stamp(work, "cfgtest", h)
    return out


# ------------------------------------------------------------------ the stimulus

def uart_params(spec) -> tuple[int, int]:
    """config_UART's ComRate (clocks per bit) and RX_TIMEOUT_VALUE (defaults: FABulous's)."""
    f = spec.proj / "Fabric" / "config_UART.v"
    t = f.read_text() if f.exists() else ""
    rate = re.search(r"ComRate\s*=\s*(?:\d+'d)?(\d+)", t)
    tmo = re.search(r"RX_TIMEOUT_VALUE\s*=\s*(?:\d+'d)?(\d+)", t)
    return int(rate.group(1)) if rate else 217, int(tmo.group(1)) if tmo else 16665


def config_frames(ff, data: dict, c: int) -> dict:
    """(column X, frame) -> {row y: word} of config c (the words export writes)."""
    from fabtest.sim import stimuli as rf
    sels, _, luts = rf.config_setup(ff, data, c)
    fr: dict = {}
    for t, w in ff.frame_words(sels, rf.lut_bits(luts)).items():
        for f in range(ff.frames):
            fr.setdefault((t[0] + 1, f), {})[t[1]] = w[f] if f < len(w) else 0
    return fr


def frame_stream(frames: dict, rows: int) -> list:
    """Sync, per frame the address word and the rows (rows .. 1), None: idle after a frame."""
    w = [SYNC]
    for (x, f), col in sorted(frames.items()):
        w.append((x << 27) | (1 << f))
        w += [col.get(y, 0) for y in range(rows, 0, -1)]
        w.append(None)
    return w + [1 << 20]


class Stim:
    def __init__(self, ins: list[str]):
        self.ins = ins
        self.rows, self.sample = [], []
        self.state = {n: 0 for n in ins}
        self.state["Rx"] = 1

    def clock(self, n: int = 1, **kw):
        self.state.update(kw)
        for _ in range(n):
            r = [self.state[k] for k in self.ins]
            r[0] = 0
            self.rows.append(r)
            self.sample.append(0)
            r = list(r)
            r[0] = 1
            self.rows.append(r)
            self.sample.append(0)

    def mark(self):
        self.clock(4)
        self.sample[-1] = 1

    def data(self, w: int) -> dict:
        return {f"SelfWriteData[{k}]": (w >> k) & 1 for k in range(32)}

    def parallel(self, words: list):
        for w in words:
            if w is None:
                self.clock(3, SelfWriteStrobe=0)
            else:
                self.clock(1, SelfWriteStrobe=1, **self.data(w))
        self.clock(4, SelfWriteStrobe=0, **self.data(0))

    def bitbang(self, words: list, H: int = 4):
        def word(d: int, ctl: int):
            for i in range(31, -1, -1):
                self.clock(H, s_data=(d >> i) & 1)
                self.clock(H, s_clk=1)
                self.clock(H, s_data=(ctl >> i) & 1)
                self.clock(H, s_clk=0)
        word(0, 0)
        for w in words:
            if w is not None:
                word(w, 0xFAB1)
        word(0, 0xFAB0)   # active off
        self.clock(8, s_data=0)

    def uart(self, words: list, hexmode: bool, rate: int, timeout: int):
        def byte(b: int):
            for bit in [0] + [(b >> k) & 1 for k in range(8)] + [1]:
                self.clock(rate, Rx=bit)
        for b in (0x00, 0xAA, 0xFF, 0x81 if hexmode else 0x01):
            byte(b)
        for w in words:
            if w is None:
                continue
            for k in (3, 2, 1, 0):
                b = (w >> (8 * k)) & 0xFF
                if hexmode:
                    for nib in (b >> 4, b & 15):
                        byte(ord("0123456789ABCDEF"[nib]))
                else:
                    byte(b)
        self.clock(timeout + 200, Rx=1)


PHASES = ("parallel0", "parallel1", "bitbang", "uart", "uarthex")


def stimuli(spec, ff, data: dict, cfgs: list[int], bits: list, skip: set = frozenset()):
    """[CFG, PINS] stimuli and the phase list; skip: phases expected as not written."""
    from fabtest.sim.fastsim import fastsim
    ins = ["CLK", "resetn", "SelfWriteStrobe"] + [f"SelfWriteData[{k}]" for k in range(32)] + ["Rx", "s_clk", "s_data"]
    rows_n = spec.params["rows"] - 2
    rate, timeout = uart_params(spec)
    cw = [config_frames(ff, data, c) for c in cfgs]
    st = Stim(ins)
    st.clock(3, resetn=0)
    st.clock(3, resetn=1)
    pos = {b: k for k, b in enumerate(map(tuple, bits))}
    cm: dict = {}
    expected = []

    def written(frames):
        for (x, f), col in frames.items():
            for y, w in col.items():
                for d in range(32):
                    k = pos.get((x, f, y, d))
                    if k is not None:
                        cm[k] = (w >> d) & 1

    def snap():
        expected.append("".join(str(cm[k]) if k in cm else "x" for k in range(len(bits))) + "xx")

    phases = []
    for i, fr in enumerate(cw):
        st.parallel(frame_stream(fr, rows_n))
        written(fr)
        st.mark()
        snap()
        phases.append((f"parallel{i}", len(fr)))
    keys = sorted(cw[-1])
    n = len(keys)
    inv = {k: {y: ~w & 0xFFFFFFFF for y, w in cw[-1][k].items()} for k in keys}
    for name, sel in (("bitbang", keys[n // 10:n // 10 + 2]), ("uart", keys[n // 5:n // 5 + 1]),
                      ("uarthex", keys[3 * n // 10:3 * n // 10 + 1])):
        fr = {k: inv[k] for k in sel}
        if name == "bitbang":
            st.bitbang(frame_stream(fr, rows_n))
        else:
            st.uart(frame_stream(fr, rows_n), name == "uarthex", rate, timeout)
        if name not in skip:
            written(fr)
        st.mark()
        snap()
        phases.append((name, len(fr)))
    outs = [f"CM[{k}]" for k in range(len(bits))] + ["ComActive", "ReceiveLED"]
    rows = np.array(st.rows, dtype=np.uint8)
    cfg = fastsim.Stim("CFG", ins, rows, np.array(st.sample, dtype=np.uint8), outs, expected)
    ps = np.zeros(len(rows), dtype=np.uint8)
    ps[2 * PINS_EVERY - 1::2 * PINS_EVERY] = 1
    pins = fastsim.Stim("PINS", ins, rows, ps, ["ComActive", "ReceiveLED"], None)
    return [cfg, pins], phases


# ------------------------------------------------------------------ the run

def inputs_key(work: Path, run_dir: Path, configs) -> str:
    """What a cfgtest run is made from: the netlist build, the run's configs, the choice."""
    h = hashlib.sha1(stamp_hash(work).encode())
    for f in (run_dir / "configs.json", run_dir / "faultsim" / "configs.json"):
        h.update(f.read_bytes() if f.exists() else b"-")
    h.update(json.dumps(configs).encode())
    return h.hexdigest()


def up_to_date(work: Path, run_dir: Path, configs=None) -> bool:
    f = run_dir / "cfgtest" / "inputs.json"
    return f.exists() and (run_dir / "cfgtest" / "report.txt").exists() and \
        json.loads(f.read_text()).get("key") == inputs_key(work, run_dir, configs)

def _depths(srcs: list[Path]) -> dict:
    """Module file stem -> depth below cfg_top (instances found in the sources)."""
    mods = {}
    for f in srcs:
        for m in re.findall(r"^\s*module\s+(\w+)", f.read_text(), flags=re.M):
            mods[m] = f
    depth = {"cfg_top": 0}
    todo = ["cfg_top"]
    while todo:
        m = todo.pop(0)
        text = mods[m].read_text() if m in mods else ""
        body = text[text.find(f"module {m}"):]
        for c in mods:
            if c not in depth and re.search(rf"^\s*{re.escape(c)}\b\s*(#|\w)", body, flags=re.M):
                depth[c] = depth[m] + 1
                todo.append(c)
    return {mods[m].stem if m != "cfg_mem" else "cfg_mem": d for m, d in depth.items() if m in mods}


def part_map(netlist: Path, srcs: list[Path]) -> dict:
    """cell name -> RTL module it comes from: the deepest source module in its src
    attribute; cells only the techmaps name (decomposed muxes) take the module of the
    cells they connect to."""
    depth = _depths(srcs)
    m = json.loads(netlist.read_text())["modules"]["cfg_top"]
    part, drv, sinks = {}, {}, {}
    for name, c in m["cells"].items():
        files = {Path(re.sub(r":[\d.\-]+$", "", x)).stem for x in c.get("attributes", {}).get("src", "").split("|") if x}
        known = [f for f in files if f in depth]
        if known:
            part[name] = max(known, key=lambda f: depth[f])
        for pin, sig in c["connections"].items():
            for b in sig:
                if isinstance(b, int):
                    if c["port_directions"][pin] == "output":
                        drv[b] = name
                    else:
                        sinks.setdefault(b, []).append(name)
    nbr: dict = {}
    for b, d in drv.items():
        for s_ in sinks.get(b, ()):
            nbr.setdefault(d, []).append(s_)
            nbr.setdefault(s_, []).append(d)
    left = [n for n in m["cells"] if n not in part]
    while left:
        got = {n: next((part[x] for x in nbr.get(n, ()) if x in part), None) for n in left}
        got = {n: p for n, p in got.items() if p}
        if not got:
            break
        part.update(got)
        left = [n for n in left if n not in part]
    return part


def run(spec, work: Path, run_dir: Path, configs: list[int] | None = None, jobs: int = 4) -> str:
    from fabtest.model.generic import GenericFabric
    from fabtest.sim.fastsim import fastsim
    t0 = time.time()
    key = inputs_key(work, run_dir, configs)
    d = build(spec, work)
    bits = json.loads((d / "cfg_bits.json").read_text())
    data = json.loads((run_dir / "configs.json").read_text())
    kept_f = run_dir / "faultsim" / "configs.json"
    if configs is None:
        kept = json.loads(kept_f.read_text())["kept"] if kept_f.exists() else [f"FF{c:02d}" for c in range(len(data["configs"]))]
        configs = [int(n[2:]) for n in kept[:2]]
    if not configs:
        raise SystemExit("fabtest cfgtest: no config to load")
    ff = GenericFabric(spec)
    nl = fastsim.Netlist("cfgtest", d / "netlist.json")
    skip: set = set()
    err = None
    for skip in (set(), {"uarthex"}, {"uart", "uarthex"}, {"bitbang", "uart", "uarthex"}):
        stims, phases = stimuli(spec, ff, data, configs, bits, skip)
        err = fastsim.fault_free(nl, stims[:1], 1)["CFG"]
        if err is None:
            break
    if err is not None:
        raise SystemExit(f"fabtest cfgtest: the fault-free load differs from the intended config bits: {err}")
    steps = stims[0].rows.shape[0]
    print(f"fabtest cfgtest: configs {configs}, {steps} steps; fault-free load = the intended config bits"
          + (f" (not delivered by the netlist: {', '.join(sorted(skip))})" if skip else ""), flush=True)
    fl = list(csv.DictReader((d / "faults.csv").open()))
    names, stat = fastsim.run_faults("cfgtest", stims, fl, jobs, nl=nl, quiet=True)
    out = run_dir / "cfgtest"
    out.mkdir(parents=True, exist_ok=True)

    def any_(s):
        v = set(s.values())
        return "D" if "D" in v else "P" if "P" in v else "T" if "T" in v else "U"
    parts = part_map(d / "netlist.json", sources(spec, d / "cfg_top.v"))
    tot, det, pos = Counter(), Counter(), Counter()
    with (out / "matrix.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "path", "polarity", "part", *names, "any"])
        for f, s in zip(fl, stat):
            a = any_(s)
            cell = f["driver"].rsplit(".", 1)[0]
            p = "ports" if f["driver"].startswith("port:") else parts.get(cell, "?")
            w.writerow([f["id"], f["path"], f["polarity"], p, *[s[c] for c in names], a])
            for k in (p, "all"):
                tot[k] += 1
                det[k] += a == "D"
                pos[k] += a == "P"
    L = [f"fabtest cfgtest: all {len(fl)} faults of the configuration logic, configs "
         f"{', '.join(f'FF{c:02d}' for c in configs)}, {time.time() - t0:.0f} s",
         "  phases: " + ", ".join(f"{n} ({k} frames)" for n, k in phases)]
    if skip:
        L.append(f"  not delivered by the fault-free netlist (expected as not written): {', '.join(sorted(skip))}")
    for k in sorted(tot, key=lambda k: (k != "all", -tot[k])):
        L.append(f"  {k:20s} {tot[k]:7d} faults: {100 * det[k] / tot[k]:6.2f}% detected, "
                 f"{100 * pos[k] / tot[k]:5.2f}% X only")
    text = "\n".join(L) + "\n"
    (out / "report.txt").write_text(text)
    (out / "inputs.json").write_text(json.dumps({"key": key, "configs": configs, "phases": phases,
                                                  "not_delivered": sorted(skip)}))
    return text
