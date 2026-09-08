"""The machine: one object a protocol drives.

A protocol script should read like the procedure, not like coordinates::

    m = Machine(effector="p300", name="300ulPipetteAccuracy")
    m.startup()
    m.get_tip()
    m.prewet()
    m.aspirate_uL(150.0)
    m.dispense("tubes", row=0, col=0)
    m.eject_tip()
    m.park()
    m.finalize()

Every constant comes from config. Volumes are microlitres; the millimetre
conversion happens inside the pipette and nowhere else. Nothing is written to
disk until :meth:`finalize` has run the simulator clean.
"""

from __future__ import annotations

import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from gcodepy.gcode import Gcode

from . import config
from .deck import Deck
from .effector import Effector, LabError, Pipette, load as load_effector
from .labware import Placement, well_name
from .simulate import Report, Simulator, Step, plot_path


class ProtocolError(LabError):
    """The protocol asked for something the machine cannot legally do."""


class Machine:
    def __init__(
        self,
        effector: str,
        name: str,
        layout: str = "layout.json",
        strict: bool = True,
        temp_log: bool = True,
    ) -> None:
        self.name = name
        self.deck = Deck.load(layout)
        self.effector: Effector = load_effector(effector)
        self.sim = Simulator(self.deck, self.effector, strict=strict)
        self.temp_log = temp_log

        self.g = Gcode(
            None,  # buffered: nothing reaches disk until finalize()
            home_position=(self.deck.x.home, self.deck.y.home, self.deck.z.home),
        )

        # ---- machine state -------------------------------------------------
        self.homed = False
        self.tip_fitted = False
        self.fluid_uL = 0.0
        self.pos: Optional[float] = None  # plunger POS
        self.current_tip: Optional[Tuple[int, int]] = None
        self._keepout_exempt = False
        self._last_aspirate: Optional[Dict[str, Any]] = None

        # ---- records -------------------------------------------------------
        self.steps: List[Step] = []
        self.dispenses: List[Dict[str, Any]] = []
        self.toggles: Dict[str, Any] = {"temp_log": temp_log}
        self.notes: List[str] = []
        self._n = 0

        for q in self.deck.open_questions:
            self.sim.warn(f"open_question:{q[:40]}", f"layout open question: {q}")

        # An unresolved conflict recorded in an effector config is not
        # documentation, it is an open crash risk. Say so on every run.
        conflict = self.effector.spec.get("z_conflict")
        if conflict:
            self.sim.warn(
                f"z_conflict:{self.effector.id}",
                f"{self.effector.id} has an UNRESOLVED Z conflict: "
                f"{conflict.get('description', '')} "
                f"Action: {conflict.get('action', 'resolve it')}",
            )

    # =====================================================================
    # low-level recording
    # =====================================================================

    def xyz(self) -> Tuple[float, float, float]:
        return (self.g.get_x(), self.g.get_y(), self.g.get_z())

    def _record(self, **kw: Any) -> Step:
        detail = kw.pop("detail", None) or {}
        if self._keepout_exempt:
            detail = {**detail, "keepout_exempt": True}
        step = Step(i=self._n, tip=self.tip_fitted, fluid_uL=self.fluid_uL,
                    gline=self.g.line_count(), detail=detail, **kw)
        self._n += 1
        self.steps.append(step)
        self.sim.check(step)
        return step

    @contextmanager
    def _engaging_ejector(self):
        """Mark the enclosed steps as the ejector engagement itself.

        Only these steps are exempt from the keep-out band. Marking by step
        rather than by operation name is what stops the approach and retract
        moves - which are ordinary traverses - from inheriting the exemption.
        """
        self._keepout_exempt = True
        try:
            yield
        finally:
            self._keepout_exempt = False

    def _raw(self, line: str, op: str, **detail: Any) -> Step:
        self.g.raw(line)
        return self._record(op=op, kind="macro", detail={"line": line, **detail})

    def _require_homed(self, what: str) -> None:
        if not self.homed:
            raise ProtocolError(f"{what} before the machine was homed. Call startup().")

    def _pipette(self) -> Pipette:
        if not isinstance(self.effector, Pipette):
            raise ProtocolError(
                f"{self.effector.id} is a {self.effector.kind}, not a pipette; it "
                f"cannot aspirate or dispense"
            )
        return self.effector

    # ---- motion ----------------------------------------------------------

    def _move_z(self, z: float, feedrate: int, op: str,
                labware: Optional[str] = None) -> None:
        frm = self.xyz()
        # Same rounding as gcodepy's move_z_absolute, so a Step is never
        # recorded for a delta too small to emit a line.
        if abs(round(z - frm[2], 6)) <= 1e-6:
            return
        self.g.move_z_absolute(z, feedrate=feedrate)
        self._record(op=op, kind="move", frm=frm, to=self.xyz(), axes="Z",
                     feedrate=feedrate, labware=labware)

    def _move_xy(self, x: float, y: float, feedrate: int, op: str,
                 labware: Optional[str] = None, order: str = "XY") -> None:
        """One axis at a time, X first. Each axis is a separately checked step.

        X-first is not a preference. The tip box requires it: after a pickup the
        effector must leave the box laterally before any Y motion.
        """
        for axis in order:
            frm = self.xyz()
            if axis == "X":
                if abs(x - frm[0]) <= 1e-6:
                    continue
                self.g.travel_absolute((x, frm[1], frm[2]), feedrate=feedrate)
            else:
                if abs(y - frm[1]) <= 1e-6:
                    continue
                self.g.travel_absolute((frm[0], y, frm[2]), feedrate=feedrate)
            self._record(op=op, kind="move", frm=frm, to=self.xyz(), axes=axis,
                         feedrate=feedrate, labware=labware)

    def goto(self, x: float, y: float, op: str = "goto",
             feedrate: Optional[int] = None,
             prefer: Optional[str] = None) -> None:
        """Lift to travel height, then traverse. The only public way to move XY.

        `prefer` is an axis-order preference, "XY" or "YX". A destination
        labware can ask for one via its `approach_order`; the keep-out router
        still overrides it when geometry demands, and says so when it does.
        """
        self._require_homed("goto()")
        f = feedrate or self.deck.feedrate("travel")
        self._move_z(self.deck.z_travel, f, op)
        self._route_xy(x, y, f, op, prefer=prefer)

    def goto_placement(self, placement: str, row: int, col: int,
                       op: str = "goto") -> Tuple[float, float]:
        """Traverse to a well, honouring that labware's approach order."""
        p = self.deck.placement(placement)
        x, y = p.xy(row, col)
        self.goto(x, y, op=op, prefer=p.labware.approach_order)
        return (x, y)

    def _route_xy(self, x: float, y: float, feedrate: int, op: str,
                  labware: Optional[str] = None,
                  prefer: Optional[str] = None) -> None:
        """Traverse to XY without ever moving Y inside a keep-out band.

        X-first is the default: it is what the tip box requires, and a pure X
        move through a band is harmless because the band only forbids Y motion.

        A destination labware can prefer "YX" instead. Both tube holders do:
        their caps make Y motion across the holder difficult, so the effector
        has to reach its Y while still clear in X and then come in along X.

        Either way the keep-out rule wins, because it is a hard collision and
        the preference is an ease-of-motion choice. X-first puts the Y leg at
        the TARGET X; Y-first puts it at the CURRENT X. Whichever end is clear
        of every band decides; if both are blocked, dogleg through a clear X.
        """
        cur_x, cur_y = self.g.get_x(), self.g.get_y()
        prefer = (prefer or "XY").upper()
        if prefer not in ("XY", "YX"):
            raise ProtocolError(f"prefer must be 'XY' or 'YX', got {prefer!r}")

        if abs(y - cur_y) <= 1e-6:
            self._move_xy(x, y, feedrate, op, labware, order="X")
            return

        dst_blocked = bool(self.deck.keepouts_hit_by_y_move(x))
        src_blocked = bool(self.deck.keepouts_hit_by_y_move(cur_x))

        # The preferred order first, if the band it would put the Y leg in is clear.
        if prefer == "YX" and not src_blocked:
            self._move_xy(x, y, feedrate, op, labware, order="YX")
            return
        if prefer == "XY" and not dst_blocked:
            self._move_xy(x, y, feedrate, op, labware, order="XY")
            return

        if not dst_blocked:
            self.sim.warn(
                f"order_override:{op}",
                f"{op}: {labware or 'this move'} prefers {prefer} but the current X "
                f"{cur_x} is inside a keep-out band, so the Y leg was moved to the "
                f"target X {x} instead.",
            )
            self._move_xy(x, y, feedrate, op, labware, order="XY")
            return
        if not src_blocked:
            if prefer == "YX":
                self._move_xy(x, y, feedrate, op, labware, order="YX")
                return
            self.sim.warn(
                f"order_override:{op}",
                f"{op}: prefers XY but the target X {x} is inside a keep-out band, "
                f"so the Y leg was done first at X {cur_x}.",
            )
            self._move_xy(x, y, feedrate, op, labware, order="YX")
            return

        via = self._nearest_clear_x(x)
        if via is None:
            raise ProtocolError(
                f"cannot reach ({x}, {y}): both the current X {cur_x} and the "
                f"target X {x} sit inside a keep-out band, and no clear X was "
                f"found to dogleg through"
            )
        self.sim.warn(
            f"dogleg:{round(via, 1)}",
            f"routing Y motion via X {via} to stay out of the keep-out band "
            f"(both {cur_x} and {x} are inside it)",
        )
        self._move_xy(via, cur_y, feedrate, op, labware, order="X")
        self._move_xy(via, y, feedrate, op, labware, order="Y")
        self._move_xy(x, y, feedrate, op, labware, order="X")

    def _nearest_clear_x(self, target: float, pad: float = 2.0) -> Optional[float]:
        """Closest X to `target` that no keep-out band forbids Y motion at."""
        best: Optional[float] = None
        for k in self.deck.keepouts:
            if k.forbids != "y_motion" or not k.contains_x(target):
                continue
            for cand in (k.x_range[0] - pad, k.x_range[1] + pad):
                if not self.deck.x.contains(cand):
                    continue
                if self.deck.keepouts_hit_by_y_move(cand):
                    continue
                if best is None or abs(cand - target) < abs(best - target):
                    best = cand
        return best

    def goto_station(self, name: str, op: Optional[str] = None) -> None:
        st = self.deck.station(name)
        self.goto(st.xy[0], st.xy[1], op=op or f"goto:{name}")

    def goto_well(self, placement: str, row: int, col: int,
                  op: str = "goto") -> Tuple[float, float]:
        """Alias for goto_placement.

        It used to be a near-duplicate that omitted the labware's
        approach_order, so it silently ignored the Y-first rule both tube
        holders need. Two ways to reach a well is one too many.
        """
        return self.goto_placement(placement, row, col, op=op)

    # ---- actuator --------------------------------------------------------

    def actuator(self, pos: float, settle: float = 0.0, op: str = "actuator") -> float:
        """Command the plunger to an absolute POS. Validated, tracked, logged."""
        eff = self.effector
        decimals = int(eff.spec.get("actuator", {}).get("decimals", 3))
        pos = round(pos, decimals)
        if isinstance(eff, Pipette):
            eff.check_pos(pos, what=op)
        before = self.pos
        macro = self.deck.macro("actuator_move")
        # Fixed width, quantized exactly once. Rounding into a variable and then
        # subtracting from it compounds two quantizations; 2 dp would inject
        # 0.63% error at the 10% ISO point, and 4 dp is below the actuator step.
        self.g.raw(f"{macro}{pos:.{decimals}f}")
        self.pos = pos
        self._record(op=op, kind="actuator", pos_before=before, pos_after=pos)
        if settle:
            self.dwell(settle, op=op)
        return pos

    def dwell(self, seconds: float, op: str = "dwell") -> None:
        """Guarded: a zero or negative settle is a no-op, not an exception."""
        if seconds and seconds > 0:
            self.g.dwell(seconds)
            self._record(op=op, kind="dwell", detail={"seconds": seconds})

    def wait(self) -> None:
        self.g.wait_finish()

    def log_temps(self, tag: str) -> None:
        if not self.temp_log:
            return
        macro = self.deck.macro("log_temps")
        self._raw(f"{macro}{tag}", op="log_temps", tag=tag)

    def note(self, text: str) -> None:
        """A comment in the gcode and a line in the manifest."""
        self.g.raw(f"; {text}")
        self.notes.append(text)
        self._record(op="note", kind="note", detail={"text": text})

    # =====================================================================
    # operations
    # =====================================================================

    def home(self, op: str = "home") -> None:
        self.g.home("Z")
        self._record(op=op, kind="home", to=self.xyz(), detail={"axes": "Z"})
        self.g.home("XY")
        self._record(op=op, kind="home", to=self.xyz(), detail={"axes": "XY"})
        macro = self.deck.macro("home_actuator")
        self._raw(macro, op=op)
        self.homed = True

    def startup(self) -> None:
        """Home everything, reference the plunger at blow-out, lift to travel."""
        self.home(op="startup")
        eff = self.effector
        if isinstance(eff, Pipette):
            # Bottomed first: no vacuum while pressing onto a tip.
            self.actuator(eff.blowout_pos, op="startup")
        self._move_z(self.deck.z_travel, self.deck.feedrate("travel"), "startup")
        self.wait()

    def park(self) -> None:
        self.goto_station("park", op="park")
        st = self.deck.station("park")
        z = self.deck.z_home if st.z is None else st.z
        self._move_z(z, self.deck.feedrate("travel"), "park")
        self.wait()

    # ---- tips ------------------------------------------------------------

    def get_tip(self, row: int = 0, col: Optional[int] = None,
                placement: str = "tips") -> Tuple[int, int]:
        """Press the nosecone into a tip and lift it out, then exit in X.

        `col` defaults to the column at the configured exit edge, which is the
        only column whose X exit path is guaranteed clear on a full rack.
        """
        self._require_homed("get_tip()")
        eff = self.effector
        if not eff.needs_tip:
            raise ProtocolError(
                f"{eff.id} does not use tips (tips.required is false in its "
                f"config), so get_tip() is meaningless"
            )
        if self.tip_fitted:
            raise ProtocolError("a tip is already fitted; eject it before picking another")

        p = self._tip_placement(placement)
        lid = p.labware.id
        exit_edge, exit_col = self._tip_exit(p)
        if col is None:
            col = self._next_free_tip_col(p, row, exit_edge)
        p.check_well(row, col)
        if (row, col) in self.sim.consumed_tips:
            raise ProtocolError(
                f"tip ({row}, {col}) in {p.name} has already been used this run. "
                f"Pressing into an empty rack hole drives the bare nosecone down "
                f"to the pick depth with nothing to stop it."
            )

        x, y = p.xy(row, col)
        press_feed = eff.feedrate("tip_press") or self.deck.feedrate("tip_press")

        self.goto(x, y, op="get_tip", prefer=p.labware.approach_order)
        self.wait()
        self._move_z(eff.z_for(lid, "approach") if self._has_z(lid, "approach")
                     else self.deck.z_travel,
                     press_feed, "get_tip", labware=lid)
        self._move_z(eff.z_for(lid, "pick"), press_feed, "get_tip", labware=lid)
        self.wait()
        self.dwell(1, op="get_tip")

        self.tip_fitted = True
        self.current_tip = (row, col)
        self.sim.record_tip_pickup(self.steps[-1], row, col)

        # Lift clear, then leave the box in X BEFORE any Y motion.
        self._move_z(self.deck.z_travel, press_feed, "get_tip", labware=lid)
        self.wait()
        self._exit_tip_box(p, row, col, exit_edge)
        return (row, col)

    def _tip_placement(self, placement: str) -> Placement:
        """The rack this effector's own config says it takes tips from.

        Routing by placement name alone would send a second effector that
        declares a different rack to the wrong one, and the error would then
        tell the operator to add the wrong rack to its Z table.
        """
        want = self.effector.tip_labware
        try:
            p = self.deck.placement(placement)
        except Exception:
            if not want:
                raise
            return self.deck.placement_for_labware(want)
        if want and p.labware.id != want:
            raise ProtocolError(
                f"{self.effector.id} takes tips from {want!r}, but placement "
                f"{placement!r} holds {p.labware.id!r}. Name the right placement, "
                f"or change tips.labware in config/effectors/{self.effector.id}.json."
            )
        return p

    def _next_free_tip_col(self, p: Placement, row: int, exit_edge: str) -> int:
        """First unused column, working inward from the exit edge.

        That order is what keeps the X exit path clear: the effector leaves the
        box across every column between the one it picked and the edge, so the
        edge column has to go first and the box empties toward the middle.
        """
        xs = p.col_x_values()
        cols = sorted(range(len(xs)), key=lambda c: xs[c],
                      reverse=(exit_edge == "high_x"))
        for c in cols:
            if (row, c) not in self.sim.consumed_tips:
                return c
        raise ProtocolError(
            f"every tip in row {row} of {p.name} has been used this run. "
            f"Reload the rack, or pick a different row."
        )

    def _has_z(self, labware_id: str, key: str) -> bool:
        entry = self.effector.spec.get("z_for_labware", {}).get(labware_id, {})
        return entry.get(key) is not None

    def _tip_exit(self, p: Placement) -> Tuple[str, int]:
        """Which X edge the effector leaves by, and the column at that edge."""
        spec = p.labware.spec.get("consume_order", {})
        edge = spec.get("exit_edge", "unconfirmed")
        xs = p.col_x_values()
        lo_col = min(range(len(xs)), key=lambda c: xs[c])
        hi_col = max(range(len(xs)), key=lambda c: xs[c])
        if edge in ("low_x", "min_x"):
            return "low_x", lo_col
        if edge in ("high_x", "max_x"):
            return "high_x", hi_col
        # Unconfirmed: pick the low-X edge and say so. The exit-path check below
        # is what actually keeps this safe - it fails loudly if the path is
        # occupied, whichever edge turns out to be right.
        self.sim.warn(
            "tip_exit_edge",
            f"{p.labware.id}.consume_order.exit_edge is {edge!r}; assuming the "
            f"low-X edge (column {lo_col}, X {xs[lo_col]}). Confirm which edge "
            f"the effector can actually leave by - if it is the high-X edge, "
            f"every tip pickup in this run exits the wrong way.",
        )
        return "low_x", lo_col

    def _exit_tip_box(self, p: Placement, row: int, col: int, edge: str) -> None:
        # One column pitch beyond the outermost well centre. That clears every
        # tip POSITION (the nearest is a full pitch away) without demanding the
        # rack's physical outline be known, which it is not. This move is not
        # redundant with the next goto: it guarantees the effector is out of the
        # box before _route_xy makes any decision that could put Y first.
        xs = p.col_x_values()
        pitch = abs(p.col_vec[0]) or 9.0
        if edge == "low_x":
            crossed = [(row, c) for c in range(len(xs)) if xs[c] < xs[col]]
            target_x = min(xs) - pitch
        else:
            crossed = [(row, c) for c in range(len(xs)) if xs[c] > xs[col]]
            target_x = max(xs) + pitch

        frm = self.xyz()
        step = Step(i=self._n, op="get_tip", kind="note", frm=frm, to=frm,
                    tip=self.tip_fitted, fluid_uL=self.fluid_uL,
                    detail={"exit_edge": edge, "crossed": crossed})
        self._n += 1
        self.steps.append(step)
        self.sim.check_tip_exit(step, row, col, crossed)

        target_x = max(self.deck.x.min, min(self.deck.x.max, target_x))
        self._move_xy(target_x, self.g.get_y(), self.deck.feedrate("travel"),
                      "get_tip", labware=p.labware.id, order="X")
        self.wait()

    def eject_tip(self) -> None:
        """Hook the tip collar on the ejector and pull UP to strip it.

        This is the highest collision-risk routine on the machine and it has
        never been validated on hardware. It re-homes afterwards because the
        strip force can skip steps.
        """
        self._require_homed("eject_tip()")
        eff = self.effector
        if not eff.can_eject_tip:
            raise ProtocolError(f"{eff.id} does not support tip ejection")
        if not self.tip_fitted:
            raise ProtocolError("no tip is fitted; nothing to eject")

        st = self.deck.station("tip_eject").spec
        f = self.deck.feedrate("travel")
        slow = self.deck.feedrate("tip_press")
        lane_x = float(st["lane_x"])

        if not st.get("validated", False):
            self.sim.warn(
                "eject_unvalidated",
                "the tip ejection routine has never been validated on hardware "
                "(deck.stations.tip_eject.validated is false). Dry-run it, then "
                "step it through manually at reduced feedrate before trusting it.",
            )

        # 1-2: approach at travel height. Routed, not hand-ordered: the lane X
        #      is itself inside the keep-out band, so the Y leg has to happen
        #      at some other X. _route_xy picks Y-first (or doglegs) on the
        #      geometry, which is exactly the required "Y first, then X".
        self._move_z(self.deck.z_travel, f, "eject_tip")
        self._route_xy(lane_x, float(st["approach_y"]), f, "eject_tip")
        self.wait()

        # 3: descend below the hook.
        self._move_z(float(st["z_below_hook"]), slow, "eject_tip")
        self.wait()

        # 4-7: the engagement itself. These waypoints are deliberately inside
        #      the keep-out band, at the one X and depth where that is the
        #      point - so they, and only they, carry the exemption.
        with self._engaging_ejector():
            # 4: lateral, bringing the collar under the hook.
            self._move_xy(lane_x, float(st["engage_y"]), slow, "eject_tip", order="Y")
            self.wait()

            # 5: UP 8 mm. +Z is down, so a smaller Z is the strip.
            self._move_z(float(st["z_strip"]), slow, "eject_tip")
            self.wait()
            self.tip_fitted = False
            self.current_tip = None

            # 6-7: back down clear of the hook, then retract out of the lane.
            self._move_z(float(st["z_below_hook"]), slow, "eject_tip")
            self._move_xy(lane_x, float(st["retract_y"]), slow, "eject_tip",
                          order="Y")

        # 8: lift. The "ejection complete" marker goes AFTER the lift, so the
        # lift itself is not what trips the must-re-home check.
        self._move_z(self.deck.z_travel, f, "eject_tip")
        self.wait()
        self._record(op="eject_tip", kind="note", frm=self.xyz(),
                     to=self.xyz(), detail={"final": True})

        # 9: mandatory re-home.
        if st.get("rehome_after", True):
            self.g.home("ALL")
            self._record(op="eject_tip", kind="home", to=self.xyz(),
                         detail={"axes": "ALL", "reason": "post-ejection"})
            self.wait()

    # ---- liquid ----------------------------------------------------------

    def goto_reservoir(self, station: str = "water") -> None:
        self.goto_station(station, op="reservoir")

    def prewet(self, cycles: Optional[int] = None, station: str = "water") -> None:
        """Wet the tip wall so the first real aspiration is not wetting dry plastic."""
        eff = self._pipette()
        if not eff.prewet_enabled:
            raise ProtocolError(
                f"{eff.id} has prewet disabled in its config; enable it there "
                f"rather than calling prewet() anyway"
            )
        n = cycles if cycles is not None else eff.prewet_cycles
        if n < eff.prewet_min_cycles:
            raise ProtocolError(
                f"{eff.id}: prewet needs at least {eff.prewet_min_cycles} cycles, "
                f"got {n}"
            )
        lid = self.deck.placement_for_labware(
            self._station_labware(station)).labware.id
        f = self.deck.feedrate("travel")

        self.goto_station(station, op="prewet")
        self.wait()
        self.actuator(eff.blowout_pos, settle=0.5, op="prewet")
        self._move_z(eff.z_for(lid, "work"), f, "prewet", labware=lid)
        self.wait()
        for _ in range(n):
            self.actuator(eff.prewet_asp_pos, settle=1, op="prewet")
            self.actuator(eff.blowout_pos, settle=1, op="prewet")
        self._move_z(self.deck.z_travel, f, "prewet", labware=lid)
        self.wait()
        self.toggles["prewet_cycles"] = n

    def _station_labware(self, station: str) -> str:
        for p in self.deck.placements.values():
            if p.station == station:
                return p.labware.id
        raise ProtocolError(
            f"no placement in this layout sits at station {station!r}; the "
            f"library cannot know what is there"
        )

    def _require_empty_tip(self, what: str) -> "Pipette":
        """Shared preconditions for anything that draws liquid."""
        eff = self._pipette()
        if eff.needs_tip and not self.tip_fitted:
            raise ProtocolError(
                f"{eff.id} requires a tip and none is fitted; call get_tip() first"
            )
        if self.fluid_uL > 1e-9:
            raise ProtocolError(
                f"{what} while already holding {self.fluid_uL} uL. Every "
                f"destination gets its own independent aspiration; dispense "
                f"before drawing again."
            )
        return eff

    def _aspirate_at(self, x: float, y: float, labware_id: str, volume_uL: float,
                     op: str = "aspirate", prefer: Optional[str] = None,
                     z_key: str = "work") -> float:
        """The aspiration sequence itself, shared by every public form.

        Every ordering guarantee lives here, which is why the station and well
        forms delegate rather than each repeating it: the air gap is armed in
        air BEFORE descending, the whole stroke is validated before a single
        line is emitted, and the anti-drip backoff happens only after the tip
        is clear of the liquid.
        """
        eff = self._pipette()

        # Validate the WHOLE stroke, including the backoff, before a single
        # line is emitted. Discovering at the backoff that the plunger has run
        # out of travel would leave a half-finished aspiration in the buffer
        # with the state saying it completed.
        d = eff.displacement_mm(volume_uL)      # raises if outside the window
        asp = eff.aspirate_pos(volume_uL)
        backoff = eff.backoff_pos(volume_uL)
        z = eff.z_for(labware_id, z_key)        # raises if never measured
        f = self.deck.feedrate("travel")

        self._last_aspirate = None              # never let a stale one survive
        self.goto(x, y, op=op, prefer=prefer)
        self.wait()
        self.actuator(eff.gap_pos, settle=eff.settle_s, op=op)  # gap armed in air
        self._move_z(z, f, op, labware=labware_id)
        self.wait()
        self.actuator(asp, settle=eff.settle_s, op=op)          # draw, settle
        self.fluid_uL = volume_uL
        self._move_z(self.deck.z_travel, f, op, labware=labware_id)
        if eff.post_asp_backoff:
            self.actuator(backoff, op=op)                       # anti-drip
        self.wait()

        self._last_aspirate = {
            "volume_uL": volume_uL,
            "displacement_mm": d,
            "aspirate_pos": asp,
            "backoff_pos": backoff,
            "source": labware_id,
        }
        return asp

    def aspirate_uL(self, volume_uL: float, station: str = "water") -> float:
        """One independent aspiration from a STATION. Microlitres, always.

        Arms the air gap in air, descends, draws, withdraws, then backs off in
        air to pull the drop up. Returns the plunger POS used.
        """
        self._require_empty_tip("aspirate_uL()")
        lid = self._station_labware(station)
        st = self.deck.station(station)
        return self._aspirate_at(st.xy[0], st.xy[1], lid, volume_uL, op="aspirate")

    def aspirate_from(self, placement: str, row: int, col: int,
                      volume_uL: float) -> float:
        """One independent aspiration from a WELL. Microlitres, always.

        The station form cannot do this: it reverse-maps a station name to
        labware, and a slot-mounted rack has no station. Returns the plunger
        POS used.
        """
        self._require_empty_tip("aspirate_from()")
        p = self.deck.placement(placement)
        x, y = p.xy(row, col)                   # raises if out of range
        return self._aspirate_at(x, y, p.labware.id, volume_uL, op="aspirate",
                                 prefer=p.labware.approach_order)

    def mix(self, placement: str, row: int, col: int, cycles: int,
            volume_uL: float, z_key: str = "bottom") -> None:
        """Homogenise a well by cycling the plunger at its BOTTOM.

        Descends deeper than a dispense would - `z_key` defaults to "bottom" -
        so the stroke actually turns the contents over instead of stirring the
        surface. Starts and ends bottomed, so the tip holds no liquid between
        cycles and never carries any away.
        """
        eff = self._require_empty_tip("mix()")
        if cycles < 1:
            raise ProtocolError(f"mix needs at least 1 cycle, got {cycles}")

        # Validate the stroke before emitting. The plunger draws UP from
        # blowout, so the top of the stroke is the position to check.
        d = eff.displacement_mm(volume_uL)      # raises if outside the window
        top = round(eff.blowout_pos - d, eff.decimals)
        eff.check_pos(top, what=f"mixing {volume_uL} uL")

        p = self.deck.placement(placement)
        lid = p.labware.id
        x, y = p.xy(row, col)
        z = eff.z_for(lid, z_key)               # raises if never measured
        f = self.deck.feedrate("travel")

        self.goto(x, y, op="mix", prefer=p.labware.approach_order)
        self.wait()
        self.actuator(eff.blowout_pos, settle=0.5, op="mix")  # bottomed, in air
        self._move_z(z, f, "mix", labware=lid)
        self.wait()
        for _ in range(cycles):
            self.actuator(top, settle=eff.settle_s, op="mix")            # draw up
            self.actuator(eff.blowout_pos, settle=eff.settle_s, op="mix")  # push back
        self._move_z(self.deck.z_travel, f, "mix", labware=lid)
        self.wait()

        self.toggles["mix_cycles"] = cycles
        self.toggles["mix_volume_uL"] = volume_uL

    def dispense(self, placement: str, row: int, col: int,
                 wall_wipe: Optional[bool] = None) -> Dict[str, Any]:
        """Full dispense to blow-out, then optionally wipe the droplet off."""
        eff = self._pipette()
        if self.fluid_uL <= 1e-9:
            raise ProtocolError(
                "dispense() with nothing aspirated. Call aspirate_uL() first."
            )
        p = self.deck.placement(placement)
        lid = p.labware.id
        x, y = p.xy(row, col)
        f = self.deck.feedrate("travel")

        # Settle the wipe question BEFORE any motion: raising after the
        # blow-out would leave a complete dispense in the buffer with no
        # manifest row to account for it.
        wipe_cfg = p.labware.wipe
        do_wipe = wall_wipe if wall_wipe is not None else bool(wipe_cfg)
        if do_wipe and not wipe_cfg:
            raise ProtocolError(
                f"wall wipe requested but {lid} has no wipe block in "
                f"labware.json. Add one, or pass wall_wipe=False."
            )

        self.goto(x, y, op="dispense", prefer=p.labware.approach_order)
        self._move_z(eff.z_for(lid, "work"), f, "dispense", labware=lid)
        self.wait()
        self.actuator(eff.blowout_pos, settle=eff.settle_s, op="dispense")
        dispensed = self.fluid_uL
        self.fluid_uL = 0.0

        if do_wipe:
            self._wall_wipe(p, x, y)
        self._move_z(self.deck.z_travel, f, "dispense", labware=lid)
        self.wait()

        asp_record = self._last_aspirate or {}
        self._last_aspirate = None  # one aspiration feeds exactly one dispense
        rec = {
            "order_index": len(self.dispenses),
            "placement": placement,
            "labware": lid,
            "slot": p.slot,
            "well": well_name(row, col),
            "row": row,
            "col": col,
            "x": x,
            "y": y,
            "z": eff.z_for(lid, "work"),
            "target_volume_uL": dispensed,
            **asp_record,
            "mass_dry_mg": None,
            "mass_wet_mg": None,
            "temp_C": None,
        }
        self.dispenses.append(rec)
        return rec

    def _wall_wipe(self, p: Placement, x: float, y: float) -> None:
        """Step to the inner wall, drag the droplet UP, then pull back off.

        Private, and it asserts its starting pose. This is the one place that
        moves XY below travel height, so calling it from anywhere but the
        bottom of a dispense would traverse the deck at well depth with the
        travel-height check suppressed.
        """
        cfg = p.labware.wipe or {}
        dx = float(cfg["dx"])
        dz = float(cfg["dz"])
        feed = int(cfg.get("feed", self.deck.feedrate("wipe")))
        lid = p.labware.id
        z_work = self.effector.z_for(lid, "work")

        here = self.xyz()
        if (abs(here[0] - x) > 1e-6 or abs(here[1] - y) > 1e-6
                or abs(here[2] - z_work) > 1e-6):
            raise ProtocolError(
                f"wall wipe must start at the dispense pose "
                f"({x}, {y}, {z_work}); the effector is at {here}"
            )

        self._move_xy(x + dx, y, feed, "wall_wipe", labware=lid, order="X")
        self._move_z(z_work - dz, feed, "wall_wipe", labware=lid)  # -dz is UP
        self._move_xy(x, y, feed, "wall_wipe", labware=lid, order="X")

    # =====================================================================
    # finalize
    # =====================================================================

    def simulate(self) -> Report:
        return self.sim.finish(held_fluid=self.fluid_uL, tip_fitted=self.tip_fitted)

    def finalize(self, outdir: str = ".", plot: bool = True) -> Dict[str, Any]:
        """Run the whole-run checks, then write gcode + manifest. Or refuse to."""
        report = self.simulate()
        out = Path(outdir).resolve()
        out.mkdir(parents=True, exist_ok=True)

        if not report.ok:
            raise ProtocolError(
                "simulation failed; refusing to write gcode.\n" + report.text()
            )

        gpath = out / f"{self.name}.gcode"
        self.g.dump(str(gpath))

        from .logbook import write_manifest
        mpath = write_manifest(self, report, out)

        ppath = None
        if plot:
            ppath = plot_path(report, str(out / f"{self.name}.toolpath.png"),
                              title=self.name)

        return {
            "gcode": str(gpath),
            "manifest": str(mpath),
            "plot": ppath,
            "report": report,
            "lines": self.g.line_count(),
            "dispenses": len(self.dispenses),
        }

    # ---- provenance ------------------------------------------------------

    def config_hashes(self) -> Dict[str, str]:
        return {
            "deck.json": self.deck.deck_cfg.sha256,
            "labware.json": self.deck.labware_cfg.sha256,
            Path(self.deck.layout_cfg.path).name: self.deck.layout_cfg.sha256,
            f"effectors/{self.effector.id}.json": self.effector.sha256,
        }

    def git_commit(self) -> Optional[str]:
        try:
            r = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(config.repo_root()),
                capture_output=True, text=True, timeout=10,
            )
            return r.stdout.strip() or None
        except Exception:
            return None

    def git_dirty(self) -> Optional[bool]:
        try:
            r = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(config.repo_root()),
                capture_output=True, text=True, timeout=10,
            )
            return bool(r.stdout.strip())
        except Exception:
            return None

    def uncalibrated(self) -> List[str]:
        return self.deck.uncalibrated() + self.effector.uncalibrated()
