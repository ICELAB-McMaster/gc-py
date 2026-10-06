"""Gripper bring-up: pick a plate, lift, record the IMU, put it back.

One cycle to try by hand, then the same cycle repeated so the IMU traces can
be compared across repetitions:

    python protocols/gripper_plate_test.py --effector gripper --cycles 1
    python protocols/gripper_plate_test.py --effector gripper --cycles 15

Each cycle is:

    traverse to the plate centre
    descend to the grip Z
    GRIPPER_GRIP MA=300
    lift to the carry Z
    GRIPPER_IMU SECONDS=3            <- the measurement
    lower to the grip Z
    GRIPPER_RELEASE                  <- never above the pick height
    lift to the carry Z

The plate is picked up and put back in the SAME slot. Nothing is transported,
which is deliberate for a bring-up test: it exercises the gripper, the lift
and the IMU without depending on a second location being right, and a failure
puts the plate back where it started.

BEFORE THIS WILL GENERATE ANYTHING
----------------------------------
Two measurements are missing and the run refuses until they exist, naming
them. In `config/effectors/gripper.json` -> `z_for_labware.sbs_96_wellplate`:

    grip   commanded Z at which the open jaws straddle a seated plate
    lift   commanded Z to carry it at - SMALLER, because +Z is DOWN

Jog with the gripper fitted and open, lower until the jaws are around the
plate skirt, and record that as `grip`. Raise until the plate clears
everything on the deck and record that as `lift`.

TWO HAZARDS FROM THE GRIPPER'S OWN API
--------------------------------------
**GRIPPER_PICK and GRIPPER_PLACE must never run on this machine.** Both do
relative Z moves with a normal printer's sign convention - `GRIPPER_PICK`
emits `G1 Z40` and calls it a lift. Here **+Z is DOWN**, so that command
drives the gripper 40 mm into the plate. This protocol emits absolute Z
through `Machine`, which knows the convention, and the library refuses those
two macros by name.

**A grip expires.** It is a controlled stall, not a clamp: full current for
20 s, reduced for 120 more, then de-energised with a fault. The plate drops.
`Machine` charges every commanded dwell against that budget and refuses a
sequence that would outlast it - but it counts DWELLS ONLY, so the real
elapsed time is longer. With a 3 s IMU capture a cycle is nowhere near it;
raising `--imu-seconds` towards 20 is where it starts to matter.

Also worth knowing: whether `RUN_SHELL_COMMAND` blocks until the gripper
process exits is install-dependent and untested here, so every gripper macro
is followed by a guard dwell. That is about 3.5 s a cycle. Test it once, set
`shell_command.blocks` true in the effector config, and they disappear.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lab.effector import LabError
from lab.machine import Machine, ProtocolError

# ---- design constants ---------------------------------------------------
PLATE = "plate1"         # picked up and put back in the same slot
GRIP_MA = 300            # 100-500; uncalibrated against force, repeatable
IMU_SECONDS = 3.0        # per cycle, while the plate is held at carry height
IMU_RATE = None          # samples/s; None lets the host choose
CYCLES = 1               # one to try by hand; 15 for a repeatability run


def build(
    effector_id: str = "gripper",
    name: str | None = None,
    plate: str = PLATE,
    cycles: int = CYCLES,
    grip_ma: int = GRIP_MA,
    imu_seconds: float = IMU_SECONDS,
    imu_rate: int | None = IMU_RATE,
    imu_csv_prefix: str | None = None,
    home_between: bool = True,
    layout: str = "layout.json",
    outdir: str = ".",
    plot: bool = True,
    strict: bool = True,
):
    name = name or (f"gripper_plate_test"
                    + (f"_x{cycles}" if cycles > 1 else ""))
    m = Machine(effector=effector_id, name=name, layout=layout,
                strict=strict, temp_log=False)

    eff = m.effector
    if eff.kind != "gripper":
        raise ProtocolError(
            f"{effector_id} is a {eff.kind}; this protocol needs a gripper"
        )

    # ---- design checks, before a single line of gcode -------------------
    if cycles < 1:
        raise ProtocolError(f"cycles must be at least 1, got {cycles}")
    if imu_seconds <= 0:
        raise ProtocolError(
            f"imu_seconds must be positive, got {imu_seconds}; there is no "
            f"point running this test without a measurement"
        )
    eff.check_grip_ma(int(grip_ma))

    p = m.deck.placement(plate)          # raises DeckError if not loaded
    eff.require_ready()                  # raises naming the unmeasured Z values

    z_grip = eff.z_for(p.labware.id, "grip")
    z_lift = eff.z_for(p.labware.id, "lift")

    # A whole cycle's dwells have to fit inside one hold, with the guard
    # dwells included. Checked here because the machine-level check only
    # fires once the budget is actually spent, and a protocol that cannot
    # work should say so before it emits anything.
    guard_s = ((eff.guard_dwell_ms("grip") or 0)
               + 2 * (eff.guard_dwell_ms("imu") or 0)) / 1000.0
    held_s = imu_seconds + guard_s
    if held_s > eff.hold_expires_s:
        raise ProtocolError(
            f"one cycle holds the plate through {held_s:g} s of commanded "
            f"dwell, past the {eff.hold_expires_s:g} s at which the hold "
            f"de-energises and the plate drops. Lower --imu-seconds."
        )

    m.toggles.update({
        "plate": plate, "cycles": cycles, "grip_ma": int(grip_ma),
        "imu_seconds": imu_seconds, "imu_rate": imu_rate,
        "home_between": home_between,
        "z_grip": z_grip, "z_lift": z_lift,
        "held_dwell_per_cycle_s": held_s,
        "shell_blocks": eff.spec.get("shell_command", {}).get("blocks"),
    })

    m.note(f"{name}: gripper bring-up, {cycles} cycle(s) on {plate}")
    m.note(f"pick and place in the SAME slot - nothing is transported")
    m.note(f"grip Z {z_grip:g}, carry Z {z_lift:g} (+Z is DOWN, so carry is "
           f"{z_grip - z_lift:g} mm above the grip)")
    m.note(f"GRIPPER_PICK / GRIPPER_PLACE are NOT used: their relative Z has "
           f"the opposite sign convention and would drive into the deck")
    if held_s > eff.hold_tier1_s:
        m.note(f"WARNING each cycle holds for {held_s:g} s of dwell, past the "
               f"{eff.hold_tier1_s:g} s full-current window")

    # ---- generate --------------------------------------------------------
    m.startup()
    m.gripper_home()                     # the only reference a timed servo has
    m.gripper_status()                   # on the record before anything moves

    for i in range(cycles):
        m.note(f"--- cycle {i + 1} of {cycles} ---")
        if i and home_between:
            # Re-reference between cycles. Timed travel drifts with rail
            # voltage, load and temperature, and homing is about a second.
            m.gripper_home()
        m.gripper_open()

        m.pick_plate(plate, ma=int(grip_ma))
        csv = (f"{imu_csv_prefix}_{i + 1:02d}.csv" if imu_csv_prefix else None)
        m.gripper_imu(imu_seconds, rate=imu_rate, csv=csv)
        m.place_plate(plate)

    m.gripper_open()                     # leave the jaws clear of the plate
    m.park()

    return m, m.finalize(outdir=outdir, plot=plot)


def summarise(m: Machine, result) -> None:
    t = m.toggles
    print(f"done -> {result['gcode']}")
    print(f"        {result['manifest']}")
    if result.get("plot"):
        print(f"        {result['plot']}")
    print(f"        {result['lines']} gcode lines")
    print()
    print(f"  {m.effector.id}  {t['cycles']} cycle(s) on {t['plate']}")
    print(f"  grip Z {t['z_grip']:g} -> carry Z {t['z_lift']:g}  "
          f"({t['z_grip'] - t['z_lift']:g} mm of lift; +Z is DOWN)")
    print(f"  grip at {t['grip_ma']} mA, IMU {t['imu_seconds']:g} s per cycle")
    print(f"  held through {t['held_dwell_per_cycle_s']:g} s of dwell per "
          f"cycle, against a {m.effector.hold_expires_s:g} s expiry")
    print(f"  grips {t.get('grips', 0)}, IMU captures "
          f"{t.get('imu_captures', 0)}, "
          f"{t.get('imu_seconds_total', 0):g} s of capture total")
    if t.get("shell_blocks") is None:
        print(f"  guard dwells ARE emitted: shell_command.blocks is untested")

    report = result["report"]
    if report.warnings:
        print()
        print(f"{len(report.warnings)} warning(s):")
        for w in report.warnings:
            print(f"  - {w}")
    unc = m.uncalibrated()
    if unc:
        print()
        print(f"{len(unc)} uncalibrated field(s) recorded in the manifest:")
        for u in unc:
            print(f"  - {u}")


def main(argv=None) -> int:
    from lab import effector as _eff

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--effector", default="gripper", choices=_eff.available(),
                    help="which end effector, i.e. config/effectors/<id>.json")
    ap.add_argument("--name", default=None, help="output basename")
    ap.add_argument("--plate", default=PLATE,
                    help="layout placement to pick up and put back")
    ap.add_argument("--cycles", type=int, default=CYCLES,
                    help="how many pick/measure/place repetitions (try 1 "
                         "first, then 15)")
    ap.add_argument("--grip-ma", type=int, default=GRIP_MA, dest="grip_ma",
                    help="grip current, 100-500 mA. Repeatable, but NOT "
                         "calibrated against force")
    ap.add_argument("--imu-seconds", type=float, default=IMU_SECONDS,
                    dest="imu_seconds", help="IMU capture per cycle")
    ap.add_argument("--imu-rate", type=int, default=IMU_RATE, dest="imu_rate",
                    help="IMU sample rate; omitted lets the host choose")
    ap.add_argument("--imu-csv-prefix", default=None, dest="imu_csv_prefix",
                    help="write each capture to <prefix>_NN.csv ON THE PI")
    ap.add_argument("--no-home-between", dest="home_between",
                    action="store_false",
                    help="skip the re-home between cycles. Timed travel "
                         "drifts, so the default re-references every cycle")
    ap.add_argument("--layout", default="layout.json")
    ap.add_argument("--outdir", default=".")
    ap.add_argument("--no-plot", dest="plot", action="store_false")
    ap.add_argument("--lenient", dest="strict", action="store_false",
                    help="collect every simulation error instead of raising "
                         "on the first. Still refuses to write the gcode.")
    a = ap.parse_args(argv)

    try:
        m, result = build(
            effector_id=a.effector, name=a.name, plate=a.plate,
            cycles=a.cycles, grip_ma=a.grip_ma, imu_seconds=a.imu_seconds,
            imu_rate=a.imu_rate, imu_csv_prefix=a.imu_csv_prefix,
            home_between=a.home_between, layout=a.layout, outdir=a.outdir,
            plot=a.plot, strict=a.strict,
        )
    except LabError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1

    summarise(m, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
