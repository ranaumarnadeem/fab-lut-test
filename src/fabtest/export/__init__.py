"""Test program export (fabtest P6).

results/<fabric>/<run>/export/
  bitstreams/<cfg>.bin   FABulous bitstream (bit_gen layout): sync header, per column and
                         frame the address word (X << 27) | (1 << f) and the rows R-2 .. 1
                         (big-endian, bit k = FrameData bit k), desync. Frames in the
                         fault-simulated order (sm_configs.frame_order), or 0 .. F-1 with
                         --canonical (byte-equal to bit_gen)
  bitstreams/<cfg>.hex   Test/makehex.py format (one byte per line, padded to 16384)
  vectors/<cfg>.vec      per vector the chip inputs and the expected chip outputs (0 / 1 / x:
                         not checked), after the load; one UserCLK (= CLK) cycle per vector
  vectors/<cfg>.mem      the same for the testbench ($readmemb: inputs, expected, mask)
  tb/fabtest_tb.v        Icarus testbench: load through SelfWriteData, apply, compare
  tb/run_icarus.sh       compile once, run every exported config
  plan.csv               configs in set-cover order with cumulative coverage and load time
  manifest.json          pins, files, word counts, load times, configs left out

The tester sequence per config: reset (resetn low), the .bin words on SelfWriteData with
SelfWriteStrobe (ConfigFSM ignores words until the sync word), then per vector: inputs at
the falling CLK edge, one rising edge, compare at the next falling edge.
"""
from __future__ import annotations

import csv
import json
import math
import os
import pickle
import re
import shutil
import subprocess
from pathlib import Path

from fabtest.paths import TB  # noqa: E402

HEADER = "00AAFF01000000010000000000000000FAB0FAB1"
HEX_BYTES = 16384
CLK_HZ = 50e6        # parallel port: one word per clock
TB_CLKS_PER_WORD = 5  # the demo testbench: 2 idle, strobe, 2 idle
UART_BAUD = 115200


# ------------------------------------------------------------ bitstream

def arch(spec) -> dict:
    """ArchSpecs of the project's bitStreamSpec.bin (FABulous gen_bitstream_spec), else defaults."""
    p = spec.proj / ".FABulous" / "bitStreamSpec.bin"
    a = {}
    if p.exists():
        a = pickle.loads(p.read_bytes()).get("ArchSpecs", {})
    return {"header": a.get("SyncHeaderHex", HEADER), "desync": a.get("DesyncBit", 20),
            "border_rows": a.get("IncludeBorderRows", False)}


def frame_words(ff, words: dict, order: list[int] | None) -> list[int]:
    """Config words after the header: per column X and frame f the address word and the
    rows R-2 .. 1 (ConfigFSM counts the row down), then desync. Model tile (x, y) is
    column x + 1 (W_IO at x = -1)."""
    out = []
    frames = order if order is not None else list(range(ff.frames))
    for X in range(ff.C):
        col = {t[1]: w for t, w in words.items() if t[0] + 1 == X}
        for f in frames:
            out.append((X << 27) | (1 << f))
            out += [(col[y][f] if y in col and f < len(col[y]) else 0) for y in range(ff.R - 2, 0, -1)]
    return out


def bitstream(ff, spec, words: dict, order: list[int] | None) -> bytes:
    a = arch(spec)
    body = frame_words(ff, words, order) + [1 << a["desync"]]
    return bytes.fromhex(a["header"]) + b"".join(w.to_bytes(4, "big") for w in body)


def parse_bitstream(ff, data: bytes, header_len: int = 20) -> dict:
    """bitstream() back to {(X, f): {row y: word}} (round-trip check)."""
    w = [int.from_bytes(data[i:i + 4], "big") for i in range(header_len, len(data), 4)]
    out, i, n = {}, 0, ff.R - 2
    while i < len(w) and not (w[i] >> 20) & 1:
        X, f = w[i] >> 27, (w[i] & ((1 << 20) - 1)).bit_length() - 1
        out[(X, f)] = {ff.R - 2 - k: w[i + 1 + k] for k in range(n)}
        i += 1 + n
    return out


