"""Compare two accuracy-test gcode files block by block.

A positional diff is useless here: inserting two lines in the tip-pickup block
shifts every later line and reports 400 differences that are really one. So
split both files into logical blocks on their LOG_TEMPS markers and compare
matching blocks against each other.

    python tools/compare_blocks.py old.gcode new.gcode
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Dict, List, Tuple

AXIS = re.compile(r"([XYZF])(-?\d+(?:\.\d+)?)")
POS = re.compile(r"ACTUATOR_MOVE\s+POS=(-?\d+(?:\.\d+)?)")
TAG = re.compile(r"LOG_TEMPS\s+TAG=(\S+)")


def split_blocks(path: Path) -> List[Tuple[str, List[str]]]:
    """[(block_name, lines)]. Blocks break at LOG_TEMPS tags."""
    blocks: List[Tuple[str, List[str]]] = []
    name, cur = "preamble", []
    for raw in path.read_text(errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith(";"):
            continue
        m = TAG.match(line)
        if m:
            blocks.append((name, cur))
            name, cur = m.group(1), []
            continue
        cur.append(line)
    blocks.append((name, cur))
    return [(n, ls) for n, ls in blocks if ls or n != "preamble"]


def normalise(lines: List[str]) -> List[str]:
    """Canonical form: numbers rounded, so formatting alone never differs."""
    out = []
    for line in lines:
        m = POS.match(line)
        if m:
            out.append(f"ACTUATOR_MOVE POS={float(m.group(1)):.3f}")
            continue
        word = line.split()[0].upper()
        if word in ("G0", "G1"):
            parts = [word]
            for axis, val in AXIS.findall(line):
                v = float(val)
                parts.append(f"{axis}{v:.4f}".rstrip("0").rstrip("."))
            out.append(" ".join(parts))
        else:
            out.append(" ".join(line.split()))
    return out


def waypoints(lines: List[str]) -> List[Tuple[float, float, float]]:
    x = y = z = None
    out = []
    for line in lines:
        if not line.upper().startswith(("G0", "G1")):
            continue
        for axis, val in AXIS.findall(line):
            v = float(val)
            if axis == "X":
                x = v
            elif axis == "Y":
                y = v
            elif axis == "Z":
                z = v
        out.append((x, y, z))
    return out


def positions(lines: List[str]) -> List[float]:
    return [float(m.group(1)) for m in (POS.match(l) for l in lines) if m]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("old")
    ap.add_argument("new")
    ap.add_argument("--show", type=int, default=3,
                    help="how many differing blocks to print in full")
    a = ap.parse_args(argv)

    old_blocks = split_blocks(Path(a.old))
    new_blocks = split_blocks(Path(a.new))
    old_map: Dict[str, List[str]] = {n: ls for n, ls in old_blocks}
    new_map: Dict[str, List[str]] = {n: ls for n, ls in new_blocks}

    print(f"old {a.old}: {len(old_blocks)} blocks")
    print(f"new {a.new}: {len(new_blocks)} blocks")
    print()

    only_old = [n for n, _ in old_blocks if n not in new_map]
    only_new = [n for n, _ in new_blocks if n not in old_map]
    if only_old:
        print(f"blocks only in old: {only_old}")
    if only_new:
        print(f"blocks only in new: {only_new}")

    shared = [n for n, _ in old_blocks if n in new_map]
    identical, differing = [], []
    for n in shared:
        if normalise(old_map[n]) == normalise(new_map[n]):
            identical.append(n)
        else:
            differing.append(n)

    print(f"shared blocks: {len(shared)}   "
          f"identical: {len(identical)}   differing: {len(differing)}")
    print()

    if identical:
        print(f"IDENTICAL ({len(identical)}):")
        print("  " + ", ".join(identical))
        print()

    if differing:
        print(f"DIFFERING ({len(differing)}):")
        for n in differing:
            o, w = normalise(old_map[n]), normalise(new_map[n])
            ow, nw = waypoints(o), waypoints(w)
            op, np_ = positions(o), positions(w)
            same_motion = ow == nw
            same_pos = op == np_
            print(f"  {n}: {len(o)} -> {len(w)} lines   "
                  f"motion {'same' if same_motion else 'DIFFERS'}   "
                  f"actuator {'same' if same_pos else 'DIFFERS'}")
        print()

        for n in differing[:a.show]:
            o, w = normalise(old_map[n]), normalise(new_map[n])
            print(f"--- block {n} ---")
            import difflib
            for line in difflib.unified_diff(o, w, "old", "new", n=1, lineterm=""):
                if not line.startswith(("---", "+++", "@@")):
                    print(f"  {line}")
            print()
        if len(differing) > a.show:
            print(f"({len(differing) - a.show} more differing blocks not shown)")

    # Per-tube-cycle summary: are all 20 dispense cycles the same shape?
    tube = [n for n in shared if re.match(r"^v[\d.]+_r\d+$", n)]
    if tube:
        shapes_old = {tuple(normalise(old_map[n])) for n in tube}
        shapes_new = {tuple(normalise(new_map[n])) for n in tube}
        print(f"tube cycles: {len(tube)} shared   "
              f"{len(shapes_old)} distinct shapes in old, "
              f"{len(shapes_new)} in new")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
