# Tool locations for this checkout. Source before running anything: . scripts/env.sh
# Defaults assume ../ext next to this repo (FABulous venv with yowasp-yosys); override as needed.
_FABTEST_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
_FABTEST_EXT="${FABLUT_EXT:-$_FABTEST_ROOT/../ext}"
export FABLUT_VENV="${FABLUT_VENV:-$_FABTEST_EXT/venv}"
export YOSYS="${YOSYS:-yowasp-yosys}"
export PYTHONPATH="$_FABTEST_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

if [ -f "$FABLUT_VENV/bin/activate" ]; then
    . "$FABLUT_VENV/bin/activate"
fi
