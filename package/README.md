# Packaging

Placeholder. fabtest will be packaged here (a Nix flake is the likely route), bundling
FABulous, yosys, Icarus Verilog and a C compiler for the fault simulation kernel.

Until then, run from a checkout:

    pip install -e .            # or: export PYTHONPATH=$PWD/src
    fabtest --help
