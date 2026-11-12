# fab-lut-test

Configuration-based manufacturing test for [FABulous](https://github.com/FPGA-Research/FABulous)
eFPGA fabrics: test configurations, stimulus and stuck-at fault simulation for the
LUT4AB tile, built only from what FABulous generates. FABulous itself is not modified.

A test is a sequence of bitstream frame writes (FrameData / FrameStrobe) followed by
input vectors. Faults are single stuck-at faults on every net of the synthesized
netlist (stems, and in the `_br` netlists every fanout branch). A fault counts as
detected only on a definite 0 / 1 difference at an observed output; every storage
element starts unknown (X), and faults left X-only are decided by enumerating all
power-up values of the elements they leave unwritten.

## Results

### LUT test: 3 configs, 100%

Configs BC1 (XNOR, I0 from Ci, reset value 1), D0C (XOR registered, reset value 0,
Ci = 1) and D1 (XOR registered, reset value 1), written into every LUT through the
normal config interface:

| netlist | faults | strict | every power-up state |
|---|---|---|---|
| cell, sky130 cells | 116 | 93.1% | **100%** |
| cell, generic gates | 218 | 96.3% | **100%** |
| cell, sky130, branch faults | 224 | 88.4% | **100%** |
| cell, generic, branch faults | 380 | 93.2% | **100%** |
| LUT4AB tile, generic, LUT faults (8 LUTs) | 1476 | 99.3% | **100%** |
| LUT4AB tile, generic, branch faults, LUT faults | 2868 | 93.8% | **100%** |

The same holds when the three configs run as one continuous sequence from a single
power-up.

### Switch matrix: first baseline

16 configs (16 = the largest mux fan-in), config c setting every switch-matrix mux to
input c mod fan-in, all 201 tile inputs driven with unique codes, all 201 tile outputs
observed (an upper bound: in a fabric the outputs are reached only through neighbouring
tiles). Every power-up state:

| netlist | switch-matrix faults | baseline | baseline + {BC1, D0C, D1} |
|---|---|---|---|
| tile, generic | 8244 | 89.9% | 92.5% |
| tile, generic, branch faults | 17276 | 85.2% | 87.7% |

Most of what the baseline misses is structural: a mux code is selected only in configs
where that mux's output is not observed, and 22 muxes (the LUT SR / EN muxes, J_SR /
J_EN and the MUX8LUT selects) are never observable in a combinational baseline.

### Switch matrix: routed configs

`configs/sm_router.py` routes every mux code to an observed tile output (greedy, Dijkstra
over free muxes, loop-free, exact model grading, PathFinder-style history, compaction).
**16 configs** (the fan-in lower bound) show every code at 0 and 1 and against every
input one select bit away. Every power-up state:

| netlist | switch-matrix faults | baseline | router | router + {BC1, D0C, D1} |
|---|---|---|---|---|
| tile, generic | 8244 | 89.9% | **97.7%** | 98.8% |
| tile, generic, branch faults | 17276 | 85.2% | **97.0%** | 98.1% |

On the generic tile every 4:1, 8:1 and 16:1 mux fault is detected. What is left has no
gate-only path to an output: the LUT SR / EN muxes and the flop logic (they need clocked
configs) and, in the branch netlist, 43 FrameStrobe branches into config latches that
escape for some power-up state. Details: `reports/rt_summary.md` (local).

### Switch matrix: clocked SR / EN configs

`configs/sm_clocked.py`: 8 configs with registered LUTs (8 = fan-in of J_SR_BEG0 /
J_EN_BEG0), every LUT through every (SR code, EN code, reset value) combination, flop
outputs routed to tile outputs, 64 clocked vectors. They test the SR / EN muxes and the
flop logic that no combinational config reaches.

| netlist | switch-matrix faults | router (16) | router + clocked (24 configs) |
|---|---|---|---|
| tile, generic | 8244 | 97.7% | **100%** |
| tile, generic, branch faults | 17276 | 97.0% | **99.84%** |

