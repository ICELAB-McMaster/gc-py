"""The deck: envelope, slots, stations, keep-out zones and what is loaded.

`Deck.load()` reads deck.json + labware.json + layout.json and resolves every
placement, so a protocol can ask for `deck.placement("tubes").well("A1")` and
get machine coordinates without ever seeing a literal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import config
from .effector import LabError
from .labware import Labware, Placement, build_placement

Vec2 = Tuple[float, float]


class DeckError(LabError):
    """Raised for a deck the config cannot describe."""


@dataclass(frozen=True)
class Axis:
    name: str
    min: float
    max: float
    home: float

    def contains(self, v: float, tol: float = 1e-6) -> bool:
        return self.min - tol <= v <= self.max + tol


@dataclass(frozen=True)
class KeepOut:
    """An X band that forbids Y motion unless the effector is high enough.

    `safe_when_z_below` is a *commanded* Z. Because +Z is down, a SMALLER
    commanded Z means physically HIGHER, so the zone is safe when
    ``z < safe_when_z_below``.
    """

    id: str
    x_range: Tuple[float, float]
    forbids: str
    safe_when_z_below: Optional[float]
    whitelist: Tuple[str, ...]
    description: str = ""
    tipped_is_error: bool = True

    def contains_x(self, x: float) -> bool:
        return self.x_range[0] <= x <= self.x_range[1]

    def spans_x(self, x0: float, x1: float) -> bool:
        """True if a straight X move from x0 to x1 enters the band at all."""
        lo, hi = (x0, x1) if x0 <= x1 else (x1, x0)
        return not (hi < self.x_range[0] or lo > self.x_range[1])

    def z_is_safe(self, z: float) -> bool:
        if self.safe_when_z_below is None:
            return False
        return z < self.safe_when_z_below


@dataclass(frozen=True)
class Station:
    name: str
    xy: Vec2
    z: Optional[float]
    spec: Dict[str, Any] = field(default_factory=dict)


class Deck:
    """Resolved deck: geometry in, machine coordinates out."""

    def __init__(
        self,
        deck_cfg: config.ConfigFile,
        labware_cfg: config.ConfigFile,
        layout_cfg: config.ConfigFile,
    ) -> None:
        self.deck_cfg = deck_cfg
        self.labware_cfg = labware_cfg
        self.layout_cfg = layout_cfg

        d = deck_cfg.data
        self.id: str = d.get("id", "deck")
        self.raw: Dict[str, Any] = d

        ax = d["axes"]
        self.x = Axis("x", float(ax["x"]["min"]), float(ax["x"]["max"]), float(ax["x"]["home"]))
        self.y = Axis("y", float(ax["y"]["min"]), float(ax["y"]["max"]), float(ax["y"]["home"]))
        self.z = Axis("z", float(ax["z"]["min"]), float(ax["z"]["max"]), float(ax["z"]["home"]))

        self.feedrates: Dict[str, int] = {k: int(v) for k, v in d["feedrates"].items()}
        zc = d["z"]
        self.z_home: float = float(zc["home"])
        self.z_travel: float = float(zc["travel"])
        self.z_stage_surface: Optional[float] = (
            None if zc.get("stage_surface") is None else float(zc["stage_surface"])
        )

        self.slots: Dict[str, Vec2] = {
            k: (float(v["center"][0]), float(v["center"][1]))
            for k, v in d.get("slots", {}).items()
        }

        self.stations: Dict[str, Station] = {}
        for name, spec in d.get("stations", {}).items():
            xy = spec["xy"]
            self.stations[name] = Station(
                name=name,
                xy=(float(xy[0]), float(xy[1])),
                z=None if spec.get("z") is None else float(spec["z"]),
                spec=spec,
            )

        self.keepouts: List[KeepOut] = [
            KeepOut(
                id=k["id"],
                x_range=(float(k["x_range"][0]), float(k["x_range"][1])),
                forbids=k.get("forbids", "y_motion"),
                safe_when_z_below=(
                    None if k.get("safe_when_z_below") is None
                    else float(k["safe_when_z_below"])
                ),
                whitelist=tuple(k.get("whitelist", [])),
                description=k.get("description", ""),
            )
            for k in d.get("keepouts", [])
        ]

        self.macros: Dict[str, str] = d.get("klipper_macros", {})

        # ---- labware catalogue + resolved placements ---------------------
        cat = labware_cfg["labware"]
        self.catalogue: Dict[str, Labware] = {
            lid: Labware(lid, spec) for lid, spec in cat.items()
        }

        self.placements: Dict[str, Placement] = {}
        for entry in layout_cfg.get("placements", []):
            lid = entry["labware"]
            if lid not in self.catalogue:
                raise DeckError(
                    f"layout placement {entry.get('name', lid)!r} references labware "
                    f"{lid!r}, which is not in labware.json. Known: "
                    f"{sorted(self.catalogue)}"
                )
            # a1_from_slot_edge needs the cell size, which is the slot pitch.
            if entry.get("slot") is not None and "_slot_pitch" not in entry:
                entry = {**entry, "_slot_pitch": list(self._raw_slot_pitch(d))}
            p = build_placement(entry, self.catalogue[lid], d)
            if p.name in self.placements:
                raise DeckError(f"duplicate placement name {p.name!r} in layout")
            self.placements[p.name] = p
            self._check_recorded_slot_center(entry, p)

        self._check_slot_occupancy()

        self.open_questions: List[str] = list(layout_cfg.get("open_questions", []))

    # ---- construction ----------------------------------------------------

    @classmethod
    def load(cls, layout: str = "layout.json") -> "Deck":
        return cls(config.load_deck(), config.load_labware(), config.load_layout(layout))

    # ---- lookups ---------------------------------------------------------

    def placement(self, name: str) -> Placement:
        try:
            return self.placements[name]
        except KeyError:
            raise DeckError(
                f"no placement named {name!r} in this layout. Loaded: "
                f"{sorted(self.placements)}"
            ) from None

    def placement_for_labware(self, labware_id: str) -> Placement:
        hits = [p for p in self.placements.values() if p.labware.id == labware_id]
        if not hits:
            raise DeckError(
                f"nothing in this layout uses labware {labware_id!r}. Loaded: "
                f"{sorted(p.labware.id for p in self.placements.values())}"
            )
        if len(hits) > 1:
            raise DeckError(
                f"{len(hits)} placements use labware {labware_id!r} "
                f"({[p.name for p in hits]}); ask for one by name instead"
            )
        return hits[0]

    def station(self, name: str) -> Station:
        try:
            return self.stations[name]
        except KeyError:
            raise DeckError(
                f"no station named {name!r}. Known: {sorted(self.stations)}"
            ) from None

    def slot_center(self, slot: int) -> Vec2:
        try:
            return self.slots[str(slot)]
        except KeyError:
            raise DeckError(f"no slot {slot}. Known: {sorted(self.slots)}") from None

    def macro(self, name: str) -> str:
        """A Klipper macro prefix. Must be declared - never defaulted.

        Falling back to a hardcoded literal would mean a mistyped or deleted
        klipper_macros entry silently emits the built-in string instead of
        failing, and the gcode would look right while addressing nothing.
        """
        try:
            return self.macros[name]
        except KeyError:
            raise DeckError(
                f"deck.json klipper_macros has no {name!r}. Known: "
                f"{sorted(k for k in self.macros if k != 'note')}"
            ) from None

    def feedrate(self, name: str) -> int:
        try:
            return self.feedrates[name]
        except KeyError:
            raise DeckError(
                f"no feedrate named {name!r}. Known: {sorted(self.feedrates)}"
            ) from None

    # ---- envelope --------------------------------------------------------

    def in_envelope(self, x: float, y: float, z: float) -> List[str]:
        """Return a list of envelope violations. Empty means fine."""
        bad = []
        if not self.x.contains(x):
            bad.append(f"X {x} outside [{self.x.min}, {self.x.max}]")
        if not self.y.contains(y):
            bad.append(f"Y {y} outside [{self.y.min}, {self.y.max}]")
        if not self.z.contains(z):
            bad.append(f"Z {z} outside [{self.z.min}, {self.z.max}]")
        return bad

    def keepouts_hit_by_y_move(self, x: float) -> List[KeepOut]:
        """Keep-out bands that a Y move at this X would enter."""
        return [k for k in self.keepouts if k.forbids == "y_motion" and k.contains_x(x)]

    def keepouts_crossed_by_x_move(self, x0: float, x1: float) -> List[KeepOut]:
        return [k for k in self.keepouts if k.spans_x(x0, x1)]

    # ---- integrity -------------------------------------------------------

    def _check_recorded_slot_center(self, entry: Dict[str, Any],
                                    p: Placement) -> None:
        """Hold a placement's convenience copy of a slot centre to the real one.

        A duplicated coordinate that nothing verifies will eventually disagree
        with its source, and the copy is the one a human reads.
        """
        recorded = entry.get("slot_center")
        if not recorded or p.slot is None:
            return
        actual = self.slots.get(str(p.slot))
        if actual is None:
            return
        if (abs(float(recorded[0]) - actual[0]) > 1e-6
                or abs(float(recorded[1]) - actual[1]) > 1e-6):
            raise DeckError(
                f"placement {p.name!r} records slot_center {list(recorded)}, but "
                f"deck.json puts slot {p.slot} at {list(actual)}. Fix whichever "
                f"is stale, or drop slot_center from the placement."
            )

    def _check_slot_occupancy(self) -> None:
        seen: Dict[int, str] = {}
        for p in self.placements.values():
            if p.slot is None:
                continue
            if p.slot in seen:
                raise DeckError(
                    f"slot {p.slot} is claimed by both {seen[p.slot]!r} and {p.name!r}"
                )
            seen[p.slot] = p.name

    def slot_seating_report(self) -> List[str]:
        """Does each slot-mounted labware actually sit in the slot it claims?

        A slot label is a claim about physical seating, and a claim nothing
        checks is one that can be wrong for months. Compares the resolved well
        bounds against the slot cell and reports the offset. Placements mounted
        `fixed` or at a `station` are exempt: they carry `near_slot` as a human
        reference, not a seating claim.
        """
        out: List[str] = []
        hw, hh = self.slot_pitch()
        for p in self.placements.values():
            if p.slot is None or p.mount != "slot":
                continue
            centre = self.slots.get(str(p.slot))
            if centre is None:
                continue
            gx, gy = p.grid_center()
            dx, dy = gx - centre[0], gy - centre[1]
            dist = (dx * dx + dy * dy) ** 0.5
            if dist <= max(hw, hh) / 2.0:
                continue
            disputed = " (flagged disputed in the layout)" if p.spec.get(
                "slot_disputed") else ""
            out.append(
                f"{p.name} claims slot {p.slot} at {list(centre)} but its grid "
                f"centre is ({gx}, {gy}) - offset ({dx:+.1f}, {dy:+.1f}), "
                f"{dist:.1f} mm, more than half a slot pitch away{disputed}. "
                f"Either the slot number or the slot frame is wrong."
            )
        return out

    # ---- the stage surface, and where it exists -------------------------

    def stage_region(self) -> Optional[Tuple[float, float, float, float]]:
        """(x_min, x_max, y_min, y_max) over which the stage surface exists.

        Derived from the slot centres expanded by half the slot pitch rather
        than stored, so it cannot drift out of step with the slot table. Off
        this region there is no stage, which is what makes the ejector's Z 145
        reachable: it hangs past the front edge.
        """
        if not self.slots:
            return None
        px, py = self.slot_pitch()
        xs = [c[0] for c in self.slots.values()]
        ys = [c[1] for c in self.slots.values()]
        return (min(xs) - px / 2.0, max(xs) + px / 2.0,
                min(ys) - py / 2.0, max(ys) + py / 2.0)

    def point_on_stage(self, x: float, y: float) -> bool:
        r = self.stage_region()
        if r is None:
            return False
        return r[0] <= x <= r[1] and r[2] <= y <= r[3]

    def slot_cell(self, x: float, y: float) -> Optional[str]:
        """Which stage location contains this point, or None if off-stage.

        Used to tell a traverse that crosses a location boundary — and so
        passes over a raised edge — from motion staying inside one location,
        which does not.
        """
        if not self.point_on_stage(x, y):
            return None
        px, py = self.slot_pitch()
        best: Optional[Tuple[float, str]] = None
        for key, (cx, cy) in self.slots.items():
            if abs(x - cx) <= px / 2.0 and abs(y - cy) <= py / 2.0:
                d = (x - cx) ** 2 + (y - cy) ** 2
                if best is None or d < best[0]:
                    best = (d, key)
        return None if best is None else best[1]

    @property
    def z_slot_edge_bare(self) -> Optional[float]:
        """Bare-nosecone Z at which the effector would strike a raised edge.

        Stored as a with-tip measurement because that is how it was taken; the
        bare equivalent is that plus the tip length, so one measurement covers
        both cases.
        """
        z = self.raw["z"]
        edge = z.get("slot_edge_with_p300_tip")
        if edge is None:
            return None
        tipped_surface = z.get("stage_surface_with_p300_tip")
        if tipped_surface is None or self.z_stage_surface is None:
            return None
        tip_len = self.z_stage_surface - float(tipped_surface)
        return float(edge) + tip_len

    @staticmethod
    def _raw_slot_pitch(deck_raw: Dict[str, Any]) -> Tuple[float, float]:
        """Slot pitch straight from the raw config, for use during __init__."""
        slots = deck_raw.get("slots", {})
        xs = sorted({float(v["center"][0]) for v in slots.values()})
        ys = sorted({float(v["center"][1]) for v in slots.values()})
        px = ((xs[-1] - xs[0]) / (len(xs) - 1)) if len(xs) > 1 else 0.0
        py = ((ys[-1] - ys[0]) / (len(ys) - 1)) if len(ys) > 1 else 0.0
        return (px, py)

    def slot_pitch(self) -> Tuple[float, float]:
        """Mean X and Y spacing between adjacent slot centres."""
        xs = sorted({c[0] for c in self.slots.values()})
        ys = sorted({c[1] for c in self.slots.values()})
        px = ((xs[-1] - xs[0]) / (len(xs) - 1)) if len(xs) > 1 else 0.0
        py = ((ys[-1] - ys[0]) / (len(ys) - 1)) if len(ys) > 1 else 0.0
        return (px, py)

    def near_slots(self) -> Dict[str, int]:
        """Placements that carry a `near_slot` reference rather than a seating."""
        return {
            p.name: p.spec["near_slot"]
            for p in self.placements.values()
            if p.spec.get("near_slot") is not None
        }

    def overlap_report(self) -> List[str]:
        """Pairwise footprint overlap between placements.

        Only reports for pairs where BOTH footprints are known. Pairs with an
        unmeasured footprint come back as an explicit "cannot check" line so an
        unknown never reads as a pass.
        """
        out: List[str] = []
        names = sorted(self.placements)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                pa, pb = self.placements[a], self.placements[b]
                fa, fb = pa.footprint_bounds(), pb.footprint_bounds()
                if fa is None or fb is None:
                    missing = [
                        p.name for p, f in ((pa, fa), (pb, fb)) if f is None
                    ]
                    out.append(
                        f"cannot check {a} vs {b}: no footprint measured for "
                        f"{', '.join(missing)}"
                    )
                    continue
                if not (fa[1] < fb[0] or fb[1] < fa[0] or fa[3] < fb[2] or fb[3] < fa[2]):
                    out.append(f"OVERLAP: {a} footprint intersects {b} footprint")
        return out

    def uncalibrated(self) -> List[str]:
        """Every field the config admits was never tuned."""
        out: List[str] = []
        for p in self.placements.values():
            for fld in p.labware.uncalibrated:
                out.append(f"{p.name}({p.labware.id}).{fld}")
        if self.z_stage_surface is None:
            out.append("deck.z.stage_surface (never measured)")
        # Structured, not grepped out of a prose note: rewording a comment must
        # not be able to make an uncalibrated field silently look calibrated.
        out.extend(f"deck.{f}" for f in self.raw.get("uncalibrated", []))
        for st_name, st in self.raw.get("stations", {}).items():
            if "validated" in st and not st["validated"]:
                out.append(
                    f"deck.stations.{st_name} (routine never validated on hardware)"
                )
        return out

    def summary(self) -> str:
        lines = [f"deck {self.id}"]
        lines.append(
            f"  envelope  X[{self.x.min},{self.x.max}] Y[{self.y.min},{self.y.max}] "
            f"Z[{self.z.min},{self.z.max}] (+Z is DOWN)"
        )
        lines.append(f"  z_travel  {self.z_travel}   z_home {self.z_home}")
        for name, p in sorted(self.placements.items()):
            x0, x1, y0, y1 = p.well_bounds()
            where = (
                f"slot {p.slot}" if p.slot is not None
                else f"station {p.station}" if p.station
                else "fixed"
            )
            lines.append(
                f"  {name:<8} {p.labware.id:<24} {where:<10} "
                f"{p.labware.rows}x{p.labware.cols}  "
                f"X[{x0:g},{x1:g}] Y[{y0:g},{y1:g}]"
            )
        for k in self.keepouts:
            lines.append(
                f"  keepout  {k.id}: X[{k.x_range[0]},{k.x_range[1]}] forbids "
                f"{k.forbids} unless Z < {k.safe_when_z_below}"
            )
        return "\n".join(lines)
