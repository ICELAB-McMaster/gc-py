"""Gripper pick test at station 9, with the IMU recording through the pick.

    python protocols/gripper_station9_test.py --cycles 1     # try this first
    python protocols/gripper_station9_test.py --cycles 15

One cycle, with every height operator-measured 2026-10-06. **+Z is DOWN**, so
a smaller Z is physically higher:

    traverse to (69.50, 178.20) at travel height, descend to Z 90
    GRIPPER_HOME                      re-reference the timed servo
    GRIPPER_OPEN
    descend to Z 107
    GRIPPER_IMU_START                 capture begins, tagged with the cycle
    GRIPPER_GRIP MA=500
    rise to Z 90
    dwell 1 s
    descend to Z 100
    GRIPPER_RELEASE                   the part falls 7 mm - see below
    dwell, so the fall and the landing are in the capture
    GRIPPER_IMU_STOP
    rise to Z 90, and round again

The IMU capture brackets the whole pick **including the release**, and
`GRIPPER_IMU_MARK` is emitted at each phase boundary, so the CSV can be cut
into `at_grip`, `gripped`, `lifted`, `held`, `at_release`, `released`. Marks
land in `<csv>.marks.csv` as elapsed seconds plus a label; join on elapsed
time. The capture file is named per cycle, so "which pick-up is this" is the
filename and "which part of the cycle" is the mark.

The capture runs on for `--post-release` seconds after the jaws open. That is
not padding: the part falls 7 mm, and a capture that stopped at the release
command would record the jaws opening but not the fall or the landing, which
is the part worth having. The guard dwell after the macro happens to cover it
today, but that disappears the moment `shell_command.blocks` is set true, so
the settle is explicit rather than borrowed.

THREE THINGS WORTH KNOWING BEFORE RUNNING IT
--------------------------------------------
**The release drops the part 7 mm.** The grip is taken at Z 107 and the
release happens at Z 100, which is 7 mm higher. `Machine.release` refuses any
drop it was not told to expect, so this protocol declares it; the run warns
and the distance goes in the manifest. Nothing here knows what the part lands
on. If it should be set down rather than dropped, make the release Z 107.

**The IMU start/stop needs `host/imu_session.py`, which is new and has never
been run.** The shipped `gripctl.py imu` is a single blocking capture with no
start or stop, so there was no way to record *through* a pick at all. The
wrapper forks that capture and SIGINTs it to finish - `cmd_imu` catches the
interrupt, aborts the board and still closes the CSV. Test it standalone
before trusting a 15-cycle run to it.

Nothing is transported: the part is picked and put back in the same place.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lab.effector import LabError
from lab.machine import Machine, ProtocolError

# ---- design constants ---------------------------------------------------
STATION = "gripper_pick_9"
GRIP_MA = 500             # the firmware clamp; ~570 mA is the hardware ceiling
HOLD_S = 1.0              # at the raised height, between lift and descent
POST_RELEASE_S = 1.0      # keep recording after the jaws open, so the 7 mm
                          # fall and the landing are in the capture
IMU_RATE = None           # samples/s; None lets the host choose
CSV_DIR = "/home/admin/imu"   # ON THE PI. Local disk: the capture is written
                              # here and copied to DEST_DIR when it stops.
# \\ICE-NAS\Data\GC_ARCCS\GripperIMU as the Pi sees it: the share is
# mounted at /mnt/nas (operator, 2026-10-06). FORWARD slashes - the capture
# runs on Linux, where a backslash is not a separator.
DEST_DIR = "/mnt/nas/GC_ARCCS/GripperIMU"
CYCLES = 1


def build(
    effector_id: str = "gripper",
    name: str | None = None,
    station: str = STATION,
    cycles: int = CYCLES,
    grip_ma: int = GRIP_MA,
    hold_s: float = HOLD_S,
    post_release_s: float = POST_RELEASE_S,
    imu_rate: int | None = IMU_RATE,
    csv_dir: str = CSV_DIR,
    dest_dir: str | None = DEST_DIR,
    imu: bool = True,
    layout: str = "layout.json",
    outdir: str = ".",
    plot: bool = True,
    strict: bool = True,
):
    name = name or ("gripper_station9_test"
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
    if hold_s < 0:
        raise ProtocolError(f"hold_s cannot be negative, got {hold_s}")
    if post_release_s < 0:
        raise ProtocolError(
            f"post_release_s cannot be negative, got {post_release_s}"
        )
    if imu and post_release_s == 0:
        raise ProtocolError(
            "post_release_s is 0, so the capture would stop the instant the "
            "jaws open and the 7 mm fall would not be in it. Give it a "
            "moment, or pass --no-imu if the recording does not matter."
        )
    eff.check_grip_ma(int(grip_ma))

    st = m.deck.station(station)          # raises DeckError if unknown
    z_approach = eff.station_z(station, "approach")
    z_grip = eff.station_z(station, "grip")
    z_release = eff.station_z(station, "release")

    # +Z is DOWN, so "down to grip" means a LARGER number than approach.
    if z_grip <= z_approach:
        raise ProtocolError(
            f"the grip height Z {z_grip} is not below the approach height "
            f"Z {z_approach}. +Z is DOWN, so grip must be the LARGER number - "
            f"as written the gripper would rise to close on the part."
        )
    if not (z_approach <= z_release <= z_grip):
        raise ProtocolError(
            f"the release height Z {z_release} is outside the approach "
            f"Z {z_approach} to grip Z {z_grip} range. Releasing above the "
            f"approach makes no sense, and below the grip would drive the "
            f"part into whatever it was picked from."
        )

    # How far the part falls when it is let go. Declared rather than
    # discovered: Machine.release refuses a drop it was not told about.
    drop_mm = z_grip - z_release

    # The hold has to survive a whole cycle. Guard dwells count, because they
    # are commanded stalls like any other.
    guard = eff.guard_dwell_ms
    held_s = hold_s + ((guard("grip") or 0)
                       + (guard("imu_mark") or 0) * 3
                       + (guard("imu_stop") or 0)) / 1000.0
    if held_s > eff.hold_expires_s:
        raise ProtocolError(
            f"one cycle holds the part through {held_s:g} s of commanded "
            f"dwell, past the {eff.hold_expires_s:g} s at which the hold "
            f"de-energises and the part drops. Shorten --hold."
        )

    m.toggles.update({
        "station": station, "station_xy": list(st.xy),
        "cycles": cycles, "grip_ma": int(grip_ma), "hold_s": hold_s,
        "post_release_s": post_release_s,
        "z_approach": z_approach, "z_grip": z_grip, "z_release": z_release,
        "release_drop_mm": drop_mm,
        "imu": imu, "imu_rate": imu_rate, "csv_dir": csv_dir,
        "dest_dir": dest_dir,
        "held_dwell_per_cycle_s": held_s,
        "shell_blocks": eff.spec.get("shell_command", {}).get("blocks"),
    })

    m.note(f"{name}: gripper pick at {station}, {cycles} cycle(s)")
    m.note(f"XY ({st.xy[0]:g}, {st.xy[1]:g}); approach Z {z_approach:g}, "
           f"grip Z {z_grip:g}, release Z {z_release:g} (+Z is DOWN)")
    m.note(f"grip at {int(grip_ma)} mA, hold {hold_s:g} s at Z {z_approach:g}")
    if drop_mm > 0:
        m.note(f"WARNING the release is {drop_mm:g} mm ABOVE the grip, so the "
               f"part FALLS {drop_mm:g} mm. Declared deliberately.")
    if imu:
        m.note(f"IMU captures to {csv_dir}/<name>_NN.csv on the Pi, with "
               f"phase marks in the matching .marks.csv")
        m.note(f"the capture spans the RELEASE and runs {post_release_s:g} s "
               f"past it, so the fall and the landing are recorded")
        if dest_dir:
            m.note(f"each capture is copied to {dest_dir} on the Pi when it "
                   f"stops - that must be a MOUNTED share, and the originals "
                   f"stay in {csv_dir} either way")
        m.note("IMU start/stop needs host/imu_session.py, which is NEW and "
               "has never been run on hardware")

    # ---- generate --------------------------------------------------------
    # Before G28, so an uninstalled gripper_macros.cfg shows up with the
    # machine still parked rather than after a full run of inert motion.
    m.gripper_preflight()
    m.startup()
    m.goto(st.xy[0], st.xy[1], op=f"goto:{station}")

    for i in range(cycles):
        tag = f"{i + 1:02d}"
        m.note(f"--- pick {i + 1} of {cycles} ---")

        # Arrive at, or return to, the approach height.
        m.move_to_z(z_approach, op="approach")

        # Home re-references the timed servo; opening is what lets it go over
        # the part. Both happen clear of it, at the approach height.
        m.gripper_home()
        m.gripper_open()

        m.move_to_z(z_grip, op="descend")

        # The GRIP goes BEFORE the capture. The board streams IMU samples in
        # a blocking loop and does not read its command queue until the
        # capture ends, so a grip sent during one is queued and fires minutes
        # later. Its own transient is recorded instead by the board's motion
        # telemetry, which is what --log --csv on the command produces.
        m.grip(ma=int(grip_ma), what=f"part@{station}",
               log_csv=(f"{csv_dir}/{name}_{tag}_grip.csv" if imu else None))

        # From here to the stop is GANTRY motion only. Klipper owns that, so
        # the board is free to stream throughout.
        if imu:
            m.gripper_imu_start(csv=f"{csv_dir}/{name}_{tag}.csv",
                                label=f"pick{tag}", rate=imu_rate,
                                dest=dest_dir)
            m.gripper_imu_mark(f"pick{tag}_gripped_{int(grip_ma)}ma_z{z_grip:g}")

        m.move_to_z(z_approach, op="lift")
        if imu:
            m.gripper_imu_mark(f"pick{tag}_lifted_z{z_approach:g}")

        m.dwell(hold_s, op="hold")
        if imu:
            m.gripper_imu_mark(f"pick{tag}_held_{hold_s:g}s")

        m.move_to_z(z_release, op="lower")
        if imu:
            m.gripper_imu_mark(f"pick{tag}_at_release_z{z_release:g}")
            # Stopped before the release because it HAS to be, not by choice.
            m.gripper_imu_stop()

        m.release(max_drop_mm=drop_mm,
                  log_csv=(f"{csv_dir}/{name}_{tag}_release.csv"
                           if imu else None))
        if post_release_s:
            m.dwell(post_release_s, op="post_release")

        m.move_to_z(z_approach, op="retreat")

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
    print(f"  {m.effector.id} at {t['station']} "
          f"({t['station_xy'][0]:g}, {t['station_xy'][1]:g}), "
          f"{t['cycles']} cycle(s)")
    print(f"  Z  approach {t['z_approach']:g} -> grip {t['z_grip']:g} -> "
          f"release {t['z_release']:g}   (+Z is DOWN)")
    print(f"  grip {t['grip_ma']} mA, hold {t['hold_s']:g} s")
    if t.get("imu"):
        print(f"  capture spans the release, +{t['post_release_s']:g} s past "
              f"it for the fall and the landing")
        print(f"  captures -> {t['csv_dir']} on the Pi"
              + (f", copied to {t['dest_dir']}" if t.get("dest_dir") else ""))
    if t["release_drop_mm"]:
        print(f"  the part FALLS {t['release_drop_mm']:g} mm on release")
    print(f"  grips {t.get('grips', 0)}, IMU sessions "
          f"{t.get('imu_sessions', 0)}, marks {t.get('imu_marks', 0)}")
    print(f"  held through {t['held_dwell_per_cycle_s']:g} s of dwell per "
          f"cycle, against a {m.effector.hold_expires_s:g} s expiry")
    if t.get("shell_blocks") is None:
        print(f"  guard dwells ARE emitted: shell_command.blocks is untested")

    report = result["report"]
    if report.warnings:
        print()
        print(f"{len(report.warnings)} warning(s):")
        for w in report.warnings:
            print(f"  - {w}")


def main(argv=None) -> int:
    from lab import effector as _eff

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--effector", default="gripper", choices=_eff.available())
    ap.add_argument("--name", default=None, help="output basename")
    ap.add_argument("--station", default=STATION,
                    help="deck station to pick from")
    ap.add_argument("--cycles", type=int, default=CYCLES,
                    help="how many picks (try 1 first, then 15)")
    ap.add_argument("--grip-ma", type=int, default=GRIP_MA, dest="grip_ma",
                    help="grip current, 100-500 mA")
    ap.add_argument("--hold", type=float, default=HOLD_S, dest="hold_s",
                    help="seconds to hold at the raised height")
    ap.add_argument("--post-release", type=float, default=POST_RELEASE_S,
                    dest="post_release_s",
                    help="seconds to keep recording after the jaws open, so "
                         "the fall and landing are captured")
    ap.add_argument("--imu-rate", type=int, default=IMU_RATE, dest="imu_rate")
    ap.add_argument("--csv-dir", default=CSV_DIR, dest="csv_dir",
                    help="LOCAL directory on the Pi the capture is written to")
    ap.add_argument("--dest-dir", default=DEST_DIR, dest="dest_dir",
                    help="where finished captures are copied. A MOUNTED path "
                         "on the Pi, with FORWARD slashes - "
                         "\\\\ICE-NAS\\Data\\GC_ARCCS\\GripperIMU is "
                         "/mnt/nas/GC_ARCCS/GripperIMU")
    ap.add_argument("--no-dest", dest="dest_dir", action="store_const",
                    const=None, help="keep the captures on the Pi only")
    ap.add_argument("--no-imu", dest="imu", action="store_false",
                    help="run the motion without recording")
    ap.add_argument("--layout", default="layout.json")
    ap.add_argument("--outdir", default=".")
    ap.add_argument("--no-plot", dest="plot", action="store_false")
    ap.add_argument("--lenient", dest="strict", action="store_false")
    a = ap.parse_args(argv)

    try:
        m, result = build(
            effector_id=a.effector, name=a.name, station=a.station,
            cycles=a.cycles, grip_ma=a.grip_ma, hold_s=a.hold_s,
            post_release_s=a.post_release_s,
            imu_rate=a.imu_rate, csv_dir=a.csv_dir, dest_dir=a.dest_dir,
            imu=a.imu,
            layout=a.layout, outdir=a.outdir, plot=a.plot, strict=a.strict,
        )
    except LabError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1

    summarise(m, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