Every power-up state. The 27 branch faults left are FrameStrobe branches into config
latches (stuck latch enables) and 12 select-line branches masked at gate level.

### Multi-tile arrays

R x C arrays of the tile netlist with FABulous wiring (`rtl/gen_array.py`,
`synth/build_array.sh`); stimulus only at the array border, observation only at the
border, config through FrameData rows and FrameStrobe columns. `configs/fab_router.py`
routes every mux code of every tile to the border. Switch-matrix faults, strict:

| array | faults | tile configs in every tile | array router + clocked | tile level, same faults |
|---|---|---|---|---|
| 2x2 | 32112 | 97.3% | **99.40%** (27 + 8 configs) | 99.40% |
| 3x3 | 72252 | 94.8% | **99.40%** (37 + 8 configs) | 99.40% |

LUT faults with the three LUT configs at array level (every LUT input driven from a
border input of its own, every LUT output routed to the border): 2x2 and 3x3 **99.46%**,
the tile level (strict). All faults, router + clocked + LUT configs: 98.97% (2x2), 99.00%
(3x3). The centre tile of the 3x3 array, which has no border port, reaches the same
coverage. An Icarus reference check (`sim/array_check.py`) compares a fault sample
against the fast kernel.
More configs than one tile needs (37 vs 16): exits to the border are shared between a
tile's own codes and the neighbour signals it relays. With 16 router configs the 3x3
array is at 99.25%.

### Scaling and the real fabric border

The array router without its slow polish step, with fill decorrelation instead (muxes
that share inputs pick different sources): 3x3 routed in 6 s instead of 32 min, fewer
configs, the same detected faults. A set cover over the full fault matrices
(`sim/array_cover.py`) and the SR / EN tests folded into the router configs keep the
config count almost flat with array size, at tile-level coverage (tile level:
switch matrix 99.40%, LUT faults 99.46%, strict):

| array | tiles | switch matrix | LUT faults | configs (all detected) | configs (99.9%) |
|---|---|---|---|---|---|
| 2x2 | 4 | 99.40% | 99.46% | 27 | 20 |
| 3x3 | 9 | 99.40% | 99.46% | 28 | 20 |
| 4x4 | 16 | 99.40% | 99.46% | 30 | 21 |
| 5x5 | 25 | 99.34% | 99.46% | 35 | 23 |

At 5x5 a fault more often makes the faulty circuit oscillate; with only the oscillating
nets at X (instead of the whole run) the switch matrix is 0.06 points below tile level.

With the border of the demo fabric instead (`rtl/gen_array.py --fabric`: W_IO column on
the west, N / S termination tiles, east edge open), a 4x3 block is reached only through
the W_IO pads: 8 inputs and 16 outputs for 12 tiles. 71 configs then detect 61.7% of the
faults (switch matrix 60.8%), about evenly in every column: every observed signal has to
enter the 16 wires the W_IO output muxes read. Test access, not the configs, is the
limit at fabric level; this is where boundary scan at the pads and on-chip compaction
come in.

### Test access built from configuration

At the fabric border the limit is observation. The fix stays within "test through
configuration": `configs/fab_router.py --backbone` first links the LUTs of every row into
a chain (each LUT output routed into an input of the next LUT, the chain end to the row's
W_IO pad) and then routes the targets into the chain LUTs' free inputs. With every LUT
registered as XOR (`sim/run_array.py --suite chain`), each chain is a signature register
made only of configuration. Fabric block 4x3, strict:

| configs | without backbone | registered backbone |
|---|---|---|
| 30 | 51.7% (61.0% with clocked + LUT configs) | 68.5% (71.7%) |
| 60, two chains per row, XOR / XNOR alternating | | **74.1%** (**76.4%**) |
| 71 (earlier best) | 61.7% | |

With two chains per row each row ends at two pads, and alternating XOR / XNOR configs
cover the faults a fixed parity cancels. Both chain styles together with the clocked and
LUT configs reach 79.4%, all suites together 82.6%. The faults left are mostly never
observed at a pad.

