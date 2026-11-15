#!/usr/bin/env python3
"""LUT4c_frame_config_dffesr config bits (one LUT of LUT4AB).

  [15:0] LUT truth table, index = {I3, I2, I1, I0mux}
  [16]   output mux: 0 = combinational LUT_out, 1 = LUT_flop
  [17]   I0 mux: 0 = I[0], 1 = Ci
  [18]   flop value loaded when SR = 1 (synchronous, gated by EN)

make_bits builds the 19-bit word; XOR_TT / XNOR_TT are the parity tables of the test
configs; rotated gives a frame order that ends on a chosen frame.
"""
from __future__ import annotations


N_CONFIG_BITS = 19
FF_BIT, I0MUX_BIT, RV_BIT = 16, 17, 18


def rotated(frames: list[int], last: int) -> list[int]:
    k = frames.index(last)
    return frames[k + 1:] + frames[:k + 1]


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


# ---------------------------------------------------------------- configs
