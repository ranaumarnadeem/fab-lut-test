# Tool and PDK locations for this checkout. Source before running anything.
# Defaults assume ../ext next to this repo (see NOTES.md); override as needed.
_FABLUT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_FABLUT_EXT="${FABLUT_EXT:-$_FABLUT_ROOT/../ext}"
export FABLUT_VENV="${FABLUT_VENV:-$_FABLUT_EXT/venv}"
export SKY130_LIB="${SKY130_LIB:-$_FABLUT_EXT/pdk/orfs/flow/platforms/sky130hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib}"
export SKY130_SC_HD="${SKY130_SC_HD:-$_FABLUT_EXT/pdk/sky130_fd_sc_hd}"
export YOSYS="${YOSYS:-yowasp-yosys}"

if [ -f "$FABLUT_VENV/bin/activate" ]; then
    . "$FABLUT_VENV/bin/activate"
fi
