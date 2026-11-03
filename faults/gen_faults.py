#!/usr/bin/env python3
"""Enumerate single stuck-at faults from a flattened yosys JSON netlist.

Stem faults: every net bit gets SA0 and SA1. A net is one yosys signal bit;
after the synth scripts' `rename -enumerate` each bit has exactly one name,
which is also its name in the written Verilog, so `force dut.<ref>` hits the
whole net (stem).

Skipped: constant bits, nets with no sink (unused wrapper input bits such as
FrameData[8]), and any net listed with --exclude (e.g. UserCLK).

Branch faults: on a netlist from synth/insbuf_branches.py every fanout branch
is its own net `<stem>__br<k>`; those nets are typed "branch", the rest
"stem". On a plain netlist all faults are stems.
"""
import argparse
import csv
import json
import re
from pathlib import Path

SIMPLE_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
BRANCH_RE = re.compile(r"__br\d+$")


def vref(name: str) -> str:
    """Verilog reference for a net name as written by write_verilog."""
    return name if SIMPLE_ID.match(name) else f"\\{name} "


def load_top(path: Path, top: str | None) -> tuple[str, dict]:
    design = json.loads(path.read_text())
    mods = design["modules"]
    if top is None:
        tops = [n for n, m in mods.items() if m.get("attributes", {}).get("top")]
        if len(tops) != 1:
            raise SystemExit(f"cannot pick top module from {sorted(mods)}")
        top = tops[0]
    return top, mods[top]


def analyse(mod: dict) -> dict:
    """Per signal bit: name ref, drivers, sinks."""
    bits: dict[int, dict] = {}

    def entry(b: int) -> dict:
        return bits.setdefault(b, {"names": [], "drivers": [], "sinks": []})

    for name, nn in mod["netnames"].items():
        w = len(nn["bits"])
        offset = nn.get("offset", 0)
        upto = nn.get("upto", 0)
        for k, b in enumerate(nn["bits"]):
            if not isinstance(b, int):
                continue
            if w == 1:
                ref, label = vref(name), name
            else:
                idx = offset + (w - 1 - k if upto else k)
                ref, label = f"{vref(name)}[{idx}]", f"{name}[{idx}]"
            entry(b)["names"].append((label, ref))

    for pname, port in mod["ports"].items():
        for k, b in enumerate(port["bits"]):
            if not isinstance(b, int):
                continue
            w = len(port["bits"])
            idx = port.get("offset", 0) + (w - 1 - k if port.get("upto", 0) else k)
            pin = f"port:{pname}[{idx}]" if w > 1 else f"port:{pname}"
            if port["direction"] == "input":
                entry(b)["drivers"].append(pin)
            elif port["direction"] == "output":
                entry(b)["sinks"].append(pin)
            else:
                raise SystemExit(f"inout port {pname} not supported")

    for cname, cell in mod["cells"].items():
        dirs = cell.get("port_directions")
        if not dirs:
            raise SystemExit(f"cell {cname} ({cell['type']}) has no port directions")
        for pin, sig in cell["connections"].items():
            for k, b in enumerate(sig):
                if not isinstance(b, int):
                    continue
                label = f"{cname}.{pin}" + (f"[{k}]" if len(sig) > 1 else "")
                if dirs[pin] == "output":
                    entry(b)["drivers"].append(label)
                elif dirs[pin] == "input":
                    entry(b)["sinks"].append(label)
                else:
                    raise SystemExit(f"{cname}.{pin}: inout not supported")
    return bits


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("netlist_json", type=Path)
    ap.add_argument("--top")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--exclude", action="append", default=[], help="net name to skip")
    args = ap.parse_args()

    _, mod = load_top(args.netlist_json, args.top)
    bits = analyse(mod)

    rows = []
    skipped = {"no_sink": [], "excluded": []}
    for b in sorted(bits):
        e = bits[b]
        if len(e["names"]) != 1:
            raise SystemExit(f"bit {b} has names {e['names']}; rerun synth with rename")
        label, ref = e["names"][0]
        if label in args.exclude:
            skipped["excluded"].append(label)
            continue
        if not e["sinks"]:
            skipped["no_sink"].append(label)
            continue
        if len(e["drivers"]) != 1:
            raise SystemExit(f"net {label} has drivers {e['drivers']}")
        ftype = "branch" if BRANCH_RE.search(label) else "stem"
        for pol in (0, 1):
            rows.append({
                "net": label, "force_ref": ref, "polarity": pol, "type": ftype,
                "driver": e["drivers"][0], "fanout": len(e["sinks"]),
                "pin": e["sinks"][0] if ftype == "branch" else "",
            })

    args.out.parent.mkdir(parents=True, exist_ok=True)
    fields = ["id", "path", "polarity", "type", "net", "force_ref", "driver", "fanout", "pin"]
    with args.out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for i, r in enumerate(rows):
            path = r["net"] if r["type"] == "stem" else f"{r['net']}->{r['pin']}"
            w.writerow({"id": i, "path": path, **r})
    nstem = sum(r["type"] == "stem" for r in rows)
    print(f"{args.out}: {len(rows)} faults ({nstem} stem, {len(rows) - nstem} branch); "
          f"skipped no_sink={skipped['no_sink']} excluded={skipped['excluded']}")


if __name__ == "__main__":
    main()
