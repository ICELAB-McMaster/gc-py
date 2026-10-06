"""Does a .gcode file line up with the deck as currently configured?

Answers one question: everywhere this file actually DOES something, is there
something there to do it to?

The distinction matters. Because XY moves one axis at a time, a run is full of
transit corners — (target X, previous Y) — that are not deck features and never
will be. Those only need to be inside the envelope and out of the keep-out
bands. What must land on a real feature is a WORKING position: somewhere the
machine leaves travel height, moves the plunger, or dwells.

    python tools/check_alignment.py FILE.gcode [FILE.gcode ...]
    python tools/check_alignment.py --shift 70 old.gcode   # test a frame guess
    python tools/check_alignment.py -v FILE.gcode          # list every match

Exit code is 1 if any working position is unrecognised or out of envelope.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lab.deck import Deck
from lab.labware import well_name

AXIS = re.compile(r"([XYZ])(-?\d+(?:\.\d+)?)")
ACTION = re.compile(r"^(ACTUATOR_MOVE|G4)\b")

# How far from a well centre still counts as that well. The wall wipe steps
# 3.2 mm sideways on purpose, so the tolerance has to clear it.
TOL = 4.0

XY = Tuple[float, float]


@dataclass
class Scan:
    working: Dict[XY, Set[float]] = field(default_factory=dict)  # xy -> z values
    transit: Dict[XY, int] = field(default_factory=dict)
    lines: int = 0


def scan(path: Path, z_travel: float) -> Scan:
    x = y = z = None
    out = Scan()
    pending: Optional[XY] = None  # xy of the last move, in case an action follows

    for raw in path.read_text(errors="replace").splitlines():
        out.lines += 1
        line = raw.split(";")[0].strip()
        if not line:
            continue

        if line.upper().startswith("G28"):
            a = line[3:].upper()
            if not a.strip():
                x = y = z = 0.0
            else:
                if "X" in a:
                    x = 0.0
                if "Y" in a:
                    y = 0.0
                if "Z" in a:
                    z = 0.0
            pending = None
            continue

        if ACTION.match(line.upper()):
            # The plunger moved or the machine paused: wherever it is, that is
            # a working position.
            if pending is not None:
                out.working.setdefault(pending, set()).add(z if z is not None else 0.0)
            continue

        if not line.upper().startswith(("G0", "G1")):
            continue

        for axis, val in AXIS.findall(line):
            if axis == "X":
                x = float(val)
            elif axis == "Y":
                y = float(val)
            else:
                z = float(val)

        if x is None or y is None:
            continue
        key = (round(x, 4), round(y, 4))
        pending = key
        if z is not None and abs(z - z_travel) > 1e-6 and z > z_travel:
            # Below travel height: the machine has committed to this spot.
            out.working.setdefault(key, set()).add(z)
        else:
            out.transit[key] = out.transit.get(key, 0) + 1
    return out


def identify(deck: Deck, x: float, y: float) -> Optional[str]:
    """What is at this position? None if nothing the deck knows about."""
    best: Optional[Tuple[float, str]] = None

    for name, p in deck.placements.items():
        n_wells = p.labware.rows * p.labware.cols
        for row in range(p.labware.rows):
            for col in range(p.labware.cols):
                wx, wy = p.xy(row, col)
                d = ((wx - x) ** 2 + (wy - y) ** 2) ** 0.5
                if d <= TOL and (best is None or d < best[0]):
                    label = f"{name}:{well_name(row, col)}" if n_wells > 1 else name
                    best = (d, label + ("" if d < 1e-6 else f" (+{d:.1f}mm)"))

    # A gripper takes a plate by its CENTRE, which is not a well, so a
    # gripper run would otherwise report every pick as "nothing on the deck
    # is here". Only labware big enough to grab is worth offering.
    for name, p in deck.placements.items():
        if p.labware.rows * p.labware.cols < 2:
            continue
        cx, cy = p.grid_center()
        d = ((cx - x) ** 2 + (cy - y) ** 2) ** 0.5
        if d <= TOL and (best is None or d < best[0]):
            best = (d, f"{name}:centre"
                       + ("" if d < 1e-6 else f" (+{d:.1f}mm)"))

    for name, st in deck.stations.items():
        d = ((st.xy[0] - x) ** 2 + (st.xy[1] - y) ** 2) ** 0.5
        if d <= TOL and (best is None or d < best[0]):
            best = (d, f"station:{name}" + ("" if d < 1e-6 else f" (+{d:.1f}mm)"))

    ej = deck.raw.get("stations", {}).get("tip_eject", {})
    if ej and abs(x - float(ej.get("lane_x", -1e9))) <= TOL:
        for k in ("approach_y", "engage_y", "retract_y"):
            if k in ej and abs(y - float(ej[k])) <= TOL:
                if best is None or best[0] > 1.0:
                    best = (1.0, f"ejector:{k}")

    return None if best is None else best[1]


def check(deck: Deck, path: Path, shift: float, verbose: bool):
    s = scan(path, deck.z_travel)
    problems: List[str] = []
    matches: Dict[str, int] = {}
    detail: List[str] = []

    for (x, y), zs in sorted(s.working.items()):
        sx = x + shift
        env = deck.in_envelope(sx, y, max(zs))
        what = identify(deck, sx, y)
        if env:
            problems.append(f"WORKING ({x}, {y}) Z{sorted(zs)}: "
                            f"outside the envelope - {'; '.join(env)}")
        elif what is None:
            problems.append(f"WORKING ({x}, {y}) Z{sorted(zs)}: "
                            f"nothing on the deck is here")
        else:
            group = what.split(":")[0]
            matches[group] = matches.get(group, 0) + 1
            if verbose:
                detail.append(f"    ({x}, {y}) Z{sorted(zs)} -> {what}")

    # Transit corners: envelope and keep-out only.
    for (x, y), n in sorted(s.transit.items()):
        sx = x + shift
        env = deck.in_envelope(sx, y, deck.z_travel)
        if env:
            problems.append(f"transit ({x}, {y}) x{n}: outside the envelope - "
                            f"{'; '.join(env)}")

    return s, matches, problems, detail


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("files", nargs="+")
    ap.add_argument("--shift", type=float, default=0.0,
                    help="add this to every X before checking, to test a frame")
    ap.add_argument("--layout", default="layout.json")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="list every working position and what it matched")
    a = ap.parse_args(argv)

    deck = Deck.load(a.layout)
    print(f"deck {deck.id}, layout {deck.layout_cfg.get('id')}"
          + (f", testing with X{a.shift:+g}" if a.shift else ""))
    print("  loaded: " + ", ".join(
        (f"{n} in slot {p.slot}" if p.slot else f"{n} at station {p.station}")
        for n, p in sorted(deck.placements.items())))
    print()

    worst = 0
    for f in a.files:
        path = Path(f)
        if not path.is_file():
            print(f"{f}: not found")
            worst = 1
            continue
        s, matches, problems, detail = check(deck, path, a.shift, a.verbose)
        tag = "ALIGNS" if not problems else f"{len(problems)} PROBLEM(S)"
        print(f"{path.name:<32} {len(s.working):>3} working, "
              f"{len(s.transit):>3} transit   {tag}")
        if matches:
            print("    matched: " + ", ".join(
                f"{k} x{v}" for k, v in sorted(matches.items())))
        for d in detail:
            print(d)
        for p in problems[:a.limit]:
            print(f"    {p}")
        if len(problems) > a.limit:
            print(f"    ... and {len(problems) - a.limit} more")
        if problems:
            worst = 1
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
