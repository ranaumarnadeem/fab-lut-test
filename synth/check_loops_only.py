#!/usr/bin/env python3
"""Fail unless every problem in a yosys `check` report is a logic loop.

A fabric tile has structural combinational loops through its switch matrix
and BELs; every other problem (undriven, multiple drivers, ...) is an error.
"""
import re
import sys

rpt = open(sys.argv[1]).read()
warnings = [ln for ln in rpt.splitlines() if ln.startswith("Warning:")]
other = [ln for ln in warnings if "logic loop" not in ln]
m = re.search(r"Found and reported (\d+) problems", rpt)
if not m:
    sys.exit(f"{sys.argv[1]}: no check summary")
if other:
    sys.exit(f"{sys.argv[1]}: problems other than logic loops:\n" + "\n".join(other[:10]))
print(f"{sys.argv[1]}: {m.group(1)} problems, all logic loops")
