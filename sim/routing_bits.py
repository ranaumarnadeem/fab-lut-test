#!/usr/bin/env python3
"""Routing-bit analysis for the LUT4AB tile test.

  python3 sim/routing_bits.py strobe --fault "FrameStrobe[1]" --pol 1
      Simulate one fault in each config with +DUMPSTATE and diff every config
      latch against the golden run; for each differing bit give its owner
      and, for switch-matrix bits, the mux it selects and whether that mux
      output is on the test routing.
  python3 sim/routing_bits.py flip
      For every bit of the test routing (configs/tile_routing.json), flip it
      in the config words of each config, simulate fault free and compare the
      observed samples with golden: essential (some sample changes in some
      config) or don't-care. Writes reports/routing_bits.md.
"""
from __future__ import annotations

import argparse
import copy
import csv
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / d) for d in ("sim", "configs", "faults", "rtl")]
import gen_faults  # noqa: E402
import run_tile as rt  # noqa: E402
import simcore  # noqa: E402
import state  # noqa: E402
import tile_configs as tc  # noqa: E402

NETLIST = "tile_generic"
BITMAP = {int(r["tile_config_bit"]): r for r in csv.DictReader((ROOT / "configs" / "tile_bitmap.csv").open())}


def latch_bits(netlist: str) -> dict[int, int]:
    """storage element index -> tile config bit (latches only)."""
    js = ROOT / "synth" / f"netlist_{netlist}.json"
    _, mod = gen_faults.load_top(js, None)
    bits = gen_faults.analyse(mod)
    pos2bit = {v: b for b, v in tc.TILE.configmem.items()}
    def stem_name(b) -> str:
        """Net name, looking back through branch buffers ($_BUF_) to the stem."""
        drv = bits[b]["drivers"][0] if bits[b]["drivers"] else ""
        cname = drv.rsplit(".", 1)[0]
        if cname in mod["cells"] and mod["cells"][cname]["type"] == "$_BUF_":
            return stem_name(mod["cells"][cname]["connections"]["A"][0])
        return bits[b]["names"][0][0]

    out = {}
    for e in state.storage_elements(js):
        if e.kind != "latch":
            continue
        conn = mod["cells"][e.cell]["connections"]
        d, en = stem_name(conn["D"][0]), stem_name(conn["E"][0])
        out[e.index] = pos2bit[(int(en[len("FrameStrobe["):-1]), int(d[len("FrameData["):-1]))]
    return out


def sel_owner() -> dict[int, tuple[str, int]]:
    """tile config bit -> (switch-matrix mux, select position)."""
    out = {}
    for name, m in tc.TILE.muxes.items():
        for i, b in enumerate(m.sel):
            out[tc.TILE.sm_base + b] = (name, i)
    return out


def route_of_net() -> dict[str, str]:
    """tile net -> description of the test route it lies on."""
    out = {}
    for r in tc.ROUTING["routes"]:
        label = (f"input route to {r['nets'][-1]}" if r["type"] == "input" else
                 f"output route {r['nets'][0]} -> {r['nets'][-1]} (observed)" if r["type"] == "output"
                 else f"{r['nets'][-1]} select")
        for n in r["nets"][1:] if r["type"] != "output" else r["nets"]:
            out.setdefault(n, label)
    return out


def describe_bit(b: int) -> dict:
    so = sel_owner().get(b)
    d = {"bit": b, "owner": BITMAP[b]["owner"], "frame": BITMAP[b]["frame"],
         "frame_data_bit": BITMAP[b]["frame_data_bit"]}
    if so:
        mux, i = so
        out_net = tc.TILE.port_net[mux]
        d.update(mux=mux, sel_pos=i, out_net=out_net,
                 on_route=route_of_net().get(out_net, "not on the test routing"))
    return d


# ---------------------------------------------------------------- strobe