### Power-up check and oscillation in the fast kernel

`sim/fastsim/powerup.py` runs the cone-split power-up enumeration on the fast kernel
(per-lane deposits, definitely-written masks): on the branch-fault tile it agrees with the
Icarus runs (604 of 604 faults for the baseline configs) in about 1.5 min instead of hours,
and decides faults where Icarus timed out. `FASTSIM_OSC=local` keeps a lane that
oscillates under its fault alive with only the oscillating nets at X; on the 5x5 array
this recovers 882 of the 1050 switch-matrix faults the whole-lane model lost.
Faults with at most 10 undefined storage elements are enumerated directly (no cones),
the kernel calls run on threads and a fault stops at its first decided group. Arrays,
all suites, every power-up state: 2x2 99.54% (strict 98.97%), 3x3 **99.57%** (strict
99.00%, all 506 X-only faults detected, 3.7 h).

### Full demo fabric

`synth/build_fullfabric.sh` synthesizes every tile type of the FABulous demo fabric and
flattens FABulous's own `eFPGA.v`: 577941 gates, 1157594 faults. The fast kernel simulates
one config of it at 0.8 ms per fault on 2 threads (all faults: about 15 min per config).

Every tile type is modelled from its generated FABulous sources (`configs/tile_model.py`:
switch matrix, pass-throughs, BEL pins), and two BELs by their function:

- DSP MULADD at config 0: Q = A * B + C, combinational.
- RegFile: a 32 x 4 memory, X until written. The router makes it defined in every test
  config: one LUT of each neighbour tile becomes a constant (0, and 1 with all four
  inputs from the 0: a LUT is a mux tree, so with X inputs even an all-ones table gives
  X), the constants go to W_en and to the same address on the write and both read ports
  (config index mod 32), the write data comes from pad inputs. These routes carry values,
  so they use wires only (never through a LUT), and loop breaking leaves them alone.

RAM_IO (BRAM data) stays opaque: its outputs, and every net they reach through fixed
wiring, are never used as a source. The router with the backbone (`sim/run_fullfabric.py
route`, two chains per row, each ending at a W_IO pad; checkpointed, `--extend --resume`)
builds two config sets of 24: MULADD model only (`configs/fab_router_fullfabric_muladd.json`),
and MULADD + RegFile (`configs/fab_router_fullfabric.json`, 7.8 h). Fault simulation of a
seeded random sample of 10,000 faults, every LUT registered (XOR / XNOR alternating), 64
clocked vectors on the 28 pads, strict (16-23 min per set on 4 threads):

| tile type | sampled | 24, BELs opaque at 12 | 24, MULADD | 24, MULADD + RegFile | **both sets, 48** |
|---|---|---|---|---|---|
| all | 10000 | 63.5% | 74.7% | 72.4% | **80.5% +- 0.8** |
| LUT4AB | 6935 | 72.8% | 81.7% | 77.0% | 86.1% +- 0.8 |
| W_IO | 175 | 70.3% | 75.4% | 78.9% | 82.9% +- 5.6 |
| DSP (wrapper, top, bottom) | 1184 | 31.6-50.6% | 66.2-76.2% | 65.3-77.4% | 73.5-83.3% |
| RAM_IO | 513 | 50.9% | 53.8% | 56.9% | 58.5% +- 4.3 |
| RegFile | 1178 | 40.8% | 47.5% | 55.5% | 61.9% +- 2.8 |

(The first column is the 12-config run before any BEL model.) The RegFile configs give up
some LUT coverage (their constants take 28 LUTs and routing), but the two sets detect
different faults; set cover (`sim/array_cover.py --matrix`) keeps all 48, 27 reach 95% of
what they detect. Every sampled fault left reaches a pad as X. The power-up check on 400
of them (`run_fullfabric.py powerup`; elements the fault-free run never writes stay X,
which keeps a detection sound) proves 0.8% +- 0.8 detected for every power-up state: the X
is not power-up state but RegFile words a config does not write and BRAM data.

