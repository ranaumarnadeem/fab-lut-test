#!/usr/bin/env python3
"""Give every fanout branch its own net, for branch stuck-at faults.

For each net with more than one sink, every sink pin (cell input) is moved
to a new net `<stem>__br<k>` driven by a `$_BUF_` from the stem. Forcing a branch net then affects only that sink, which `force` on
a cell input pin cannot do (iverilog collapses ports). The stem keeps its
name, so stem faults are unchanged. Output: a yosys JSON netlist; yosys
writes the matching Verilog (see run_synth.sh).
"""
import argparse
import json
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("netlist_json", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    design = json.loads(args.netlist_json.read_text())
    tops = [n for n, m in design["modules"].items() if m.get("attributes", {}).get("top")]
    if len(tops) != 1:
        raise SystemExit(f"cannot pick top module: {tops}")
    mod = design["modules"][tops[0]]

    names: dict[int, str] = {}
    for name, nn in mod["netnames"].items():
        w = len(nn["bits"])
        for k, b in enumerate(nn["bits"]):
            if isinstance(b, int):
                if b in names:
                    raise SystemExit(f"bit {b} has two names; run synth with rename first")
                idx = nn.get("offset", 0) + (w - 1 - k if nn.get("upto", 0) else k)
                names[b] = name if w == 1 else f"{name}[{idx}]"

    # sinks: (kind, owner, pin, index); kind is "cell" or "port"
    sinks: dict[int, list[tuple[str, str, str, int]]] = {}
    for cname, cell in mod["cells"].items():
        for pin, sig in cell["connections"].items():
            if cell["port_directions"][pin] != "input":
                continue
            for k, b in enumerate(sig):
                if isinstance(b, int):
                    sinks.setdefault(b, []).append(("cell", cname, pin, k))
    for pname, port in mod["ports"].items():
        if port["direction"] != "output":
            continue
        for k, b in enumerate(port["bits"]):
            if isinstance(b, int):
                sinks.setdefault(b, []).append(("port", pname, "", k))

    next_bit = max(names) + 1
    nbuf = 0
    for b, lst in sorted(sinks.items()):
        if len(lst) < 2:
            continue
        stem = names[b]
        for br, (kind, owner, pin, k) in enumerate(lst):
            nb = next_bit
            next_bit += 1
            if kind != "cell":
                raise SystemExit(f"net {stem} drives output port {owner} and other sinks; "
                                 "branching a port is not supported")
            mod["cells"][owner]["connections"][pin][k] = nb
            mod["netnames"][f"{stem}__br{br}"] = {"hide_name": 0, "bits": [nb], "attributes": {}}
            mod["cells"][f"brbuf_{nbuf}"] = {
                "hide_name": 0, "type": "$_BUF_", "parameters": {}, "attributes": {},
                "port_directions": {"A": "input", "Y": "output"},
                "connections": {"A": [b], "Y": [nb]},
            }
            nbuf += 1

    args.out.write_text(json.dumps(design, indent=1))
    print(f"{args.out}: {nbuf} branch buffers")


if __name__ == "__main__":
    main()
