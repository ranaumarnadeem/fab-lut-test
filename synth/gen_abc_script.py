#!/usr/bin/env python3
"""Write the ABC script LibreLane would generate for a SYNTH_STRATEGY.

Uses LibreLane's own ABCScriptCreator so the script matches the FABulous tile
flow. Config values are LibreLane defaults except CLOCK_PERIOD, which comes
from the demo project's Tile/include/gds_config.yaml (20.0 ns).
"""
import argparse
import shutil
import sys
from pathlib import Path

import librelane

ap = argparse.ArgumentParser()
ap.add_argument("--strategy", default="AREA 2")
ap.add_argument("--clock-period", type=float, default=20.0)
ap.add_argument("--out", type=Path, required=True)
args = ap.parse_args()

sys.path.insert(0, str(Path(librelane.__file__).parent / "scripts" / "pyosys"))
from construct_abc_script import ABCScriptCreator  # noqa: E402

config = {
    "CLOCK_PERIOD": args.clock_period,
    "SYNTH_ABC_LEGACY_REFACTOR": False,
    "SYNTH_ABC_LEGACY_REWRITE": False,
    "SYNTH_ABC_USE_MFS3": False,
    "SYNTH_ABC_AREA_USE_NF": False,
    "MAX_FANOUT_CONSTRAINT": 10,
    "MAX_TRANSITION_CONSTRAINT": None,
    "SYNTH_ABC_BUFFERING": False,
    "SYNTH_SIZING": False,
}
out_dir = args.out.parent
generated = Path(ABCScriptCreator(config).generate_abc_script(str(out_dir), args.strategy))
if generated.resolve() != args.out.resolve():
    shutil.move(generated, args.out)
