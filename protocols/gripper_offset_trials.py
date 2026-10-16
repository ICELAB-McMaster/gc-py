"""Offset trials at station 9: how much X misalignment can the gripper pick at?

    python protocols/gripper_offset_trials.py                         # 0,20,40 x 5
    python protocols/gripper_offset_trials.py --grip-z 108 --offsets 0,10,20,30
    python protocols/gripper_offset_trials.py --trials 3 --randomise --seed 7

The sliced form of SDL_Gcode `Gripper/host/offset_trials.py`. The procedure
and the data are that script's; the coordinates, heights and sequence are
`gripper_station9_test.py`'s. **The two things this file is for tuning are the
pick depth `--grip-z` (107) and the X offsets `--offsets` (0,20,40).**

One trial, at X = 69.50 + offset, Y 178.20. **+Z is DOWN**, so a smaller Z is
physically higher:

    GRIPPER_TRIAL                     context: trial, run order, offset, seed
    (cooldown dwell, if the hold-duty budget needs one)
    traverse to X, rise/descend to Z 90
    GRIPPER_TRIAL_HOME                re-reference - refused while holding
    GRIPPER_OPEN
    descend to --grip-z
    GRIPPER_TRIAL_BURST before_grip   board IMU, jaws open, stationary
    GRIPPER_TRIAL_GRIP MA=500         the board's reply becomes grip_status
    rise to Z 90
    GRIPPER_TRIAL_BURST raised        board IMU, holding, stationary
    dwell --hold
    descend to --release-z (Z 100)
    GRIPPER_TRIAL_BURST lowered
    GRIPPER_TRIAL_RELEASE             the part falls grip-z minus release-z
    dwell --post-release
    rise to Z 90

THE DATA is written exactly as offset_trials.py writes it - its columns, its
tilt maths and its copy-to-share are imported, not restated:

    <name>_<stamp>_plate.csv    the Pi's IMU, streamed the whole run
    <name>_<stamp>_pcb.csv      the board's IMU, in the three bursts per trial
    <name>_<stamp>.log

in ~/gripper_captures on the Pi, copied to /mnt/nas/GC_ARCCS/GripperIMU at the
end. Join the two on `wall_clock`. `grip_status` is blank on rows taken before
the grip resolved; to pull a failed trial, find the trial numbers whose
grip_status is not `done` and filter on `trial`.

THREE WAYS THIS DIFFERS FROM THE SCRIPT, all because a G-code program cannot
branch on what the gripper reported:

* A refused grip is still lifted, held and lowered - the next line is already
  written. Lifting empty jaws is harmless, and the raised and lowered bursts
  of that trial are skipped on the Pi, so the PCB file has the rows the script
  would have written.
* The gripper is homed every trial, as gripper_station9_test does, rather than
  once. Homing re-references the timed servo before each open, so an open
  never starts from wherever the last release left the jaws. The host refuses
  the home if the board still reports holding, which is the one case where
  homing would crush the plate.
* Hold-duty pacing is decided at SLICE time: the cooldown dwells are in the
  G-code, computed from a timing model that estimates energised time high and
  everything else low, so it errs toward waiting too long.

If GRIPPER_TRIALS_BEGIN reports a preflight failure, the program still runs:
a shell command cannot stop the program that called it. Read the console.
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lab.effector import LabError
from lab.machine import Machine, ProtocolError

# ---- design constants ---------------------------------------------------
STATION = "gripper_pick_9"
OFFSETS = "0,20,40"       # mm in X from the station centre, signed allowed
TRIALS = 5                # per offset, as offset_trials
GRIP_MA = 500
HOLD_S = 1.0              # gripper_station9_test's hold, not the script's 3
POST_RELEASE_S = 1.0
SETTLE_S = 0.4            # before the before_grip burst
SETTLE_RAISED_S = 0.3     # before the raised and lowered bursts
RATE_HZ = 50              # plate stream; orientation work
BOARD_SAMPLES = 25        # x 20 ms = 0.5 s, the same span as the script's
BOARD_INTERVAL_MS = 20    # 50 x 10 ms, at 50 Hz like everything else
DUTY_MARGIN = 0.8         # of the board's hold-duty budget, as the script

# Timing model. Energised time is estimated HIGH and everything else LOW, so
# the duty pacing errs toward waiting too long rather than too little. These
# are not measurements - they are allowances, and they are named so that a
# measured number has an obvious place to go.
SHELL_OVERHEAD_HI_S = 0.6     # python start + CAN open, per shell command
GRIP_HI_S = 1.5               # GRIP to stall-and-hold on the board
GRIP_LO_S = 1.0
TIER1_FRACTION = 0.8          # refuse a span past this share of tier 1


def parse_offsets(text: str) -> list[float]:
    try:
        offs = [float(x) for x in text.split(",") if x.strip()]
    except ValueError:
        raise ProtocolError(
            f"--offsets must be comma-separated numbers, got {text!r}"
        ) from None
    if not offs:
        raise ProtocolError("--offsets is empty")
    return offs


def build_plan(offsets, trials, randomise, seed):
    """offset_trials.build_plan, verbatim in behaviour.

    Same loop, same random.Random(seed).shuffle - so a given seed gives the
    SAME trial order here as in the script, and a run can be compared trial
    for trial against one taken with it.
    """
    plan = []
    for off in offsets:
        for rep in range(trials):
            plan.append({"offset_mm": off, "rep": rep})
    if randomise:
        random.Random(seed).shuffle(plan)
    for i, item in enumerate(plan):
        item["trial"] = i
    return plan


def _move_s(dist_mm: float, feed_mm_min: float) -> float:
    return abs(dist_mm) / max(1.0, feed_mm_min / 60.0)


def build(
    effector_id: str = "gripper",
    name: str | None = None,
    station: str = STATION,
    offsets: str = OFFSETS,
    trials: int = TRIALS,
    grip_z: float | None = None,
    approach_z: float | None = None,
    release_z: float | None = None,
    grip_ma: int = GRIP_MA,
    hold_s: float = HOLD_S,
    post_release_s: float = POST_RELEASE_S,
    settle_s: float = SETTLE_S,
    settle_raised_s: float = SETTLE_RAISED_S,
    open_ms: int | None = None,
    randomise: bool = False,
    seed: int | None = None,
    rate_hz: int = RATE_HZ,
    board_samples: int = BOARD_SAMPLES,
    board_interval_ms: int = BOARD_INTERVAL_MS,
    z_feed: int | None = None,
    duty_margin: float = DUTY_MARGIN,
    dest_dir: str | None = None,
    layout: str = "layout.json",
    outdir: str = ".",
    plot: bool = True,
    strict: bool = True,
):
    offs = parse_offsets(offsets)

    # ---- geometry -------------------------------------------------------
    # Load once for the effector, so the defaults come from the station's
    # measured heights and the name can carry the tuned depth.
    probe = Machine(effector=effector_id, name="probe", layout=layout,
                    strict=strict, temp_log=False)
    eff = probe.effector
    if eff.kind != "gripper":
        raise ProtocolError(
            f"{effector_id} is a {eff.kind}; this protocol needs a gripper"
        )
    st = probe.deck.station(station)
    z_approach = eff.station_z(station, "approach") if approach_z is None \
        else float(approach_z)
    z_grip = eff.station_z(station, "grip") if grip_z is None else float(grip_z)
    z_release = eff.station_z(station, "release") if release_z is None \
        else float(release_z)
    del probe

    m_name = name or f"gripper_offset_trials_z{z_grip:g}"
    data_name = f"offsets_z{z_grip:g}"
    m = Machine(effector=effector_id, name=m_name, layout=layout,
                strict=strict, temp_log=False)
    eff = m.effector

    # ---- design checks, before a single line of gcode -------------------
    if trials < 1:
        raise ProtocolError(f"trials must be at least 1, got {trials}")
    for label, v in (("hold", hold_s), ("post_release", post_release_s),
                     ("settle", settle_s), ("settle_raised", settle_raised_s)):
        if v < 0:
            raise ProtocolError(f"{label} cannot be negative, got {v}")
    if not 0 < duty_margin <= 1:
        raise ProtocolError(f"duty_margin must be in (0, 1], got {duty_margin}")
    eff.check_grip_ma(int(grip_ma))
    if open_ms is not None:
        eff.check_open_ms(int(open_ms))

    # +Z is DOWN, so the grip is the LARGER number and the approach smaller.
    if z_grip <= z_approach:
        raise ProtocolError(
            f"the grip depth Z {z_grip:g} is not below the approach Z "
            f"{z_approach:g}. +Z is DOWN, so --grip-z must be the LARGER "
            f"number - as written the gripper would rise to close on the plate."
        )
    # Release must be between the two: above the approach is pointless, and
    # BELOW the grip (a larger Z) drives the held plate into the deck it was
    # picked from.
    if not (z_approach <= z_release <= z_grip):
        raise ProtocolError(
            f"the release Z {z_release:g} must lie between the approach "
            f"Z {z_approach:g} and the grip Z {z_grip:g}. A release Z larger "
            f"than the grip is physically LOWER than where the plate sat, so "
            f"lowering to it pushes the held plate into the deck. If you "
            f"deepened --grip-z, --release-z may need to follow it."
        )
    drop_mm = z_grip - z_release

    if randomise and seed is None:
        # Generated and recorded, as the script does. It makes this slice
        # unrepeatable unless the printed seed is passed back in.
        seed = random.randrange(2 ** 32)
    plan = build_plan(offs, trials, randomise, seed)

    # ---- the timing model ----------------------------------------------
    feed_t = m.deck.feedrate("travel")
    feed_z = z_feed or feed_t
    g = eff.guard_dwell_ms
    burst_busy = lambda settle: (int(round(settle * 1000))          # noqa: E731
                                 + board_samples * board_interval_ms)
    burst_s = board_samples * board_interval_ms / 1000.0
    lift_mm = z_grip - z_approach
    lower_mm = z_grip - z_release

    def guard_s(op, busy_ms=0):
        return (g(op, busy_ms=busy_ms) or 0) / 1000.0

    # Energised from GRIP to RELEASE - estimated HIGH: host time AND guard.
    span_hi = (GRIP_HI_S + guard_s("trial_grip")
               + SHELL_OVERHEAD_HI_S + guard_s("trial_phase")          # lifting
               + _move_s(lift_mm, feed_z)
               + SHELL_OVERHEAD_HI_S + settle_raised_s + burst_s
               + guard_s("trial_burst", burst_busy(settle_raised_s))   # raised
               + hold_s
               + SHELL_OVERHEAD_HI_S + guard_s("trial_phase")          # lowering
               + _move_s(z_release - z_approach, feed_z)
               + SHELL_OVERHEAD_HI_S + settle_raised_s + burst_s
               + guard_s("trial_burst", burst_busy(settle_raised_s))   # lowered
               + SHELL_OVERHEAD_HI_S)                                   # release
    # The guard dwells inside that span - what blocks=true would give back.
    span_guards = (guard_s("trial_grip") + 2 * guard_s("trial_phase")
                   + 2 * guard_s("trial_burst", burst_busy(settle_raised_s)))
    tier1 = eff.hold_tier1_s
    if span_hi > TIER1_FRACTION * tier1:
        raise ProtocolError(
            f"each grip is energised for up to ~{span_hi:.1f} s (estimated "
            f"high), over {TIER1_FRACTION:.0%} of the {tier1:g} s tier-1 hold. "
            f"Past tier 1 the board drops to its reduced current partway "
            f"through the grip, and the trial stops being the constant-current "
            f"experiment it claims to be. Shorten --hold or the settles, take "
            f"fewer --board-samples, or - once RUN_SHELL_COMMAND is confirmed "
            f"to block - set shell_command.blocks true, which removes "
            f"{span_guards:.1f} s of guard dwells from every grip."
        )

    def pre_grip_lo(x_from, x_to, z_from):
        """Trial start to GRIP, estimated LOW: dwells and moves only."""
        t = 0.0
        if abs(x_to - x_from) > 1e-6:
            t += _move_s(z_from - m.deck.z_travel, feed_t)
            t += _move_s(x_to - x_from, feed_t)
            t += _move_s(z_approach - m.deck.z_travel, feed_z)
        t += guard_s("trial_home") + guard_s("open")
        t += _move_s(lift_mm, feed_z)
        t += guard_s("trial_burst", burst_busy(settle_s))
        return t

    # After GRIP, to the next trial's start, estimated LOW.
    post_grip_lo = (GRIP_LO_S + guard_s("trial_grip") + _move_s(lift_mm, feed_z)
                    + guard_s("trial_burst", burst_busy(settle_raised_s))
                    + hold_s + _move_s(z_release - z_approach, feed_z)
                    + guard_s("trial_burst", burst_busy(settle_raised_s))
                    + guard_s("trial_release") + post_release_s
                    + _move_s(z_release - z_approach, feed_z))

    window = eff.hold_duty_window_s
    budget = eff.hold_duty_max_s * duty_margin
    spans: list[tuple[float, float]] = []

    def used(at):
        edge = at - window
        return sum(max(0.0, b - max(a, edge)) for a, b in spans)

    m.toggles.update({
        "station": station, "station_xy": list(st.xy),
        "offsets_mm": offs, "trials_per_offset": trials,
        "trial_count": len(plan), "randomise": bool(randomise), "seed": seed,
        "z_approach": z_approach, "z_grip": z_grip, "z_release": z_release,
        "release_drop_mm": drop_mm, "grip_ma": int(grip_ma),
        "hold_s": hold_s, "post_release_s": post_release_s,
        "rate_hz": rate_hz, "board_samples": board_samples,
        "board_interval_ms": board_interval_ms,
        "energised_span_hi_s": round(span_hi, 2),
        "duty_budget_s": budget, "duty_window_s": window,
        "data_name": data_name,
    })

    m.note(f"{m_name}: {len(offs)} offsets x {trials} = {len(plan)} trials, "
           f"{'randomised, seed ' + str(seed) if randomise else 'blocked order'}")
    m.note(f"centre X {st.xy[0]:g}, Y {st.xy[1]:g}; offsets {offs} -> X "
           + ", ".join(f"{st.xy[0] + o:g}" for o in offs))
    m.note(f"Z approach {z_approach:g}, grip {z_grip:g}, release "
           f"{z_release:g} (+Z is DOWN)")
    if drop_mm > 0:
        m.note(f"WARNING the release is {drop_mm:g} mm ABOVE the grip, so the "
               f"plate FALLS {drop_mm:g} mm every trial. Declared deliberately.")
    m.note(f"data -> ~/gripper_captures/{data_name}_<stamp>_plate.csv and "
           f"_pcb.csv on the Pi, joined on wall_clock, copied to the NAS")

    # ---- generate --------------------------------------------------------
    m.gripper_preflight()                 # before G28, see that method
    m.startup()
    m.trials_begin(data_name, rate=rate_hz, dest=dest_dir, banner={
        "centre_x": f"{st.xy[0]:g}",
        "offsets": ",".join(f"{o:g}" for o in offs),
        "trials": trials, "trials_total": len(plan),
        "approach_z": f"{z_approach:g}", "grip_z": f"{z_grip:g}",
        "release_z": f"{z_release:g}", "ma": int(grip_ma),
        "hold": f"{hold_s:g}", "randomise": 1 if randomise else 0,
        "board_interval_ms": board_interval_ms,
        **({"seed": seed} if seed is not None else {}),
    })

    clock = 0.0
    x_prev, z_prev = m.g.get_x(), m.g.get_z()
    cooldowns = []
    for run_order, item in enumerate(plan):
        off = item["offset_mm"]
        x = st.xy[0] + off

        m.note(f"--- trial {run_order + 1} of {len(plan)}: offset {off:+g} mm, "
               f"X {x:g} ---")
        m.trial(item["trial"], run_order, off, x, randomise, seed)

        # Duty pacing: wait until this grip's high-estimated span fits the
        # window as it will stand at the moment of gripping.
        pre = pre_grip_lo(x_prev, x, z_prev)
        wait = 0.0
        while used(clock + wait + pre) + span_hi > budget:
            wait += 0.5
            if wait > window:
                raise ProtocolError(
                    f"one grip of ~{span_hi:.1f} s does not fit a "
                    f"{budget:g} s duty budget at all"
                )
        if wait:
            m.dwell(wait, op="duty_cooldown")
            cooldowns.append(wait)
        clock += wait + pre
        spans.append((clock, clock + span_hi))
        clock += post_grip_lo

        m.trial_phase("traverse", x=x, z=z_approach)
        m.goto(x, st.xy[1], op=f"goto:{station}{off:+g}")
        m.move_to_z(z_approach, op="approach", feedrate=z_feed)

        m.gripper_home(op="trial_home")
        m.gripper_open(open_ms)

        m.trial_phase("descend", z=z_grip)
        m.move_to_z(z_grip, op="descend", feedrate=z_feed)
        m.trial_burst("before_grip", board_samples, board_interval_ms,
                      settle_s=settle_s, phase="settle")

        m.grip(ma=int(grip_ma), what=f"plate@{station}{off:+g}",
               op="trial_grip")

        m.trial_phase("lifting", z=z_approach)
        m.move_to_z(z_approach, op="lift", feedrate=z_feed)
        m.trial_burst("raised", board_samples, board_interval_ms,
                      settle_s=settle_raised_s, phase="raised")
        m.dwell(hold_s, op="hold")

        m.trial_phase("lowering", z=z_release)
        m.move_to_z(z_release, op="lower", feedrate=z_feed)
        m.trial_burst("lowered", board_samples, board_interval_ms,
                      settle_s=settle_raised_s)

        m.release(max_drop_mm=drop_mm, op="trial_release")
        m.dwell(post_release_s, op="post_release")

        m.trial_phase("retract", z=z_approach)
        m.move_to_z(z_approach, op="retract", feedrate=z_feed)
        x_prev, z_prev = x, z_approach

    m.trials_end()
    m.park()

    m.toggles["duty_cooldowns_s"] = [round(c, 1) for c in cooldowns]
    m.toggles["estimated_minutes_lo"] = round(clock / 60.0, 1)
    return m, m.finalize(outdir=outdir, plot=plot)


def summarise(m: Machine, result) -> None:
    t = m.toggles
    print(f"done -> {result['gcode']}")
    print(f"        {result['manifest']}")
    if result.get("plot"):
        print(f"        {result['plot']}")
    print(f"        {result['lines']} gcode lines")
    print()
    print(f"  {t['trial_count']} trials: offsets {t['offsets_mm']} x "
          f"{t['trials_per_offset']}, "
          + (f"randomised, seed {t['seed']}" if t["randomise"]
             else "blocked order"))
    x0 = t["station_xy"][0]
    print(f"  X {', '.join(f'{x0 + o:g}' for o in t['offsets_mm'])} at "
          f"Y {t['station_xy'][1]:g}")
    print(f"  Z approach {t['z_approach']:g} -> grip {t['z_grip']:g} -> "
          f"release {t['z_release']:g}   (+Z is DOWN)")
    if t["release_drop_mm"]:
        print(f"  the plate FALLS {t['release_drop_mm']:g} mm on every release")
    print(f"  grip {t['grip_ma']} mA, hold {t['hold_s']:g} s; energised up to "
          f"~{t['energised_span_hi_s']:g} s per grip (tier 1 is 20 s)")
    cd = t.get("duty_cooldowns_s") or []
    if cd:
        print(f"  {len(cd)} duty cooldown(s), {sum(cd):.0f} s total, to stay "
              f"inside {t['duty_budget_s']:g} s of hold per "
              f"{t['duty_window_s']:g} s")
    print(f"  at least ~{t['estimated_minutes_lo']:g} min (a lower bound)")
    print(f"  plate IMU {t['rate_hz']} Hz throughout; board IMU "
          f"{t['board_samples']} x {t['board_interval_ms']} ms at before_grip, "
          f"raised, lowered")
    print(f"  -> {t['data_name']}_<stamp>_plate.csv, _pcb.csv, .log on the Pi")

    report = result["report"]
    if report.warnings:
        print()
        print(f"{len(report.warnings)} warning(s):")
        for w in report.warnings:
            print(f"  - {w}")


def main(argv=None) -> int:
    from lab import effector as _eff

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    tune = ap.add_argument_group("the parameters being tuned")
    tune.add_argument("--grip-z", type=float, default=None, dest="grip_z",
                      help="pick depth - the Z the jaws close at (default: the "
                           "station's measured 107). +Z is DOWN: larger is "
                           "deeper")
    tune.add_argument("--offsets", default=OFFSETS,
                      help="X offsets from the station centre in mm, comma "
                           f"separated, signed allowed (default {OFFSETS})")
    tune.add_argument("--trials", type=int, default=TRIALS,
                      help=f"per offset (default {TRIALS})")

    geo = ap.add_argument_group("geometry - station 9's measured values by default")
    geo.add_argument("--station", default=STATION)
    geo.add_argument("--approach-z", type=float, default=None, dest="approach_z")
    geo.add_argument("--release-z", type=float, default=None, dest="release_z",
                     help="Z the jaws open at (default the station's 100). "
                          "Away from --grip-z the plate FALLS the difference")

    grip = ap.add_argument_group("grip")
    grip.add_argument("--ma", type=int, default=GRIP_MA, dest="grip_ma")
    grip.add_argument("--hold", type=float, default=HOLD_S, dest="hold_s")
    grip.add_argument("--post-release", type=float, default=POST_RELEASE_S,
                      dest="post_release_s")
    grip.add_argument("--open-ms", type=int, default=None, dest="open_ms",
                      help="open duration after each home; default is the "
                           "board's own. Refused past 765 ms")

    order = ap.add_argument_group("order")
    order.add_argument("--randomise", action="store_true")
    order.add_argument("--seed", type=int, default=None,
                       help="generated and printed if --randomise and not given")

    smp = ap.add_argument_group("sampling and pacing")
    smp.add_argument("--rate", type=int, default=RATE_HZ, dest="rate_hz",
                     help=f"plate IMU Hz (default {RATE_HZ})")
    smp.add_argument("--board-samples", type=int, default=BOARD_SAMPLES)
    smp.add_argument("--board-interval-ms", type=int, default=BOARD_INTERVAL_MS)
    smp.add_argument("--settle", type=float, default=SETTLE_S, dest="settle_s")
    smp.add_argument("--settle-raised", type=float, default=SETTLE_RAISED_S,
                     dest="settle_raised_s")
    smp.add_argument("--z-feed", type=int, default=None, dest="z_feed",
                     help="mm/min for the gripper's Z moves (default: travel, "
                          "as gripper_station9_test)")
    smp.add_argument("--duty-margin", type=float, default=DUTY_MARGIN)

    io = ap.add_argument_group("io")
    io.add_argument("--effector", default="gripper", choices=_eff.available())
    io.add_argument("--name", default=None, help="gcode basename")
    io.add_argument("--dest-dir", default=None, dest="dest_dir",
                    help="where the Pi copies the data (default the NAS, "
                         "/mnt/nas/GC_ARCCS/GripperIMU). Forward slashes")
    io.add_argument("--layout", default="layout.json")
    io.add_argument("--outdir", default=".")
    io.add_argument("--no-plot", dest="plot", action="store_false")
    io.add_argument("--lenient", dest="strict", action="store_false")
    a = ap.parse_args(argv)

    try:
        m, result = build(
            effector_id=a.effector, name=a.name, station=a.station,
            offsets=a.offsets, trials=a.trials, grip_z=a.grip_z,
            approach_z=a.approach_z, release_z=a.release_z,
            grip_ma=a.grip_ma, hold_s=a.hold_s,
            post_release_s=a.post_release_s, settle_s=a.settle_s,
            settle_raised_s=a.settle_raised_s, open_ms=a.open_ms,
            randomise=a.randomise, seed=a.seed, rate_hz=a.rate_hz,
            board_samples=a.board_samples,
            board_interval_ms=a.board_interval_ms, z_feed=a.z_feed,
            duty_margin=a.duty_margin, dest_dir=a.dest_dir,
            layout=a.layout, outdir=a.outdir, plot=a.plot, strict=a.strict,
        )
    except LabError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1

    summarise(m, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
