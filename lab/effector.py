"""End effectors. One JSON file per effector, one class per `kind`.

Adding a new end effector is a new file in ``config/effectors/`` plus, if its
`kind` is new, a class here. Nothing else in the library changes.

Volumes are always requested in microlitres. The conversion to plunger
millimetres happens here and nowhere else, from stored coefficients, so a
displacement can never drift out of step with the volume it claims to be.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from . import config


class LabError(Exception):
    """Base for every error this library raises.

    A protocol can catch this one class and print the message instead of a
    traceback; the messages are written to tell the operator what to do.
    """


class EffectorError(LabError):
    """Raised for an effector that cannot do what was asked of it."""


class VolumeError(EffectorError):
    """Requested volume is outside what this effector can meter."""


class Effector:
    """Base: identity, config access, and the per-labware Z table."""

    kind = "generic"

    def __init__(self, cfg: config.ConfigFile) -> None:
        self.cfg = cfg
        self.spec: Dict[str, Any] = cfg.data
        self.id: str = self.spec["id"]
        self.display_name: str = self.spec.get("display_name", self.id)
        self.sha256: str = cfg.sha256

    # ---- Z ---------------------------------------------------------------

    def z_for(self, labware_id: str, key: str = "work") -> float:
        """Absolute Z for this effector at this labware.

        Kept per-effector rather than per-labware because a fitted tip shifts
        the reference point: the same tube needs a different commanded Z with a
        300 uL tip on than with a bare nosecone.
        """
        table = self.spec.get("z_for_labware", {})
        entry = table.get(labware_id)
        if entry is None:
            raise EffectorError(
                f"effector {self.id} has no Z entry for labware {labware_id!r}. "
                f"Known: {sorted(table)}. Add it to "
                f"config/effectors/{self.id}.json -> z_for_labware."
            )
        if key not in entry:
            raise EffectorError(
                f"effector {self.id}, labware {labware_id!r}: no Z key {key!r}. "
                f"Known: {sorted(k for k in entry if not k.endswith('note'))}"
            )
        v = entry[key]
        if v is None:
            raise EffectorError(
                f"effector {self.id}, labware {labware_id!r}, Z {key!r} is null - "
                f"it has never been measured. Measure it and put it in the config."
            )
        return float(v)

    def z_is_uncalibrated(self, labware_id: str) -> bool:
        return bool(
            self.spec.get("z_for_labware", {}).get(labware_id, {}).get("uncalibrated")
        )

    # ---- flags -----------------------------------------------------------

    @property
    def needs_tip(self) -> bool:
        return bool(self.spec.get("tips", {}).get("required", False))

    @property
    def tip_labware(self) -> Optional[str]:
        return self.spec.get("tips", {}).get("labware")

    @property
    def can_eject_tip(self) -> bool:
        return bool(self.spec.get("tips", {}).get("eject_supported", False))

    @property
    def is_placeholder(self) -> bool:
        return self.spec.get("status") == "placeholder"

    def uncalibrated(self) -> List[str]:
        return [f"{self.id}.{f}" for f in self.spec.get("uncalibrated", [])]

    def feedrate(self, name: str, default: Optional[int] = None) -> Optional[int]:
        v = self.spec.get("feedrates", {}).get(name, default)
        return None if v is None else int(v)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.id}>"


class Pipette(Effector):
    """Air-displacement pipette: plunger positions and a volume calibration."""

    kind = "pipette"

    def __init__(self, cfg: config.ConfigFile) -> None:
        super().__init__(cfg)
        a = self.spec["actuator"]
        self.blowout_pos: float = float(a["blowout_pos"])
        self.air_gap: float = float(a["air_gap"])
        self.gap_pos: float = float(a["gap_pos"])
        self.post_asp_backoff: float = float(a.get("post_asp_backoff", 0.0))
        self.stroke_margin: float = float(a.get("stroke_margin", 0.0))
        self.pos_min: float = float(a.get("pos_min", 0.0))
        self.settle_s: float = float(a.get("settle_s", 1.0))
        self.decimals: int = int(a.get("decimals", 3))

        c = self.spec["calibration"]
        if c.get("model") != "linear_mm_per_uL":
            raise EffectorError(
                f"{self.id}: unsupported calibration model {c.get('model')!r}"
            )
        self.slope: float = float(c["slope"])
        self.intercept: float = float(c["intercept"])
        self.window: Tuple[float, float] = (
            float(c["window_uL"][0]), float(c["window_uL"][1])
        )

        self.nominal_uL: float = float(self.spec["nominal_uL"])

        if self.gap_pos != self.blowout_pos - self.air_gap:
            raise EffectorError(
                f"{self.id}: gap_pos {self.gap_pos} != blowout_pos "
                f"{self.blowout_pos} - air_gap {self.air_gap}. Fix the config; "
                f"the air gap is not what it claims to be."
            )

    @property
    def equation(self) -> str:
        """The curve as text, DERIVED from the coefficients in use.

        The config carries an `equation` string too, but it is free text that
        nothing cross-checks - exactly the kind of duplicate that drifted from
        its own table in the scripts this replaces. Reports use this one.
        """
        return f"d_mm = {self.slope} * V_uL + {self.intercept}"

    # ---- volume <-> displacement ----------------------------------------

    def displacement_mm(self, volume_uL: float) -> float:
        """Plunger travel for a volume, from the stored calibration only."""
        self.check_volume(volume_uL)
        return round(self.slope * volume_uL + self.intercept, self.decimals)

    def volume_uL(self, displacement_mm: float) -> float:
        """Inverse, for reporting a stroke back as a volume."""
        return round((displacement_mm - self.intercept) / self.slope, 4)

    def aspirate_pos(self, volume_uL: float) -> float:
        """Plunger POS that holds this volume, with the air gap already armed."""
        pos = round(self.gap_pos - self.displacement_mm(volume_uL), self.decimals)
        self.check_pos(pos, what=f"aspirating {volume_uL} uL")
        return pos

    def backoff_pos(self, volume_uL: float) -> float:
        pos = round(self.aspirate_pos(volume_uL) - self.post_asp_backoff, self.decimals)
        self.check_pos(pos, what=f"post-aspirate backoff at {volume_uL} uL")
        return pos

    @property
    def max_volume_uL(self) -> float:
        """Largest volume before the backoff would drive POS under the floor.

        The floor is ``pos_min + stroke_margin``, the same one check_pos uses,
        and the constrained POS is the one AFTER the backoff - the machine
        retracts a further `post_asp_backoff` in air once clear of the water,
        so checking the aspirate POS alone validates a position it never visits.

            gap_pos - (slope*V + intercept) - backoff >= pos_min + margin
        """
        headroom = (
            self.gap_pos
            - self.pos_min
            - self.stroke_margin
            - self.intercept
            - self.post_asp_backoff
        )
        return round(headroom / self.slope, 4)

    def check_volume(self, volume_uL: float) -> None:
        lo, hi = self.window
        if not (lo - 1e-9 <= volume_uL <= hi + 1e-9):
            raise VolumeError(
                f"{self.id}: {volume_uL} uL is outside the calibration window "
                f"[{lo}, {hi}] uL. The curve was never fitted there; the "
                f"displacement would be an extrapolation."
            )
        if volume_uL > self.max_volume_uL + 1e-9:
            raise VolumeError(
                f"{self.id}: {volume_uL} uL needs more stroke than there is. Max "
                f"is {self.max_volume_uL} uL before POS drops below the "
                f"{self.stroke_margin} mm margin off the hard stop."
            )

    def check_pos(self, pos: float, what: str = "move") -> None:
        lo = self.pos_min + self.stroke_margin
        if pos < lo - 1e-9:
            raise EffectorError(
                f"{self.id}: {what} would put plunger POS at {pos}, below the "
                f"{lo} mm floor (pos_min {self.pos_min} + margin "
                f"{self.stroke_margin})"
            )
        if pos > self.blowout_pos + 1e-9:
            raise EffectorError(
                f"{self.id}: {what} would put plunger POS at {pos}, past blowout "
                f"{self.blowout_pos}"
            )

    # ---- prewet ----------------------------------------------------------

    @property
    def prewet_enabled(self) -> bool:
        return bool(self.spec.get("prewet", {}).get("enabled", False))

    @property
    def prewet_cycles(self) -> int:
        return int(self.spec.get("prewet", {}).get("cycles", 0))

    @property
    def prewet_min_cycles(self) -> int:
        return int(self.spec.get("prewet", {}).get("min_cycles", 0))

    @property
    def prewet_asp_pos(self) -> float:
        p = self.spec.get("prewet", {})
        if "asp_pos" in p and p["asp_pos"] is not None:
            return float(p["asp_pos"])
        return round(self.blowout_pos - float(p["displacement_mm"]), self.decimals)

    # ---- reporting -------------------------------------------------------

    def table(self, volumes: List[float]) -> List[Dict[str, float]]:
        rows = []
        for v in volumes:
            rows.append({
                "volume_uL": v,
                "displacement_mm": self.displacement_mm(v),
                "aspirate_pos": self.aspirate_pos(v),
                # backoff_pos, not a bare subtraction: it validates, so the
                # table can never report a POS check_pos would reject.
                "backoff_pos": self.backoff_pos(v),
            })
        return rows

    def fractions(self, fracs: List[float]) -> List[float]:
        """Test volumes at fractions of nominal, e.g. [0.1, 0.5, 0.75, 1.0]."""
        return [round(f * self.nominal_uL, 4) for f in fracs]


class Gripper(Effector):
    """Plate gripper: a continuous-rotation servo on a rack and pinion.

    Nothing about it is positional. The servo's feedback output has failed, so
    the firmware runs open loop and every motion is TIMED - "open for 400 ms",
    never "open to 30 degrees". Gripping is a controlled stall at a commanded
    current, which expires. The three constraints that shape every method:

    * **Closing self-references, opening does not.** Closing ends at a hard
      stop, which is what makes HOME meaningful. Opening ends at nothing: past
      full travel the racks leave the pinion and the mechanism is a hand
      rebuild. Hence `open_ms_max`, and hence refusing rather than clamping.
    * **It must be homed** before OPEN, CLOSE or GRIP will run at all.
    * **A hold expires** after about 140 s, in three tiers. A sequence that
      carries a plate for longer has to be redesigned, not retried.
    """

    kind = "gripper"

    def __init__(self, cfg: config.ConfigFile) -> None:
        super().__init__(cfg)
        a = self.spec.get("actuator", {})
        # The fallback is the SAFE value, not the historic one: a config that
        # lost this key must not quietly get back the 850 that ran the jaws
        # off the racks.
        self.open_ms_max: int = int(a.get("open_ms_max", 765))
        self.grip_ma_default: int = int(a.get("grip_ma_default", 300))
        self.grip_ma_min: int = int(a.get("grip_ma_min", 100))
        self.grip_ma_max: int = int(a.get("grip_ma_max", 500))

        h = self.spec.get("hold", {})
        self.hold_expires_s: float = float(h.get("expires_s", 140.0))
        self.hold_tier1_s: float = float(h.get("tier1_s", 20.0))
        # Energised-hold budget over a rolling window, enforced by the board.
        self.hold_duty_window_s: float = float(h.get("duty_window_s", 300.0))
        self.hold_duty_max_s: float = float(h.get("duty_max_s", 150.0))

        self.requires_home: bool = bool(self.spec.get("requires_home", True))
        self.carries_labware: bool = bool(self.spec.get("carries_labware", True))
        self.held: Optional[str] = None

    # ---- the Klipper macro names ---------------------------------------

    def macro(self, name: str) -> str:
        """The GRIPPER_* macro for an operation. Raises rather than guessing.

        No default on purpose: a fallback literal would mean a renamed or
        missing macro emits a command that Klipper does not recognise, and the
        run stops mid-sequence with a plate in the jaws.
        """
        macros = self.spec.get("macros", {})
        if name not in macros or not isinstance(macros[name], str):
            known = sorted(k for k, v in macros.items()
                           if isinstance(v, str) and not k.endswith("note"))
            raise EffectorError(
                f"{self.id}: no Klipper macro named {name!r}. Known: {known}. "
                f"Add it to config/effectors/{self.id}.json -> macros, and to "
                f"gripper_macros.cfg."
            )
        return macros[name]

    @property
    def forbidden_macros(self) -> List[str]:
        """Macros that exist but must never be emitted on this machine."""
        return list(self.spec.get("macros", {}).get("do_not_use", []))

    # ---- guards ---------------------------------------------------------

    def check_open_ms(self, ms: int) -> None:
        """Refuse an open longer than the firmware clamp.

        The firmware would clamp this silently. Refusing instead keeps the
        gcode an honest record of what was asked for - and if someone is
        asking for more than the clamp, the assumption behind the sequence is
        already wrong and should be fixed rather than truncated.
        """
        if ms <= 0:
            raise EffectorError(
                f"{self.id}: open needs a positive duration, got {ms} ms."
            )
        if ms > self.open_ms_max:
            raise EffectorError(
                f"{self.id}: open for {ms} ms exceeds the {self.open_ms_max} ms "
                f"firmware clamp. Opening has NO hard stop - past full travel "
                f"the racks come off the pinion and the mechanism has to be "
                f"rebuilt by hand. There is no software recovery. Do not try "
                f"to defeat the clamp."
            )

    def check_grip_ma(self, ma: int) -> None:
        if not (self.grip_ma_min <= ma <= self.grip_ma_max):
            raise EffectorError(
                f"{self.id}: grip current {ma} mA is outside "
                f"{self.grip_ma_min}-{self.grip_ma_max}. {self.grip_ma_max} is "
                f"the firmware clamp and the hardware ceiling is about 570 mA; "
                f"a setpoint above the clamp is silently reduced, which is why "
                f"this refuses rather than passing it on."
            )

    # Ops that grip, for guard purposes: the move after them is a lift.
    _GRIP_OPS = frozenset({"grip", "trial_grip"})

    def guard_dwell_ms(self, op: str, busy_ms: int = 0) -> Optional[int]:
        """How long to stall after a macro, while shell blocking is unproven.

        Returns None once `shell_command.blocks` is true. Until then a move
        following a gripper macro could start while the gripper is still
        running, which during a grip means lifting before the jaws have closed.

        `busy_ms` is how long the host is known to be occupied by this command
        beyond starting it - a board-IMU burst's settle plus its samples. The
        board reads no command queue while it streams, so a guard shorter than
        that would let the next GRIPPER_* command be queued behind the burst.

        Ops in `shell_command.unguarded_ops` get no guard at all: they write a
        context file and touch neither the gantry nor the board.
        """
        sc = self.spec.get("shell_command", {})
        if sc.get("blocks"):
            return None
        if op in sc.get("unguarded_ops", ()):
            return None
        if op in self._GRIP_OPS:
            return int(sc.get("guard_dwell_grip_ms", 2000)) + int(busy_ms)
        return int(sc.get("guard_dwell_ms", 1500)) + int(busy_ms)

    def station_z(self, station: str, key: str) -> float:
        """Commanded Z for this effector at a named deck station.

        Separate from z_for_labware because a station is one position rather
        than a grid - the gripper's pick point in slot 9 is a measurement of
        where the part is, not of a labware footprint. Same rule as every
        other Z here: it lives per-effector, because the jaw reference sits
        at a different height from any pipette nosecone.
        """
        table = self.spec.get("z_for_station", {})
        entry = table.get(station)
        if entry is None:
            raise EffectorError(
                f"effector {self.id} has no Z entry for station {station!r}. "
                f"Known: {sorted(table)}. Add it to "
                f"config/effectors/{self.id}.json -> z_for_station."
            )
        if key not in entry:
            raise EffectorError(
                f"effector {self.id}, station {station!r}: no Z key {key!r}. "
                f"Known: {sorted(k for k in entry if not k.endswith('note'))}"
            )
        v = entry[key]
        if v is None:
            raise EffectorError(
                f"effector {self.id}, station {station!r}, Z {key!r} is null - "
                f"it has never been measured."
            )
        return float(v)

    def require_ready(self) -> None:
        """Refuse to generate motion while the geometry is unmeasured.

        Checks the VALUES, not the config's own `uncalibrated` list. That list
        is a human declaration and goes stale in both directions: a filled-in
        number nobody removed from it would block forever, and a field quietly
        set to null but never added to it would sail through. The null is the
        machine-checkable fact.
        """
        missing = []
        for lid, entry in (self.spec.get("z_for_labware") or {}).items():
            for key, value in entry.items():
                if key.endswith("note") or key == "uncalibrated":
                    continue
                if value is None:
                    missing.append(f"z_for_labware.{lid}.{key}")
        if missing:
            raise EffectorError(
                f"{self.id}: cannot generate plate motion, these are still "
                f"null in config/effectors/{self.id}.json: {sorted(missing)}. "
                f"Measure them by jogging with the gripper fitted and open, "
                f"then fill them in."
            )


_KINDS = {"pipette": Pipette, "gripper": Gripper}


def load(effector_id: str, allow_placeholder: bool = False) -> Effector:
    """Load one end effector by id, i.e. config/effectors/<id>.json."""
    known = config.available_effectors()
    if effector_id not in known:
        raise EffectorError(
            f"no effector {effector_id!r}. Available: {sorted(known)}. "
            f"Add config/effectors/{effector_id}.json to define a new one."
        )
    cfg = config.load_effector_spec(effector_id)
    kind = cfg.data.get("kind")
    cls = _KINDS.get(kind)
    if cls is None:
        raise EffectorError(
            f"{effector_id}: unknown effector kind {kind!r}. Known kinds: "
            f"{sorted(_KINDS)}. Add a class to lab/effector.py to support it."
        )
    eff = cls(cfg)
    if eff.is_placeholder and not allow_placeholder:
        raise EffectorError(
            f"{effector_id} is a placeholder: every numeric field is unmeasured "
            f"and it must not be used to generate motion. Pass "
            f"allow_placeholder=True only to inspect the file."
        )
    return eff


def available() -> List[str]:
    return sorted(config.available_effectors())
