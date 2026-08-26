"""Dry-run checking. No hardware, no Moonraker, no file written.

The simulator sees every step as it is generated and raises on the first hard
violation, naming the step index and the operation that produced it. A run that
fails here never becomes a .gcode file.

Two rules deserve stating explicitly because they read backwards:

* **+Z is DOWN.** A larger commanded Z is physically lower. "Above Z 60" means
  ``z < 60``.
* **A check that cannot be evaluated is not a pass.** Where a required
  measurement is missing (tip length, stage surface, labware rim) the simulator
  records an explicit `cannot check` warning rather than staying silent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .deck import Deck, KeepOut
from .effector import Effector, LabError, Pipette

XYZ = Tuple[float, float, float]


class SimulationError(LabError):
    """A hard check failed. The gcode must not be written."""

    def __init__(self, step: "Step", message: str) -> None:
        self.step = step
        self.message = message
        super().__init__(
            f"step {step.i} [{step.op}/{step.kind}]: {message}\n"
            f"  at {step.frm} -> {step.to}  POS {step.pos_before} -> {step.pos_after}"
            f"  tip={step.tip}  fluid={step.fluid_uL} uL"
        )


@dataclass
class Step:
    """One generated action, with the machine state it produced."""

    i: int
    op: str
    kind: str  # move | actuator | home | dwell | macro | note
    frm: Optional[XYZ] = None
    to: Optional[XYZ] = None
    axes: str = ""
    pos_before: Optional[float] = None
    pos_after: Optional[float] = None
    tip: bool = False
    fluid_uL: float = 0.0
    feedrate: Optional[int] = None
    labware: Optional[str] = None
    detail: Dict[str, Any] = field(default_factory=dict)
    gline: int = 0

    @property
    def moves_y(self) -> bool:
        return "Y" in self.axes

    @property
    def moves_xy(self) -> bool:
        return "X" in self.axes or "Y" in self.axes


# Operations allowed to move XY somewhere other than the travel height. Both
# genuinely need it: wall_wipe works inside a well, and the ejector engagement
# happens 140 mm down. Keep-out exemption is NOT granted here - it is granted
# per-step via detail["keepout_exempt"], so an approach move cannot inherit it
# from the operation it belongs to.
_LOW_XY_OK = {"wall_wipe", "eject_tip"}


@dataclass
class Report:
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    steps: int = 0
    path: List[Tuple[float, float, float, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def text(self) -> str:
        out = [f"simulation: {self.steps} steps, "
               f"{len(self.errors)} error(s), {len(self.warnings)} warning(s)"]
        for e in self.errors:
            out.append(f"  ERROR   {e}")
        for w in self.warnings:
            out.append(f"  warning {w}")
        return "\n".join(out)


class Simulator:
    """Incremental checker. Feed it steps; it raises on the first hard failure."""

    def __init__(self, deck: Deck, effector: Effector, strict: bool = True) -> None:
        self.deck = deck
        self.effector = effector
        self.strict = strict
        self.report = Report()
        self._warned: Set[str] = set()

        # tip-box consumption, for the post-pickup exit-path check
        self.consumed_tips: Set[Tuple[int, int]] = set()
        self._pending_eject_home = False
        self._eject_step: Optional[Step] = None
        self._finished = False

    # ---- warnings ---------------------------------------------------------

    def warn(self, key: str, message: str) -> None:
        """Record a warning once per distinct key, so a loop cannot spam it."""
        if key in self._warned:
            return
        self._warned.add(key)
        self.report.warnings.append(message)

    def fail(self, step: Step, message: str) -> None:
        self.report.errors.append(f"step {step.i} [{step.op}]: {message}")
        if self.strict:
            raise SimulationError(step, message)

    # ---- the tip-aware Z envelope ----------------------------------------

    def tip_length(self) -> Optional[float]:
        lid = self.effector.tip_labware
        if not lid or lid not in self.deck.catalogue:
            return None
        v = self.deck.catalogue[lid].spec.get("tip", {}).get("length")
        return None if v is None else float(v)

    def effective_z_limit(
        self, tip_fitted: bool, clearance: float = 0.0
    ) -> Tuple[Optional[float], List[str]]:
        """Deepest legal commanded Z, plus whatever is missing to compute it.

        With +Z down, a fitted tip extends the reference point downward, so the
        maximum commanded Z SHRINKS by the tip length the moment a tip goes on.

        Returns ``(limit, missing)``. The missing list matters as much as the
        limit: measuring z.stage_surface alone would otherwise switch the check
        on for bare moves and leave it off for the deeper, more dangerous
        tipped ones, with nothing saying so.
        """
        missing: List[str] = []
        surf = self.deck.z_stage_surface
        if surf is None:
            missing.append("deck z.stage_surface")

        tl = None
        if tip_fitted:
            tl = self.tip_length()
            if tl is None:
                lid = self.effector.tip_labware or "the tip labware"
                missing.append(f"{lid} tip.length")

        if missing:
            return None, missing
        return (surf - clearance if not tip_fitted else surf - tl - clearance), []

    # ---- per-step checks --------------------------------------------------

    def check(self, step: Step) -> None:
        self.report.steps = max(self.report.steps, step.i + 1)

        if step.kind == "home":
            self._pending_eject_home = False
            self._eject_step = None
        elif self._pending_eject_home and step.kind in ("move", "actuator"):
            bad = self._eject_step or step
            self._pending_eject_home = False
            self.fail(
                bad,
                "a tip ejection was not followed by G28. The strip force can skip "
                "steps on an open-loop machine, so commanded position is no longer "
                "trustworthy. Re-homing after every ejection is mandatory.",
            )

        if step.kind == "move":
            self._check_move(step)
        elif step.kind == "actuator":
            self._check_actuator(step)

        if step.op == "eject_tip" and step.detail.get("final"):
            self._pending_eject_home = True
            self._eject_step = step

        if step.to is not None and all(v is not None for v in step.to):
            self.report.path.append((*step.to, step.op))

    def _check_move(self, step: Step) -> None:
        assert step.to is not None and step.frm is not None
        x, y, z = step.to

        bad = self.deck.in_envelope(x, y, z)
        if bad:
            self.fail(step, "outside the machine envelope: " + "; ".join(bad))

        limit, missing = self.effective_z_limit(step.tip)
        if limit is None:
            # Keyed on tip state: the tipped case can be unresolvable while the
            # bare case is fine, and that must not be swallowed as a duplicate.
            self.warn(
                f"z_limit:{'tipped' if step.tip else 'bare'}",
                f"cannot check absolute crash depth for "
                f"{'TIPPED' if step.tip else 'bare'} moves: "
                f"{' and '.join(missing)} unmeasured. The tip-aware Z envelope "
                f"is unknown, so only the kinematic limit and per-labware Z were "
                f"checked.",
            )
        elif z > limit + 1e-6:
            self.fail(
                step,
                f"Z {z} is below the effective limit {limit} "
                f"(tip {'fitted' if step.tip else 'off'})",
            )

        # XY must happen at the travel height, or not at all.
        if step.moves_xy and step.op not in _LOW_XY_OK:
            zt = self.deck.z_travel
            if abs(step.frm[2] - zt) > 1e-6 or abs(z - zt) > 1e-6:
                self.fail(
                    step,
                    f"XY traverse at Z {step.frm[2]} -> {z}, not the travel height "
                    f"{zt}. Every traverse must be at travel height; this is the "
                    f"crash mode that costs a tip.",
                )

        # Keep-out bands forbid Y motion unless the effector is high enough.
        if step.moves_y:
            for k in self.deck.keepouts_hit_by_y_move(step.frm[0]):
                self._check_keepout(step, k, z)

        # Traverse clearance over loaded labware.
        if step.moves_xy and step.op not in _LOW_XY_OK:
            self._check_traverse_clearance(step)

        # A dispense must not drive into the bottom of the vessel.
        if step.labware:
            lw = self.deck.catalogue.get(step.labware)
            bottom = lw.z("bottom") if lw else None
            if bottom is not None and z > bottom + 1e-6:
                self.fail(
                    step,
                    f"Z {z} is past the {step.labware} well bottom {bottom}",
                )
            elif lw is not None and bottom is None:
                self.warn(
                    f"bottom:{step.labware}",
                    f"cannot check well-bottom crash for {step.labware}: its "
                    f"z.bottom is unmeasured",
                )

    def _check_keepout(self, step: Step, k: KeepOut, z: float) -> None:
        # The exemption is carried on the individual step, not inferred from
        # the operation name. Only the waypoints that actually engage the
        # ejector are exempt; the approach and retract that surround them are
        # ordinary traverses and get checked like any other.
        if step.detail.get("keepout_exempt") and step.op in k.whitelist:
            return
        if step.tip:
            self.fail(
                step,
                f"Y motion at X {step.frm[0]} is inside keep-out band {k.id} "
                f"X[{k.x_range[0]}, {k.x_range[1]}] WITH A TIP FITTED. The safe-Z "
                f"limit ({k.safe_when_z_below}) was stated for the bare effector; "
                f"a fitted tip reaches lower and the real limit is unmeasured, so "
                f"any tipped traverse through the band is an error.",
            )
        if not k.z_is_safe(z):
            self.fail(
                step,
                f"Y motion at X {step.frm[0]} is inside keep-out band {k.id} "
                f"X[{k.x_range[0]}, {k.x_range[1]}] at Z {z}. Safe only when "
                f"Z < {k.safe_when_z_below} (i.e. physically above it).",
            )

    def _check_traverse_clearance(self, step: Step) -> None:
        """Does the whole swept segment clear every loaded labware?

        Testing only the destination would miss the common case: a traverse
        that starts over the rack and ends at the reservoir sweeps the whole
        way across it, and labware that is never a move endpoint would never be
        checked at all. _move_xy emits one axis per step, so the swept region
        is the axis-aligned box between frm and to.
        """
        assert step.to is not None and step.frm is not None
        z = step.to[2]
        sx0, sx1 = sorted((step.frm[0], step.to[0]))
        sy0, sy1 = sorted((step.frm[1], step.to[1]))

        for name, p in self.deck.placements.items():
            top = p.labware.z("top")
            if top is None:
                self.warn(
                    f"ztop:{name}",
                    f"cannot check traverse clearance over {name} "
                    f"({p.labware.id}): its z.top rim height is unmeasured",
                )
                continue
            # Prefer the physical outline: the rim extends past the outermost
            # well centres, so well_bounds alone would miss a traverse that
            # clips the edge of the rack.
            bounds = p.footprint_bounds()
            if bounds is None:
                bounds = p.well_bounds()
                self.warn(
                    f"footprint:{name}",
                    f"{name} ({p.labware.id}) has no measured footprint, so the "
                    f"traverse-clearance check uses outermost well centres and "
                    f"will not catch a path that clips the rim",
                )
            x0, x1, y0, y1 = bounds
            overlaps = not (sx1 < x0 or sx0 > x1 or sy1 < y0 or sy0 > y1)
            if overlaps and z > top - 1e-6:
                self.fail(
                    step,
                    f"the path from {step.frm[:2]} to {step.to[:2]} sweeps over "
                    f"{name} at Z {z}, which is at or below its rim {top}",
                )

    def _check_actuator(self, step: Step) -> None:
        pos = step.pos_after
        if pos is None:
            return
        eff = self.effector
        if isinstance(eff, Pipette):
            lo = eff.pos_min + eff.stroke_margin
            if pos < lo - 1e-9:
                self.fail(
                    step,
                    f"plunger POS {pos} is below the floor {lo} "
                    f"(pos_min {eff.pos_min} + stroke_margin {eff.stroke_margin})",
                )
            if pos > eff.blowout_pos + 1e-9:
                self.fail(
                    step,
                    f"plunger POS {pos} is past blowout {eff.blowout_pos}",
                )

    # ---- tip box ----------------------------------------------------------

    def record_tip_pickup(self, step: Step, row: int, col: int) -> None:
        self.consumed_tips.add((row, col))

    def check_tip_exit(
        self,
        step: Step,
        row: int,
        col: int,
        crossed: Sequence[Tuple[int, int]],
    ) -> None:
        """After a pickup the X exit must cross only already-consumed positions.

        Passing over an occupied tip position shears the fresh tip straight off
        the nosecone.
        """
        occupied = [rc for rc in crossed if rc not in self.consumed_tips]
        if occupied:
            self.fail(
                step,
                f"the X exit path from tip ({row}, {col}) passes over positions "
                f"{occupied}, which still hold tips. Passing over an occupied "
                f"position shears the tip off. Consume columns starting at the "
                f"exit edge and work inward.",
            )

    # ---- whole-run checks -------------------------------------------------

    def finish(self, held_fluid: float, tip_fitted: bool) -> Report:
        # Idempotent: a caller that inspects the report and then calls
        # finalize() would otherwise get every whole-run line duplicated, and
        # the manifest would record the pile-up.
        if self._finished:
            return self.report
        self._finished = True

        if self._pending_eject_home:
            self.report.errors.append(
                "the run ends on a tip ejection with no G28 after it. "
                "Re-homing after every ejection is mandatory."
            )
        if held_fluid > 1e-9:
            self.report.warnings.append(
                f"the run ends still holding {held_fluid} uL of fluid"
            )
        if tip_fitted and self.effector.can_eject_tip:
            self.report.warnings.append(
                "the run ends with a tip still fitted and never ejects it"
            )
        for line in self.deck.slot_seating_report():
            self.report.warnings.append("slot seating: " + line)
        for line in self.deck.overlap_report():
            if line.startswith("OVERLAP"):
                self.report.errors.append(line)
            else:
                self.report.warnings.append(line)
        return self.report


# --------------------------------------------------------------------------
# toolpath plot
# --------------------------------------------------------------------------

def plot_path(report: Report, out_path: str, title: str = "toolpath") -> Optional[str]:
    """XY toolpath coloured by Z, plus a Z-vs-step trace. Needs matplotlib."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None

    if not report.path:
        return None

    xs = [p[0] for p in report.path]
    ys = [p[1] for p in report.path]
    zs = [p[2] for p in report.path]

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 11),
                                   gridspec_kw={"height_ratios": [3, 1]})

    ax1.plot(xs, ys, "-", color="0.75", linewidth=0.7, zorder=1)
    sc = ax1.scatter(xs, ys, c=zs, cmap="viridis", s=14, zorder=2)
    ax1.set_xlabel("X (mm)")
    ax1.set_ylabel("Y (mm)")
    ax1.set_title(f"{title} - XY path, colour = Z (larger Z is LOWER)")
    ax1.set_aspect("equal", adjustable="box")
    fig.colorbar(sc, ax=ax1, label="Z (mm, +Z down)")

    ax2.plot(range(len(zs)), zs, linewidth=0.9)
    ax2.invert_yaxis()  # so "down" looks down
    ax2.set_xlabel("step")
    ax2.set_ylabel("Z (mm)")
    ax2.set_title("Z vs step (axis inverted: lower on the plot is lower in space)")

    fig.tight_layout()
    fig.savefig(out_path, dpi=130)
    plt.close(fig)
    return out_path
