"""Labware geometry and its placement on the deck.

Geometry (rows, cols, pitch, well naming) is slot-independent and lives in
labware.json. WHERE a piece of labware sits lives in layout.json. This module
joins the two and answers exactly one question: given a well, what are its
machine coordinates?

Every placement, whatever its grid model, reduces to the same three vectors::

    xy(row, col) = origin + col * col_vec + row * row_vec

so the pitch model, the three-corner solve and a single-position station are
all the same arithmetic downstream.
"""

from __future__ import annotations

import math
import string
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

Vec2 = Tuple[float, float]


def _rotate(v: Vec2, degrees: float) -> Vec2:
    """Rotate a vector CCW about the origin. Exact for multiples of 90."""
    deg = degrees % 360.0
    if deg == 0.0:
        return (v[0], v[1])
    if deg == 90.0:
        return (-v[1], v[0])
    if deg == 180.0:
        return (-v[0], -v[1])
    if deg == 270.0:
        return (v[1], -v[0])
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    return (v[0] * c - v[1] * s, v[0] * s + v[1] * c)


def well_name(row: int, col: int) -> str:
    """0-indexed (row, col) -> 'A1' style label. Row 0 = A, col 0 = 1."""
    if not 0 <= row < len(string.ascii_uppercase):
        raise ValueError(f"row {row} has no single-letter name")
    return f"{string.ascii_uppercase[row]}{col + 1}"


def parse_well(name: str) -> Tuple[int, int]:
    """'B3' -> (1, 2)."""
    name = name.strip().upper()
    if len(name) < 2 or not name[0].isalpha() or not name[1:].isdigit():
        raise ValueError(f"{name!r} is not a well name like 'A1'")
    return string.ascii_uppercase.index(name[0]), int(name[1:]) - 1


@dataclass(frozen=True)
class Labware:
    """Slot-independent geometry, straight out of labware.json."""

    id: str
    spec: Dict[str, Any]

    @property
    def display_name(self) -> str:
        return self.spec.get("display_name", self.id)

    @property
    def type(self) -> str:
        return self.spec.get("type", "unknown")

    @property
    def grid(self) -> Dict[str, Any]:
        return self.spec.get("grid", {"model": "single", "rows": 1, "cols": 1})

    @property
    def rows(self) -> int:
        return int(self.grid.get("rows", 1))

    @property
    def cols(self) -> int:
        return int(self.grid.get("cols", 1))

    @property
    def model(self) -> str:
        return self.grid.get("model", "single")

    @property
    def footprint(self) -> Optional[Vec2]:
        fp = self.spec.get("footprint")
        return (float(fp[0]), float(fp[1])) if fp else None

    @property
    def wipe(self) -> Optional[Dict[str, Any]]:
        return self.spec.get("wipe")

    @property
    def uncalibrated(self) -> List[str]:
        return list(self.spec.get("uncalibrated", []))

    def z(self, key: str) -> Optional[float]:
        v = self.spec.get("z", {}).get(key)
        return None if v is None else float(v)


