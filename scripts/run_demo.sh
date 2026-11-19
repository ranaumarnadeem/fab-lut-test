#!/bin/bash
# End to end on the FABulous demo fabric with a stored config set (no routing):
#   scripts/run_demo.sh [FABulous project] [config set] [run name] [sample]
# init + synth (build/fabtest/<fabric>), then results/<fabric>/<run>/: faultsim, report,
# export, Icarus replay of every exported config.
set -e
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJ="${1:-${FABTEST_DEMO:-$ROOT/../ext/demo}}"
CFG="${2:-$ROOT/tests/golden/configs/v3.json}"
RUN="${3:-demo_v3}"
SAMPLE="${4:-2000}"
FT="$ROOT/scripts/fabtest"
WORK=$("$FT" init "$PROJ" | sed -n 's/^fabtest init: .* -> //p; s/^fabtest init: \(.*\) is up to date$/\1/p')
"$FT" synth "$WORK"
"$FT" faultsim "$WORK" --run "$RUN" --configs-from "$CFG" --sample "$SAMPLE"
"$FT" export "$WORK" --run "$RUN"
"$ROOT/results/$(basename "$WORK")/$RUN/export/tb/run_icarus.sh"
