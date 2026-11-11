#!/usr/bin/env python3
"""Configuration-logic test (Phase 13): bitstreams through every config port.

  python3 sim/run_cfgtop.py check                 fault-free load = the intended config bits
  python3 sim/run_cfgtop.py faultsim [--jobs 4]   all faults of faults/cfgtop.csv

Netlist synth/netlist_cfgtop (synth/build_cfgtop.sh): FABulous's config path
(eFPGA_Config with config_UART, bitbang and ConfigFSM, the frame data registers and
frame selects) driving a latch model of the fabric's ConfigMem (CM[], one latch per
used config bit). One test, sampled after every phase:

  1. reset, then config FF00 of the full-fabric test (configs/fab_router_fullfabric.json)
     through SelfWriteData / SelfWriteStrobe (one word per clock, 3 idle clocks after
     every frame: LongFrameStrobe stays high 2 clocks after the last row word);
  2. config FF01 the same way;
  3. bitbang (s_clk / s_data, 8 clocks per bit): two frames with the inverted data;
  4. UART binary (00 AA FF 01, 217 clocks per bit): one frame, inverted;
  5. UART hex (00 AA FF 81, ASCII hex): one frame, inverted. FABulous's config_UART
     resets its nibble state on every clock without a stop-bit tick, so in hex mode no
     byte is ever completed (Icarus on the RTL agrees): the fault-free run writes
     nothing here, a fault that makes the hex path write is detected.

Every phase writes data different from what the memory holds, so a write that does
not happen is seen. A fault is detected (strict) when some config bit, ComActive or
ReceiveLED differs from the fault-free run at a sample. Faults of the config memory
model are not in the list. Writes reports/cfgtop_matrix.csv.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "configs"), str(ROOT / "sim"), str(ROOT / "sim" / "fastsim")]
import fastsim  # noqa: E402

SYNC, DESYNC = 0xFAB0FAB1, 1 << 20
ROWS = 14
IN = ["CLK", "resetn", "SelfWriteStrobe"] + [f"SelfWriteData[{k}]" for k in range(32)] + ["Rx", "s_clk", "s_data"]
COM_RATE, RX_TIMEOUT = 217, 16665


def config_words() -> dict:
    """(column X, frame) -> {row y: word} for FF00 and FF01 (as run_fullfabric's stimuli)."""
    import fullfabric
    import sm_configs as sc
    import wio_model as wm
    ff = fullfabric.FullFabric()
    data = json.loads((ROOT / "configs" / "fab_router_fullfabric.json").read_text())
    inv = {ff.tn(t): t for t in ff.tiles}
    out = []
    for c in (0, 1):
        sels = {inv[n]: s for n, s in data["configs"][c].items()}
        for t in ff.tiles:
            sels.setdefault(t, {})
        tt = sc.cell.XNOR_TT if c % 2 else sc.cell.XOR_TT
        lut_bits = {t: {x: sc.cell.make_bits(tt, ff=1, i0_from_ci=0, reset_value=0) for x in sc.LUTS}
                    for t in ff.lut_tiles}
        for t in ff.lut_tiles:
            for x, val in sels[t].get("__lut", {}).items():
                lut_bits[t][x] = sc.cell.make_bits(0xFFFF if val else 0)
        fr = {}
        for t in ff.tiles:
            typ = ff.types[t]
            if typ == "LUT4AB":
                w = sc.frame_words(sc.config_word(sels[t], lut_bits[t]))
            elif typ == "W_IO":
                w = wm.frame_words(wm.config_word(sels[t], sels[t].get("__cbits", 0)))
            elif ff.gen[typ].configmem:
                w = ff.gen[typ].frame_words(ff.gen[typ].config_word(sels[t]))
            else:
                continue
            for f in range(20):
                fr.setdefault((t[0] + 1, f), {})[t[1]] = w[f] if f < len(w) else 0
        out.append(fr)
    return out


def frame_stream(frames: dict) -> list[int]:
    """Words of one load: sync, per frame a header (column in [31:27], one-hot frame in
    [19:0]) and the 14 row words (row 14 first: ConfigFSM counts row_index down), desync."""
    w = [SYNC]
    for (x, f), rows in sorted(frames.items()):
        w.append((x << 27) | (1 << f))
        w += [rows.get(y, 0) for y in range(ROWS, 0, -1)]
        w.append(None)   # frame end: idle clocks before the next header (parallel port)
    w.append(DESYNC)
    return w


class Stim:
    def __init__(self):
        self.rows, self.sample, self.state = [], [], {n: 0 for n in IN}
        self.state.update(Rx=1)

    def clock(self, n: int = 1, **kw):
        """n clock cycles with the inputs kw (set in the low phase)."""
        self.state.update(kw)
        for _ in range(n):
            r = [self.state[k] for k in IN]
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

    def bitbang(self, words: list[int], H: int = 4):
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
        word(0, 0xFAB0)    # active off
        self.clock(8, s_data=0)

    def uart(self, words: list[int], hexmode: bool):
        def byte(b: int):
            for bit in [0] + [(b >> k) & 1 for k in range(8)] + [1]:
                self.clock(COM_RATE, Rx=bit)
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
        self.clock(RX_TIMEOUT + 200, Rx=1)


def build(bits: list) -> tuple[fastsim.Stim, list[str]]:
    cw = config_words()
    st = Stim()
    st.clock(3, resetn=0)
    st.clock(3, resetn=1)
    cm = {}
    expected = []
    pos = {(x, f, y, d): k for k, (x, f, y, d) in enumerate(bits)}

    def written(frames):
        for (x, f), rows in frames.items():
            for y, w in rows.items():
                for d in range(32):
                    if (x, f, y, d) in pos:
                        cm[pos[(x, f, y, d)]] = (w >> d) & 1

    def snap():
        expected.append("".join(str(cm[k]) if k in cm else "x" for k in range(len(bits))) + "xx")

    for c in (0, 1):
        st.parallel(frame_stream(cw[c]))
        written(cw[c])
        st.mark()
        snap()
    inv = lambda fr: {k: {y: ~w & 0xFFFFFFFF for y, w in rows.items()} for k, rows in fr.items()}  # noqa: E731
    keys = sorted(cw[1])
    for name, sel in (("bitbang", keys[20:22]), ("uart", keys[40:41]), ("uarthex", keys[60:61])):
        fr = inv({k: cw[1][k] for k in sel})
        if name == "bitbang":
            st.bitbang(frame_stream(fr))
        else:
            st.uart(frame_stream(fr), hexmode=name == "uarthex")
        if name != "uarthex":
            written(fr)   # hex mode: FABulous's config_UART never completes a byte (see NOTES)
        st.mark()
        snap()
    outs = [f"CM[{k}]" for k in range(len(bits))] + ["ComActive", "ReceiveLED"]
    return fastsim.Stim("CFG", IN, np.array(st.rows, dtype=np.uint8), np.array(st.sample, dtype=np.uint8),
                        outs, expected), outs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["check", "faultsim"])
    ap.add_argument("--jobs", type=int, default=4)
    a = ap.parse_args()
    bits = json.loads((ROOT / "rtl" / "config" / "cfg_bits.json").read_text())
    t0 = time.time()
    st, _ = build(bits)
    print(f"cfgtop: {st.rows.shape[0]} steps, {int(st.sample.sum())} samples ({time.time() - t0:.0f} s)", flush=True)
    nl = fastsim.Netlist("cfgtop")
    fastsim.Engine(nl, [st], W=1).run_batch([])
    print(f"cfgtop: fault-free load = the intended config bits ({time.time() - t0:.0f} s)", flush=True)
    if a.cmd == "check":
        return
    fl = list(csv.DictReader((ROOT / "faults" / "cfgtop.csv").open()))
    cfgs, stat = fastsim.run_faults("cfgtop", [st], fl, a.jobs, nl=nl)
    with (ROOT / "reports" / "cfgtop_matrix.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "path", "polarity", "CFG"])
        for f, s in zip(fl, stat):
            w.writerow([f["id"], f["path"], f["polarity"], s["CFG"]])
    tot, det = Counter(), Counter()
    for f, s in zip(fl, stat):
        m = re.match(r"(\w+?)(?:_inst|_\d+)?\.", f["path"])
        k = re.sub(r"_\d+$", "", f["path"].split(".")[0]) if "." in f["path"] else "top-level wiring"
        for kk in (k, "all"):
            tot[kk] += 1
            det[kk] += s["CFG"] == "D"
    print(f"cfgtop: {len(fl)} faults in {time.time() - t0:.0f} s; status {dict(Counter(s['CFG'] for s in stat))}")
    for k in sorted(tot, key=lambda k: -tot[k]):
        print(f"  {k:28s} {tot[k]:6d}: {100 * det[k] / tot[k]:6.2f}%")


if __name__ == "__main__":
    main()
