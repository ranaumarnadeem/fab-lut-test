# fab-lut-test (fabtest)

Configuration-based manufacturing test for [FABulous](https://github.com/FPGA-Research/FABulous)
eFPGA fabrics, without scan chains and without changing FABulous. From a FABulous project
with its generated RTL, fabtest builds test configurations (bitstreams), input vectors
and the expected outputs for a tester, and measures their stuck-at fault coverage on a
gate-level netlist of the whole fabric.

```bash
scripts/fabtest run /path/to/fabulous_project      # init, synth, route, faultsim, report, export
```

Each config routes signals from the input pads through the switch matrices, LUTs and BELs
to the output pads (observation chains of XOR LUTs, RegFile and BlockRAM pinned to defined
values); the tester loads it through the configuration port, applies its vectors and
compares the outputs. The export holds one bitstream and one vector file per config, a
manifest, a test plan and an Icarus testbench that replays everything on the RTL.

Demo fabric (16 x 10 tiles, 1,169,398 faults; sampled fault simulation):

| | coverage |
|---|---|
| 96 configs, 64 vectors each (the `demo` suite, 50k sample) | 89.02% +- 0.27 (router before the bidirectional cover; with it +1.2 points on a 2,000-fault comparison) |
| 96 configs, 256 vectors each | 89.96% |
| 256 configs | 92.78% +- 0.72 (switch matrices 94.4%) |
| configuration logic (`fabtest cfgtest`: FSM, frame registers, UART, bitbang) | 73.3% of 9,758 faults |
| clock network (`fabtest clockcheck`) | 198 of 278 faults |
| proven untestable (`fabtest untestable`, SAT per tile) | 16 of the 5,488 undetected (sample; 6 not decided) |

Validated on four more fabrics built with FABulous (other layouts, generic multiplexers,
an unknown BEL, an uneven RegFile / DSP / BlockRAM mix): the configs pass a fault-free
check against the gates, replay in Icarus on FABulous's RTL and are byte-equal to
FABulous `bit_gen`.

Fault model: single stuck-at faults on every net of the synthesized netlist. A fault
counts as detected only on a definite 0 / 1 difference at an observed output; every
storage element starts unknown (X), and faults left X-only can be decided by enumerating
the power-up values of the elements they leave unwritten (`fabtest powerup`).

## Results (in the order they were obtained)

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

### Full fabric with its BlockRAMs

`synth/build_fabbram.sh` adds the 7 BlockRAMs of `eFPGA_top.v` to the full fabric:
FABulous's `BlockRAM_1KB` wrapper unchanged, its SRAM macro (a blackbox in FABulous, a
hard macro tested by memory BIST) as a behavioural model without faults: write and read
address registered on the clock, a read of the address written in the same cycle X
(undefined for the dual-port macro). 823214 cells, 1170166 faults (13048 in the BRAM
wrappers). The model reads the BRAMs as 256 x 32 memories, X until written. In the BRAM
test configs (`run_fullfabric.py --bram`) the C inputs select 32-bit ports (GND inputs of
the RAM_IO muxes, C4 from a constant LUT or the write enable from a pad), address bits
1..7 are the same constant on both ports, write address bit 0 comes from a pad p and read
address bit 0 from NOT p (a LUT with a fixed table), the data from a second pad: a read
never meets the write of its cycle and returns the other word. (Read address = write
address was the first try: every write cycle reads X, which the registered XOR chains
spread everywhere: 36% alone.) Same 10,000 sampled faults, the earlier sets simulated on
this netlist too:

| part | sampled | MULADD 24 | RegFile 24 | BRAM 24 | **all three, 72** |
|---|---|---|---|---|---|
| all | 10000 | 74.1% | 72.6% | 74.7% | **83.8% +- 0.7** |
| LUT4AB | 6822 | 82.0% | 78.1% | 78.8% | 88.7% +- 0.8 |
| RAM_IO | 519 | 57.0% | 59.7% | 80.9% | 84.4% +- 3.1 |
| W_IO | 184 | 79.3% | 84.2% | 87.0% | 89.7% +- 4.4 |
| DSP (wrapper, top, bottom) | 1198 | 69.5-72.5% | 67.1-72.5% | 69.9-73.0% | 79.0-84.3% |
| RegFile | 1174 | 43.4% | 54.9% | 55.4% | 63.1% +- 2.8 |
| BlockRAM wrapper | 95 | 0% | 0% | 21.1% | 21.1% +- 8.2 |

