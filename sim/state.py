"""Storage elements of a netlist: discovery, models, monitors, deposits.

Shared by the cell runner (sim/run.py) and the tile runner (sim/run_tile.py).

- `storage_elements(json)`: every latch / flop cell with its enable and
  clock nets and the hierarchical path of its state reg.
- `model_sources(...)`: copies of the cell model files with the original
  latch/flop definitions removed, plus sim/seq_models.v (reg-based,
  X-pessimistic, UDP-faithful). Used for every simulation.
- `monitor_vh(...)`: per-element "possibly written" flags. An element is
  written when its enable is not 0 (latch), or when its clock has a possible
  rising edge while its enable is not 0 (flop), at any time from t = 2 ns.
  Events at 1 ns are the fault being applied (e.g. the 0 -> 1 of a
  stuck-at-1 clock) and are not writes: the real fault was there from power
  up. A latch whose enable is not 0 at 2 ns (stuck-at-1 strobe) is written.
- `deposit_vh(...)`: that 2 ns check, then `+PU<i>=<0|1>` plusargs deposit a
  value into element i with force/release.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "faults"))
import gen_faults  # noqa: E402

SEQ_MODELS = ROOT / "sim" / "seq_models.v"
MAX_SEGMENTS = 16   # sequence mode: state dumps per config segment (+SEGEND<j>)

# cell type -> (kind, enable pin, clock pin or None, state path below the cell)
STORAGE_TYPES = {
    "sky130_fd_sc_hd__dlxtp_1": ("latch", "GATE", None, "base.dlatch0.Q"),
    "sky130_fd_sc_hd__edfxtp_1": ("flop", "DE", "CLK", "base.dff0.Q"),
    "$_DLATCH_P_": ("latch", "E", None, "Q"),
    "$_DFFE_PP_": ("flop", "E", "C", "Q"),
}
# Anything that looks sequential but is not in STORAGE_TYPES is an error.
SEQ_TYPE_RE = re.compile(r"(DLATCH|DFF|SDFF|ALDFF|SR_|dlx|dfx|dfr|dfs|dfb|edf|sdf|dlr|dll|dlclk)")

# Original definitions replaced by sim/seq_models.v
REPLACED_UDPS = ["sky130_fd_sc_hd__udp_dlatch$P", "sky130_fd_sc_hd__udp_dff$P"]
REPLACED_SIMCELLS = ["$_DLATCH_P_", "$_DFFE_PP_"]


@dataclass(frozen=True)
class Element:
    index: int
    cell: str
    ctype: str
    kind: str
    en_ref: str
    clk_ref: str | None
    state_ref: str
    label: str  # net name of the element's output, for reports


def storage_elements(netlist_json: Path) -> list[Element]:
    _, mod = gen_faults.load_top(netlist_json, None)
    bits = gen_faults.analyse(mod)

    def ref(b) -> str:
        if not isinstance(b, int):
            raise SystemExit(f"constant on a storage control pin: {b}")
        return bits[b]["names"][0][1]

    out: list[Element] = []
    for cname, cell in sorted(mod["cells"].items()):
        t = cell["type"]
        if t not in STORAGE_TYPES:
            if SEQ_TYPE_RE.search(t):
                raise SystemExit(f"cell {cname}: sequential type {t} has no model in state.py")
            continue
        kind, en, clk, spath = STORAGE_TYPES[t]
        conns = cell["connections"]
        qpin = "Q"
        label = bits[conns[qpin][0]]["names"][0][0]
        out.append(Element(
            index=len(out), cell=cname, ctype=t, kind=kind,
            en_ref=ref(conns[en][0]),
            clk_ref=ref(conns[clk][0]) if clk else None,
            state_ref=f"{gen_faults.vref(cname)}.{spath}",
            label=label,
        ))
    return out


def _strip_blocks(text: str, kw_open: str, kw_close: str, names: list[str]) -> str:
    for name in names:
        pat = re.compile(rf"^{kw_open}\s+{re.escape(name)}\s*\(.*?^{kw_close}\b", re.M | re.S)
        text, n = pat.subn(f"// {name}: replaced by sim/seq_models.v", text)
        if n > 1:
            raise SystemExit(f"{name} defined {n} times")
    return text


def model_sources(sources: list[Path], out_dir: Path) -> list[Path]:
    """Patch copies of the model files; netlist files are passed through."""
    out_dir.mkdir(parents=True, exist_ok=True)
    result = []
    for src in sources:
        if src.name == "sky130_cells.v":
            text = _strip_blocks(src.read_text(), "primitive", "endprimitive", REPLACED_UDPS)
        elif src.name == "simcells.v":
            esc = ["\\" + n for n in REPLACED_SIMCELLS]
            text = _strip_blocks(src.read_text(), "module", "endmodule", esc)
        else:
            result.append(src)
            continue
        dst = out_dir / src.name
        dst.write_text(text)
        result.append(dst)
    result.append(SEQ_MODELS)
    return result


def monitor_vh(elems: list[Element]) -> str:
    """Module-level code, from 2 ns:

    `se_wr[i]`: element i possibly written (enable not 0 / a possible rising
    clock edge with enable not 0).
    `se_dw[i]`: element i definitely written (enable exactly 1 / a 0 -> 1
    clock edge with enable exactly 1).
    "Never possibly written" is the U of the full enumerator (Part A);
    "never definitely written" is the U of the cone-split enumerator. The
    second is a superset and still sound: no element is written before 2 ns,
    so any value deposited there is a power-up state.
    """
    n = max(len(elems), 1)
    lines = ["    integer " + ", ".join(f"se_e{j}" for j in range(MAX_SEGMENTS)) + ";",
             "    initial begin"]
    lines += [f"        if (!$value$plusargs(\"SEGEND{j}=%d\", se_e{j})) se_e{j} = -1;" for j in range(MAX_SEGMENTS)]
    lines += ["    end",
              f"    reg [{n - 1}:0] se_wr, se_dw, se_ck;",
             "    initial begin se_wr = 0; se_dw = 0; se_ck = {" + str(n) + "{1'bx}}; end"]
    for e in elems:
        en = f"dut.{e.en_ref}"
        i = e.index
        if e.kind == "latch":
            lines.append(f"    always @({en}) if ($time >= 2) begin "
                         f"if ({en} !== 1'b0) se_wr[{i}] = 1'b1; "
                         f"if ({en} === 1'b1) se_dw[{i}] = 1'b1; end")
        else:
            clk = f"dut.{e.clk_ref}"
            lines.append(
                f"    always @({clk}) begin if ($time >= 2) begin "
                f"if ({clk} !== 1'b0 && {en} !== 1'b0) se_wr[{i}] = 1'b1; "
                f"if (se_ck[{i}] === 1'b0 && {clk} === 1'b1 && {en} === 1'b1) se_dw[{i}] = 1'b1; end "
                f"se_ck[{i}] = {clk}; end"
            )
    return "\n".join(lines) + "\n"


def deposit_vh(elems: list[Element]) -> str:
    lines = []
    for e in elems:
        if e.kind == "latch":
            lines.append(f"        if (dut.{e.en_ref} !== 1'b0) se_wr[{e.index}] = 1'b1;")
            lines.append(f"        if (dut.{e.en_ref} === 1'b1) se_dw[{e.index}] = 1'b1;")
    for e in elems:
        s = f"dut.{e.state_ref}"
        lines.append(
            f"        if ($value$plusargs(\"PU{e.index}=%d\", pu_v)) begin "
            f"if (pu_v) force {s} = 1'b1; else force {s} = 1'b0; release {s}; end"
        )
    return "\n".join(lines) + "\n"


def report_vh(elems: list[Element], dump: bool = True) -> str:
    """End of run: never-written elements, and with +DUMPSTATE every state
    (`dump` False for the original UDP models, whose state is unreachable)."""
    n = len(elems)
    lines = [f"        for (k = 0; k < {n}; k = k + 1) begin "
             "if (!se_wr[k]) $display(\"NW %0d\", k); "
             "if (!se_dw[k]) $display(\"ND %0d\", k); end"]
    if elems and dump:
        lines.append("        if ($test$plusargs(\"DUMPSTATE\")) begin")
        lines += [f"            $display(\"ST {e.index} %b\", dut.{e.state_ref});" for e in elems]
        lines.append("        end")
    return "\n".join(lines) + "\n"


def segdump_vh(elems: list[Element], dump: bool = True) -> str:
    """In the step loop: at step +SEGEND<j>=<k> (j < MAX_SEGMENTS), print every element's
    state as "SG <j> <i> <v>" (sequence mode: state at the end of segment j)."""
    if not (elems and dump):
        return "\n"
    lines = []
    for j in range(MAX_SEGMENTS):
        lines.append(f"            if (k == se_e{j}) begin")
        lines += [f"                $display(\"SG {j} {e.index} %b\", dut.{e.state_ref});" for e in elems]
        lines.append("            end")
    return "\n".join(lines) + "\n"