@dataclass
class Placement:
    """One piece of labware, at a definite spot on the deck."""

    name: str
    labware: Labware
    origin: Vec2
    col_vec: Vec2
    row_vec: Vec2
    mount: str
    rotation: float = 0.0
    slot: Optional[int] = None
    station: Optional[str] = None
    spec: Dict[str, Any] = field(default_factory=dict)

    # ---- well resolution -------------------------------------------------

    def xy(self, row: int, col: int) -> Vec2:
        self.check_well(row, col)
        x = self.origin[0] + col * self.col_vec[0] + row * self.row_vec[0]
        y = self.origin[1] + col * self.col_vec[1] + row * self.row_vec[1]
        return (round(x, 4), round(y, 4))

    def well(self, name: str) -> Vec2:
        return self.xy(*parse_well(name))

    def check_well(self, row: int, col: int) -> None:
        if not (0 <= row < self.labware.rows and 0 <= col < self.labware.cols):
            raise IndexError(
                f"{self.name}: well (row={row}, col={col}) is outside "
                f"{self.labware.id} which is {self.labware.rows} x {self.labware.cols}"
            )

    def all_wells(self) -> List[Tuple[int, int]]:
        return [
            (r, c)
            for r in range(self.labware.rows)
            for c in range(self.labware.cols)
        ]

    # ---- extents ---------------------------------------------------------

    def corner_wells(self) -> List[Tuple[int, int]]:
        r, c = self.labware.rows - 1, self.labware.cols - 1
        return [(0, 0), (0, c), (r, 0), (r, c)]

    def well_bounds(self) -> Tuple[float, float, float, float]:
        """(x_min, x_max, y_min, y_max) over WELL CENTRES."""
        pts = [self.xy(r, c) for r, c in self.corner_wells()]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return (min(xs), max(xs), min(ys), max(ys))

    def grid_center(self) -> Vec2:
        x0, x1, y0, y1 = self.well_bounds()
        return (round((x0 + x1) / 2.0, 4), round((y0 + y1) / 2.0, 4))

    def footprint_bounds(self) -> Optional[Tuple[float, float, float, float]]:
        """Physical outline bounds, or None if the footprint was never measured.

        Returning None rather than a guess keeps the overlap check honest: it
        reports "cannot check" instead of silently passing.
        """
        fp = self.labware.footprint
        if fp is None:
            return None
        cx, cy = self.grid_center()
        hw, hh = fp[0] / 2.0, fp[1] / 2.0
        return (cx - hw, cx + hw, cy - hh, cy + hh)

    def col_x_values(self) -> List[float]:
        return [self.xy(0, c)[0] for c in range(self.labware.cols)]

    def row_y_values(self) -> List[float]:
        return [self.xy(r, 0)[1] for r in range(self.labware.rows)]

    def __repr__(self) -> str:
        return (
            f"<Placement {self.name} {self.labware.id} {self.mount} "
            f"origin={self.origin} {self.labware.rows}x{self.labware.cols}>"
        )


# --------------------------------------------------------------------------
# building placements from layout.json
# --------------------------------------------------------------------------

def _pitch_vectors(labware: Labware) -> Tuple[Vec2, Vec2]:
    g = labware.grid
    px = float(g["pitch_x"])
    py = float(g["pitch_y"])
    cd = g.get("col_dir", [1, 0])
    rd = g.get("row_dir", [0, -1])
    return (
        (px * float(cd[0]), px * float(cd[1])),
        (py * float(rd[0]), py * float(rd[1])),
    )


def _corner_vectors(labware: Labware, corners: Dict[str, Any]) -> Tuple[Vec2, Vec2, Vec2]:
    """Solve pitch AND rotation from three measured well centres."""
    for key in ("origin", "along_col", "along_row"):
        if key not in corners:
            raise KeyError(
                f"{labware.id}: grid model 'corners' needs a {key!r} entry in the "
                f"placement's corners block"
            )
    o = (float(corners["origin"][0]), float(corners["origin"][1]))
    ac = (float(corners["along_col"][0]), float(corners["along_col"][1]))
    ar = (float(corners["along_row"][0]), float(corners["along_row"][1]))

    ncol, nrow = labware.cols - 1, labware.rows - 1
    if ncol <= 0 or nrow <= 0:
        raise ValueError(f"{labware.id}: corner solve needs at least 2 rows and 2 cols")

    col_vec = ((ac[0] - o[0]) / ncol, (ac[1] - o[1]) / ncol)
    row_vec = ((ar[0] - o[0]) / nrow, (ar[1] - o[1]) / nrow)

    # If the placement records what the solve is expected to produce, hold the
    # solve to it. A stored number nothing checks is how the old scripts' own
    # displacement tables drifted away from the equations above them.
    want = corners.get("solved")
    if want:
        checks = [
            ("col_pitch", want.get("col_pitch"), math.hypot(*col_vec)),
            ("row_pitch", want.get("row_pitch"), math.hypot(*row_vec)),
        ]
        for label, expected, got in checks:
            if expected is not None and abs(float(expected) - got) > 1e-6:
                raise ValueError(
                    f"{labware.id}: the three measured corners solve to a "
                    f"{label} of {got:.6f}, but the placement records "
                    f"{expected}. One of them is stale - re-measure the rack, "
                    f"or delete corners.solved."
                )
        centre = want.get("grid_center")
        if centre:
            cx = o[0] + (col_vec[0] * ncol + row_vec[0] * nrow) / 2.0
            cy = o[1] + (col_vec[1] * ncol + row_vec[1] * nrow) / 2.0
            if (abs(cx - float(centre[0])) > 1e-4
                    or abs(cy - float(centre[1])) > 1e-4):
                raise ValueError(
                    f"{labware.id}: corners solve to a grid centre of "
                    f"({cx:.4f}, {cy:.4f}), but the placement records "
                    f"{centre}. Re-measure, or delete corners.solved."
                )
    return o, col_vec, row_vec


