#!/usr/bin/env python3
"""Trace every verdict change caused by the idle FrameData fix (Phase 8, 0a).

For each fault whose verdict changed (cell: build/pre_idle matrices vs
reports; tile: reports/<n>_rerun_changes.csv) and each config whose status
changed, the fault is simulated with FrameData returning to 0 after
configuration (old) and keeping the last frame word (FABulous), with
+DUMPSTATE. Reported: the latches that end different from golden, their bit,
owner and FrameData position, the idle FrameData bit at that position, the
last frame the config writes, and the hypothesis check

  H: a strobe stuck at 1 on the last frame written re-latches that frame's
     own word, so its latches end correct and the fault is invisible.

Writes reports/idle_trace.md.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / d) for d in ("sim", "configs", "faults", "rtl")]
import gen_faults  # noqa: E402
import simcore  # noqa: E402
import state  # noqa: E402

REPORTS = ROOT / "reports"
BUILD = ROOT / "build"


def stem_name(mod, bits, b) -> str:
    drv = bits[b]["drivers"][0] if bits[b]["drivers"] else ""
    cname = drv.rsplit(".", 1)[0]
    if cname in mod["cells"] and mod["cells"][cname]["type"] == "$_BUF_":
        return stem_name(mod, bits, mod["cells"][cname]["connections"]["A"][0])
    return bits[b]["names"][0][0]


def latch_positions(js: Path) -> dict[int, tuple[int, int]]:
    """storage element index -> (frame strobe, FrameData bit) of its latch."""
    _, mod = gen_faults.load_top(js, None)
    bits = gen_faults.analyse(mod)
    out = {}
    for e in state.storage_elements(js):
        if e.kind != "latch":
            continue
        conn = mod["cells"][e.cell]["connections"]
        en = stem_name(mod, bits, conn["GATE" if "GATE" in conn else "E"][0])
        d = stem_name(mod, bits, conn["D"][0])
        out[e.index] = (int(en.split("[")[1].rstrip("]")), int(d.split("[")[1].rstrip("]")))
    return out


def changed_cell(n: str) -> list[tuple[dict, dict]]:
    a = {r["id"]: r for r in csv.DictReader((BUILD / "pre_idle" / f"{n}_matrix.csv").open())}
    b = {r["id"]: r for r in csv.DictReader((REPORTS / f"{n}_matrix.csv").open())}
    cols = [c for c in next(iter(b.values())) if c not in ("id", "path", "polarity", "type")]
    return [(a[i], b[i]) for i in b if any(a[i][c] != b[i][c] for c in cols)]


def changed_tile(n: str) -> list[tuple[dict, dict]]:
    out = []
    for r in csv.DictReader((REPORTS / f"{n}_rerun_changes.csv").open()):
        old = {"id": r["id"], "path": r["path"], "polarity": r["polarity"]}
        new = dict(old)
        for c in ("BC1", "D0C", "D1", "any"):
            old[c], new[c] = r[f"old_{c}"], r[f"new_{c}"]
        out.append((old, new))
    return out


def make_stims(tile: bool, names: list[str]) -> dict[str, dict]:
    """Stimuli for both idle modes: name -> mode -> (path, nsteps, last frame, idle word, bitpos map)."""
    import configs as cfg
    import tile_configs as tc
    mod = tc if tile else cfg
    res = {}
    for mode in ("zero", "last"):
        mod.IDLE_FD = mode
        d = BUILD / "trace_stim" / ("tile" if tile else "cell") / mode
        d.mkdir(parents=True, exist_ok=True)
        for name in names:
            seq = (tc.TILE_CONFIGS[name] if tile else cfg.CONFIGS[name])()
            path = d / f"{name}.stim"
            seq.write_stim(path)
            fs_steps = [s for s in seq.steps if s.fs]
            last_frame = fs_steps[-1].fs.bit_length() - 1
            res.setdefault(name, {})[mode] = (path, len(seq.steps), last_frame, seq.steps[-1].fd)
    mod.IDLE_FD = "last"
    return res


def owner_of(tile: bool, frame: int, dbit: int) -> str:
    if tile:
        import tile_configs as tc
        import routing_bits as rb
        pos2bit = {v: b for b, v in tc.TILE.configmem.items()}
        b = pos2bit[(frame, dbit)]
        return f"tile bit {b} {rb.BITMAP[b]['owner']}"
    import configs as cfg
    for k, m in enumerate(cfg.BITMAP):
        if (m.frame_strobe, m.frame_data_bit) == (frame, dbit):
            return f"cell bit {k}"
    return "?"


def trace(n: str, tile: bool, changes, configs: list[str]) -> list[str]:
    js = ROOT / "synth" / f"netlist_{n}.json"
    pos = latch_positions(js)
    faults = [f for f in csv.DictReader((ROOT / "faults" / f"{n}.csv").open())
              if f["id"] in {o["id"] for o, _ in changes}]
    if tile:
        import run_tile as rt
        vvp, _ = rt.compile_netlist(n, faults, tag="_trace")
    else:
        import run as rc
        vvp = rc.compile_target(n, faults)
    stims = make_stims(tile, configs)
    gold = {(c, m): simcore.run_vvp(vvp, [f"+STIM={p}", f"+NSTEPS={k}", "+DUMPSTATE"])
            for c in configs for m, (p, k, _, _) in stims[c].items()}
    lines = []
    for old, new in changes:
        fid = int(old["id"])
        strobe = None
        if old["path"].startswith("FrameStrobe[") and old["polarity"] == "1":
            strobe = int(old["path"].split("[")[1].split("]")[0])
        for c in configs:
            if old[c] == new[c]:
                continue
            row = {}
            for m, (p, k, last, idle) in stims[c].items():
                pa = [f"+STIM={p}", f"+NSTEPS={k}", "+DUMPSTATE"]
                f = simcore.run_vvp(vvp, pa, fid)
                g = gold[(c, m)]
                diff = []
                for i, (fr, db) in pos.items():
                    if g.state[i] != f.state[i]:
                        diff.append(f"{owner_of(tile, fr, db)} (frame {fr}, FD[{db}]) "
                                    f"{g.state[i]}->{f.state[i]}, idle FD[{db}]={(idle >> db) & 1}")
                row[m] = (simcore.classify(g.samples, f.samples), diff, last, idle)
            (so, do, last, _), (sn, dn, _, idle) = row["zero"], row["last"]
            if strobe is None:
                h = "n/a (not a strobe SA1)"
            elif strobe == last:
                h = "confirmed: last frame, no latch differs" if not dn else "REJECTED: latches differ"
            else:
                h = (f"not the last frame (last = {last}); latches take the idle word: "
                     + ("all at their intended value" if not dn else "some differ, see list"))
            lines.append(f"| {n} | {old['path']} SA{old['polarity']} | {c} | {old[c]} -> {new[c]} "
                         f"(sim: {so} -> {sn}) | {last} | {'; '.join(do) or 'none'} | "
                         f"{'; '.join(dn) or 'none'} | {h} |")
    return lines


def main() -> int:
    out = ["# Verdict changes from the idle FrameData fix, traced in simulation", "",
           "Old: FrameData returns to 0 after configuration. New: FrameData keeps the "
           "last frame word (FABulous Frame_Data_Reg). Latches listed are those whose "
           "end-of-run value differs from golden (golden -> faulty), with the idle "
           "FrameData bit at the latch's FrameData position.", "",
           "| netlist | fault | config | matrix old -> new (sim) | last frame written | "
           "latches differing, old idle | latches differing, new idle | hypothesis H |",
           "|---|---|---|---|---|---|---|---|"]
    import configs as cfg
    import tile_configs as tc
    for n in ("sky130", "generic", "sky130_br", "generic_br"):
        out += trace(n, False, changed_cell(n), cfg.TEST_CONFIGS)
        print(n, "traced", flush=True)
    for n in ("tile_generic", "tile_generic_br"):
        out += trace(n, True, changed_tile(n), list(tc.TILE_CONFIGS))
        print(n, "traced", flush=True)
    (REPORTS / "idle_trace.md").write_text("\n".join(out) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