def cmd_strobe(args) -> int:
    stims = rt.write_stimuli()
    faults = [f for f in rt.load_faults(NETLIST)
              if f["net"] == args.fault and f["polarity"] == str(args.pol) and f["type"] == "stem"]
    if len(faults) != 1:
        raise SystemExit(f"fault {args.fault} SA{args.pol} not found")
    vvp, _ = rt.compile_netlist(NETLIST, faults, tag="_dbg")
    fid = int(faults[0]["id"])
    lb = latch_bits(NETLIST)
    out = []
    for c, (stim, n, exp, _) in stims.items():
        pa = [f"+STIM={stim}", f"+NSTEPS={n}", "+DUMPSTATE"]
        g = simcore.run_vvp(vvp, pa)
        f = simcore.run_vvp(vvp, pa, fid)
        diff = sorted(lb[i] for i in lb if g.state[i] != f.state[i])
        st = simcore.classify(g.samples, f.samples)
        out.append((c, st, diff, {b: (g.state[i], f.state[i]) for i, b in lb.items() if b in diff}))
    return out


def print_strobe(res) -> None:
    for c, st, diff, vals in res:
        print(f"{c}: status {st}, {len(diff)} latches differ")
        for b in diff:
            d = describe_bit(b)
            g, f = vals[b]
            print(f"  bit {b} {d['owner']} (frame {d['frame']}, FrameData[{d['frame_data_bit']}]) "
                  f"golden {g} faulty {f}"
                  + (f"; selects {d['mux']} S{d['sel_pos']} -> {d['out_net']}: {d['on_route']}"
                     if "mux" in d else ""))


# ---------------------------------------------------------------- flip

_F: dict = {}


def _init(vvp, golden):
    _F.update(vvp=Path(vvp), golden=golden)


def _flip(job):
    bit, cfg, stim, n = job
    r = simcore.run_vvp(_F["vvp"], [f"+STIM={stim}", f"+NSTEPS={n}"], timeout=rt.TIMEOUT_S)
    g = _F["golden"][cfg]
    if r.timeout:
        return bit, cfg, "T", []
    changed = [k for k, (a, b) in enumerate(zip(g, r.samples)) if a != b]
    st = simcore.classify(g, r.samples)
    return bit, cfg, st, changed[:3]


def cmd_flip(args) -> int:
    stims = rt.write_stimuli()
    vvp, _ = rt.compile_netlist(NETLIST, None, tag="_flip")
    golden = rt.golden_runs(vvp, stims)
    bits = sorted(int(b) for b in tc.ROUTING["switch_matrix_bits_set"])
    d = rt.BUILD / "flip_stim"
    d.mkdir(parents=True, exist_ok=True)
    jobs = []
    orig = tc.ROUTING
    for b in bits:
        mod = copy.deepcopy(orig)
        mod["switch_matrix_bits_set"][str(b)] ^= 1
        tc.ROUTING = mod
        for c, f in tc.TILE_CONFIGS.items():
            seq = f()
            path = d / f"{c}_{b}.stim"
            seq.write_stim(path)
            jobs.append((b, c, str(path), len(seq.steps)))
    tc.ROUTING = orig
    res: dict[int, dict[str, tuple]] = {b: {} for b in bits}
    with ProcessPoolExecutor(max_workers=args.jobs, initializer=_init,
                             initargs=(str(vvp), golden)) as ex:
        for b, c, st, ch in ex.map(_flip, jobs, chunksize=8):
            res[b][c] = (st, ch)
    (rt.BUILD / "routing_flip.json").write_text(json.dumps(res, indent=1))
    write_report(res)
    return 0


def _bitval(b: int) -> int:
    return tc.ROUTING["switch_matrix_bits_set"].get(str(b), 0)


def _selected(mux: str) -> int:
    m = tc.TILE.muxes[mux]
    return sum(_bitval(tc.TILE.sm_base + s) << j for j, s in enumerate(m.sel))


def carried(net: str, seen: frozenset = frozenset()) -> str:
    """What a tile net carries in the configured, fault-free tile."""
    pin_of_port = {v: k for k, v in tc.ROUTING["lut_inputs"].items()}
    if net in pin_of_port:
        return f"stimulus of {pin_of_port[net]}"
    if net == "Ci[0]":
        return "stimulus of Ci"
    if net in tc.TILE.tile_inputs:
        return "a tile input held at 0"
    if net.startswith(("GND", "VCC")):
        return net
    drv = [o for o in tc.TILE.muxes if tc.TILE.port_net[o] == net]
    if not drv:
        return f"output {net}"
    if net in seen:
        return "a combinational loop (X)"
    m = tc.TILE.muxes[drv[0]]
    k = 0 if m.kind == "direct" else _selected(drv[0])
    src = m.inputs[k]
    if src.startswith(("GND", "VCC")):
        return src
    return carried(tc.TILE.port_net[src], seen | {net})