Test time: a full config is 200 frames (3,401 words); between consecutive configs 159 of
the 200 frames change. Through the parallel `SelfWriteData` port at 50 MHz the 48 configs
load in about 4-6 ms; bitbang about 0.7 s, UART about 50 s. The 64 vectors per config are
negligible. No area is added: every test structure is configuration.

### Configuration logic

`synth/build_cfgtop.sh` builds FABulous's configuration path from `eFPGA_top.v` unchanged
(eFPGA_Config with config_UART, bitbang and ConfigFSM, the 14 frame data registers, the 10
frame selects) with a latch model of the fabric's ConfigMem in place of the fabric (one
latch per used config bit, 69,482; no faults on the model): 4,874 gates, 8,606 faults.
`sim/run_cfgtop.py` loads two test configs through SelfWriteData, two frames through
bitbang and one through the UART (binary and hex), each with data different from what
the memory holds; a fault is detected when a config bit, ComActive or ReceiveLED (also
every 16 clocks) differs. 1.04M steps, 29 min on 3 threads:

| part | faults | strict | config bits left unwritten |
|---|---|---|---|
| bitbang | 1356 | 91.3% | 10 |
| Frame_Data_Reg | 776 | 66.2% | 234 |
| config_UART | 3680 | 57.4% | 20 |
| Frame_Select | 126 | 53.2% | 55 |
| ConfigFSM | 618 | 38.8% | 277 |
| all | 8606 | **60.5%** | 919 |

A fault that stops frames from being written leaves those config bits at their power-up
value (X here); on silicon that fabric is almost surely broken, but not for every
power-up value. Most undetected UART faults are in the checksum, the LED blink counter
and the hex path, seen only on ReceiveLED.

### Findings about FABulous

- config_UART's hex mode never delivers a word: in GET_DATA the nibble state goes back to
  HIGH_NIBBLE on every clock without a stop-bit tick, so the low nibble is never seen
  (demo and current template, Verilog and VHDL; Icarus on the RTL: binary 1 word, hex 0).
  Binary mode works.

- After configuration FrameData keeps the last word written (`Frame_Data_Reg` is never
  cleared), and ConfigFSM addresses every frame explicitly, so the frame write order is
  a free test parameter. Choosing which frame each config writes last exposes stuck-at-1
  FrameStrobe faults that the default order hides.
- 2:1 mux AND-term faults need configs where the unselected input is the opposite of
  the selected one (BC1, D0C).
- The LUT4AB switch matrix: 194 muxes plus 81 wires, 462 config bits, every bit a mux
  select, binary encoding, no padded codes; the model matches the generated RTL on all
  1248 mux codes.
- Writing frames one at a time between two loop-free configs passes through
  combinational loops; the frame order can be chosen to avoid them.

## Setup

Tested on Ubuntu 24.04 (Linux or WSL).

- FABulous (a fabric generated with `FABulous create-project demo` and
  `run_FABulous_fabric`; the LUT4AB files used are copied into `rtl/`)
- Yosys 0.66 (`pip install yowasp-yosys`), Icarus Verilog 12, Python 3.12
- sky130 cell models and Liberty (for the sky130 cell netlists only)

`env.sh` sets the tool and PDK paths; by default it expects them in `../ext`. Source it
first:

```bash
. ./env.sh
```

The synthesized netlists and fault lists are checked in, so the simulation steps run
without a synthesis setup.

## Running

Cell (one LUT4c), all four netlists:

```bash
python3 sim/run.py check
python3 sim/run.py faultsim generic
python3 sim/run.py exhaust generic --configs BC1 D0C D1 --suffix _rec
python3 sim/analyze.py
```

Tile, LUT configs:

```bash
python3 sim/run_tile.py check
python3 sim/run_tile.py faultsim tile_generic --jobs 4
python3 sim/run_conesplit.py run tile_generic --only . --xonly --jobs 4
python3 sim/tile_analyze.py
```