A third set (`configs/fab_router_fullbram_v3.json`) also varies the BRAM modes per config
(C0..C3 write and read width 32 / 16 / 8 bit, C5 output register; even and odd data bits
from two pads, so the half / byte selects take every value; the model follows the
wrapper's RTL in every mode) and has the RegFile write 4 words per config (address bits
0, 1 from pads, the reads through NAND LUTs on the other inputs' constant 1: the read
never meets the word written this cycle; bits 2..4 by config index). 50,000 sampled
faults, the four sets on the netlist with BlockRAMs:

| part | sampled | MULADD | RegFile | BRAM v2 | BRAM v3 | **all four, 96** |
|---|---|---|---|---|---|---|
| all | 50000 | 73.8% | 72.2% | 74.2% | 70.9% | **86.0% +- 0.3** |
| LUT4AB | 34402 | 81.2% | 77.2% | 77.7% | 72.9% | 89.0% +- 0.3 |
| W_IO | 902 | 82.9% | 84.6% | 85.9% | 85.6% | 91.0% +- 1.9 |
| RAM_IO | 2588 | 57.6% | 59.2% | 80.8% | 78.9% | 86.4% +- 1.3 |
| DSP (wrapper, top, bottom) | 5722 | 71.2-75.0% | 68.5-76.8% | 69.8-76.3% | 64.9-69.7% | 81.5-88.7% |
| RegFile | 5747 | 44.8% | 55.4% | 56.6% | 59.2% | 73.6% +- 1.1 |
| BlockRAM wrapper | 588 | 0% | 0% | 27.2% | 51.9% | 52.6% +- 4.0 |

Every fault left reaches a pad as X in some config (none stays unobserved). Set cover
keeps all 96; 39 reach 95% of what they detect, 70 reach 99%. Loading 96 configs through
the parallel port at 50 MHz takes about 7-13 ms per chip (39 configs: 3-5 ms).

With the rotated, shared pinning (below), 96 configs routed by `fabtest route` in one run
reach **89.1% +- 0.3** on the same 50,000 faults (every fault in every config,
`scripts/compare_with_stored.py`): LUT4AB 92.0%, RegFile 79.3%, DSP 81.5-89.6%, RAM_IO
86.8%, W_IO 92.2%, BlockRAM wrapper 81.6%. Both 96-config sets together: 91.5%.

### Configuration logic

