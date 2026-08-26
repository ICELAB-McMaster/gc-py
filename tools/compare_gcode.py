"""Compare two gcode files by what they DO, not byte for byte.

Byte identity is the wrong target. The refactor deliberately changed number
formatting (``Z5.0`` -> ``Z5``, ``POS=25.0`` -> ``POS=25.000``) and line
endings are platform text mode, so a plain diff is all noise. What matters is
whether the machine ends up in the same places holding the same volumes.

So this compares three things separately, which is what the handoff asked for:

* **motion** - the ordered sequence of resolved (X, Y, Z) waypoints
* **actuator** - the ordered sequence of plunger POS values
* **structure** - the ordered sequence of command opcodes

Usage::

    python tools/compare_gcode.py old.gcode new.gcode
    python tools/compare_gcode.py old.gcode new.gcode --tol 0.001
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import List, Optional, Tuple

AXIS = re.compile(r"([XYZEF])(-?\d+(?:\.\d+)?)")
POS = re.compile(r"ACTUATOR_MOVE\s+POS=(-?\d+(?:\.\d+)?)")

Waypoint = Tuple[Optional[float], Optional[float], Optional[float], int]


def parse(path: Path):
    """Resolve a gcode file into waypoints, POS values and an opcode list."""
    x = y = z = None
    waypoints: List[Waypoint] = []
    positions: List[Tuple[int, float]] = []
    opcodes: List[str] = []

    for lineno, raw in enumerate(path.read_text(errors="replace").splitlines(), 1):
        line = raw.split(";")[0].strip()
        if not line:
            continue

        m = POS.match(line)
        if m:
            positions.append((lineno, float(m.group(1))))
            opcodes.append("ACTUATOR_MOVE")
            continue

        word = line.split()[0].upper()
        opcodes.append(word)

        if word in ("G0", "G1"):
            for axis, val in AXIS.findall(line):
                v = float(val)
                if axis == "X":
                    x = v
                elif axis == "Y":
                    y = v
                elif axis == "Z":
                    z = v
            waypoints.append((x, y, z, lineno))
        elif word == "G28":
            axes = line[3:].upper()
            if not axes.strip():
                x = y = z = 0.0
            else:
                if "X" in axes:
                    x = 0.0
                if "Y" in axes:
                    y = 0.0
                if "Z" in axes:
                    z = 0.0
            waypoints.append((x, y, z, lineno))

    return waypoints, positions, opcodes


def _close(a, b, tol: float) -> bool:
    if a is None or b is None:
        return a is b
    return abs(a - b) <= tol


def compare_seq(name, a, b, tol, fmt, limit) -> List[str]:
    out = []
    if len(a) != len(b):
        out.append(f"{name}: count differs - old {len(a)}, new {len(b)}")
    shown = 0
    for i, (pa, pb) in enumerate(zip(a, b)):
        if name == "motion":
            same = all(_close(u, v, tol) for u, v in zip(pa[:3], pb[:3]))
        elif name == "actuator":
            same = _close(pa[1], pb[1], tol)
        else:
            same = pa == pb
        if not same:
            if shown < limit:
                out.append(f"  [{i}] old {fmt(pa)}   new {fmt(pb)}")
            shown += 1
    if shown > limit:
        out.append(f"  ... and {shown - limit} more")
    if shown:
        out.insert(0 if not out or not out[0].startswith(name) else 1,
                   f"{name}: {shown} differing entr{'y' if shown == 1 else 'ies'}")
    elif len(a) == len(b):
        out.append(f"{name}: identical ({len(a)} entries)")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("old")
    ap.add_argument("new")
    ap.add_argument("--tol", type=float, default=1e-4,
                    help="numeric tolerance in mm (default 1e-4)")
    ap.add_argument("--limit", type=int, default=15,
                    help="max differing entries to print per category")
    a = ap.parse_args(argv)

    old_p, new_p = Path(a.old), Path(a.new)
    wo, po, oo = parse(old_p)
    wn, pn, on = parse(new_p)

    print(f"old  {old_p}  ({len(wo)} waypoints, {len(po)} actuator, "
          f"{len(oo)} commands)")
    print(f"new  {new_p}  ({len(wn)} waypoints, {len(pn)} actuator, "
          f"{len(on)} commands)")
    print()

    lines = []
    lines += compare_seq("motion", wo, wn, a.tol,
                         lambda p: f"({p[0]}, {p[1]}, {p[2]}) L{p[3]}", a.limit)
    lines += compare_seq("actuator", po, pn, a.tol,
                         lambda p: f"POS={p[1]} L{p[0]}", a.limit)
    lines += compare_seq("structure", oo, on, a.tol, str, a.limit)
    print("\n".join(lines))

    # Distinct-position summary is more useful than a positional diff when the
    # counts differ, because one inserted move shifts everything after it.
    print()
    so, sn = {w[:3] for w in wo}, {w[:3] for w in wn}
    only_old = sorted(so - sn)
    only_new = sorted(sn - so)
    print(f"distinct positions: old {len(so)}, new {len(sn)}, "
          f"shared {len(so & sn)}")
    for label, s in (("only in old", only_old), ("only in new", only_new)):
        if s:
            print(f"  {label} ({len(s)}):")
            for p in s[:a.limit]:
                print(f"    {p}")
            if len(s) > a.limit:
                print(f"    ... and {len(s) - a.limit} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
