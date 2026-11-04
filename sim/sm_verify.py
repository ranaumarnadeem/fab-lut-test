#!/usr/bin/env python3
"""Check configs/sm_model.py against the generated switch-matrix RTL.

Simulates rtl/LUT4AB_switch_matrix.v with rtl/models_pack.v (Icarus) for
F = max fan-in configurations: in config c every mux is set to input
c mod fan-in by sm_model's encoding. Every switch-matrix input port gets a
unique w-bit code (w = ceil(log2(N + 2)), so no code is all 0 or all 1) and
then its complement (2w vectors). Each output must equal the model's
selected input (GND0 = 0, VCC0 = 1) on every vector. Together the configs
exercise every code of every mux.
"""
from __future__ import annotations

import math
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "configs"))
import sm_model  # noqa: E402

BUILD = ROOT / "build" / "sm_verify"
SM_V = ROOT / "rtl" / "LUT4AB_switch_matrix.v"
MODELS = ROOT / "rtl" / "models_pack.v"


def main() -> None:
    BUILD.mkdir(parents=True, exist_ok=True)
    hdr = SM_V.read_text().split(");", 1)[0]
    ins = [p for p in re.findall(r"input\s+(\w+)\s*,", hdr)]
    outs = re.findall(r"output\s+(\w+)\s*,?", hdr)
    nbits = int(re.search(r"NoConfigBits=(\d+)", hdr).group(1))
    muxes = sm_model.MUXES
    if set(outs) != set(muxes):
        raise SystemExit("output ports differ from the model")
    w = math.ceil(math.log2(len(ins) + 2))
    code = {p: i + 1 for i, p in enumerate(ins)}
    vecs = []
    for b in range(w):
        vecs.append({p: (code[p] >> b) & 1 for p in ins})
    vecs += [{p: 1 - v[p] for p in ins} for v in vecs]
    F = max(m.fan_in for m in muxes.values())

    tb = ["`timescale 1ns/1ps", "module tb;",
          f"  reg [{len(ins) - 1}:0] in_v; reg [{nbits - 1}:0] cfg;",
          f"  wire [{len(outs) - 1}:0] out_v;",
          f"  reg [{len(ins) - 1}:0] mem_in [0:{len(vecs) - 1}];",
          f"  reg [{nbits - 1}:0] mem_cfg [0:0];",
          "  integer k;",
          "  LUT4AB_switch_matrix dut ("]
    tb += [f"    .{p}(in_v[{i}])," for i, p in enumerate(ins)]
    tb += [f"    .{p}(out_v[{i}])," for i, p in enumerate(outs)]
    tb += ["    .ConfigBits(cfg), .ConfigBits_N(~cfg));",
           "  initial begin",
           '    $readmemb("cfg.mem", mem_cfg); $readmemb("in.mem", mem_in);',
           "    cfg = mem_cfg[0];",
           f"    for (k = 0; k < {len(vecs)}; k = k + 1) begin",
           "      in_v = mem_in[k]; #1; $display(\"V %b\", out_v);",
           "    end",
           "    $finish;", "  end", "endmodule"]
    (BUILD / "tb.v").write_text("\n".join(tb) + "\n")
    vvp = BUILD / "sim.vvp"
    subprocess.run(["iverilog", "-g2005", "-o", str(vvp), str(BUILD / "tb.v"), str(SM_V), str(MODELS)],
                   check=True)
    with (BUILD / "in.mem").open("w") as f:
        for v in vecs:
            f.write("".join(str(v[p]) for p in reversed(ins)) + "\n")

    sm_base = sm_model.TILE.sm_base
    bad, checked = 0, 0
    for c in range(F):
        cfg = [0] * nbits
        sel = {}
        for m in muxes.values():
            if m.model == "direct":
                sel[m.out] = m.inputs[0]
                continue
            k = c % m.fan_in
            for tb_, v in m.code_for(k).items():
                cfg[tb_ - sm_base] = v
            sel[m.out] = m.inputs[k]
        (BUILD / "cfg.mem").write_text("".join(map(str, reversed(cfg))) + "\n")
        res = subprocess.run(["vvp", "-n", str(vvp)], cwd=BUILD, capture_output=True, text=True, check=True)
        got = [ln[2:] for ln in res.stdout.splitlines() if ln.startswith("V ")]
        if len(got) != len(vecs):
            raise SystemExit(f"config {c}: {len(got)} samples")
        for j, (v, s) in enumerate(zip(vecs, got)):
            bits = s[::-1]  # out_v[0] first
            for i, o in enumerate(outs):
                src = sel[o]
                exp = 0 if src.port.startswith("GND") else 1 if src.port.startswith("VCC") else v[src.port]
                checked += 1
                if bits[i] != str(exp):
                    bad += 1
                    if bad <= 10:
                        print(f"config {c} vector {j}: {o} = {bits[i]}, model {src.port} = {exp}")
    codes = sum(m.fan_in for m in muxes.values() if m.model != "direct")
    print(f"{len(ins)} inputs (w = {w}, {len(vecs)} vectors), {len(outs)} outputs, {F} configs, "
          f"{codes} mux codes; {checked} output samples checked, {bad} mismatches")
    if bad:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