`fabtest cfgtest <work>` (or `fabtest run --cfgtest`) tests the logic that loads the
fabric, from any FABulous project: eFPGA_top without the fabric and the macros, unchanged
otherwise (eFPGA_Config with ConfigFSM, config_UART and bitbang, the frame data registers,
the frame selects), with a latch model of the fabric's ConfigMem in place of the fabric
(one latch per used config bit, from the tile types' ConfigMem; no faults on the model).
The test is the one the exported program runs:

- **fill:** test configs loaded in full through SelfWriteData, each checked on silicon by
  its own fabric vectors (here: every config bit after the load);
- **port test:** reset, the final config through SelfWriteData with the port frames
  inverted, those frames with their true data through bitbang (4 frames) and the UART
  (1 frame), then the final config's fabric vectors (here: every config bit at the end).
  The frames are the ones with the most used config bits, from different columns.

The status pins ComActive / ReceiveLED are compared every 16 clocks. A port the
fault-free netlist does not deliver is found by the tool and reported; its frames are
still sent with other data and must not arrive (FABulous's config_UART hex mode never
completes a byte). **Power-up:** a config bit a faulty load never writes keeps its
power-up value; when its fault-free value is 0 after one load and 1 after another, the
fault is seen for either value (the fill configs make 89% of the demo's bits take both).
Every fault simulated (RegFile set: final FF00, fill FF01..FF04; 1.09M steps, 52 min on 4
threads):

| part | demo faults | strict | + power-up |
|---|---|---|---|
| config_UART | 5010 | 55.5% | 55.7% |
| bitbang | 1422 | 91.9% | 92.7% |
| Frame_Data_Reg | 996 | 94.5% | 94.5% |
| Frame_Select | 838 | 49.0% | 95.7% |
| ConfigFSM | 732 | 38.0% | 81.4% |
| eFPGA_Config | 684 | 59.6% | 92.5% |
| all | 9758 | 63.2% | **73.3%** |

`fabtest export --port-test` (and `fabtest run --cfgtest`) writes the port test as tester
vectors (`export/ports/ports.vec`: per clock the config inputs and the expected status
pins, the fabric pads not driven) and a testbench that replays it on eFPGA_top and then
runs the final config's fabric vectors without another load. Icarus on FABulous's RTL:
the demo (96-config set, 530,162 port cycles, 4.7 min) and F1 (190,106) pass, the status
pins on every cycle and the vectors of FF00 after the load through bitbang and the UART.

The research version of Phase 13 (two loads, every phase observed) gave 60.5% of 8,606
faults; with its Yosys (0.33) fabtest builds the same netlist and fault list, and the
statuses match except 17 that the old kernel also detects in a small batch. It had left
out 1,218 faults (the frame data and frame select register outputs, whose nets carry the
model's port names). Most faults left are in config_UART: the checksum, the LED blink
counter and the hex path.

### Other fabrics (validation fabrics F1 to F4)

fabtest on four fabrics generated from the demo tiles with other layouts
(`tests/fabrics/make.py`, FABulous `run_fab`), 24 configs (`fabtest run --suite default`):

| fabric | layout | faults | coverage | Icarus replay | bit_gen |
|---|---|---|---|---|---|
| F1 | 4 x 5: W_IO, 3 x LUT4AB, RAM_IO, 1 BlockRAM | 71,402 | **85.8%** | 24 / 24 | byte-equal |
| F2 | 6 x 5: RegFile next to W_IO and at the east edge, no DSP | 159,906 | **70.9%** | 24 / 24 | byte-equal |
| F3 | 5 x 5, MultiplexerStyle generic, a CUST column with a BEL no plugin knows | 114,816 | **77.1%** | 24 / 24 | byte-equal |
| F4 | 7 x 8, uneven: RegFile / LUT4AB column, RegFile column, DSP, RAM_IO, 3 BlockRAMs | 328,652 | **73.1% +- 0.6** (20k sample) | 24 / 24 | byte-equal |

What the demo did not show: a fabric two rows high bounces its 4-wires between the N and
S term tiles into undriven rings (F1: 336 nets), and FABulous leaves the west-going inputs
of the east column open (F2: 192); both are X in the model and the netlist.

F3 and F4 found three more:

- **Generic muxes.** With `MultiplexerStyle generic` FABulous writes each switch-matrix mux
  as `assign #80 X = X_input[ConfigBits[hi:lo]]` instead of a `cus_mux` cell. The parser
  reads both forms (same select code), so LUT4AB and W_IO keep their full models.
- **Delays without a timescale.** The `#80` is `GenerateDelayInSwitchMatrix`. FABulous RTL
  has no `` `timescale`` and BEL files end with `` `resetall``, so Icarus took 80 s per mux
  and every pad read X. The export now puts `timescale.v` (1 ps) before each RTL file.
- **Unconnected inputs.** eFPGA.v leaves the `Ci` of a LUT4AB under a RegFile unconnected
  (RegFile has no `Co`). Such inputs are now undriven (X), like the open wires at the edge.

An unknown BEL comes out opaque: `fabtest info --bels` and the report name it
("opaque, outputs X and faults not tested: ALU4 in CUST x3"), and its tile's switch matrix
is still tested (CUST: 53.6% of the 23,082 non-ALU4 faults, none of the ALU4 faults).
On F4 the RegFiles at two tiles from the nearest LUT take their constants at the
`const_radius` limit (in the 4-config gate run, 8 to 15 of about 450 backbone links and
pins per config fail, as on F2), and RegFile is the lowest part (57.6%); LUT4AB 80.2%, DSP 71.6 / 79.7%, RAM_IO 84.3%, BlockRAM 77.9%.

### Untestable faults: a SAT proof per fault

`fabtest untestable` takes the faults a run leaves undetected and proves, one by one with
SAT (pysat, CaDiCaL), which of them no test can detect. The problem is the fault's tile
(both tiles for a wire between two) with everything that enters it free: the neighbours'
wires, the pads, every config bit and every flop and latch state. The fault is testable in
the tile when some assignment makes the fault-free and the faulty tile differ at a tile
output or in the next state of a flop or latch. No such assignment (UNSAT) is a proof for
the fabric: a real test has a first clock where the two differ while their stored state is
still equal. Faults whose cone reaches a flop clock are not decided. A fault comes out:

- **dead**: its cone reaches no tile output and no flop or latch (a wire that leaves the
  fabric, a mux nobody reads)
- **redundant**: observable, but SAT finds no difference (logic on a constant input)

The report then gives the coverage of the testable faults too.

| run | undetected | untestable (dead / redundant) | coverage | of testable |
|---|---|---|---|---|
| demo, 96 configs, 50k sample (Yosys 0.71 netlist) | 5,488 | 16 (6 / 10); 6 not decided | 89.02% | 89.05% |
| F1, 24 configs, every fault | 10,110 | 84 (44 / 40) | 85.84% | 85.94% |
| F2, 24 configs, every fault | 46,511 | 7,856 (7,856 / 0) | 70.91% | **74.58%** |
| F3, 24 configs, every fault | 26,304 | 5,808 (5,592 / 216) | 77.09% | **81.20%** |
| F4, 24 configs, 20k sample | 5,374 | 19 (11 / 8) | 73.13% | 73.20% |

On the demo, 5,466 of the 5,488 undetected faults are testable in their tile, 16 are
untestable and 6 are not decided (their cone reaches a flop clock). So what is left is
test generation, not untestable logic. The small
fabrics lose whole wires: F2 (6 wide) and F3 (5 wide) are narrower than the 6-tile E6 / W6
wires, which leave the fabric unread. F3's 216 redundant faults are the GND0 mux input of
`MultiplexerStyle generic` (synthesised as an AND with a constant 0; the custom style has
none). Soundness (earlier run): none of the 44,540 detected faults of the demo sample comes out
untestable, and the fabrics gate checks 200 detected faults per fabric. 37 ms per fault on
the demo (one thread).

What stops the testable ones (demo, earlier run: 600 of the 5,442, `Checker.check(..., fixes=)`: the
tile's config latches fixed to each of the 96 loaded configs, its data and flop states free):

| | faults |
|---|---|
| some loaded config detects it in the tile (median 10 configs) | 320 (53%) |
| no loaded config can (a config gap) | 189 (32%) |
| no config found, some solves over the conflict budget (DSP, RegFile) | 88 (15%) |
| untestable | 3 |

506 of the 600 are switch-matrix mux cells. For the 320, few tile inputs are X in the
fault-free run (median 2%); with the X inputs free and every other input at its final
value, 135 are detected, 163 need other input values. So the gap is spread over config
choice (a select code or LUT table no config uses), input combinations (the other mux
inputs at the opposite value) and observation, not X alone.

Targeted configs from the SAT models (an experiment; its code is not kept). Every mux of the flat netlist as a gate tree (the forward
cone of its select bits, so no names are needed: F1 1,348 of 1,348 muxes); per undetected
fault in a tree a SAT model on its trees, lifted to the values that force a difference at
one mux output: one select code and one or two inputs for most faults. The router places
these (codes, inputs from pads, the output to observation) in extra configs. On F1 they do
not beat the router's own extra configs:

| F1, 8 more configs on top of | ordinary configs | targeted configs |
|---|---|---|
| 24 configs (85.84%; 4 more) | +1,327 faults | +1,340 (targets after the cover), +690 (before) |
| 80 configs (91.22%, router near saturation) | +428 | +405 (after), +332 (before) |

The targeted configs detect nearly the same faults (378 of their 405 also by the ordinary
ones): a target fails where the router fails, mostly because the mux it needs is taken by
another route in that config. What helps is more configs: F1 85.84% with 24, 91.22% with 80.

More configs and `fabtest compact` (demo, the 96-config set extended by the router to 256:
93.50% of the router items instead of 83.5%, 50 min; fault simulation of a 5,000-fault
sample, every config, 3.5 h on 4 threads):

| configs (router order) | 24 | 48 | 96 | 160 | 256 |
|---|---|---|---|---|---|
| coverage (+- 0.7 to 1.2) | 77.98% | 84.14% | 89.00% | 91.72% | **92.78%** |

`fabtest compact` (greedy set cover of the fault simulation) keeps 92 configs for 99% of
that (91.88%), 48 for 95%, 134 for all of it. On faults it did not choose from (cover on
half the sample, measured on the other half) the gain over the router's own first k is
small: 79.40 vs 77.76% at 24, 89.92 vs 89.08% at 92, 90.40 vs 91.00% at 134. The router
order is close to greedy already, and 2,500 faults are too few to choose among 256
configs.

`fabtest compact --by items` covers the router items every config shows in the whole
fabric instead (fabric.grade, 667,058 items, 4 min for 256 configs, cached in
`items.npz`), so the fault sample only measures. Same demo run, 5,000 faults:

| configs | 24 | 48 | 96 | 128 | 160 |
|---|---|---|---|---|---|
| router order (the first k) | 77.98% | 84.14% | 89.00% | 90.94% | 91.72% |
| item cover | 77.80% | 84.76% | 89.64% | 91.18% | 91.92% |
| fault cover, in-sample (optimistic) | 81.26% | 88.24% | 92.02% | 92.66% | 92.78% |

Every one of the 256 configs adds items (none can go for free); 99% of the items take
201 configs (92.54%). The router already builds each config for the most new items, so
its own order is nearly the item cover: the choice is the number of configs (tester time)
against coverage, along the first row.

RegFile (the weakest part: 76.80% of its 556 sampled faults with the 96 configs, 80.9%
with 256). Its tiles show 68.9% of their router items after 256 configs (the fabric
93.5%): the router has no edge through a RegFile and the backbone pins it the same way in
every config, so the jump muxes into its pins stay open (61 of its 106 undetected faults
there). Tried on the demo, 96 configs, the RegFile faults and 1,000 others of the sample:

| change | RegFile | other | why |
|---|---|---|---|
| none (the 96 configs) | 76.80% | 90.60% | |
| router edges through the RegFile data path | 68.35% | 70.20% | reads are X until written: X into the observation chains |
| pin routes prefer open codes, B address apart from A | 72.12% | 83.40% | longer pin routes block each other: 23.6 pin failures per config (3.3) |
| + read data routed into observation | stopped | | a read is combinational in its address: loops through the address helpers |
| 16 RegFile configs (B address apart, two read bits per RegFile point to point to its row's pads; code not kept) on top of the 96 | 79.14% | (all) 89.44% | vs 16 ordinary configs: 78.60%, 90.28% |

The dedicated configs add 13 of the 129 RegFile faults the 96 miss (ordinary configs 10)
and less elsewhere (their routes take the row's pads and muxes): not worth it as they are.

Vectors instead of configs (`fabtest.toml [vectors] random`, default 54: with the 10
counting vectors 64 per config). The demo's 96 configs with 246 random vectors (256 per
config), the sample's 550 faults the 64-vector set misses: 48 detected, 89.00% -> 89.96%,
about what 16 more configs give (90.28%). On the tester a config costs 60 us to load
(parallel, 50 MHz; about 1 s through the UART) and 1.3 us for 64 vectors: the 192 more
vectors in all 96 configs take 0.37 ms, 16 more configs 0.98 ms.

SAT-generated vectors for a config (the configured fabric from the state its vectors leave,
pads free, faults the tile check says the config sets up) found none for 68 (fault, config)
pairs over 2 clocks: the observation chains are registered, so a fault's effect takes up
to a chain's length (about 60 LUTs) in clocks to reach a pad, far more than an unrolled SAT
of the configured fabric (530k gates per clock) can reach.

Clock network. FABulous clocks every tile from `UserCLK` through a `clk_buf` per tile
(UserCLK -> UserCLKo, a chain along each column); synthesis turned each into a wire, so the
whole clock tree was one net and its segments were not in the fault list. They are now
kept as buffer cells (`FABTEST_KEEP_CLKBUF=0`: the old netlist; demo: 158 more cells, 296
more faults, 278 on the 139 clock nets), the report counts them as "clock network" and
`fabtest clockcheck` simulates each with random power-up states (below; 210 s on the demo,
its line then in the report). A stuck segment freezes the flops after it at their power-up values:
every one is X-only (P) in the fault simulation. With 4 random power-up states of the
3,612 fabric flops and 8 configs of the demo set:

| clock segments of | detected in every state |
|---|---|
| LUT4AB columns | 156 of 156 |
| RegFile column | 26 of 26 |
| term tiles, root `UserCLK` | 16 of 16 |
| W_IO column | 0 of 26 |
| RAM_IO column (+ term tile) | 0 of 28 |
| DSP column (+ term tile) | 0 of 26 |

198 of 278 (71%). The three columns where no test uses a flop: the W_IO registered pad
paths, the RAM_IO pass registers and the MULADD registers are never configured (the model
treats them as unknown sources), so nothing after a broken clock there changes.

Bidirectional cover (the default; `FABTEST_BIDIR=0`: the router before it, `1`: step 1
only). The router used to route only the output side of a tested mux code (forward from
the mux to an observed net); its input carried whatever drove it. Now:

1. the selected input is routed backward (Dijkstra through free muxes, wires and LUTs)
   to a net that carries a pad-driven signal, one that none of the mux's other inputs
   carries;
2. one other input of the mux (rotated per config) is routed back to a different signal,
   so the two take opposite values in some vector (the select and decode faults of a mux
   need that);
3. backward paths prefer open codes, as the forward routes do.

Demo, 96 configs each, the same 2,000 faults of the sample (current netlist):

| | before | step 1 | steps 1 to 3 |
|---|---|---|---|
| all faults | 87.90% | 88.80% | **89.10%** |
| LUT4AB | 90.87% | 91.59% | 92.03% |
| RegFile | 76.72% | 77.59% | 78.02% |
| router items | 83.5% | 86.9% | 88.4% |
| routing time | 31 min | 33 min | 34 min |

Steps 1 to 3 against before: 55 faults gained, 31 lost (+1.2 points), at no extra
configs or tester time.

### Pinning: shared constants, rotated per config

RegFiles and BlockRAMs are pinned to defined values with constant LUTs (no switch-matrix
mux that reaches them has a GND / VCC input) and helper LUTs (NOT pad). Taking them always
from the same neighbour tiles and letters kept the same LUTs out of the observation chains
and the same pinned routes in every config (F2: the same 22 LUTs in all 24 configs, 49.1%).
Now a pin takes a constant of the config within two tiles before a new one is made, and
the helper tiles, letters and pads rotate with the config: over 24 configs the borrowed
LUTs cover 64 of F2's 64 LUTs and 610 of the demo's 672.

| | before | after |
|---|---|---|
| F2, all faults | 49.1% | **70.9%** |
| F1, all faults | 82.4% | **85.8%** |
| demo, 24 configs, random 10k of the 50k sample | 70.7% (v3) | **77.7%** |
| demo, 96 configs, the 50k sample, every fault in every config | 86.0% (four sets) | **89.1%** |

On the demo every part gains (LUT4AB 73.1 -> 81.0, DSP 61.4 -> 66.5, RAM_IO 77.2 -> 82.5,
BlockRAM 54.4 -> 84.2); routing covers 423,530 items in 24 configs (v3: 389,647).
With 96 configs RegFile goes from 73.6% to 79.3% and the BlockRAM wrapper from 52.6% to
81.6%; 39 configs reach 95% of the union, 70 reach 99%, as before.

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

- FABulous, with the fabric generated (`FABulous -p <project> run "load_fabric; run_fab"`):
  fabtest reads the project and its generated RTL, it does not run FABulous
- Yosys (`pip install yowasp-yosys`), Icarus Verilog 12, Python 3.11+ with numpy and
  pybind11, a C / C++ compiler (the fault simulation kernel and the native router are
  built on first use)
- for `fabtest untestable`: python-sat (`pip install python-sat`, or `pip install .[sat]`)

`scripts/env.sh` sets the tool paths (default: a FABulous venv in `../ext`) and puts `src/`
on `PYTHONPATH`; `scripts/fabtest` runs the command line from the checkout. Packaging (Nix
is the likely route) will live in `package/`.

```bash
. scripts/env.sh
```

## Running

One command, from a FABulous project with the generated fabric RTL to the test program:

```bash
scripts/fabtest run /path/to/fabulous_project --suite demo     # -> results/<fabric>/demo/
```

It does init, synth, route (the suite's passes), faultsim, report and export (and with
`--cfgtest` the configuration-logic test), each step
skipped when its inputs did not change: a second run, or one after a crash, only does what
is left (faultsim resumes per chunk; interrupted routing starts again). `--configs N` cuts
the suite to N configs, `--sample N` sets the sampled faults (0: every fault), `--run NAME`
the run directory.

Nothing a step made is deleted on its own:

- A fault simulation whose inputs changed (netlist, configs, kernel, `[vectors]`) is moved
  to `faultsim/stale/<time>/`.
- A run that holds another fault sample (another `--sample` or seed) is refused: use
  another `--run`, or `--force` to move the old sample aside.
- `fabtest run` on a run whose configs it did not route (`fabtest route`, `--configs-from`,
  `compact`) is refused; `--force` keeps them as `configs.<time>.json`.
- `compact --to` never replaces another run.

Every step writes its files atomically, so an interrupted step is redone, never trusted.
The work dir argument of the steps can also be the FABulous project itself.
Suites (`src/fabtest/suites/`): `default` (24 configs) and `demo` (96 configs, the 89.1%
set); a suite file lists routing passes, each on top of the passes before it:

```toml
name = "mine"
[[pass]]
configs = 24
chains = 2
```

An optional `fabtest.toml` in the FABulous project sets the fabric's knobs (unknown keys
are refused; every key optional):

```toml
[route]
max_pop = 3000          # Dijkstra exploration per route (default: demo value, scaled by tile count)
max_reach = 10000       # incremental loop check
loop_max_moves = 20000  # mux moves to break the loops of a config
bram_max_pop = 60000    # routes from the pads to a BlockRAM column
const_radius = 2        # a pinned pin takes an existing constant this many tiles away
[faultsim]
sample = 2000
seed = 1
chunk = 2000
jobs = 4
[bels]
opaque = []             # BEL or macro modules modelled as X sources (not tested)
[vectors]
random = 54             # random vectors per config (cheap: ~1 clock each)
```

The steps one by one:

```bash
scripts/fabtest init /path/to/fabulous_project          # copy + parse -> build/fabtest/<fabric>
scripts/fabtest info build/fabtest/<fabric> --bels      # tiles, BELs and the plugin of each
scripts/fabtest synth build/fabtest/<fabric>            # gate-level netlist and fault list
scripts/fabtest route build/fabtest/<fabric> --run r1 --configs 24
scripts/fabtest faultsim build/fabtest/<fabric> --run r1 --sample 2000
scripts/fabtest powerup build/fabtest/<fabric> --run r1 --sample 400
scripts/fabtest untestable build/fabtest/<fabric> --run r1   # SAT: undetected faults no test detects
scripts/fabtest compact build/fabtest/<fabric> --run r1 --target 99 --by items   # fewest configs
scripts/fabtest clockcheck build/fabtest/<fabric> --run r1   # clock network, random power-up states
scripts/fabtest export build/fabtest/<fabric> --run r1  # bitstreams, vectors, testbench, plan
results/<fabric>/r1/export/tb/run_icarus.sh             # replay on the generated RTL
scripts/fabtest cfgtest build/fabtest/<fabric> --run r1 # configuration logic, every fault
scripts/fabtest export build/fabtest/<fabric> --run r1 --port-test   # + the config-port test
scripts/fabtest bitgen-check build/fabtest/<fabric> --run r1 --only 0,1,2,3
```

`scripts/run_demo.sh` does init, synth, faultsim, export and the Icarus replay on the demo
fabric with a stored config set. Every run writes its own directory `results/<fabric>/<run>/`
(`configs.json`, `faultsim/`, `report.txt`, `export/`); `faultsim` and `export` take
`--configs-from JSON` to start a run from a stored config set. `results/` is local.

Tests (the demo fabric; R1 needs the stored 50k-fault run in `results/`, local, and
reports SKIP without it):

```bash
python3 tests/run_all.py             # quick, about 10 min
python3 tests/run_all.py --full      # also R1, R2, all 96 configs, Icarus replay, F1 to F4, fabtest run
```

## Layout

| path | contents |
|---|---|
| `src/fabtest/` | the tool: `cli.py`, `project.py` (work dir), `spec.py` + `fabulous_loader.py` (the FABulous project), `config.py` (`fabtest.toml`, suites), `suites/`, `bels/` (BEL plugins), `pipeline.py` (run, route, faultsim, report, untestable, compact, clockcheck), `cfgtest/` (configuration-logic test) |
| `src/fabtest/model/` | fabric models: `generic.py` (GenericFabric), `fullfabric.py`, `fabric.py`, tile models (`sm_model.py`, `wio_model.py`, `tile_model.py`, `tile_parse.py`), LUT bits (`configs.py`, `sm_configs.py`) |
| `src/fabtest/patterns/` | `config/`: test config router (`fab_router.py`); `faults/`: fault list generator |
| `src/fabtest/sim/` | fault simulator: `fastsim/` (C kernel and driver, power-up check), `stimuli.py`, `conesplit.py`, `graph.py`; `untestable.py` (SAT proof per fault) |
| `src/fabtest/synth/` | netlist flow (yosys templates, techmaps; `clkbuf_keep.v`: the tiles' clock buffers as cells) |
| `src/fabtest/export/` | test program: `.bin` / `.hex` / `.vec`, plan, manifest, Icarus testbench |
| `src/fabtest/native/` | C++ (pybind11) router search and loop check, built on first use into `build/native` (`FABTEST_NATIVE=0`: the Python path, same results) |
| `rtl/` | input RTL: LUT4AB, W_IO and term tile copies, the demo fabric copy, `bram/sram_model.v` |
| `tb/` | testbench templates (`fabtest_tb.v.in`, `fabtest_ports_tb.v.in`, `sram_sim.v`) |
| `tests/` | `run_all.py`; `model/`, `patterns/` (R0 / R1 / R2), `export/`, `cli/` (commands, `fabtest run`), `cfgtest/`, `sat/` (the untestable check), `fabrics/` (F1 to F4: `make.py` builds them with FABulous), `rtl/` (Icarus replay), `golden/` (reference hashes and the four config sets) |
| `scripts/` | `env.sh`, `fabtest`, `run_demo.sh`, `compare_with_stored.py` (a config set against the stored 96-config demo run in `results/`, local) |
| `package/` | packaging (placeholder) |

The results above up to the full fabric were produced with research scripts of the earlier
phases (the script names in those sections); they are retired from the repository.

## Not covered yet

the SRAM macro itself (memory BIST, e.g. a soft MBIST configured into the fabric), the
faults that only reach a pad as X (power-up values, words not yet written), the registered
modes of W_IO, RAM_IO and DSP (and so the clock segments of their columns), several clock
domains, the configuration logic together with the fabric in one simulation (it is tested
against a model of the config memory), bridging faults and timing.

## License

See `LICENSE`.