def _resolve_origin(
    entry: Dict[str, Any],
    labware: Labware,
    deck: Dict[str, Any],
    name: str,
    needs_grid: bool,
    col_vec: Vec2 = (0.0, 0.0),
    row_vec: Vec2 = (0.0, 0.0),
) -> Vec2:
    """Where is well (0, 0)?

    An explicit anchor_xy always wins. Otherwise derive it from a station or a
    slot centre, plus either a stored a1_from_center override or, for a grid
    declared centred on its footprint, the grid span.
    """
    if entry.get("anchor_xy"):
        a = entry["anchor_xy"]
        return (float(a[0]), float(a[1]))

    if entry.get("station"):
        st = deck.get("stations", {}).get(entry["station"])
        if st is None:
            raise KeyError(
                f"placement {name!r} references unknown station {entry['station']!r}"
            )
        xy = st["xy"]
        return (float(xy[0]), float(xy[1]))

    if entry.get("slot") is not None:
        slot_key = str(entry["slot"])
        slots = deck.get("slots", {})
        if slot_key not in slots:
            raise KeyError(f"placement {name!r} references unknown slot {slot_key}")
        cx, cy = (float(v) for v in slots[slot_key]["center"])
        if not needs_grid:
            return (cx, cy)

        override = entry.get("a1_from_center") or labware.spec.get("a1_from_center")
        if override:
            return (cx + float(override[0]), cy + float(override[1]))

        if not labware.spec.get("grid_centered_on_footprint"):
            raise ValueError(
                f"placement {name!r}: labware {labware.id} is slot-mounted with no "
                f"anchor_xy and no a1_from_center, and its grid is not declared "
                f"centred on its footprint. Cannot guess where well A1 is - add "
                f"anchor_xy or a1_from_center to the placement."
            )
        span_x = (labware.cols - 1) * col_vec[0] + (labware.rows - 1) * row_vec[0]
        span_y = (labware.cols - 1) * col_vec[1] + (labware.rows - 1) * row_vec[1]
        return (cx - span_x / 2.0, cy - span_y / 2.0)

    raise ValueError(
        f"placement {name!r} has no anchor_xy, no slot and no station - "
        f"there is no way to know where it is"
    )


def build_placement(
    entry: Dict[str, Any],
    labware: Labware,
    deck: Dict[str, Any],
) -> Placement:
    """Turn one layout.json placements[] entry into a resolved Placement."""
    name = entry.get("name", labware.id)
    mount = entry.get("mount", "fixed")
    rotation = float(entry.get("rotation", 0) or 0)
    model = labware.model
    slot = entry.get("slot")
    station = entry.get("station")

    if model == "corners":
        corners = entry.get("corners")
        if not corners:
            raise KeyError(
                f"placement {name!r} uses labware {labware.id} whose grid model is "
                f"'corners', so the placement must supply a 'corners' block"
            )
        if rotation:
            raise ValueError(
                f"placement {name!r}: rotation is meaningless with the 'corners' "
                f"model - the measured corners already encode any rotation. Set "
                f"rotation to 0 and re-measure if the rack moved."
            )
        origin, col_vec, row_vec = _corner_vectors(labware, corners)
        return Placement(name, labware, origin, col_vec, row_vec, mount,
                         rotation=0.0, slot=slot, station=station, spec=entry)

    if model == "single":
        origin = _resolve_origin(entry, labware, deck, name, needs_grid=False)
        return Placement(name, labware, origin, (0.0, 0.0), (0.0, 0.0), mount,
                         rotation=rotation, slot=slot, station=station, spec=entry)

    if model != "pitch":
        raise ValueError(f"{labware.id}: unknown grid model {model!r}")

    col_vec, row_vec = _pitch_vectors(labware)
    col_vec = _rotate(col_vec, rotation)
    row_vec = _rotate(row_vec, rotation)
    origin = _resolve_origin(entry, labware, deck, name, needs_grid=True,
                             col_vec=col_vec, row_vec=row_vec)
    return Placement(name, labware, origin, col_vec, row_vec, mount,
                     rotation=rotation, slot=slot, station=station, spec=entry)
