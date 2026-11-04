#!/usr/bin/env python3
"""Configs BC1, D0C, D1 applied to all 8 LUTs of one LUT4AB tile.

The cell config functions of configs/configs.py are reused through
`TileSeq`, which has the same step / clock / write_config interface but
drives tile ports through the fixed test routing (configs/tile_routing.json):

- write_config(bits) writes all 20 frames: the 19-bit cell config in every
  LUT (same config for LA..LH), the test-routing switch-matrix bits, all
  other bits 0.
- step(i=v) gives v to every LUT unless v is a list of 8 (LA..LH); ci goes
  to the tile Ci[0] (carry into LA; LB..LH get the previous LUT's Co).
- sr / en drive the tile inputs routed to J_SR / J_EN, shared by all LUTs.

BC1 (I0 = Ci) needs every LUT to see all 32 (I, Ci) combinations. With one
vector for all LUTs, LUT k > A gets Ci = I1 whenever I1 = I2, so 8 combos
would be unreachable. `bc_schedule` gives each LUT its own order of the 32
combos, chosen so that its Ci (the previous LUT's carry) matches.

Stimulus word (32 hex digits per step):
  [31:0] FrameData  [51:32] FrameStrobe  [83:52] I of LA..LH (4 bits each,
  LA lowest)  [84] Ci  [85] SR  [86] EN  [87] UserCLK  [88] sample
Samples: O of LA..LH (at their routed outputs), then Co.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "configs"))
sys.path.insert(0, str(ROOT / "rtl"))
import configs as cell  # noqa: E402
import tile_parse as tp  # noqa: E402

LUTS = "ABCDEFGH"
# FrameData after configuration: "last" = keeps the last frame word (FABulous
# Frame_Data_Reg, see NOTES.md); "zero" = the old testbench, for tracing only.
IDLE_FD = "last"
TILE = tp.parse_tile(ROOT / "rtl", "LUT4AB")
ROUTING = json.loads((ROOT / "configs" / "tile_routing.json").read_text())
N_FRAMES = 1 + max(f for f, _ in TILE.configmem.values())
LUT_BITS = {x: list(range(*TILE.bels[f"Inst_L{x}_LUT4c_frame_config_dffesr"])) +
            [TILE.bels[f"Inst_L{x}_LUT4c_frame_config_dffesr"][1]] for x in LUTS}


# Frame write order (Phase 8, 0a): see configs.py. "rotated" ends each config
# on LAST_FRAME[name] (different per config); "natural" is 0..19.
FRAME_ORDER_MODE = "rotated"
LAST_FRAME = {"BC1": 19, "D0C": 12, "D1": 6}


def frame_order(name: str) -> list[int]:
    frames = list(range(N_FRAMES))
    if FRAME_ORDER_MODE == "natural" or name not in LAST_FRAME:
        return frames
    return cell.rotated(frames, LAST_FRAME[name])


def tile_config(cell_bits: int) -> dict[int, int]:
    """All 616 tile config bits for one cell config in every LUT."""
    v = {b: 0 for b in TILE.configmem}
    for x in LUTS:
        lo = LUT_BITS[x][0]
        for k in range(cell.N_CONFIG_BITS):
            v[lo + k] = (cell_bits >> k) & 1
    for b, val in ROUTING["switch_matrix_bits_set"].items():
        v[int(b)] = val
    return v


def frame_words(cfg: dict[int, int]) -> list[int]:
    words = [0] * N_FRAMES
    for b, val in cfg.items():
        f, d = TILE.configmem[b]
        if val:
            words[f] |= 1 << d
    return words


@dataclass
class TStep:
    fd: int = 0
    fs: int = 0
    i: tuple[int, ...] = (0,) * 8
    ci: int = 0
    sr: int = 0
    en: int = 0
    clk: int = 0
    sample: int = 0
    note: str = ""

    def encode(self) -> str:
        w = (self.fd & 0xFFFFFFFF) | ((self.fs & 0xFFFFF) << 32)
        for k, v in enumerate(self.i):
            w |= (v & 0xF) << (52 + 4 * k)
        w |= (self.ci & 1) << 84 | (self.sr & 1) << 85 | (self.en & 1) << 86
        w |= (self.clk & 1) << 87 | (self.sample & 1) << 88
        return f"{w:032x}"


@dataclass
class TileSeq:
    name: str
    description: str
    steps: list[TStep] = field(default_factory=list)
    _cur: TStep = field(default_factory=TStep)
    configs_written: list[dict[int, int]] = field(default_factory=list)
    frame_orders: list[list[int]] = field(default_factory=list)
    seg_ends: list[int] = field(default_factory=list)   # sequence mode: last step of each config

    def step(self, sample: bool = False, note: str = "", **kw) -> None:
        if "i" in kw and isinstance(kw["i"], int):
            kw["i"] = (kw["i"],) * 8
        for k, v in kw.items():
            if not hasattr(self._cur, k):
                raise KeyError(k)
            setattr(self._cur, k, tuple(v) if k == "i" else v)
        self.steps.append(TStep(**{**self._cur.__dict__, "sample": int(sample), "note": note}))

    def write_config(self, bits: int, order: list[int] | None = None) -> None:
        """All frames, in `order` (default: frame_order(self.name))."""
        order = list(order) if order is not None else frame_order(self.name)
        if sorted(order) != list(range(N_FRAMES)):
            raise ValueError(f"order {order} is not a permutation of the {N_FRAMES} frames")
        cfg = tile_config(bits)
        self.configs_written.append(cfg)
        self.frame_orders.append(order)
        words = frame_words(cfg)
        for f in order:
            word = words[f]
            self.step(fd=word, fs=0, note=f"frame{f} data")
            self.step(fs=1 << f, note=f"frame{f} strobe")
            self.step(fs=0, note=f"frame{f} release")
        # Configuration done. As in FABulous (Frame_Select / Frame_Data_Reg, see
        # NOTES.md): FrameStrobe idles at 0 and FrameData keeps the last frame
        # word written (Frame_Data_Reg is never cleared).
        if IDLE_FD == "zero":   # pre-fix testbench behaviour, kept for tracing only
            self.step(fd=0, fs=0, note="config idle (FrameData 0)")
        else:
            self.step(fs=0, note="config idle")

    def clock(self, sample: bool = True, note: str = "") -> None:
        self.step(clk=1, note=note)
        self.step(clk=0, sample=sample, note=note)

    def write_stim(self, path: Path) -> None:
        path.write_text("".join(s.encode() + "\n" for s in self.steps))
        path.with_suffix(".order").write_text(
            "".join(" ".join(map(str, o)) + "\n" for o in self.frame_orders))


def maj(a: int, b: int, c: int) -> int:
    return (a & b) | (a & c) | (b & c)


def bc_schedule() -> list[tuple[tuple[int, ...], int]]:
    """32 steps of (per-LUT I, tile Ci); every LUT sees every (I, Ci) once."""
    steps_ci = [c for c in (0, 1) for _ in range(16)]          # LA's Ci per step
    per_lut: list[list[int]] = []
    ci_seq = steps_ci
    for x in LUTS:
        pool = {0: list(range(16)), 1: list(range(16))}
        seq = []
        for c in ci_seq:
            if not pool[c]:
                raise SystemExit(f"L{x}: no combo left with Ci={c}")
            seq.append(pool[c].pop(0))
        per_lut.append(seq)
        ci_seq = [maj(c, (v >> 1) & 1, (v >> 2) & 1) for c, v in zip(ci_seq, seq)]
    return [(tuple(per_lut[k][t] for k in range(8)), steps_ci[t]) for t in range(32)]


def lut_ci(i: tuple[int, ...], ci: int) -> list[int]:
    out, c = [], ci
    for v in i:
        out.append(c)
        c = maj(c, (v >> 1) & 1, (v >> 2) & 1)
    return out + [c]   # last entry: tile Co


# ---------------------------------------------------------------- configs

def tcfg_bc1() -> TileSeq:
    s = TileSeq("BC1", "XNOR, I0 from Ci, reset value 1; every LUT sees all (I, Ci)")
    cell._setup(s, cell.make_bits(cell.XNOR_TT, i0_from_ci=1, reset_value=1))
    for t, (i, ci) in enumerate(bc_schedule()):
        s.step(i=i, ci=ci, sample=True, note=f"BC step {t}")
    return s


def tcfg_d0c() -> TileSeq:
    s = TileSeq("D0C", "D0 with tile Ci = 1 (LB..LH: Ci = 1 unless I1 = I2 = 0)")
    cell._seq_test(s, 0, ci=1)
    return s


def tcfg_d1() -> TileSeq:
    s = TileSeq("D1", "registered output, reset value 1, Ci = 0")
    cell._seq_test(s, 1)
    return s


TILE_CONFIGS = {f().name: f for f in (tcfg_bc1, tcfg_d0c, tcfg_d1)}


def sequence(names: list[str]) -> TileSeq:
    """Sequence mode: the configs as one run from a single power-up."""
    s = TileSeq("SEQ_" + "_".join(names), "sequence " + " -> ".join(names))
    for n in names:
        sub = TILE_CONFIGS[n]()
        s.steps += sub.steps
        s.configs_written += sub.configs_written
        s.frame_orders += sub.frame_orders
        s.seg_ends.append(len(s.steps) - 1)
    return s


# ---------------------------------------------------------------- model

def tile_model(seq: TileSeq) -> list[str]:
    """Expected samples: O of LA..LH then Co, 'x' where unknown."""
    latch: dict[int, int | None] = {b: None for b in TILE.configmem}
    ff: list[int | None] = [None] * 8
    prev_clk = 0
    out = []
    target = seq.configs_written[-1] if seq.configs_written else None
    for s in seq.steps:
        for b, (f, d) in TILE.configmem.items():
            if (s.fs >> f) & 1:
                latch[b] = (s.fd >> d) & 1
        routed = target is not None and all(
            latch[int(b)] == v for b, v in ROUTING["switch_matrix_bits_set"].items())
        cis = lut_ci(s.i, s.ci)
        o = []
        for k, x in enumerate(LUTS):
            bits = [latch[b] for b in LUT_BITS[x]]
            if not routed or any(v is None for v in bits):
                lut_out = None
            else:
                i0 = cis[k] if bits[cell.I0MUX_BIT] else s.i[k] & 1
                idx = (s.i[k] & 0b1110) | i0
                lut_out = bits[idx]
            if routed and s.clk and not prev_clk and s.en:
                ff[k] = bits[cell.RV_BIT] if s.sr else lut_out
            if not routed or bits[cell.FF_BIT] is None:
                o.append(None)
            else:
                o.append(ff[k] if bits[cell.FF_BIT] else lut_out)
        prev_clk = s.clk
        if s.sample:
            out.append("".join("x" if v is None else str(v) for v in o) + str(cis[8]))
    return out


def ci_coverage(seq: TileSeq) -> dict[str, set[tuple[int, int]]]:
    """Per LUT, the (I, Ci) pairs applied at sampled steps."""
    cov = {x: set() for x in LUTS}
    for s in seq.steps:
        if s.sample:
            cis = lut_ci(s.i, s.ci)
            for k, x in enumerate(LUTS):
                cov[x].add((s.i[k], cis[k]))
    return cov


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=ROOT / "build" / "tile_stim")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    for name, f in TILE_CONFIGS.items():
        seq = f()
        seq.write_stim(args.out / f"{name}.stim")
        exp = tile_model(seq)
        (args.out / f"{name}.expected").write_text("\n".join(exp) + "\n")
        cov = ci_coverage(seq)
        print(f"{name}: {len(seq.steps)} steps, {len(exp)} samples; (I, Ci) pairs per LUT: "
              + " ".join(f"L{x}={len(cov[x])}" for x in LUTS))


if __name__ == "__main__":
    main()
