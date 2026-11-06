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

### Findings about FABulous

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

Results go to `reports/`. Fault simulation of the branch-fault tile netlist
(`tile_generic_br`) takes about 4.5 hours on 4 cores; runs are checkpointed and resume
after an interruption.

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
| `configs/` | config models: cell (`configs.py`), tile LUT configs (`tile_configs.py`), test routing, switch-matrix model (`sm_model.py`) and baseline (`sm_configs.py`) |
| `sim/` | runners (`run.py`, `run_tile.py`, `run_sm.py`), exhaustive power-up (`run_conesplit.py`), loop check, analysis |

## Not covered yet

Configs that select and observe each switch-matrix path in the same config, registered
and MUX8LUT configs for the muxes a combinational test cannot observe, multi-tile and
full-fabric tests, an exact minimum config set, bridging faults and timing.

## License

See `LICENSE`.
