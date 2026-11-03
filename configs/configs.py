#!/usr/bin/env python3
"""Test configurations for the single LUT4c_frame_config_dffesr cell.

A config is a self-contained stimulus sequence: frame writes through the
wrapper's FrameData/FrameStrobe ports, then functional vectors. Each step is
one tester cycle; the testbench applies a step, waits for it to settle and
samples O/Co when the step is flagged. A small Python model of the cell gives
the expected value of every sample, used to sanity check the golden runs.

Config bit layout (verified in NOTES.md):
  [15:0] LUT truth table, index = {I3, I2, I1, I0mux}
  [16]   output mux: 0 = combinational LUT_out, 1 = LUT_flop
  [17]   I0 mux: 0 = I[0], 1 = Ci
  [18]   flop value loaded when SR = 1 (synchronous, gated by EN)

Stimulus file format (one line per step, 16 hex digits):
  [31:0] FrameData  [51:32] FrameStrobe  [55:52] I  [56] Ci  [57] SR
  [58] EN  [59] UserCLK  [60] sample
Add a config by writing a function that returns a Seq and adding it to
CONFIGS.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
BITMAP_CSV = HERE / "la_bitmap.csv"
N_CONFIG_BITS = 19
FF_BIT, I0MUX_BIT, RV_BIT = 16, 17, 18


@dataclass(frozen=True)
class LatchMap:
    frame_data_bit: int
    frame_strobe: int


def load_bitmap(path: Path = BITMAP_CSV) -> list[LatchMap]:
    rows = list(csv.DictReader(path.open()))
    if [int(r["config_bit"]) for r in rows] != list(range(N_CONFIG_BITS)):
        raise SystemExit(f"{path}: expected config bits 0..{N_CONFIG_BITS - 1}")
    return [LatchMap(int(r["frame_data_bit"]), int(r["frame_strobe"])) for r in rows]


BITMAP = load_bitmap()
FRAMES = sorted({m.frame_strobe for m in BITMAP})


def make_bits(truth_table: int, ff: int = 0, i0_from_ci: int = 0, reset_value: int = 0) -> int:
    if not 0 <= truth_table < (1 << 16):
        raise ValueError("truth table must be 16 bits")
    return truth_table | (ff << FF_BIT) | (i0_from_ci << I0MUX_BIT) | (reset_value << RV_BIT)


def parity_table(invert: bool = False) -> int:
    tt = 0
    for i in range(16):
        if (bin(i).count("1") & 1) ^ invert:
            tt |= 1 << i
    return tt


XOR_TT = parity_table()
XNOR_TT = parity_table(invert=True)


@dataclass
class Step:
    fd: int = 0
    fs: int = 0
    i: int = 0
    ci: int = 0
    sr: int = 0
    en: int = 0
    clk: int = 0
    sample: int = 0
    note: str = ""

    def encode(self) -> str:
        word = (
            (self.fd & 0xFFFFFFFF)
            | ((self.fs & 0xFFFFF) << 32)
            | ((self.i & 0xF) << 52)
            | ((self.ci & 1) << 56)
            | ((self.sr & 1) << 57)
            | ((self.en & 1) << 58)
            | ((self.clk & 1) << 59)
            | ((self.sample & 1) << 60)
        )
        return f"{word:016x}"


@dataclass
class Seq:
    name: str
    description: str
    steps: list[Step] = field(default_factory=list)
    _cur: Step = field(default_factory=Step)

    def step(self, sample: bool = False, note: str = "", **kw: int) -> None:
        for k, v in kw.items():
            if not hasattr(self._cur, k):
                raise KeyError(k)
            setattr(self._cur, k, v)
        s = Step(**{**self._cur.__dict__, "sample": int(sample), "note": note})
        self.steps.append(s)

    def write_config(self, bits: int) -> None:
        """Write every frame that holds a bit of this cell: data, strobe, release."""
        for frame in FRAMES:
            data = 0
            for b, m in enumerate(BITMAP):
                if m.frame_strobe == frame and (bits >> b) & 1:
                    data |= 1 << m.frame_data_bit
            self.step(fd=data, fs=0, note=f"frame{frame} data")
            self.step(fs=1 << frame, note=f"frame{frame} strobe")
            self.step(fs=0, note=f"frame{frame} release")
        self.step(fd=0)

    def clock(self, sample: bool = True, note: str = "") -> None:
        self.step(clk=1, note=note)
        self.step(clk=0, sample=sample, note=note)

    def write_stim(self, path: Path) -> None:
        path.write_text("".join(s.encode() + "\n" for s in self.steps))


def cell_model(seq: Seq) -> list[tuple[int | None, int | None]]:
    """Expected (O, Co) per sampled step; None means X."""
    latch: list[int | None] = [None] * N_CONFIG_BITS
    ff: int | None = None
    prev_clk = 0
    out = []
    for s in seq.steps:
        for b, m in enumerate(BITMAP):
            if (s.fs >> m.frame_strobe) & 1:
                latch[b] = (s.fd >> m.frame_data_bit) & 1
        i0mux = None if latch[I0MUX_BIT] is None else (s.ci if latch[I0MUX_BIT] else s.i & 1)
        if i0mux is None:
            lut_out = None
        else:
            idx = (s.i & 0b1110) | i0mux
            lut_out = latch[idx]
        if s.clk and not prev_clk and s.en:
            ff = latch[RV_BIT] if s.sr else lut_out
        prev_clk = s.clk
        if latch[FF_BIT] is None:
            o = None
        else:
            o = ff if latch[FF_BIT] else lut_out
        i1, i2 = (s.i >> 1) & 1, (s.i >> 2) & 1
        co = (s.ci & i1) | (s.ci & i2) | (i1 & i2)
        if s.sample:
            out.append((o, co))
    return out


# ---------------------------------------------------------------- configs

def _setup(seq: Seq, bits: int) -> None:
    """UserCLK stopped, SR held, frames written, flop reset, SR released.

    SR is synchronous and only acts when EN = 1, so holding it does nothing on
    its own: one UserCLK pulse with SR = EN = 1 loads the reset value. Without
    it the flop stays X in the combinational configs.
    """
    seq.step(clk=0, sr=1, en=0, i=0, ci=0, note="hold SR")
    seq.write_config(bits)
    seq.step(en=1, note="EN for reset")
    seq.clock(sample=False, note="reset flop to bit 18")
    seq.step(sr=0, en=0, note="release SR")


def _comb_sweep(seq: Seq, ci_values: tuple[int, ...] = (0,)) -> None:
    for ci in ci_values:
        for v in range(16):
            seq.step(i=v, ci=ci, sample=True, note=f"I={v:04b} Ci={ci}")


def cfg_a() -> Seq:
    s = Seq("A", "XOR truth table, comb output, I0 from I[0]; sweep I, Ci=0")
    _setup(s, make_bits(XOR_TT))
    _comb_sweep(s)
    return s


def cfg_b() -> Seq:
    s = Seq("B", "XNOR truth table, comb output, I0 from I[0]; sweep I, Ci=0")
    _setup(s, make_bits(XNOR_TT))
    _comb_sweep(s)
    return s


def cfg_a2() -> Seq:
    s = Seq("A2", "write B, then rewrite A (every LUT latch toggles); sweep I")
    _setup(s, make_bits(XNOR_TT))
    s.write_config(make_bits(XOR_TT))
    _comb_sweep(s)
    return s


def cfg_c() -> Seq:
    s = Seq("C", "XOR truth table, comb output, I0 from Ci; sweep I and Ci")
    _setup(s, make_bits(XOR_TT, i0_from_ci=1))
    _comb_sweep(s, ci_values=(0, 1))
    return s


def cfg_bc() -> Seq:
    s = Seq("BC", "merge candidate for B + C: XNOR, I0 from Ci; sweep I and Ci")
    _setup(s, make_bits(XNOR_TT, i0_from_ci=1))
    _comb_sweep(s, ci_values=(0, 1))
    return s


def _seq_test(s: Seq, rv: int) -> None:
    """Registered output. LUT = XOR, Ci = 0, so LUT_out = parity(I).

    Every hold/priority check starts from a flop value that differs from the
    value the faulty behaviour would load, so the fault shows on O.
    """
    nrv = 1 - rv
    v_nrv = 0b0001 if nrv else 0b0000  # input with LUT_out = not rv
    _setup(s, make_bits(XOR_TT, ff=1, reset_value=rv))
    # Load the reset value with LUT_out opposite to it.
    s.step(sr=1, en=1, i=v_nrv)
    s.clock(note="SR=1 EN=1 -> rv")
    s.step(sr=0)
    # Capture LUT_out for every input with EN=1.
    for v in range(16):
        s.step(i=v, en=1)
        s.clock(note=f"EN=1 capture I={v:04b}")
    # EN=0 holds a 0 against data 1, and a 1 against data 0.
    s.step(i=0b0000, en=1)
    s.clock(note="EN=1 capture 0")
    s.step(i=0b0001, en=0)
    s.clock(note="EN=0 hold 0")
    s.step(i=0b0001, en=1)
    s.clock(note="EN=1 capture 1")
    s.step(i=0b0000, en=0)
    s.clock(note="EN=0 hold 1")
    # SR is gated by EN: SR=1 EN=0 holds not-rv.
    s.step(i=v_nrv, en=1, sr=0)
    s.clock(note="EN=1 capture not rv")
    s.step(i=v_nrv, en=0, sr=1)
    s.clock(note="SR=1 EN=0 hold")
    # SR has priority over data: LUT_out = not rv, SR=1 EN=1 -> rv.
    s.step(i=v_nrv, en=1, sr=1)
    s.clock(note="SR=1 EN=1 -> rv")
    s.step(i=v_nrv, en=1, sr=0)
    s.clock(note="EN=1 capture not rv")
    # No clock: O must hold while inputs change.
    for v in (0b0000, 0b0001, 0b0011, 0b0111):
        s.step(i=v, sample=True, note="no clock, O holds")


def cfg_d0() -> Seq:
    s = Seq("D0", "registered output, reset value 0; SR/EN/clock checks")
    _seq_test(s, 0)
    return s


def cfg_d1() -> Seq:
    s = Seq("D1", "registered output, reset value 1; SR/EN/clock checks")
    _seq_test(s, 1)
    return s


def cfg_and() -> Seq:
    s = Seq("AND", "4-input AND, comb output (Phase 1 wrapper check)")
    _setup(s, make_bits(1 << 15))
    _comb_sweep(s)
    return s


CONFIGS = {
    f().name: f for f in (cfg_a, cfg_b, cfg_a2, cfg_c, cfg_d0, cfg_d1, cfg_bc, cfg_and)
}
# Plan configs first, then merge candidates. AND is only the Phase 1 check.
TEST_CONFIGS = ["A", "B", "A2", "C", "D0", "D1", "BC"]


def main() -> None:
    ap = argparse.ArgumentParser(description="Write stimulus and expected files")
    ap.add_argument("--out", type=Path, default=HERE.parent / "build" / "stim")
    ap.add_argument("names", nargs="*", default=list(CONFIGS))
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    for name in args.names:
        seq = CONFIGS[name]()
        seq.write_stim(args.out / f"{name}.stim")
        exp = cell_model(seq)
        with (args.out / f"{name}.expected").open("w") as f:
            for o, co in exp:
                f.write(f"{'x' if o is None else o}{'x' if co is None else co}\n")
        print(f"{name}: {len(seq.steps)} steps, {len(exp)} samples")


if __name__ == "__main__":
    main()