def hexfile(data: bytes, nbytes: int = HEX_BYTES) -> str:
    """Test/makehex.py: one byte per line (two hex digits), padded with "0" lines."""
    if len(data) > nbytes:
        raise SystemExit(f"fabtest export: bitstream of {len(data)} bytes > {nbytes} (MAX_BITBYTES)")
    return "".join(f"{b:02x}\n" for b in data) + "0\n" * (nbytes - len(data))


# ------------------------------------------------------------ FASM (bit_gen cross-check)

def fasm(ff, spec, words: dict) -> tuple[str, list[str]]:
    """FASM whose bit_gen bitstream sets exactly the bits of `words`: per tile every feature
    of the spec (TileSpecs) with at least one 1 whose positions all match the tile's bits
    (a pip with an all-zero code needs no line). Returns (text, problems): set bits no
    feature explains (the model and FABulous disagree on a config bit)."""
    ts = pickle.loads((spec.proj / ".FABulous" / "bitStreamSpec.bin").read_bytes())["TileSpecs"]
    fb = ff.frame_bits
    lines, problems = [], []
    for t, w in sorted(words.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        name = f"X{t[0] + 1}Y{t[1]}"
        bits = {f * fb + k for f, x in enumerate(w) for k in range(fb) if (x >> k) & 1}
        covered = set()
        for feat, enc in ts.get(name, {}).items():
            ones = {p for p, v in enc.items() if int(v)}
            if ones and all((int(v) == 1) == (p in bits) for p, v in enc.items()):
                lines.append(f"{name}.{feat}")
                covered |= ones
        for p in sorted(bits - covered):
            problems.append(f"{name}: bit {p} (frame {p // fb}, bit {p % fb}) set by no FASM feature")
    return "\n".join(lines) + "\n", problems


# ------------------------------------------------------------ vectors

def pins(spec) -> dict:
    """eFPGA port bit -> chip pin (eFPGA_top.v), from the spec's port map."""
    return {p: m["pin"] for p, m in spec.d["top"]["port_map"].items() if "pin" in m}


def _pin_key(p: str):
    m = re.fullmatch(r"(\w+)\[(\d+)\]", p)
    return (m.group(1), int(m.group(2))) if m else (p, 0)


def vector_table(st, spec) -> tuple[list, list, list]:
    """(chip input pins, chip output pins, [(input bits, expected string)] per vector)."""
    pm = pins(spec)
    pad_cols = [(j, pm[n]) for j, n in enumerate(st.in_names) if n in pm and n != "UserCLK"]   # UserCLK = CLK
    clk = st.in_names.index("UserCLK")
    ins = sorted({p for _, p in pad_cols}, key=_pin_key)
    outs_idx = [(k, pm[n]) for k, n in enumerate(st.out_names) if n in pm]
    outs = sorted({p for _, p in outs_idx}, key=_pin_key)
    rows = st.rows
    first = next(i for i in range(len(rows)) if rows[i][clk])   # first UserCLK edge: vector 0
    exp = iter(st.expected)
    table = []
    for r in range(first - 1, len(rows), 3):
        if r + 2 >= len(rows):
            break
        iv = {p: int(rows[r][j]) for j, p in pad_cols}
        if st.sample[r + 2]:
            e = next(exp)
            ov = {p: e[k] for k, p in outs_idx}
        else:
            ov = {}
        table.append(("".join(str(iv[p]) for p in ins), "".join(ov.get(p, "x") for p in outs)))
    return ins, outs, table


def write_vectors(path: Path, name: str, ins, outs, table) -> None:
    L = [f"# fabtest test vectors, config {name}: after the bitstream load, per vector V <inputs> <expected>",
         "# inputs applied at the falling CLK edge, one rising edge, outputs compared at the next falling",
         "# edge; x = not compared",
         "INPUTS " + " ".join(ins), "OUTPUTS " + " ".join(outs)]
    L += [f"V {i} {o}" for i, o in table]
    path.with_suffix(".vec").write_text("\n".join(L) + "\n")
    # testbench memory: {inputs, expected, mask}, MSB first = the last pin
    m = []
    for i, o in table:
        e = "".join("1" if c == "1" else "0" for c in o)
        k = "".join("0" if c == "x" else "1" for c in o)
        m.append((i + e + k)[::-1])
    path.with_suffix(".mem").write_text("\n".join(m) + "\n")


# ------------------------------------------------------------ testbench

def testbench(spec, ins, outs) -> str:
    top = spec.d["top"]["ports"]
    roles_in = {"CLK", "resetn", "SelfWriteStrobe", "SelfWriteData", "Rx", "s_clk", "s_data"}
    ni, no = len(ins), len(outs)
    decl, conn = [], []
    for n, (d, w) in top.items():
        rng = f"[{w - 1}:0] " if w > 1 else ""
        if d == "input":
            init = {"Rx": " = 1'b1", "resetn": " = 1'b1"}.get(n, " = 0")
            decl.append(f"reg {rng}{n}{init};")
        else:
            decl.append(f"wire {rng}{n};")
        conn.append(f"    .{n}({n})")
    bad_in = [p for p in ins if _pin_key(p)[0] in roles_in]
    assert not bad_in, bad_in
    drive = "\n".join(f"            {p} = v[{k}];" for k, p in enumerate(ins))
    obs = "{" + ", ".join(outs[::-1]) + "}"
    t = (TB / "fabtest_tb.v.in").read_text()
    for k, v in {"@NI@": str(ni), "@NO@": str(no), "@DECL@": "\n".join(decl), "@CONN@": ",\n".join(conn),
                 "@HEX_LAST@": str(HEX_BYTES - 1), "@HEX_BYTES@": str(HEX_BYTES), "@OBS@": obs,
                 "@DRIVE@": drive}.items():
        t = t.replace(k, v)
    return t


MODEL_MODULES = {"sram_1rw1r_32_256_8_sky130"}   # empty black boxes in the RTL: tb/sram_sim.v models them
BLACKBOX = re.compile(r"\(\*\s*blackbox\s*\*\)\s*module\s+(\w+)\b.*?endmodule", re.S)


def rtl_files(spec, tb: Path) -> list[str]:
    """Fabric RTL for Icarus: Tile/**/*.v and Fabric/*.v, one file per name (the demo's
    Makefile copies them into one directory); a file holding an empty black box of a
    modelled macro is copied into tb/ without it."""
    seen = {}
    for d in ("Tile", "Fabric"):
        for f in sorted((spec.proj / d).rglob("*.v")):
            seen.setdefault(f.name, f)
    out = []
    for name, f in sorted(seen.items()):
        text = f.read_text()
        stripped = BLACKBOX.sub(lambda m: "" if m.group(1) in MODEL_MODULES else m.group(0), text)
        if stripped != text:
            (tb / name).write_text(stripped)
            out.append(name)
        else:
            out.append(str(f))
    return out


def run_script(spec, configs: list[tuple[str, int, int]]) -> str:
    runs = "\n".join(f'run {n} {nb} {nv}' for n, nb, nv in configs)
    return f"""#!/bin/bash
# generated by fabtest/export.py: Icarus replay of the exported test configs on eFPGA_top
# (files.f: the fabric RTL; sram_sim.v: the BlockRAM SRAM model)
set -e
cd "$(dirname "$0")"
iverilog -g2012 -s fabtest_tb -o fabtest_tb.vvp -c files.f sram_sim.v fabtest_tb.v
fail=0
run() {{
    out=$(vvp -n fabtest_tb.vvp +hex=../bitstreams/$1.hex +vec=../vectors/$1.mem +nbytes=$2 +nv=$3)
    echo "$1: $(echo "$out" | grep -E '^(PASS|FAIL)')"
    echo "$out" | grep -E '^MISMATCH' | head -3 || true
    echo "$out" | grep -q '^PASS' || fail=1
}}
{runs}
exit $fail
"""


# ------------------------------------------------------------ plan (set cover)

def cover_plan(run: Path, load_s: dict) -> list[dict]:
    """Greedy set cover over the fault simulation chunks: per step the config detecting
    the most not yet detected sampled faults."""
    fdir = run / "faultsim"
    rows = [r for p in sorted(fdir.glob("chunk_*.csv")) for r in csv.DictReader(p.open())]
    if not rows:
        return []
    cfgs = [c for c in rows[0] if c not in ("id", "path", "polarity")]
    det = {c: {i for i, r in enumerate(rows) if r[c] == "D"} for c in cfgs}
    n = len(rows)
    done, out, t = set(), [], 0.0
    left = list(cfgs)
    while left:
        c = max(left, key=lambda c: (len(det[c] - done), -cfgs.index(c)))
        new = det[c] - done
        if not new and out:
            break
        done |= new
        left.remove(c)
        t += load_s.get(c, 0.0)
        p = len(done) / n
        out.append({"config": c, "new": len(new), "coverage": round(100 * p, 3),
                    "pm95": round(100 * 1.96 * math.sqrt(p * (1 - p) / n), 3), "load_s": round(t, 6)})
    final = out[-1]["coverage"]
    for mark in (95, 99):
        for r in out:
            if r["coverage"] >= final * mark / 100:
                r.setdefault("marks", []).append(f"{mark}% of final")
                break
    return out


# ------------------------------------------------------------ driver

def export(spec, work: Path, run: Path, only: list[int] | None = None, canonical: bool = False) -> Path:
    from fabtest.sim.fastsim import fastsim
    from fabtest.sim import stimuli as rf
    from fabtest.model.generic import GenericFabric
    out = run / "export"
    for d in ("bitstreams", "vectors", "tb"):
        (out / d).mkdir(parents=True, exist_ok=True)
    cfg_path = run / "configs.json"
    data = json.loads(cfg_path.read_text())
    ff = GenericFabric(spec)
    idx = only if only is not None else list(range(len(data["configs"])))
    fsj = run / "faultsim" / "configs.json"
    kept = set(json.loads(fsj.read_text())["kept"]) if fsj.exists() and only is None else None
    manifest = {"spec": str(work / "spec.json"), "fabulous_version": spec.d.get("fabulous_version"),
                "config_set": str(cfg_path), "frame_order": "canonical" if canonical else "fault-simulated",
                "configs": {}, "left_out": []}
    runs = []
    ins = outs = None
    stims = {c: rf.build_stim(ff, data, c) for c in idx}
    if kept is None:   # no fault simulation of these configs: check them here
        nl = fastsim.Netlist("fabtest", work / "synth" / "netlist.json")
        res = fastsim.fault_free(nl, list(stims.values()))
        kept = {n for n, err in res.items() if err is None}
        for n, err in res.items():
            if err is not None:
                print(f"  {n} left out: {err}", flush=True)
        del nl
    for c in idx:
        name = f"FF{c:02d}"
        sels, order, luts = rf.config_setup(ff, data, c)
        st = stims[c]
        ok = name in kept
        if not ok:
            manifest["left_out"].append(name)
            continue
        words = ff.frame_words(sels, rf.lut_bits(luts))
        bs = bitstream(ff, spec, words, None if canonical else order)
        (out / "bitstreams" / f"{name}.bin").write_bytes(bs)
        (out / "bitstreams" / f"{name}.hex").write_text(hexfile(bs))
        ins, outs, table = vector_table(st, spec)
        write_vectors(out / "vectors" / name, name, ins, outs, table)
        nw = len(bs) // 4
        manifest["configs"][name] = {
            "bin": f"bitstreams/{name}.bin", "hex": f"bitstreams/{name}.hex", "vec": f"vectors/{name}.vec",
            "bytes": len(bs), "words": nw, "vectors": len(table),
            "checked_outputs": sum(o.count("0") + o.count("1") for _, o in table),
            "load_s": {"parallel_50MHz": nw / CLK_HZ, "tb_5clk_per_word_50MHz": nw * TB_CLKS_PER_WORD / CLK_HZ,
                       f"uart_{UART_BAUD}": len(bs) * 10 / UART_BAUD},
            "vectors_s_50MHz": len(table) / CLK_HZ}
        runs.append((name, len(bs), len(table)))
        print(f"  {name}: {len(bs)} bytes, {len(table)} vectors", flush=True)
    if ins is not None:
        manifest["pins"] = {"inputs": ins, "outputs": outs}
        (out / "tb" / "fabtest_tb.v").write_text(testbench(spec, ins, outs))
        shutil.copy(TB / "sram_sim.v", out / "tb" / "sram_sim.v")
        (out / "tb" / "files.f").write_text("\n".join(rtl_files(spec, out / "tb")) + "\n")
        sh = out / "tb" / "run_icarus.sh"
        sh.write_text(run_script(spec, runs))
        sh.chmod(0o755)
    plan = cover_plan(run, {n: v["load_s"]["parallel_50MHz"] + v["vectors_s_50MHz"]
                             for n, v in manifest["configs"].items()})
    if plan:
        with (out / "plan.csv").open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["step", "config", "new_detected", "coverage_pct", "pm95_pct", "cum_time_s", "marks"])
            for k, r in enumerate(plan):
                w.writerow([k + 1, r["config"], r["new"], r["coverage"], r["pm95"], r["load_s"],
                            " ".join(r.get("marks", []))])
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    print(f"fabtest export: {len(manifest['configs'])} configs ({len(manifest['left_out'])} left out) -> {out}")
    return out


