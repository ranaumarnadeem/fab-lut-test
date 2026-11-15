#!/usr/bin/env python3
"""fabtest P6 gate: exported test programs round-trip to the fault-simulated stimulus.

  python3 tests/test_export_demo.py [<work dir>] [--configs-from JSON] [--only 0,1]

Per config (export run first, `fabtest export`): the .bin parses back to the frame words
of the model (FullFabric.frame_words of config_setup), in the fault-simulated frame order
and with the sync header / desync of the spec; the .hex holds the same bytes; every .vec
line equals the stimulus rows (pad inputs) and the expected outputs after the pin map.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "configs"), str(ROOT / "sim")]


def main() -> None:
    args = sys.argv[1:]
    opt = {k: args[i + 1] for i, k in enumerate(args) if k.startswith("--")}
    pos = [a for i, a in enumerate(args) if not a.startswith("--") and (i == 0 or not args[i - 1].startswith("--"))]
    w = Path(pos[0]) if pos else ROOT / "build" / "fabtest" / "demo-d278ebfd"
    from fabtest import export as ex, spec as specmod
    from fabtest.model import GenericFabric
    import run_fullfabric as rf
    s = specmod.load(w)
    ff = GenericFabric(s)
    cfg_path = Path(opt.get("--configs-from", ROOT / "configs" / "fab_router_fullbram_v3.json"))
    data = json.loads(cfg_path.read_text())
    only = [int(v) for v in opt.get("--only", "0,1").split(",")]
    fails = []
    for c in only:
        name = f"FF{c:02d}"
        bs = (w / "export" / "bitstreams" / f"{name}.bin").read_bytes()
        sels, order, luts = rf.config_setup(ff, data, c)
        words = ff.frame_words(sels, rf.lut_bits(luts))
        a = ex.arch(s)
        if bs[:20] != bytes.fromhex(a["header"]) or int.from_bytes(bs[-4:], "big") != 1 << a["desync"]:
            fails.append(f"{name}: header / desync")
        got = ex.parse_bitstream(ff, bs)
        keys = [(X, f) for X in range(ff.C) for f in order]
        if list(got) != keys:
            fails.append(f"{name}: frame order")
        for (X, f), rows in got.items():
            for y, v in rows.items():
                t = (X - 1, y)
                want = words[t][f] if t in words and f < len(words[t]) else 0
                if v != want:
                    fails.append(f"{name}: X{X}Y{y} frame {f}: {v:08x} != {want:08x}")
        hx = (w / "export" / "bitstreams" / f"{name}.hex").read_text().split()
        if bytes(int(h, 16) for h in hx[:len(bs)]) != bs or any(h != "0" for h in hx[len(bs):]):
            fails.append(f"{name}: .hex")
        st = rf.build_stims(ff, cfg_path, only=[c], quiet=True)[0]
        ins, outs, table = ex.vector_table(st, s)
        lines = [l.split() for l in (w / "export" / "vectors" / f"{name}.vec").read_text().splitlines()]
        hdr = {l[0]: l[1:] for l in lines if l and l[0] in ("INPUTS", "OUTPUTS")}
        vec = [(l[1], l[2]) for l in lines if l and l[0] == "V"]
        if hdr.get("INPUTS") != ins or hdr.get("OUTPUTS") != outs or vec != table:
            fails.append(f"{name}: .vec")
        if len(vec) != len(ff.vecs) or sum(1 for _, o in vec if set(o) != {"x"}) != len(st.expected):
            fails.append(f"{name}: vector / sample count")
        # pad inputs of vector k = the model's vector k on the border inputs
        pm = ex.pins(s)
        for k, (iv, _) in enumerate(vec):
            bits = {p: iv[j] for j, p in enumerate(ins)}
            want = {pm[ff.port(n)]: str((ff.vecs[k] >> j) & 1) for j, n in enumerate(ff.border_in)}
            if any(bits[p] != v for p, v in want.items()):
                fails.append(f"{name}: vector {k} inputs")
                break
        print(f"  {name}: {len(bs)} bytes, {len(got)} frames, {len(vec)} vectors", flush=True)
    for f in fails[:20]:
        print("  FAIL " + f)
    print(f"test_export_demo: {'pass' if not fails else 'FAIL'} ({len(only)} configs, {len(fails)} differences)")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