Tile, switch-matrix baseline:

```bash
python3 sim/sm_verify.py
python3 sim/run_sm.py check
python3 sim/run_sm.py faultsim tile_generic --jobs 4
python3 sim/run_sm.py faultsim tile_generic --sequence --jobs 4
python3 sim/run_conesplit.py run tile_generic --suite sm --only . --xonly --exclude 'FrameStrobe\[[0-9]+\]' --jobs 4
python3 sim/sm_analyze.py
```

Tile, routed switch-matrix configs:

```bash
python3 configs/sm_router.py
python3 sim/run_sm.py --suite router faultsim tile_generic --jobs 4
python3 sim/run_sm.py --suite router faultsim tile_generic --sequence --jobs 4
python3 sim/run_sm.py --suite clocked faultsim tile_generic --jobs 4
python3 sim/rt_analyze.py
```

Arrays (the netlists and fault lists are built, not checked in):

```bash
./synth/build_array.sh 3 3
python3 configs/fab_router.py 3 3 --no-polish
python3 sim/run_array.py faultsim 3 3 --suite router --tag _nopolish --jobs 4
python3 sim/run_array.py faultsim 3 3 --suite merged --tag _nopolish --jobs 4
python3 sim/run_array.py faultsim 3 3 --suite clocked --jobs 4
python3 sim/run_array.py faultsim 3 3 --suite lut --jobs 4
python3 sim/run_array.py report
python3 sim/array_check.py 3 3 --suite router
python3 sim/array_cover.py 3 3 --suites merged_nopolish lut router_nopolish clocked
./synth/build_wio.sh && ./synth/build_fabric.sh 4 3
python3 configs/fab_router.py 4 3 --fabric --no-polish
python3 sim/array_powerup.py 3 3
```

Full demo fabric:

```bash
./synth/build_fullfabric.sh
python3 sim/run_fullfabric.py route --configs 12 --chains 2
python3 sim/run_fullfabric.py faultsim --sample 10000 --jobs 4
python3 sim/run_fullfabric.py powerup --sample 400
python3 sim/array_cover.py --matrix reports/fullfabric_chain_sample_matrix.csv
```

Configuration logic:

```bash
./synth/build_cfgtop.sh
python3 sim/run_cfgtop.py faultsim --jobs 3
```

Results go to `reports/`. Tile fault simulation uses the C kernel in `sim/fastsim`
by default (about 1 s for all 20,890 `tile_generic_br` faults on the 16 SM configs, 4
threads) and runs 20 random faults through Icarus as a reference check (`--check N`, 0
turns it off). `--engine icarus` runs everything through Icarus (about 4.5 hours for
`tile_generic_br` on 4 cores; checkpointed, resumes after an interruption).

Rebuilding from scratch (needs the full setup):

```bash
./synth/run_synth.sh
python3 faults/gen_faults.py synth/netlist_generic.json --out faults/generic.csv
python3 faults/tile_regions.py tile_generic
```

## Layout

| path | contents |
|---|---|
| `rtl/` | FABulous LUT4AB tile files, cell and tile wrappers, `tile_parse.py` |
| `synth/` | Yosys scripts and the synthesized netlists (sky130 cells, generic gates, branch variants) |
| `faults/` | fault list generator, fault lists, tile fault regions |
| `configs/` | config models: cell (`configs.py`), tile LUT configs (`tile_configs.py`), test routing, switch-matrix model (`sm_model.py`), baseline (`sm_configs.py`) and router (`sm_router.py`, `sm_router.json`), clocked SR / EN configs (`sm_clocked.py`) |
| `sim/` | runners (`run.py`, `run_tile.py`, `run_sm.py`), exhaustive power-up (`run_conesplit.py`), loop check, analysis |

## Not covered yet

A RAM_IO / BRAM model (opaque in the full-fabric run), RegFile words other than the one a
config writes, the configuration logic together with the fabric in one simulation (it is
tested against a model of the config memory), bridging faults and timing.

## License

See `LICENSE`.