def _signal_seq(pin: str, cfg: str) -> list[int]:
    seq = tc.TILE_CONFIGS[cfg]()
    if pin == "Ci":
        return [s.ci for s in seq.steps]
    x, j = "ABCDEFGH".index(pin[1]), int(pin[-1])
    return [(s.i[x] >> j) & 1 for s in seq.steps]


def explain(b: int) -> str:
    """Why a flip of routing bit b leaves every observed sample unchanged."""
    mux, i = sel_owner()[b]
    m = tc.TILE.muxes[mux]
    k = _selected(mux)
    k2 = k ^ (1 << i)
    sn = tc.TILE.port_net.get(m.inputs[k], m.inputs[k])
    an = tc.TILE.port_net.get(m.inputs[k2], m.inputs[k2])
    cs, ca = carried(sn), carried(an)
    why = f"{mux}: selects A{k} ({sn}, {cs}); flip selects A{k2} ({an}, {ca})"
    if cs.startswith("stimulus of L") and ca.startswith("stimulus of L"):
        p0, p1 = cs.split()[-1], ca.split()[-1]
        parts = []
        for c in tc.TILE_CONFIGS:
            if p0.endswith("_I0") and c == "BC1":
                parts.append("BC1: I0 unused (I0 = Ci)")
            elif _signal_seq(p0, c) == _signal_seq(p1, c):
                parts.append(f"{c}: {p0} and {p1} get identical vectors")
            else:
                parts.append(f"{c}: vectors differ (needs investigation)")
        why += ". " + "; ".join(parts)
    return why


def write_report(res) -> None:
    rows = []
    for b, per in sorted(res.items()):
        essential = any(st in ("D", "P") or ch for st, ch in per.values())
        d = describe_bit(b)
        rows.append((b, d, per, essential))
    ess = [r for r in rows if r[3]]
    dc = [r for r in rows if not r[3]]
    out = ["# Test routing bits: essential vs don't-care", "",
           "Each bit of `configs/tile_routing.json` (`switch_matrix_bits_set`) is flipped "
           "on its own in the config words of BC1, D0C and D1; the tile netlist G is "
           "simulated fault free and the observed samples (8 routed LUT outputs, Co[0]) "
           "are compared with golden. Essential: some sample changes in some config. "
           "Don't-care: no observed change in any config.", "",
           f"{len(rows)} bits: {len(ess)} essential, {len(dc)} don't-care.", ""]
    out += ["## Don't-care bits", "", "| bit | owner | frame | value | mux, select | why |",
            "|---|---|---|---|---|---|"]
    val = tc.ROUTING["switch_matrix_bits_set"]
    for b, d, per, _ in dc:
        out.append(f"| {b} | {d['owner']} | {d['frame']} | {val[str(b)]} | "
                   f"{d.get('mux', '-')} S{d.get('sel_pos', '-')} | {explain(b)} |")
    out += ["", "## Essential bits", "", "| bit | owner | value | mux, select | on route | "
            "BC1 | D0C | D1 |", "|---|---|---|---|---|---|---|---|"]
    for b, d, per, _ in ess:
        out.append(f"| {b} | {d['owner']} | {val[str(b)]} | {d.get('mux', '-')} S{d.get('sel_pos', '-')} | "
                   f"{d.get('on_route', '-')} | " + " | ".join(per[c][0] for c in tc.TILE_CONFIGS) + " |")
    (ROOT / "build" / "routing_bits_draft.md").write_text("\n".join(out) + "\n")
    print(f"{len(rows)} bits: {len(ess)} essential, {len(dc)} don't-care")
    for b, d, per, _ in dc:
        print(f"  don't-care {b} {d['owner']} value {val[str(b)]}: {explain(b)} [{d.get('on_route')}]")


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("strobe")
    s.add_argument("--fault", required=True)
    s.add_argument("--pol", type=int, required=True)
    f = sub.add_parser("flip")
    f.add_argument("--jobs", type=int, default=4)
    args = ap.parse_args()
    if args.cmd == "strobe":
        print_strobe(cmd_strobe(args))
        return 0
    return cmd_flip(args)


if __name__ == "__main__":
    sys.exit(main())
