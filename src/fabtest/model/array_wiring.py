#!/usr/bin/env python3
"""Generate rtl/array_<R>x<C>.v: an R x C array of LUT4AB tiles (Phase 10).

Each tile is the synthesized tile netlist (module tile_top of
synth/netlist_tile_generic.v), instance X<x>Y<y> (x = column from the west,
y = row from the north, as in FABulous). Wiring as FABulous generates it
(ext/demo/Fabric/eFPGA.v, e.g. Tile_X1Y2_LUT4AB): every <D>END / <D>MID input
bus takes the neighbour's <D>BEG / <D>BEGb output bus whole:
  from the south tile (x, y+1): N1END, N2MID, N2END, N4END, NN4END, Ci <- Co,
      UserCLK <- UserCLKo, FrameStrobe <- FrameStrobe_O
  from the west tile (x-1, y):  E1END, E2MID, E2END, EE4END, E6END,
      FrameData <- FrameData_O
  from the north tile (x, y-1): S1END, S2MID, S2END, S4END, SS4END
  from the east tile (x+1, y):  W1END, W2MID, W2END, WW4END, W6END
Border: an input with no neighbour inside the array is an array input port
X<x>Y<y>_<port>; a routing output with no reader inside is an array output
port X<x>Y<y>_<port> (the tester reaches the array border). Config: one
FrameData_R<y> bus per row (into the west tile), one FrameStrobe_C<x> bus per
column (into the south tile), one UserCLK into every south tile. FrameData_O
(east), FrameStrobe_O and UserCLKo (north) of the border tiles are left
unconnected.

Run: python3 rtl/gen_array.py R C   (writes rtl/array_<R>x<C>.v)
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

from fabtest.paths import REPO as ROOT  # noqa: E402

# input port -> (dx, dy, neighbour output port)
FROM = {
    "N1END": (0, 1, "N1BEG"), "N2MID": (0, 1, "N2BEG"), "N2END": (0, 1, "N2BEGb"),
    "N4END": (0, 1, "N4BEG"), "NN4END": (0, 1, "NN4BEG"), "Ci": (0, 1, "Co"),
    "UserCLK": (0, 1, "UserCLKo"), "FrameStrobe": (0, 1, "FrameStrobe_O"),
    "E1END": (-1, 0, "E1BEG"), "E2MID": (-1, 0, "E2BEG"), "E2END": (-1, 0, "E2BEGb"),
    "EE4END": (-1, 0, "EE4BEG"), "E6END": (-1, 0, "E6BEG"), "FrameData": (-1, 0, "FrameData_O"),
    "S1END": (0, -1, "S1BEG"), "S2MID": (0, -1, "S2BEG"), "S2END": (0, -1, "S2BEGb"),
    "S4END": (0, -1, "S4BEG"), "SS4END": (0, -1, "SS4BEG"),
    "W1END": (1, 0, "W1BEG"), "W2MID": (1, 0, "W2BEG"), "W2END": (1, 0, "W2BEGb"),
    "WW4END": (1, 0, "WW4BEG"), "W6END": (1, 0, "W6BEG"),
}
CONFIG_OUT = {"FrameData_O", "FrameStrobe_O", "UserCLKo"}


# Termination tiles (rtl/term/*_switch_matrix.list): pure wires, bit order reversed.
# N_term above the north row: <S>END of the tile <- its own <N>BEG, reversed.
N_TERM = {"S1END": "N1BEG", "S2MID": "N2BEG", "S2END": "N2BEGb", "S4END": "N4BEG", "SS4END": "NN4BEG"}
# S_term below the south row: <N>END of the tile <- its own <S>BEG, reversed; Ci <- GND (Co0, GND0).
S_TERM = {"N1END": "S1BEG", "N2MID": "S2BEG", "N2END": "S2BEGb", "N4END": "S4BEG", "NN4END": "SS4BEG"}
# W_IO west of column 0: tile <E>END <- W_IO <E>BEG, W_IO <W>END <- tile <W>BEG (same names).
WIO_E = {"E1END": "E1BEG", "E2MID": "E2BEG", "E2END": "E2BEGb", "EE4END": "EE4BEG", "E6END": "E6BEG"}
WIO_W = {"W1END": "W1BEG", "W2MID": "W2BEG", "W2END": "W2BEGb", "WW4END": "WW4BEG", "W6END": "W6BEG"}