def bitgen_check(spec, work: Path, cfg_path: Path, only: list[int]) -> bool:
    """Canonical .bin = FABulous bit_gen of our FASM, byte for byte."""
    from fabtest.sim import stimuli as rf
    from fabtest.model.generic import GenericFabric
    out = work / "bitgen_check"
    out.mkdir(parents=True, exist_ok=True)
    data = json.loads(Path(cfg_path).read_text())
    ff = GenericFabric(spec)
    tool = os.environ.get("FABTEST_BIT_GEN") or shutil.which("bit_gen") or "/home/user/ext/venv/bin/bit_gen"
    ok = True
    for c in only:
        name = f"FF{c:02d}"
        sels, _, luts = rf.config_setup(ff, data, c)
        words = ff.frame_words(sels, rf.lut_bits(luts))
        ours = bitstream(ff, spec, words, None)
        (out / f"{name}.ours.bin").write_bytes(ours)
        text, problems = fasm(ff, spec, words)
        (out / f"{name}.fasm").write_text(text)
        r = subprocess.run([tool, "genBitstream", str(out / f"{name}.fasm"),
                            str(spec.proj / ".FABulous" / "bitStreamSpec.bin"), str(out / f"{name}.bin")],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  {name}: bit_gen failed: {(r.stdout + r.stderr)[-800:]}")
            ok = False
            continue
        theirs = (out / f"{name}.bin").read_bytes()
        same = theirs == ours
        nd = sum(a != b for a, b in zip(theirs, ours)) + abs(len(theirs) - len(ours))
        print(f"  {name}: {'byte-equal' if same else f'{nd} bytes differ'} ({len(ours)} bytes), "
              f"{len(text.splitlines())} FASM features, {len(problems)} problems", flush=True)
        for p in problems[:8]:
            print("    " + p)
        ok &= same and not problems
    return ok
