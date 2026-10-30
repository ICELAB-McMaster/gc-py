"""Regression tests. Run with pytest, or directly:  python tests/test_lab.py

The geometry tests pin coordinates that were verified three independent ways:
against the hardcoded constants in the old scripts, against the separate
hardcoded lists in examples/PipetteCurve.py, and against the XY actually
emitted into 300ulPipetteAccuracy.gcode. If one of these fails, the deck config
moved and somebody needs to know.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "protocols"))

from lab import effector as eff_mod
from lab.deck import Deck
from lab.effector import EffectorError, Pipette, VolumeError
from lab.machine import Machine, ProtocolError
from lab.simulate import SimulationError

# The per-run layout.json is the operator's bench and changes between runs, so
# behaviour tests use a fixture that holds every piece of labware at once. Only
# the handful of tests that deliberately assert the SHIPPED bench call
# Deck.load(TEST_LAYOUT) with no argument.
TEST_LAYOUT = "layout_test.json"

FAILURES: list[str] = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)
        print(f"  FAIL  {msg}")
    return cond


def raises(exc, fn, msg):
    try:
        fn()
    except exc:
        return True
    except Exception as e:  # wrong exception type is still a failure
        FAILURES.append(f"{msg} (raised {type(e).__name__}: {e})")
        print(f"  FAIL  {msg} (raised {type(e).__name__})")
        return False
    FAILURES.append(f"{msg} (nothing raised)")
    print(f"  FAIL  {msg} (nothing raised)")
    return True if False else False


# ==========================================================================
# geometry
# ==========================================================================

def test_tube_rack_layout():
    """The 0.65 mL tube holder: 12 mm in X, 18 mm in Y, 5 rows x 10 cols.

    Reads the test fixture, not the bench: this holder comes on and off the
    deck, and its geometry is worth asserting either way.
    """
    d = Deck.load(TEST_LAYOUT)
    p = d.placement("tubes")

    check(p.labware.rows == 5 and p.labware.cols == 10,
          f"tube rack should be 5x10, got {p.labware.rows}x{p.labware.cols}")
    check(p.labware.grid["pitch_x"] == 12.0, "tube rack X pitch should be 12.0")
    check(p.labware.grid["pitch_y"] == 18.0, "tube rack Y pitch should be 18.0")
    # A1 is the DIRECTLY MEASURED well centre (operator, 2026-08-27). It
    # supersedes (256.0, 223.0), which was the old scripts' X 186 with the +70
    # frame correction applied - a shifted estimate, never a measurement.
    check(p.xy(0, 0) == (256.6, 218.5), f"A1 should be (256.6, 218.5), got {p.xy(0,0)}")

    # Column index advances toward LOWER X; row index toward LOWER Y.
    expect_x = [256.6, 244.6, 232.6, 220.6, 208.6, 196.6, 184.6, 172.6, 160.6, 148.6]
    expect_y = [218.5, 200.5, 182.5, 164.5, 146.5]
    check(p.col_x_values() == expect_x, f"tube col X wrong: {p.col_x_values()}")
    check(p.row_y_values() == expect_y, f"tube row Y wrong: {p.row_y_values()}")
    check(p.grid_center() == (202.6, 182.5),
          f"tube grid centre should be (202.6, 182.5), got {p.grid_center()}")
    check(p.xy(3, 4) == (208.6, 164.5), "last accuracy tube should be (208.6, 164.5)")

    # The measured anchor seats the rack better in slot 8 than the shifted
    # estimate did: 4.8 mm from the slot centre against the estimate's 8.7.
    cx, cy = p.grid_center()
    sx, sy = d.slot_center(8)
    off = ((cx - sx) ** 2 + (cy - sy) ** 2) ** 0.5
    check(off < 5.0, f"tube grid centre should sit within 5 mm of slot 8, got {off:.2f}")


def test_tiprack_corner_solve():
    """Real pitch is 9.0909 / 9.10, not the nominal 9.00."""
    d = Deck.load(TEST_LAYOUT)
    p = d.placement("tips")

    check(abs(p.col_vec[0] - 100.0 / 11) < 1e-9,
          f"tiprack col pitch should be 100/11, got {p.col_vec[0]}")
    check(abs(p.row_vec[1] + 63.7 / 7) < 1e-9,
          f"tiprack row pitch should be -63.7/7, got {p.row_vec[1]}")
    # The solve must close exactly on the two far measured corners.
    check(p.xy(0, 11) == (118.0, 399.2), f"col 11 should be X118, got {p.xy(0,11)}")
    check(p.xy(7, 0) == (18.0, 335.5), f"row 7 should be Y335.5, got {p.xy(7,0)}")
    check(p.grid_center() == (68.0, 367.35),
          f"tiprack centre should be (68, 367.35), got {p.grid_center()}")

    # Assuming 9.00 would land 1.0 mm off by the last column.
    naive = 18.0 + 11 * 9.0
    check(abs(p.xy(0, 11)[0] - naive - 1.0) < 1e-9,
          "a 9.00 mm assumption should be exactly 1.0 mm short at column 11")


def test_out_of_range_well():
    d = Deck.load(TEST_LAYOUT)
    p = d.placement("tubes")
    raises(IndexError, lambda: p.xy(5, 0), "row 5 of a 5-row rack should raise")
    raises(IndexError, lambda: p.xy(0, 10), "col 10 of a 10-col rack should raise")


def test_keepout_misses_the_tubes_in_use():
    """In the corrected frame the ejector band clears every tube the test visits.

    Before the +70 correction the band appeared to cover columns 2 and 3, which
    the accuracy protocol uses. It actually covers 8 and 9, which nothing uses.
    """
    d = Deck.load(TEST_LAYOUT)
    p = d.placement("tubes")
    band = next(k for k in d.keepouts if k.id == "tip_ejector")
    inside = [c for c in range(p.labware.cols) if band.contains_x(p.xy(0, c)[0])]
    check(inside == [8, 9],
          f"tube columns inside the X155 band should be [8, 9], got {inside}")
    used = [c for c in inside if c <= 4]
    check(not used,
          f"the accuracy test uses columns 0-4; these are in the band: {used}")


# ==========================================================================
# calibration
# ==========================================================================

def test_p300_calibration():
    e = eff_mod.load("p300")
    check(isinstance(e, Pipette), "p300 should load as a Pipette")
    expect = {30.0: (1.721, 23.279), 150.0: (8.117, 16.883),
              225.0: (12.114, 12.886), 300.0: (16.112, 8.888)}
    for v, (d_mm, asp) in expect.items():
        check(e.displacement_mm(v) == d_mm,
              f"p300 d({v}) should be {d_mm}, got {e.displacement_mm(v)}")
        check(e.aspirate_pos(v) == asp,
              f"p300 asp({v}) should be {asp}, got {e.aspirate_pos(v)}")
    check(abs(e.max_volume_uL - 429.2308) < 1e-3,
          f"p300 max volume should be ~429.23, got {e.max_volume_uL}")


def test_p125_calibration():
    e = eff_mod.load("p125")
    expect = {12.5: (1.642, 23.358), 62.5: (8.012, 16.988),
              93.75: (11.993, 13.007), 125.0: (15.974, 9.026)}
    for v, (d_mm, asp) in expect.items():
        check(e.displacement_mm(v) == d_mm,
              f"p125 d({v}) should be {d_mm}, got {e.displacement_mm(v)}")
        check(e.aspirate_pos(v) == asp,
              f"p125 asp({v}) should be {asp}, got {e.aspirate_pos(v)}")
    check(abs(e.max_volume_uL - 180.1491) < 1e-3,
          f"p125 max volume should be ~180.15, got {e.max_volume_uL}")


def test_volume_window():
    e = eff_mod.load("p300")
    raises(VolumeError, lambda: e.displacement_mm(5.0),
           "5 uL is below the p300 window and should raise")
    raises(VolumeError, lambda: e.displacement_mm(400.0),
           "400 uL is above the p300 window and should raise")


def test_max_volume_accounts_for_backoff():
    """The stroke check must include the post-aspirate backoff.

    The old script asserted gap_pos - d >= margin, which validated a POS the
    machine never visits: it then retracts a further `backoff` in air.
    """
    e = eff_mod.load("p300")
    naive = (e.gap_pos - e.stroke_margin - e.intercept) / e.slope
    check(e.max_volume_uL < naive - 15.0,
          f"max volume {e.max_volume_uL} should be well under the "
          f"backoff-ignoring {naive:.3f}")


# ==========================================================================
# effector loading
# ==========================================================================

def test_one_json_per_effector():
    avail = eff_mod.available()
    for eid in ("p125", "p300", "gripper"):
        check(eid in avail, f"{eid} should have its own config/effectors/{eid}.json")


def test_gripper_loads_but_refuses_plate_motion():
    """The command model is real; the geometry is not.

    The gripper used to be a placeholder that would not load at all. Its
    commands are now taken from the hardware's own API, so it loads - but
    every z_for_labware value is still null, so anything that would move it
    to a plate refuses. Loading and being ready are different questions.
    """
    g = eff_mod.load("gripper")          # no allow_placeholder needed now
    check(g.kind == "gripper", "gripper should dispatch to the Gripper class")
    check(not g.is_placeholder,
          "the gripper is unmeasured, not a placeholder")
    raises(EffectorError, g.require_ready,
           "unmeasured plate geometry should refuse to generate motion")
    check(any("z_for_labware" in u for u in g.uncalibrated()),
          f"the missing Z values must be on the record: {g.uncalibrated()}")


def test_gripper_models_timed_motion_not_position():
    """The servo has no position, so no *_pos field may come back."""
    g = eff_mod.load("gripper")
    a = g.spec["actuator"]
    pos_fields = [k for k in a if k.endswith("_pos")]
    check(not pos_fields,
          f"a positional field is meaningless on open-loop hardware: "
          f"{pos_fields}")
    check(g.spec["no_position"]["has_feedback"] is False,
          "the config must say outright that there is no feedback")


def test_gripper_refuses_to_open_past_the_firmware_clamp():
    """Opening has no hard stop; past full travel the racks leave the pinion."""
    g = eff_mod.load("gripper")
    # 765 is the firmware's OPEN_LIMIT_MS: 850 "still ran slightly long and
    # the jaws came off the racks" (boardpins.py).
    check(g.open_ms_max == 765, f"the clamp is 765 ms, got {g.open_ms_max}")
    g.check_open_ms(765)                                  # the limit itself is fine
    raises(EffectorError, lambda: g.check_open_ms(766),
           "one millisecond past the clamp must refuse")
    raises(EffectorError, lambda: g.check_open_ms(0),
           "a zero-length open should raise rather than emit nothing")


def test_gripper_refuses_a_current_outside_the_window():
    g = eff_mod.load("gripper")
    for ok in (100, 300, 500):
        g.check_grip_ma(ok)
    for bad in (99, 501, 570):
        raises(EffectorError, lambda b=bad: g.check_grip_ma(b),
               f"{bad} mA is outside 100-500 and must refuse")


def test_gripper_will_not_emit_the_relative_move_macros():
    """GRIPPER_PICK and GRIPPER_PLACE invert Z for this machine.

    Both emit a relative move with a normal printer's sign convention - a
    positive Z they call a lift. Here +Z is DOWN, so that drives the gripper
    into the deck. They are named in the config so the library can refuse
    them by name rather than relying on nobody calling them.
    """
    g = eff_mod.load("gripper")
    check(set(g.forbidden_macros) == {"GRIPPER_PICK", "GRIPPER_PLACE"},
          f"both relative-move macros must be forbidden, got "
          f"{g.forbidden_macros}")
    m = _gripper_machine()
    m.startup()
    m.effector.spec["macros"]["status"] = "GRIPPER_PICK"   # force the path
    raises(ProtocolError, m.gripper_status,
           "emitting a forbidden macro must refuse")


def _gripper_machine(**kw):
    from lab.machine import Machine
    return Machine(effector="gripper", name="t", layout=TEST_LAYOUT,
                   temp_log=False, **kw)


def test_gripper_must_be_homed_before_it_moves():
    m = _gripper_machine()
    m.startup()
    raises(ProtocolError, m.gripper_open,
           "opening before homing must refuse - the firmware refuses it too")
    raises(ProtocolError, lambda: m.grip(ma=300),
           "gripping before homing must refuse")
    m.gripper_home()
    check(m.gripper_homed, "home should set the flag")
    m.gripper_open()                                      # now legal


def test_every_gripper_macro_is_fenced_by_m400():
    """Klipper queues moves; without M400 the shell command runs early."""
    m = _gripper_machine()
    m.startup()
    m.gripper_home()
    emitted = [ln for ln in m.g.getvalue().splitlines() if ln.strip()]
    i = emitted.index("GRIPPER_HOME")
    check(emitted[i - 1].startswith("M400"),
          f"M400 must immediately precede the macro, got {emitted[i - 1]!r}")
    # And the guard dwell follows, while shell blocking is unproven.
    check(emitted[i + 1].startswith("G4"),
          f"a guard dwell should follow the macro, got {emitted[i + 1]!r}")


def test_release_above_the_pick_height_is_refused():
    """"Never release at height" is rule 1 of the gripper's API."""
    m = _gripper_machine()
    m.startup()
    m.gripper_home()
    m.gripper_open()
    m._move_z(40.0, 600, "probe")        # +Z is DOWN, so this is low
    m.grip(ma=300, what="plate")
    check(m.gripper_holding == "plate", "the grip should be recorded")
    m._move_z(10.0, 600, "probe")        # lifted 30 mm
    raises(ProtocolError, m.release,
           "releasing 30 mm above the pick height would drop the plate")
    m._move_z(40.0, 600, "probe")        # back down
    m.release()
    check(m.gripper_holding is None, "release should clear the hold")


def test_a_carried_plate_is_no_fly_over_the_tip_rack():
    """A plate shears the standing tips off exactly as a fitted tip does."""
    m = _gripper_machine()
    m.startup()
    m.gripper_home()
    m.gripper_open()
    m.grip(ma=300, what="plate")
    rects = m.deck.no_fly_rects()
    check(rects, "the tip rack should be a no-fly zone")
    box = rects[0][1]
    mid_y = (box[2] + box[3]) / 2.0
    m.goto(box[1] + 20.0, mid_y, op="carry")
    raises(SimulationError,
           lambda: m._move_xy(box[0] - 20.0, mid_y, 6000, "carry", order="X"),
           "carrying a plate straight across the tip rack must fail")


def test_a_hold_that_outlasts_its_expiry_is_refused():
    """A grip is a controlled stall and de-energises at about 140 s."""
    m = _gripper_machine()
    m.startup()
    m.gripper_home()
    m.gripper_open()
    m.grip(ma=300, what="plate")
    m.dwell(30.0)                        # past tier 1, still holding
    check(any("tier-1" in w for w in m.sim.report.warnings),
          f"passing tier 1 should warn: {m.sim.report.warnings}")
    raises(ProtocolError, lambda: m.dwell(120.0),
           "dwelling past the expiry must refuse rather than drop the plate")


def _station9(**kw):
    import importlib
    import gripper_station9_test as g9
    importlib.reload(g9)
    with tempfile.TemporaryDirectory() as td:
        kw.setdefault("plot", False)
        return g9.build(outdir=td, layout=TEST_LAYOUT, **kw)[0]


def test_station9_heights_run_in_the_order_the_operator_specified():
    """approach 90 -> grip 107 -> lift 90 -> lower 100 -> retreat 90.

    +Z is DOWN throughout, so 107 is the lowest point and 90 the highest.
    """
    m = _station9(cycles=1)
    # Select by op rather than slicing the list: startup and park also move
    # Z, and an index that happens to work today breaks the moment either
    # gains a step.
    cycle_ops = ("approach", "descend", "lift", "lower", "retreat")
    cycle = [s.to[2] for s in m.steps
             if s.kind == "move" and s.axes == "Z" and s.op in cycle_ops]
    check(cycle == [90.0, 107.0, 90.0, 100.0, 90.0],
          f"the cycle should be 90, 107, 90, 100, 90; got {cycle}")

    d = m.deck.station("gripper_pick_9")
    check((round(d.xy[0], 2), round(d.xy[1], 2)) == (69.50, 178.20),
          f"station 9 pick point should be (69.50, 178.20), got {d.xy}")
    xy = [s for s in m.steps if s.kind == "move" and s.axes in ("X", "Y")]
    check(xy and all(abs(s.to[2] - m.deck.z_travel) < 1e-6 for s in xy),
          "every XY leg must happen at travel height")


def _gripper_capability_config(**flags):
    """Temp LAB_CONFIG with capture_concurrency overridden."""
    import json as _json, shutil
    from lab import config as cfgmod
    d = Path(tempfile.mkdtemp())
    for src in cfgmod.config_dir().glob("*.json"):
        shutil.copy(src, d / src.name)
    shutil.copytree(cfgmod.config_path("effectors"), d / "effectors")
    spec = _json.loads((d / "effectors" / "gripper.json").read_text())
    spec.setdefault("capture_concurrency", {}).update(flags)
    (d / "effectors" / "gripper.json").write_text(_json.dumps(spec))
    return d


def test_the_spanning_capture_is_emitted_once_the_board_can_take_commands():
    """API.md's sequence, gated on a capability the firmware lacks.

    The host half of that documentation is real - imu_session.py forks, so
    the Pi does not block. The board half is not: gripnode.imu_read() is a
    blocking loop and drain() says the queue is not read until it returns.
    Commit 965823c changed API.md and nothing else.

    So both sequences live here and one flag picks between them. This pins
    the one that is NOT currently in use, so enabling it is a tested change
    rather than a hopeful one.
    """
    import importlib
    import gripper_station9_test as g9

    with _WithConfig(_gripper_capability_config(
            commands_during_capture=True)):
        importlib.reload(g9)
        with tempfile.TemporaryDirectory() as td:
            m, _ = g9.build(outdir=td, layout=TEST_LAYOUT, plot=False)

        def first(op):
            return next(s.i for s in m.steps
                        if s.op == op and s.kind == "macro")
        start, stop = first("gripper:imu_start"), first("gripper:imu_stop")
        grip, rel = first("gripper:grip"), first("gripper:release")

        check(start < grip < rel < stop,
              f"the capture must bracket BOTH the grip and the release: "
              f"start {start}, grip {grip}, release {rel}, stop {stop}")
        check(m.toggles["spanning_capture"] is True,
              "the manifest must record which sequence was emitted")

        settle = [s.i for s in m.steps if s.op == "post_release"]
        check(settle and rel < settle[0] < stop,
              "the settle after the release belongs inside the capture, or "
              "the 7 mm fall is not recorded")

        marks = [s.detail.get("label") for s in m.steps
                 if s.op == "gripper:imu_mark" and s.kind == "macro"]
        check(any("before_grip" in l for l in marks),
              f"the spanning sequence should mark before the grip: {marks}")
        check(any("released" in l for l in marks),
              f"and after the release: {marks}")

    importlib.reload(g9)


def test_the_default_sequence_keeps_the_gripper_commands_outside():
    """And the shipped config still emits the one the firmware supports."""
    m = _station9(cycles=1)
    check(m.toggles["spanning_capture"] is False,
          "the shipped capability must still be off")

    def first(op):
        return next(s.i for s in m.steps if s.op == op and s.kind == "macro")
    check(first("gripper:grip") < first("gripper:imu_start"),
          "the grip must precede the capture while the board queues commands")
    check(first("gripper:imu_stop") < first("gripper:release"),
          "and the capture must be stopped before the release")


def test_spanning_mode_uses_a_plate_only_capture():
    """Only the plate IMU can record through a gripper command.

    The board IMU goes over CAN, so capturing it means CMD_IMU_READ, which
    gripnode runs as a blocking loop - a command sent during one is queued,
    not executed. The plate IMU is a local I2C read on the Pi.
    """
    import importlib
    import gripper_station9_test as g9
    importlib.reload(g9)
    with tempfile.TemporaryDirectory() as td:
        m, _ = g9.build(outdir=td, layout=TEST_LAYOUT, plot=False,
                        imu_mode="spanning")

    check(m.toggles["plate_only"] is True,
          "spanning must use a plate-only capture while the board blocks")
    start = next(s for s in m.steps
                 if s.op == "gripper:imu_start" and s.kind == "macro")
    check(start.detail["plate_only"] is True,
          "the macro must carry PLATE_ONLY")
    emitted = [ln for ln in m.g.getvalue().splitlines() if "IMU_START" in ln]
    check(emitted and "PLATE_ONLY=1" in emitted[0],
          f"PLATE_ONLY=1 must reach the gcode, got {emitted[:1]}")

    def first(op):
        return next(s.i for s in m.steps if s.op == op and s.kind == "macro")
    check(first("gripper:imu_start") < first("gripper:grip")
          < first("gripper:release") < first("gripper:imu_stop"),
          "a plate-only capture must bracket both the grip and the release")


def test_spanning_is_refused_without_a_capability_that_allows_it():
    """Neither route available means the grip would be queued."""
    import importlib
    import gripper_station9_test as g9
    with _WithConfig(_gripper_capability_config(
            commands_during_capture=False, plate_only_spans_commands=False)):
        importlib.reload(g9)
        with tempfile.TemporaryDirectory() as td:
            raises(ProtocolError,
                   lambda: g9.build(outdir=td, layout=TEST_LAYOUT,
                                    plot=False, imu_mode="spanning"),
                   "spanning with no route to it must refuse, not emit a "
                   "sequence whose grip never runs")
    importlib.reload(g9)


def test_a_plate_only_session_allows_gripper_commands():
    m = _gripper_machine()
    m.startup()
    m.gripper_home()
    m.gripper_open()
    m.gripper_imu_start(label="spanning01", plate_only=True)
    m.grip(ma=300)                       # allowed: nothing is on the bus
    m.release()
    m.gripper_imu_stop()

    # and a capture that includes the board IMU still refuses
    m.gripper_imu_start(label="both01")
    raises(ProtocolError, lambda: m.grip(ma=300),
           "a capture including the board IMU still blocks commands")
    m.gripper_imu_stop()


def test_station9_preflights_before_it_moves():
    """An uninstalled gripper_macros.cfg must show up before G28.

    Klipper does not always stop on an unknown command: observed 2026-10-06,
    a run with the macros missing reported "Unknown command: GRIPPER_HOME"
    and carried on, so the gantry did the whole sequence with the gripper
    inert. Nothing here can force an abort - what it can do is put the first
    failure before any motion.
    """
    m = _station9(cycles=1)
    first_macro = next(s for s in m.steps if s.kind == "macro")
    first_move = next(s for s in m.steps
                      if s.kind in ("move", "home"))
    check(first_macro.op == "gripper:version",
          f"the version must come first - a stale INSTALLED cfg is the "
          f"other way this fails, and it fails on a line the repo has "
          f"already fixed. Got {first_macro.op}")
    ops = [s.op for s in m.steps if s.kind == "macro"][:3]
    check(ops == ["gripper:version", "gripper:ping", "gripper:status"],
          f"preflight should be version, ping, status; got {ops}")
    check(first_macro.i < first_move.i,
          f"the preflight must precede any motion: ping at {first_macro.i}, "
          f"first move at {first_move.i}")
    check(any("Unknown command" in n for n in m.notes),
          f"the gcode must say what an unknown command means: {m.notes[:3]}")


def test_station9_lets_the_host_name_and_publish_the_capture():
    """LABEL is enough: the host adds the timestamp and copies to the NAS.

    It used to be told an explicit CSV and DEST. Both are host defaults now
    (~/gripper_captures, then /mnt/nas/GC_ARCCS/GripperIMU), and a timestamp
    is always appended - so forcing a path here would only create a second
    place for the two to disagree. What this still has to pin is that a
    capture is NAMED, because an unnamed one cannot be found afterwards.
    """
    m = _station9(cycles=1)
    start = next(s for s in m.steps
                 if s.op == "gripper:imu_start" and s.kind == "macro")
    check(start.detail.get("label"),
          f"every capture must carry a label, got {start.detail}")
    check(start.detail.get("csv") is None and start.detail.get("dest") is None,
          f"neither path should be forced while the host defaults are right: "
          f"csv={start.detail.get('csv')} dest={start.detail.get('dest')}")
    emitted = [ln for ln in m.g.getvalue().splitlines() if "IMU_START" in ln]
    check(emitted and "CSV=" not in emitted[0] and "DEST=" not in emitted[0],
          f"and neither should reach the gcode: {emitted[:1]}")

    # An override still works, and still goes somewhere local.
    import importlib
    import gripper_station9_test as g9
    importlib.reload(g9)
    with tempfile.TemporaryDirectory() as td:
        m2, _ = g9.build(outdir=td, layout=TEST_LAYOUT, plot=False,
                         csv_dir="/home/admin/imu")
    s2 = next(s for s in m2.steps
              if s.op == "gripper:imu_start" and s.kind == "macro")
    check(s2.detail["csv"].startswith("/home/admin/"),
          f"an overridden capture path must stay local, got {s2.detail['csv']}")


def test_a_unc_path_is_refused_as_an_imu_destination():
    """The capture runs on Linux; a UNC path is not a path there.

    Passed through, it would become a filename with backslashes in it and
    the data would land somewhere nobody looks.
    """
    m = _gripper_machine()
    m.startup()
    m.gripper_home()
    raises(ProtocolError,
           lambda: m.gripper_imu_start(csv="/home/admin/imu/x.csv",
                                       dest=r"\\ICE-NAS\Data\GC_ARCCS"),
           "a Windows UNC destination must refuse")
    m.gripper_imu_start(csv="/home/admin/imu/x.csv",
                        dest="/mnt/nas/GC_ARCCS/GripperIMU")
    m.gripper_imu_stop()


def test_station9_imu_brackets_the_pick_and_marks_each_phase():
    m = _station9(cycles=1)
    ops = [(s.i, s.op) for s in m.steps if s.kind == "macro"]
    starts = [i for i, op in ops if op == "gripper:imu_start"]
    stops = [i for i, op in ops if op == "gripper:imu_stop"]
    marks = [i for i, op in ops if op == "gripper:imu_mark"]
    grips = [i for i, op in ops if op == "gripper:grip"]
    rels = [i for i, op in ops if op == "gripper:release"]

    check(len(starts) == len(stops) == 1,
          f"one capture per cycle, got {len(starts)} starts {len(stops)} stops")
    # The grip and release transients are unrecordable by the IMU, so they
    # are taken from the board's own motion telemetry instead.
    gm = next(s for s in m.steps if s.op == "gripper:grip" and s.kind == "macro")
    rm = next(s for s in m.steps
              if s.op == "gripper:release" and s.kind == "macro")
    check(gm.detail.get("log_csv", "").endswith("_grip.csv"),
          f"the grip should log its own current trace, got "
          f"{gm.detail.get('log_csv')}")
    check(rm.detail.get("log_csv", "").endswith("_release.csv"),
          f"the release should log its own trace, got "
          f"{rm.detail.get('log_csv')}")
    # The board streams IMU samples in a blocking loop and does not read its
    # command queue until the capture ends, so no gripper command may sit
    # inside one - it would be queued and fire minutes later.
    check(grips[0] < starts[0],
          "the grip must come BEFORE the capture opens")
    check(stops[0] < rels[0],
          "the capture must be stopped BEFORE the release is commanded")
    check(len(marks) == 4,
          f"four phase marks - gripped, lifted, held, at_release; got "
          f"{len(marks)}")
    check(all(starts[0] < i < stops[0] for i in marks),
          "every mark must fall inside the open capture")

    # kind=="macro" matters: the guard dwell that follows each macro is
    # recorded under the SAME op name, so an unfiltered list is half Nones.
    labels = [s.detail.get("label") for s in m.steps
              if s.op == "gripper:imu_mark" and s.kind == "macro"]
    check(all(l and l.startswith("pick01_") for l in labels),
          f"each mark must name the pick it belongs to: {labels}")
    check(any("gripped_500ma" in l for l in labels),
          f"the grip current belongs in the label: {labels}")


def test_no_gripper_command_may_be_sent_during_a_capture():
    """The board queues commands it receives while streaming.

    board/gripnode.py imu_read() is a blocking loop, and drain() says it
    outright: "the queue is not read until the motion returns". A grip sent
    during a capture is not refused by the board - it is QUEUED, and fires
    when the capture ends, long after the gantry has moved on. Observed
    2026-10-06 as a gripctl timeout with no reply.
    """
    m = _gripper_machine()
    m.startup()
    m.gripper_home()
    m.gripper_open()
    m.gripper_imu_start(csv="/home/admin/imu/x.csv")
    for call, what in ((lambda: m.grip(ma=300), "grip"),
                       (lambda: m.gripper_open(), "open"),
                       (lambda: m.gripper_home(), "home"),
                       (lambda: m.gripper_status(), "status")):
        raises(ProtocolError, call,
               f"{what} during a capture must refuse - the board would queue "
               f"it")
    # A mark and the stop do get through: ABORT and STOP set the flag
    # immediately rather than being queued.
    m.gripper_imu_mark("still_ok")
    m.gripper_imu_stop()
    m.grip(ma=300)                       # and now it is allowed again


def test_station9_marks_and_sessions_refuse_outside_a_capture():
    m = _station9(cycles=1)
    raises(ProtocolError, lambda: m.gripper_imu_mark("stray"),
           "a mark with no capture open would be written to nothing")
    raises(ProtocolError, m.gripper_imu_stop,
           "stopping a capture that is not running should raise")


def test_station9_declares_the_seven_millimetre_drop():
    """Releasing 7 mm above the grip is deliberate, and must be said."""
    m = _station9(cycles=1)
    check(m.toggles["release_drop_mm"] == 7.0,
          f"the drop should be 107 - 100 = 7 mm, got "
          f"{m.toggles['release_drop_mm']}")
    check(any("falls that far" in w for w in m.sim.report.warnings),
          f"a declared drop must still warn: {m.sim.report.warnings}")


def test_an_undeclared_drop_is_still_refused():
    """The rule moved from "never" to "this far, on purpose" - not away."""
    m = _gripper_machine()
    m.startup()
    m.gripper_home()
    m.gripper_open()
    m.move_to_z(107.0, op="probe")
    m.grip(ma=500, what="part")
    m.move_to_z(100.0, op="probe")        # 7 mm above the grip
    raises(ProtocolError, m.release,
           "the default max_drop_mm is 0, so this must refuse")
    raises(ProtocolError, lambda: m.release(max_drop_mm=6.0),
           "a declared drop smaller than the real one must still refuse")
    m.release(max_drop_mm=7.0)            # declared, allowed
    check(m.gripper_holding is None, "the release should have happened")


def test_station9_repeats_cleanly_for_fifteen_cycles():
    m = _station9(cycles=15)
    check(m.toggles["grips"] == 15, f"15 grips, got {m.toggles['grips']}")
    check(m.toggles["imu_sessions"] == 15,
          f"one capture per pick, got {m.toggles['imu_sessions']}")
    check(m.toggles["imu_marks"] == 4 * 15, f"4 marks x 15 picks, got "
          f"{m.toggles['imu_marks']}")
    check(m.gripper_holding is None, "the run must not end holding the part")

    # Every cycle re-homes, because timed travel drifts.
    homes = [s for s in m.steps
             if s.op == "gripper:home" and s.kind == "macro"]
    check(len(homes) == 15, f"one re-home per cycle, got {len(homes)}")

    # And each capture is named for its own pick, so the files do not collide.
    labels = [s.detail.get("label") for s in m.steps
              if s.op == "gripper:imu_start" and s.kind == "macro"]
    check(len(set(labels)) == 15,
          f"15 distinctly labelled captures, got {len(set(labels))}")
    check(labels[0].endswith("_01") and labels[-1].endswith("_15"),
          f"captures should be numbered per pick: {labels[0]} .. {labels[-1]}")


def _gripper_station_config(**z):
    """Temp LAB_CONFIG with the station-9 heights overridden."""
    import json as _json, shutil
    from lab import config as cfgmod
    d = Path(tempfile.mkdtemp())
    for src in cfgmod.config_dir().glob("*.json"):
        shutil.copy(src, d / src.name)
    shutil.copytree(cfgmod.config_path("effectors"), d / "effectors")
    spec = _json.loads((d / "effectors" / "gripper.json").read_text())
    spec["z_for_station"]["gripper_pick_9"].update(z)
    (d / "effectors" / "gripper.json").write_text(_json.dumps(spec))
    return d


def test_station9_refuses_heights_in_the_wrong_sense():
    """+Z is DOWN, so grip must be the LARGER number than approach.

    Getting this backwards is the single most likely way to write this
    protocol wrong, and it would drive the gripper upward onto the part.
    """
    import importlib
    import gripper_station9_test as g9

    # grip ABOVE approach
    with _WithConfig(_gripper_station_config(grip=80.0)):
        importlib.reload(g9)
        with tempfile.TemporaryDirectory() as td:
            raises(ProtocolError,
                   lambda: g9.build(outdir=td, layout=TEST_LAYOUT, plot=False),
                   "a grip height above the approach must refuse")

    # release outside the approach..grip band
    with _WithConfig(_gripper_station_config(release=120.0)):
        importlib.reload(g9)
        with tempfile.TemporaryDirectory() as td:
            raises(ProtocolError,
                   lambda: g9.build(outdir=td, layout=TEST_LAYOUT, plot=False),
                   "releasing below the grip would drive the part down")
    importlib.reload(g9)


def test_unknown_effector():
    raises(EffectorError, lambda: eff_mod.load("p1000"),
           "an unknown effector id should raise")


def test_p125_has_no_tips_or_prewet():
    e = eff_mod.load("p125")
    check(not e.needs_tip, "p125 config says it takes no tip")
    check(not e.prewet_enabled, "p125 config says it does not prewet")


# ==========================================================================
# machine state guards
# ==========================================================================

def _machine(effector="p300", **kw):
    kw.setdefault("layout", TEST_LAYOUT)
    return Machine(effector=effector, name="unit_test", **kw)


def test_dispense_without_aspirate():
    m = _machine()
    m.startup()
    m.get_tip()
    raises(ProtocolError, lambda: m.dispense("tubes", 0, 0),
           "dispensing with nothing aspirated should raise")


def test_aspirate_without_tip():
    m = _machine()
    m.startup()
    raises(ProtocolError, lambda: m.aspirate_uL(150.0),
           "p300 aspirating with no tip should raise")


def test_double_aspirate():
    m = _machine()
    m.startup()
    m.get_tip()
    m.aspirate_uL(150.0)
    raises(ProtocolError, lambda: m.aspirate_uL(150.0),
           "aspirating while already holding fluid should raise")


def test_double_tip():
    m = _machine()
    m.startup()
    m.get_tip()
    raises(ProtocolError, lambda: m.get_tip(),
           "picking a second tip without ejecting should raise")


def test_eject_without_tip():
    m = _machine()
    m.startup()
    raises(ProtocolError, lambda: m.eject_tip(),
           "ejecting with no tip fitted should raise")


def test_goto_before_home():
    m = _machine()
    raises(ProtocolError, lambda: m.goto(100.0, 100.0),
           "moving before homing should raise")


def test_p125_rejects_tip_ops():
    m = _machine("p125")
    m.startup()
    raises(ProtocolError, lambda: m.get_tip(),
           "p125 takes no tips, so get_tip should raise")


# ==========================================================================
# simulator
# ==========================================================================

def test_slots_are_seated_correctly():
    """Every slot-mounted labware sits in the slot the operator named.

    Asserts the SHIPPED bench deliberately - config/layout.json is the per-run
    file, so this is the test that notices when it stops matching reality.
    """
    d = Deck.load()
    bad = d.slot_seating_report()
    check(not bad, "slot seating problems: " + "; ".join(bad))
    expect = {"tips": 3, "big_tubes": 5, "plate1": 1, "plate2": 2}
    got = {n: p.slot for n, p in d.placements.items() if p.slot is not None}
    check(got == expect, f"shipped bench should be {expect}, got {got}")
    check(d.near_slots().get("water") == 9, "water sits at slot 9")


def test_front_row_is_high_y():
    """Slots 1-3 are the front row (largest Y); 7-9 the back (smallest)."""
    d = Deck.load()
    front = [d.slot_center(n)[1] for n in (1, 2, 3)]
    mid = [d.slot_center(n)[1] for n in (4, 5, 6)]
    back = [d.slot_center(n)[1] for n in (7, 8, 9)]
    check(len(set(front)) == 1 and len(set(mid)) == 1 and len(set(back)) == 1,
          "each row should share one Y")
    check(front[0] > mid[0] > back[0],
          f"front Y {front[0]} should exceed mid {mid[0]} should exceed back {back[0]}")


def test_keepout_blocks_low_y_move():
    """A Y move inside the band below Z 60 must fail."""
    m = _machine()
    m.startup()
    m.get_tip()
    # Park inside the band, drop below the safe Z, then try to move Y.
    m.goto(155.0, 300.0)
    # 61 is past the band's safe-Z of 60 but still clear of the tipped stage
    # limit of 62.5, so this isolates the keep-out rule.
    m._move_z(61.0, 600, "unit_test")

    def bad_y():
        m._move_xy(155.0, 250.0, 6000, "unit_test", order="Y")

    raises(SimulationError, bad_y,
           "a Y move at X155 with Z 61 should be blocked by the keep-out band")


def test_xy_traverse_must_be_at_travel_height():
    m = _machine()
    m.startup()
    m.get_tip()
    m._move_z(40.0, 600, "unit_test")

    def bad_traverse():
        m._move_xy(200.0, 300.0, 6000, "unit_test", order="X")

    raises(SimulationError, bad_traverse,
           "an XY traverse below travel height should be blocked")


def test_envelope():
    """Off-envelope is refused by the router before anything is emitted.

    It used to be emitted and then caught by the simulator. Refusing up front
    is better - nothing enters the buffer - but it means the exception is a
    ProtocolError from the router rather than a SimulationError. Both are
    LabError, which is what a protocol catches.
    """
    from lab.effector import LabError
    m = _machine()
    m.startup()
    n = m.g.line_count()
    raises(LabError, lambda: m.goto(500.0, 100.0),
           "X 500 is outside the 0-420 envelope and should be blocked")
    check(m.g.line_count() == n,
          f"the refused move emitted {m.g.line_count() - n} gcode line(s)")


def test_tip_exit_path_over_occupied_positions():
    """Picking a tip away from the exit edge shears it on the way out."""
    m = _machine()
    m.startup()
    raises(SimulationError, lambda: m.goto(0, 0) or m.get_tip(row=0, col=5),
           "picking column 5 on a full rack should fail the exit-path check")


def test_eject_is_followed_by_home():
    """A generated ejection must always be followed by G28."""
    m = _machine()
    m.startup()
    m.get_tip()
    m.eject_tip()
    homes = [s for s in m.steps if s.kind == "home"]
    check(any(s.detail.get("reason") == "post-ejection" for s in homes),
          "eject_tip should emit a post-ejection home")
    check(not m.tip_fitted, "the tip should be gone after ejection")


def test_band_routing_avoids_y_motion_in_band():
    """Approaching a tube column inside the band must not move Y there."""
    m = _machine()
    m.startup()
    m.get_tip()
    m.aspirate_uL(150.0)
    m.dispense("tubes", row=0, col=3)  # X150, inside the band
    band = next(k for k in m.deck.keepouts if k.id == "tip_ejector")
    offenders = [
        s for s in m.steps
        if s.kind == "move" and "Y" in s.axes and s.frm
        and band.contains_x(s.frm[0]) and s.op != "eject_tip"
    ]
    check(not offenders,
          f"{len(offenders)} Y move(s) happened inside the keep-out band: "
          f"{[(s.op, s.frm) for s in offenders[:3]]}")


# ==========================================================================
# the Z envelope, measured 2026-08-26
# ==========================================================================

def test_stage_surface_and_tip_length():
    """114.75 bare, 62.5 with a tip, so the tip is 52.25 mm long."""
    d = Deck.load()
    check(d.z_stage_surface == 114.75, f"bare stage should be 114.75")
    check(d.catalogue["tiprack_300_96"].spec["tip"]["length"] == 52.25,
          "tip length should be 52.25, derived from 114.75 - 62.5")
    check(d.z_slot_edge_bare == 105.25,
          f"raised edge bare-ref should be 105.25, got {d.z_slot_edge_bare}")


def test_travel_height_is_now_verified_not_assumed():
    """z_travel 5.0 clears the raised edges with the longest tip fitted."""
    d = Deck.load()
    tip = d.catalogue["tiprack_300_96"].spec["tip"]["length"]
    edge_tipped = d.z_slot_edge_bare - tip
    check(edge_tipped == 53.0, f"tipped edge limit should be 53.0, got {edge_tipped}")
    check(d.z_travel < edge_tipped,
          f"travel {d.z_travel} must be above the tipped edge limit {edge_tipped}")
    check(edge_tipped - d.z_travel >= 40.0,
          f"only {edge_tipped - d.z_travel} mm of edge clearance at travel height")


def test_tipped_stage_crash_is_blocked():
    m = _machine()
    m.startup()
    m.get_tip()
    m.goto(202.0, 187.0)          # over the tube rack, on the stage
    raises(SimulationError, lambda: m._move_z(63.0, 600, "unit_test"),
           "Z 63 with a tip is past the 62.5 stage floor and should be blocked")


def test_bare_stage_crash_is_blocked():
    m = _machine("p125")          # p125 takes no tip
    m.startup()
    m.goto(202.0, 187.0)
    raises(SimulationError, lambda: m._move_z(116.0, 600, "unit_test"),
           "Z 116 bare is past the 114.75 stage floor and should be blocked")


def test_raised_edge_blocks_a_low_traverse_across_locations():
    """Crossing a location boundary below Z 53 with a tip clips an edge."""
    m = _machine()
    m.startup()
    m.get_tip()
    m.goto(202.0, 187.0)          # slot 8
    m._move_z(58.0, 600, "unit_test")   # legal: above the 62.5 floor

    def cross():
        m._move_xy(70.0, 187.0, 6000, "unit_test", order="X")  # slot 8 -> 9

    raises(SimulationError, cross,
           "a tipped traverse from slot 8 to slot 9 at Z 58 should clip an edge")


def test_wall_wipe_inside_one_location_is_still_allowed():
    """The wipe works at tube depth but never leaves the cell, so it is fine."""
    m = _machine()
    m.startup()
    m.get_tip()
    m.aspirate_uL(150.0)
    m.dispense("tubes", row=0, col=0)   # includes the wall wipe at Z 45 and 41
    wipes = [s for s in m.steps if s.op == "wall_wipe" and s.kind == "move"]
    check(wipes, "the dispense should have emitted wipe moves")
    cells = {m.deck.slot_cell(s.to[0], s.to[1]) for s in wipes}
    check(cells == {"8"}, f"the wipe should stay inside slot 8, touched {cells}")


def test_ejector_is_off_the_stage():
    """Z 145 is only reachable because the ejector hangs past the stage."""
    d = Deck.load()
    st = d.station("tip_eject").spec
    x = float(st["lane_x"])
    for key in ("approach_y", "engage_y", "retract_y"):
        y = float(st[key])
        check(not d.point_on_stage(x, y),
              f"ejector {key} ({x}, {y}) must be off the stage for Z "
              f"{st['z_below_hook']} to be safe")


def test_ejection_passes_the_envelope_checks():
    m = _machine()
    m.startup()
    m.get_tip()
    m.eject_tip()
    check(not m.sim.report.errors,
          f"the ejection should pass every envelope check: {m.sim.report.errors}")


# ==========================================================================
# labware heights and approach order
# ==========================================================================

def test_holder_heights_convert_to_commanded_z():
    """A height above the stage, not a stored Z: the Z depends on the tip."""
    from lab.simulate import Simulator
    d = Deck.load(TEST_LAYOUT)
    sim = Simulator(d, eff_mod.load("p300"))
    p = d.placement("tubes")
    check(p.labware.height_mm == 35.0, "the 0.65 mL holder is 35 mm tall")

    bare, _ = sim.labware_top_z(p, tip_fitted=False)
    tipped, _ = sim.labware_top_z(p, tip_fitted=True)
    check(bare == 79.75, f"35 mm tall tops out at Z 79.75 bare, got {bare}")
    check(tipped == 27.5, f"and at Z 27.5 with a tip, got {tipped}")
    check(d.z_travel < tipped,
          f"travel {d.z_travel} must clear the holder top {tipped}")
    check(abs(sim.max_labware_height() - 57.5) < 1e-9,
          f"max clearable height should be 57.5, got {sim.max_labware_height()}")


def test_empty_height_is_flagged_as_incomplete():
    """35 mm is the EMPTY holder; tubes and caps make it taller."""
    d = Deck.load(TEST_LAYOUT)
    check(d.placement("tubes").labware.height_is_empty_only,
          "the tube holder should be flagged as empty-height-only")
    m = _machine()
    m.startup()
    m.get_tip()
    m.aspirate_uL(150.0)
    m.dispense("tubes", 0, 0)
    check(any("EMPTY holder" in w for w in m.sim.report.warnings),
          "a run over the holder should warn that contents are unaccounted for")


def test_tube_holder_is_approached_y_first():
    """Caps make Y motion across the holder difficult, so Y goes first."""
    d = Deck.load(TEST_LAYOUT)
    check(d.placement("tubes").labware.approach_order == "YX",
          "the 0.65 mL holder should ask for YX")

    m = _machine()
    m.startup()
    m.get_tip()
    m.aspirate_uL(150.0)
    m.dispense("tubes", row=0, col=0)

    legs = [s for s in m.steps
            if s.op == "dispense" and s.kind == "move" and s.axes in ("X", "Y")]
    check([s.axes for s in legs] == ["Y", "X"],
          f"the approach should be Y then X, got {[s.axes for s in legs]}")

    # The Y leg must happen clear of the holder in X.
    p = d.placement("tubes")
    x0, x1, _, _ = p.well_bounds()
    yleg = [s for s in legs if s.axes == "Y"][0]
    check(not (x0 <= yleg.frm[0] <= x1),
          f"the Y leg ran at X {yleg.frm[0]}, inside the holder span "
          f"[{x0}, {x1}] - the whole point is to be clear of it")


def test_every_dispense_approach_is_y_first():
    import accuracy_test
    with tempfile.TemporaryDirectory() as td:
        m, _ = accuracy_test.build(effector_id="p300", name="order_probe",
                                   layout=TEST_LAYOUT, outdir=td, plot=False)
    legs = [s for s in m.steps
            if s.op == "dispense" and s.kind == "move" and s.axes in ("X", "Y")]
    orders, cur = [], []
    for s in legs:
        cur.append(s.axes)
        if s.axes == "X" and len(cur) >= 2:
            orders.append("".join(cur))
            cur = []
    check(orders == ["YX"] * 20,
          f"all 20 approaches should be YX, got {set(orders)}")


def test_1p5_holder_anchors_from_the_location_edges():
    """A1 resolves from the location edges to the MEASURED (252.50, 305.50).

    Tests the shipped layout rather than an injected copy: the holder is really
    in slot 5 now, so the real config is what should be asserted.
    """
    d = Deck.load()
    p = d.placement("big_tubes")
    cx, cy = d.slot_center(5)
    px, py = d.slot_pitch()

    check(p.slot == 5, f"the 1.5 mL holder should be in slot 5, got {p.slot}")
    check(p.xy(0, 0) == (252.5, 305.5),
          f"A1 should be the measured (252.5, 305.5), got {p.xy(0, 0)}")

    # The insets were calibrated FROM that measurement, so they must reproduce
    # it against slot 5's cell rather than the other way round.
    edge = p.labware.spec["a1_from_slot_edge"]
    check(edge["x_inset"] == 15.25 and edge["y_inset"] == 13.125,
          f"insets should be the calibrated 15.25 / 13.125, got "
          f"{edge['x_inset']} / {edge['y_inset']}")
    check(p.xy(0, 0) == (round(cx + px / 2 - edge["x_inset"], 4),
                         round(cy + py / 2 - edge["y_inset"], 4)),
          "A1 should equal the cell edges minus the stored insets")

    check(p.labware.rows == 3 and p.labware.cols == 6,
          f"should be 3x6, got {p.labware.rows}x{p.labware.cols}")
    check(abs(p.col_x_values()[0] - p.col_x_values()[1]) == 20.0,
          "columns should be 20 mm apart")
    check(abs(p.row_y_values()[0] - p.row_y_values()[1]) == 30.0,
          "rows should be 30 mm apart")

    # Well CENTRES inside the location.
    x0, x1, y0, y1 = p.well_bounds()
    check(x0 >= cx - px / 2 and x1 <= cx + px / 2
          and y0 >= cy - py / 2 and y1 <= cy + py / 2,
          f"the grid should fit inside its location, got X[{x0}, {x1}] "
          f"Y[{y0}, {y1}]")

    # And the TUBES too, which is what ruled out a 7th column: it would sit
    # 0.25 mm inside the low-X edge, so an 11 mm tube would overhang slot 6
    # by 5.25 mm.
    tube_d = 11.0
    overhang = [(c, x) for c, x in enumerate(p.col_x_values())
                if x - tube_d / 2 < cx - px / 2]
    check(not overhang,
          f"no column's tube may overhang the location; these do: {overhang}")

    check(p.labware.height_mm == 45.0, "the 1.5 mL holder is 45 mm tall")
    check(p.labware.approach_order == "YX",
          "the 1.5 mL holder should also ask for YX")
    check(not d.slot_seating_report(),
          f"seating should be clean: {d.slot_seating_report()}")


def test_ejection_variant_is_functionally_identical():
    """Adding ejection must change nothing before the ejection."""
    import accuracy_test
    with tempfile.TemporaryDirectory() as td:
        plain, _ = accuracy_test.build(effector_id="p300", name="fe_plain",
                                       layout=TEST_LAYOUT, outdir=td, plot=False)
        eject, _ = accuracy_test.build(effector_id="p300", name="fe_eject",
                                       eject_tip=True, layout=TEST_LAYOUT,
                                       outdir=td, plot=False)

    def working(m):
        return {(d["x"], d["y"], d["z"], d["target_volume_uL"],
                 d["aspirate_pos"], d["well"]) for d in m.dispenses}

    check(working(plain) == working(eject),
          "every dispense should be identical between the two")
    check(len(eject.steps) > len(plain.steps),
          "the ejecting version should have extra steps")
    check(plain.tip_fitted and not eject.tip_fitted,
          "only the ejecting version should end with the tip off")



# ==========================================================================
# well-form aspiration, and mixing
# ==========================================================================

def _p300_with_z(**depths):
    """Temp LAB_CONFIG with the unmeasured depths filled in provisionally.

    The real values are null on purpose, so any test that needs to exercise
    motion into a plate or the 1.5 mL tube has to supply its own.
    """
    import json as _json, shutil
    from lab import config as cfgmod
    d = Path(tempfile.mkdtemp())
    for src in cfgmod.config_dir().glob("*.json"):
        shutil.copy(src, d / src.name)
    shutil.copytree(cfgmod.config_path("effectors"), d / "effectors")
    spec = _json.loads((d / "effectors" / "p300.json").read_text())
    for lid, keys in depths.items():
        spec["z_for_labware"].setdefault(lid, {}).update(keys)
    (d / "effectors" / "p300.json").write_text(_json.dumps(spec))
    return d


class _WithConfig:
    def __init__(self, path):
        self.path = str(path)

    def __enter__(self):
        self.old = os.environ.get("LAB_CONFIG")
        os.environ["LAB_CONFIG"] = self.path
        return self

    def __exit__(self, *exc):
        if self.old is None:
            os.environ.pop("LAB_CONFIG", None)
        else:
            os.environ["LAB_CONFIG"] = self.old
        return False


def test_plate_column_direction_runs_toward_low_x():
    """A1 is at the HIGH-X end. Getting this backwards moves every well 99 mm.

    col_dir was [1, 0] until 2026-09-08, which put A1 at the low-X end and so
    placed the whole grid one full column span from where it actually is. The
    operator's measurement of plate1's A1 (387, 399) against the predicted
    (286.5, 396.5) is what exposed it - a 100.5 mm error, of which 99 mm is
    exactly the 11 x 9 mm span.
    """
    d = Deck.load()
    p = d.placement("plate1")
    check(p.col_vec[0] < 0,
          f"increasing column index must move toward LOWER X, got "
          f"col_vec {p.col_vec}")
    check(p.row_vec[1] < 0,
          f"increasing row index must move toward LOWER Y, got "
          f"row_vec {p.row_vec}")
    xs = p.col_x_values()
    check(xs[0] > xs[-1], f"A1's column must be the highest X, got {xs}")
    check(abs((xs[0] - xs[-1]) - 99.0) < 1e-9,
          f"the column span should be 11 x 9 = 99 mm, got {xs[0] - xs[-1]}")

    # The tiprack deliberately runs the other way, from its own corner solve.
    check(d.placement("tips").col_vec[0] > 0,
          "the tiprack's column sense is measured and runs toward HIGHER X; "
          "the two conventions must not be unified")


def test_plates_resolve_in_slots_1_and_2():
    """plate1's A1 is measured; plate2's is inferred from the same offset."""
    d = Deck.load()
    for name, slot, a1 in (("plate1", 1, (387.0, 399.0)),
                           ("plate2", 2, (251.0, 399.0))):
        p = d.placement(name)
        check(p.slot == slot, f"{name} should be in slot {slot}, got {p.slot}")
        check(p.xy(0, 0) == a1, f"{name} A1 should be {a1}, got {p.xy(0, 0)}")
        check((p.labware.rows, p.labware.cols) == (8, 12),
              f"{name} should be 8x12, got {p.labware.rows}x{p.labware.cols}")
        cx, cy = d.slot_center(slot)
        px, py = d.slot_pitch()
        x0, x1, y0, y1 = p.well_bounds()
        check(x0 >= cx - px / 2 and x1 <= cx + px / 2
              and y0 >= cy - py / 2 and y1 <= cy + py / 2,
              f"{name} should fit its cell, got X[{x0}, {x1}] Y[{y0}, {y1}]")

    # The stored offset must reproduce the MEASUREMENT it was derived from,
    # not the other way round.
    off = d.catalogue["sbs_96_wellplate"].spec["a1_from_center"]
    cx, cy = d.slot_center(1)
    check((cx + off[0], cy + off[1]) == (387.0, 399.0),
          f"a1_from_center {off} against slot 1 must give the measured "
          f"(387, 399), got {(cx + off[0], cy + off[1])}")

    # The two wells centrifuge_test uses must clear the ejector band. Note the
    # band now covers plate2's HIGH column indices, not the low ones.
    band = next(k for k in d.keepouts if k.id == "tip_ejector")
    for name in ("plate1", "plate2"):
        for well in ((3, 5), (4, 6)):
            x = d.placement(name).xy(*well)[0]
            check(not band.contains_x(x),
                  f"{name} well {well} at X {x} is inside the keep-out band")
    inband = [c for c in range(12)
              if band.contains_x(d.placement("plate2").xy(0, c)[0])]
    check(inband == [10, 11],
          f"plate2's banded columns should be [10, 11], got {inband}")


def test_a_tipped_traverse_never_crosses_the_tip_rack():
    """The tips stand above the rack; a fitted tip shears on them.

    Not a Z problem - the fitted tip hangs below the nosecone, so travelling
    higher does not help. Observed on hardware 2026-09-08: the water station
    at X 70 sits underneath the rack in X, so every unrouted approach to water
    from high Y went straight through it.
    """
    import accuracy_test  # noqa: F401  (keeps the protocols path warm)
    import importlib
    import centrifuge_test
    importlib.reload(centrifuge_test)

    with _WithConfig(_p300_with_z()):
        with tempfile.TemporaryDirectory() as td:
            m, _ = centrifuge_test.build(layout=TEST_LAYOUT, outdir=td,
                                         plot=False)

    d = m.deck
    rects = d.no_fly_rects()
    check(rects, "the tip rack should be a no-fly zone")
    names = [n for n, _ in rects]
    check(names == ["tips"], f"only the tip rack should be no-fly, got {names}")

    bad = [
        (s.i, s.op, s.axes, s.frm[:2], s.to[:2])
        for s in m.steps
        if s.kind == "move" and s.axes in ("X", "Y") and s.tip
        and s.op != "get_tip"
        for _, rect in rects
        if d.segment_hits_rect(s.frm[:2], s.to[:2], rect)
    ]
    check(not bad, f"{len(bad)} tipped leg(s) cross the tip rack: {bad[:3]}")


def test_leaving_the_tip_box_goes_toward_x_zero_then_y():
    """Exit in X clear of the box, and only then move Y."""
    m = _machine()
    m.startup()
    m.get_tip()
    box = m.deck.no_fly_rects()[0][1]
    exit_legs = [s for s in m.steps
                 if s.op == "get_tip" and s.kind == "move" and s.tip
                 and s.axes in ("X", "Y")]
    check(exit_legs, "the pickup should be followed by an exit move")
    first = exit_legs[0]
    check(first.axes == "X" and first.to[0] < first.frm[0],
          f"the exit must move X toward 0, got {first.axes} "
          f"{first.frm[:2]} -> {first.to[:2]}")
    check(first.to[0] < box[0],
          f"the exit must clear the box's low-X edge {box[0]}, "
          f"stopped at {first.to[0]}")

    # The stopping point is one column pitch past the outermost well centre
    # plus the labware's own exit_clearance_mm - derived here from the same
    # config fields, so this test cannot drift from the number in use.
    p = m.deck.placement("tips")
    pitch = abs(p.col_vec[0])
    extra = p.labware.spec["consume_order"]["exit_clearance_mm"]
    want = min(p.col_x_values()) - pitch - extra
    check(abs(first.to[0] - want) < 1e-4,
          f"the exit should stop at X {want:.4f} (pitch {pitch:.4f} + "
          f"{extra} mm extra), got {first.to[0]}")
    check(extra > 0.0,
          f"exit_clearance_mm should buy real margin, got {extra}")
    check(box[0] - first.to[0] > pitch / 2.0,
          f"the exit clears the rack body by only "
          f"{box[0] - first.to[0]:.2f} mm")

    # The next leg must be the Y move, taken at that cleared X.
    n = len(m.steps)
    m.goto(70.0, 175.0, op="probe")
    legs = [s for s in m.steps[n:] if s.kind == "move" and s.axes in ("X", "Y")]
    check(legs and legs[0].axes == "Y",
          f"the move off the box should start with Y, got "
          f"{[s.axes for s in legs]}")
    check(legs[0].frm[0] < box[0],
          f"that Y leg must happen clear of the box, at X {legs[0].frm[0]}")


def test_hand_rolled_move_over_the_tip_rack_is_refused():
    """The router avoids it; the simulator must also catch a bypass.

    Straight after a pickup the effector sits clear of the box in X but still
    inside its Y span, so a bare X move across is the exact mistake.
    """
    m = _machine()
    m.startup()
    m.get_tip()
    box = m.deck.no_fly_rects()[0][1]
    here = m.xyz()
    check(here[0] < box[0] and box[2] <= here[1] <= box[3],
          f"expected to be clear in X and inside the box in Y, at {here[:2]}")

    raises(SimulationError,
           lambda: m._move_xy(box[1] + 20.0, here[1], 6000, "probe", order="X"),
           "an X leg straight across the tip rack with a tip on should fail")


def test_router_doglegs_in_y_to_cross_the_tip_rack():
    """Both ends inside the box's Y span: only a Y dogleg gets there."""
    m = _machine()
    m.startup()
    m.get_tip()
    box = m.deck.no_fly_rects()[0][1]
    n = len(m.steps)
    # plate1 D6 sits at Y 372, inside the box's Y span, on the far side in X.
    m.goto_placement("plate1", 3, 5, op="probe")
    legs = [s for s in m.steps[n:] if s.kind == "move" and s.axes in ("X", "Y")]
    check(len(legs) == 3, f"expected a 3-leg dogleg, got {[s.axes for s in legs]}")
    crossing = [(s.axes, s.frm[:2], s.to[:2]) for s in legs
                if m.deck.segment_hits_rect(s.frm[:2], s.to[:2], box)]
    check(not crossing, f"no leg may cross the box, these do: {crossing}")
    check(any("dogleg via Y" in w for w in m.sim.report.warnings),
          f"the Y dogleg should be recorded: {m.sim.report.warnings}")


def test_a_null_commanded_depth_refuses_rather_than_guessing():
    """A null COMMANDED depth must stop the run; a null crash LIMIT must warn.

    That distinction is the whole design: descending to a depth nobody measured
    is a crash, whereas not knowing where the floor is only means the check
    cannot be evaluated. Every commanded depth is measured now, so this proves
    both halves - the refusal against a deliberately blanked config, and the
    warning against the real one.
    """
    e = eff_mod.load("p300")

    # Nothing the protocols command may be null any more.
    null_commanded = [
        (lid, key)
        for lid, entry in e.spec["z_for_labware"].items()
        for key in ("work", "aspirate", "mix")
        if key in entry and entry[key] is None
    ]
    check(not null_commanded,
          f"these commanded depths are still null and would block a run: "
          f"{null_commanded}")

    # The refusal still works: blank one out and it stops the run. It has to
    # be the key the draw actually uses - 'aspirate' outranks 'work' now, so
    # nulling 'work' alone would leave a perfectly good depth in place.
    with _WithConfig(_p300_with_z(
            tube_rack_1p5={"work": None, "aspirate": None})):
        m = _machine()
        m.startup()
        m.get_tip()
        raises(EffectorError,
               lambda: m.aspirate_from("big_tubes", 0, 0, volume_uL=100.0),
               "a null aspiration Z should refuse rather than pick a depth")

    # And a null 'aspirate' refuses even though 'work' is measured: falling
    # back to the dispense depth would be a silent 19 mm shallower draw,
    # which on this tube means aspirating air off the top.
    with _WithConfig(_p300_with_z(
            tube_rack_1p5={"work": 35.0, "aspirate": None})):
        m = _machine()
        m.startup()
        m.get_tip()
        raises(EffectorError,
               lambda: m.aspirate_from("big_tubes", 0, 0, volume_uL=100.0),
               "a null 'aspirate' must not quietly fall back to 'work'")

    # The tube floor is unmeasured, and that WARNS instead of blocking.
    check(e.spec["z_for_labware"]["tube_rack_1p5"].get("bottom") is None,
          "this test assumes the 1.5 mL tube floor is still unmeasured")
    m = _machine()
    m.startup()
    m.get_tip()
    m.aspirate_from("big_tubes", 0, 0, volume_uL=100.0)
    check(any("well-bottom crash for tube_rack_1p5" in w
              for w in m.sim.report.warnings),
          f"an unmeasured floor should warn, not pass silently: "
          f"{m.sim.report.warnings}")


def test_plate_depths_are_measured_and_ordered():
    """work must sit above the well floor, and bottom is a limit not a target."""
    e = eff_mod.load("p300")
    plate = e.spec["z_for_labware"]["sbs_96_wellplate"]
    check(plate["work"] == 53.5, f"plate work should be 53.5, got {plate['work']}")
    check(plate["bottom"] == 60.8,
          f"plate bottom should be 60.8, got {plate['bottom']}")
    # +Z is DOWN, so the dispense depth must be a SMALLER number than the floor.
    check(plate["work"] < plate["bottom"],
          "the dispense depth must be above the well floor")
    check(plate["bottom"] - plate["work"] > 5.0,
          f"only {plate['bottom'] - plate['work']} mm of clearance above the floor")
    # And the floor itself must be reachable at all.
    from lab.simulate import Simulator
    sim = Simulator(Deck.load(), e)
    limit, missing = sim.effective_z_limit(tip_fitted=True)
    check(not missing and plate["bottom"] < limit,
          f"the plate floor {plate['bottom']} must be above the tipped stage "
          f"limit {limit}")


def test_well_bottom_crash_is_enforced_from_the_effector_table():
    """The limit is tip-dependent, so it cannot live in labware.json."""
    m = _machine()
    m.startup()
    m.get_tip()
    m.aspirate_uL(100.0)
    m.goto_placement("plate1", 3, 5)
    # 61.0 is past the measured 60.8 well floor.
    raises(SimulationError, lambda: m._move_z(61.0, 600, "probe",
                                              labware="sbs_96_wellplate"),
           "descending past the plate's well floor should be blocked")


def test_aspirate_from_reaches_the_well():
    """A draw uses the 'aspirate' depth, which outranks 'work'.

    The two are deliberately different numbers: a dispense wants to be above
    the liquid, a draw wants the orifice under it. The fixture sets both so
    the precedence is what is being asserted, not just the arithmetic.
    """
    with _WithConfig(_p300_with_z(
            tube_rack_1p5={"work": 45.0, "aspirate": 54.0})):
        m = _machine()
        m.startup()
        m.get_tip()
        pos = m.aspirate_from("big_tubes", 0, 0, volume_uL=100.0)
        e = eff_mod.load("p300")
        check(pos == e.aspirate_pos(100.0),
              f"should return the aspirate POS {e.aspirate_pos(100.0)}, got {pos}")
        check(m.fluid_uL == 100.0, f"should hold 100 uL, got {m.fluid_uL}")
        zs = [s.to[2] for s in m.steps
              if s.op == "aspirate" and s.kind == "move" and s.axes == "Z"]
        check(54.0 in zs,
              f"should have descended to the aspirate Z 54.0, got {zs}")
        check(45.0 not in zs,
              f"the draw must NOT use the shallower work Z 45.0, got {zs}")
        # It must honour the holder's Y-first approach.
        legs = [s for s in m.steps
                if s.op == "aspirate" and s.kind == "move" and s.axes in ("X", "Y")]
        check([s.axes for s in legs][:2] == ["Y", "X"],
              f"approach should be Y then X, got {[s.axes for s in legs][:2]}")
        check(m._last_aspirate["source"] == "tube_rack_1p5",
              "the aspirate record should name the source labware")


def test_aspirate_falls_back_to_work_without_an_aspirate_key():
    """Only labware that needs the distinction has to declare it.

    The water reservoir has one depth and no 'aspirate' key, so a station
    draw still uses 'work' - the new key is opt-in, not a migration.
    """
    e = eff_mod.load("p300")
    water = e.spec["z_for_labware"]["reservoir_water"]
    check("aspirate" not in water,
          f"this test assumes water has no aspirate key, got {sorted(water)}")

    m = _machine()
    m.startup()
    m.get_tip()
    m.aspirate_uL(150.0)
    zs = [s.to[2] for s in m.steps
          if s.op == "aspirate" and s.kind == "move" and s.axes == "Z"]
    check(water["work"] in zs,
          f"should have used water's work Z {water['work']}, got {zs}")


def test_the_tube_draws_deeper_than_it_dispenses():
    """The shipped numbers, and that both clear the floor limit."""
    e = eff_mod.load("p300")
    tube = e.spec["z_for_labware"]["tube_rack_1p5"]
    check(tube["work"] == 35.0, f"dispense should be 35.0, got {tube['work']}")
    check(tube["aspirate"] == 54.0,
          f"the draw should be 54.0, got {tube['aspirate']}")
    # +Z is DOWN, so drawing deeper means a LARGER number.
    check(tube["aspirate"] > tube["work"],
          "the draw must be deeper than the dispense, or it takes air off "
          "the top as the level falls")

    from lab.simulate import Simulator
    sim = Simulator(Deck.load(), e)
    limit, missing = sim.effective_z_limit(tip_fitted=True)
    check(not missing and tube["aspirate"] < limit,
          f"the draw depth {tube['aspirate']} must stay above the tipped "
          f"stage limit {limit}")


def test_aspirate_from_refuses_bad_state():
    with _WithConfig(_p300_with_z(tube_rack_1p5={"work": 45.0})):
        m = _machine()
        m.startup()
        raises(ProtocolError,
               lambda: m.aspirate_from("big_tubes", 0, 0, volume_uL=100.0),
               "aspirating from a well with no tip should raise")
        m.get_tip()
        m.aspirate_from("big_tubes", 0, 0, volume_uL=100.0)
        raises(ProtocolError,
               lambda: m.aspirate_from("big_tubes", 0, 1, volume_uL=100.0),
               "aspirating while already holding fluid should raise")

    with _WithConfig(_p300_with_z(tube_rack_1p5={"work": 45.0})):
        m = _machine()
        m.startup()
        m.get_tip()
        raises(IndexError,
               lambda: m.aspirate_from("big_tubes", 0, 99, volume_uL=100.0),
               "an out-of-range well should raise")


def test_mix_cycles_at_the_mix_depth():
    """Three distinct depths: work to dispense, mix to stir, bottom to avoid."""
    with _WithConfig(_p300_with_z(
            tube_rack_1p5={"work": 40.0, "mix": 45.0, "bottom": 55.0})):
        m = _machine()
        m.startup()
        m.get_tip()
        n = len(m.steps)
        m.mix("big_tubes", 0, 0, cycles=5, volume_uL=200.0)

        e = eff_mod.load("p300")
        top = round(e.blowout_pos - e.displacement_mm(200.0), e.decimals)
        acts = [s.pos_after for s in m.steps[n:] if s.kind == "actuator"]
        # Five draw/push pairs. The priming blowout only appears when the
        # plunger is not already bottomed - here startup() left it there, and
        # emitting a move to where it already is would be noise.
        check(acts == [top, e.blowout_pos] * 5,
              f"expected 5 draw/push pairs, got {acts}")
        check(m.pos == e.blowout_pos,
              f"a mix must end bottomed, got {m.pos}")
        check(m.fluid_uL == 0.0,
              f"a mix must not leave fluid held, got {m.fluid_uL}")

        # Every cycle happens at the MIX Z: deeper than work, above the floor.
        at = {s.to[2] for s in m.steps[n:]
              if s.kind == "move" and s.axes == "Z"}
        check(45.0 in at, f"should have descended to the mix Z 45.0, got {at}")
        check(55.0 not in at,
              "must NOT descend to the well bottom - that is a crash limit")
        # +Z is down, so deeper is a larger number.
        check(40.0 < 45.0 < 55.0, "work < mix < bottom, shallowest first")


def test_mix_refuses_bad_state():
    with _WithConfig(_p300_with_z(
            tube_rack_1p5={"work": 40.0, "mix": 45.0, "bottom": 55.0})):
        m = _machine()
        m.startup()
        m.get_tip()
        raises(ProtocolError,
               lambda: m.mix("big_tubes", 0, 0, cycles=0, volume_uL=200.0),
               "zero cycles should raise")
        raises(VolumeError,
               lambda: m.mix("big_tubes", 0, 0, cycles=3, volume_uL=400.0),
               "a stroke outside the calibration window should raise")
        m.aspirate_from("big_tubes", 0, 0, volume_uL=100.0)
        raises(ProtocolError,
               lambda: m.mix("big_tubes", 0, 0, cycles=3, volume_uL=200.0),
               "mixing while holding fluid should raise")


def test_goto_well_honours_approach_order():
    """It used to drop `prefer` and ignore the holders' Y-first rule."""
    m = _machine()
    m.startup()
    m.get_tip()
    n = len(m.steps)
    m.goto_well("big_tubes", 0, 0, op="probe")
    legs = [s.axes for s in m.steps[n:] if s.kind == "move" and s.axes in ("X", "Y")]
    check(legs == ["Y", "X"], f"should be Y then X, got {legs}")


def test_centrifuge_protocol_balances_the_plates():
    # Only the tube's work Z is still unmeasured; the rest is the real config.
    with _WithConfig(_p300_with_z(tube_rack_1p5={"work": 40.0})):
        import importlib
        import centrifuge_test
        importlib.reload(centrifuge_test)
        with tempfile.TemporaryDirectory() as td:
            m, _ = centrifuge_test.build(layout=TEST_LAYOUT, outdir=td,
                                         plot=False)

        dose = centrifuge_test.PLATE_DOSE_uL
        per_plate = {}
        for d in m.dispenses:
            per_plate.setdefault(d["placement"], []).append(d["target_volume_uL"])
        for pname in ("plate1", "plate2"):
            check(per_plate.get(pname) == [dose, dose],
                  f"{pname} should get 2x{dose:g} uL, got {per_plate.get(pname)}")
        check(sum(per_plate["plate1"]) == sum(per_plate["plate2"]),
              "the two plates must receive equal mass - that is the whole point")

        wells = {d["placement"]: [x["well"] for x in m.dispenses
                                 if x["placement"] == d["placement"]]
                 for d in m.dispenses}
        check(wells["plate1"] == wells["plate2"] == ["D6", "E7"],
              f"both plates should use D6 and E7, got {wells}")
        left = (centrifuge_test.TUBE_PRELOAD_uL + centrifuge_test.STOCK_uL
                - 2 * dose)
        check(m.toggles["tube_contents_at_end_uL"] == left,
              f"the tube should finish with {left:g} uL, got "
              f"{m.toggles['tube_contents_at_end_uL']}")
        check(left > 0.0,
              f"the design must leave liquid in the tube, got {left:g} uL")
        check(m.toggles["eject_tip"], "the tip should be ejected by default")
        check(not m.tip_fitted, "the run should end with no tip fitted")
        homes = [s for s in m.steps if s.kind == "home"]
        check(any(s.detail.get("reason") == "post-ejection" for s in homes),
              "the ejection must be followed by a G28, which returns home")


def test_centrifuge_protocol_gives_each_plate_one_source():
    """plate1 from water only, plate2 from the tube only.

    Both plates still finish at 200 uL, so the mass-balance test passes
    either way - which is exactly why the source needs its own assertion.
    The source is read off the aspirate that fed each dispense: an aspirate
    at the water station is a water dose, one at the tube's own coordinates
    is a stock dose.
    """
    with _WithConfig(_p300_with_z(tube_rack_1p5={"work": 40.0})):
        import importlib
        import centrifuge_test
        importlib.reload(centrifuge_test)
        with tempfile.TemporaryDirectory() as td:
            m, _ = centrifuge_test.build(layout=TEST_LAYOUT, outdir=td,
                                         plot=False)

        water_xy = m.deck.station("water").xy
        tube_xy = m.deck.placement("big_tubes").xy(0, 0)

        # Walk the steps in order; each dispense is fed by the most recent
        # aspirate, and where that aspirate happened names the source.
        source, sources = None, {}
        for s in m.steps:
            if s.op in ("aspirate", "aspirate_from") and s.kind == "move" \
                    and s.to is not None:
                at = (round(s.to[0], 1), round(s.to[1], 1))
                if at == (round(water_xy[0], 1), round(water_xy[1], 1)):
                    source = "water"
                elif at == (round(tube_xy[0], 1), round(tube_xy[1], 1)):
                    source = "stock"
            elif s.op == "dispense" and s.labware == "sbs_96_wellplate":
                p = next(d["placement"] for d in m.dispenses
                         if abs(d["x"] - s.to[0]) < 1e-6
                         and abs(d["y"] - s.to[1]) < 1e-6)
                sources.setdefault(p, set()).add(source)

        check(sources.get("plate1") == {"water"},
              f"plate1 should be water only, got {sources.get('plate1')}")
        check(sources.get("plate2") == {"stock"},
              f"plate2 should be tube stock only, got {sources.get('plate2')}")
        check(m.toggles["water_plate"] == "plate1"
              and m.toggles["stock_plate"] == "plate2",
              f"the manifest must name which plate got which source, got "
              f"{m.toggles.get('water_plate')} / "
              f"{m.toggles.get('stock_plate')}")

        # Still a valid centrifuge pair.
        per_plate = {}
        for d in m.dispenses:
            per_plate.setdefault(d["placement"], []).append(
                d["target_volume_uL"])
        want = 2 * centrifuge_test.PLATE_DOSE_uL
        check(sum(per_plate["plate1"]) == sum(per_plate["plate2"]) == want,
              f"both plates must still finish at {want:g} uL, got {per_plate}")


def test_centrifuge_protocol_refuses_one_plate_for_both_sources():
    """Naming the same plate twice is the old two-source behaviour."""
    with _WithConfig(_p300_with_z(tube_rack_1p5={"work": 40.0})):
        import importlib
        import centrifuge_test
        importlib.reload(centrifuge_test)
        with tempfile.TemporaryDirectory() as td:
            raises(ProtocolError,
                   lambda: centrifuge_test.build(
                       water_plate="plate1", stock_plate="plate1",
                       layout=TEST_LAYOUT, outdir=td, plot=False),
                   "one plate for both sources should raise")


def test_v21_corners_are_opposite_and_reachable():
    """H1 and A12: high X/low Y and low X/high Y, diagonally opposite."""
    with _WithConfig(_p300_with_z(tube_rack_1p5={"work": 40.0})):
        import importlib
        import centrifuge_test_v2_1 as v21
        importlib.reload(v21)
        with tempfile.TemporaryDirectory() as td:
            m, _ = v21.build(layout=TEST_LAYOUT, outdir=td, plot=False)

        p = m.deck.placement("plate2")
        wells = {d["well"]: (d["x"], d["y"]) for d in m.dispenses
                 if d["placement"] == "plate2"}
        check(set(wells) == {"H1", "A12"},
              f"plate2 should use the two corners, got {sorted(wells)}")

        xs = [x for x, _ in [p.xy(r, c) for r, c in p.corner_wells()]]
        ys = [y for _, y in [p.xy(r, c) for r, c in p.corner_wells()]]
        check(wells["H1"] == (max(xs), min(ys)),
              f"H1 should be the high-X low-Y corner {(max(xs), min(ys))}, "
              f"got {wells['H1']}")
        check(wells["A12"] == (min(xs), max(ys)),
              f"A12 should be the low-X high-Y corner {(min(xs), max(ys))}, "
              f"got {wells['A12']}")

        # Diagonally opposite means their midpoint is the grid centre, which
        # is what keeps the plate internally balanced.
        mid = ((wells["H1"][0] + wells["A12"][0]) / 2.0,
               (wells["H1"][1] + wells["A12"][1]) / 2.0)
        check(mid == p.grid_center(),
              f"the two corners should straddle the grid centre "
              f"{p.grid_center()}, got {mid}")


def test_v21_reaches_the_banded_corner_without_y_motion_in_the_band():
    """A12 sits at X 152, inside the ejector band X[148, 162].

    Reachable only because the router takes the Y leg at a clear X and comes
    in along X - a pure X move through the band is harmless, Y motion in it
    with a tip fitted is a hard error.
    """
    with _WithConfig(_p300_with_z(tube_rack_1p5={"work": 40.0})):
        import importlib
        import centrifuge_test_v2_1 as v21
        importlib.reload(v21)
        with tempfile.TemporaryDirectory() as td:
            m, _ = v21.build(layout=TEST_LAYOUT, outdir=td, plot=False)

        band = m.deck.keepouts[0]
        a12 = m.deck.placement("plate2").xy(0, 11)
        check(band.contains_x(a12[0]),
              f"this test assumes A12 at X {a12[0]} is inside the band "
              f"{band.x_range}; if the plate moved, retarget it")

        offenders = [(s.i, s.op, s.frm[:2], s.to[:2]) for s in m.steps
                     if s.kind == "move" and s.axes == "Y"
                     and band.contains_x(s.frm[0])
                     and not s.detail.get("keepout_exempt")]
        check(not offenders,
              f"no unexempt Y leg may happen inside the band: {offenders}")


def test_v21_tube_arithmetic_and_the_exactly_empty_refusal():
    """The tube must end with liquid in it, whatever the numbers are.

    Derived from the protocol's own constants rather than pinned, because the
    operator changes these and the invariant - a positive residual, and every
    dose landing in the stock plate - is what the test is actually about.
    """
    with _WithConfig(_p300_with_z(tube_rack_1p5={"work": 40.0})):
        import importlib
        import centrifuge_test_v2_1 as v21
        importlib.reload(v21)
        with tempfile.TemporaryDirectory() as td:
            m, _ = v21.build(layout=TEST_LAYOUT, outdir=td, plot=False)
            t = m.toggles
            n_wells = len(v21.PLATE2_WELLS)
            available = v21.TUBE_PRELOAD_uL + v21.STOCK_uL
            left = available - n_wells * v21.PLATE_DOSE_uL

            check(t["tube_contents_after_stock_uL"] == available,
                  f"{v21.TUBE_PRELOAD_uL:g} preloaded + {v21.STOCK_uL:g} added "
                  f"= {available:g}, got {t['tube_contents_after_stock_uL']}")
            check(t["tube_contents_at_end_uL"] == left,
                  f"should leave {left:g} uL, got {t['tube_contents_at_end_uL']}")
            check(left > 0.0,
                  f"the tube must not be drawn to nothing, got {left:g} uL")

            per_plate = {}
            for d in m.dispenses:
                per_plate.setdefault(d["placement"], []).append(
                    d["target_volume_uL"])
            check(per_plate.get("plate2") == [v21.PLATE_DOSE_uL] * n_wells,
                  f"plate2 should get {n_wells} x {v21.PLATE_DOSE_uL:g} uL, "
                  f"got {per_plate.get('plate2')}")
            check("plate1" not in per_plate,
                  f"nothing may be dispensed into plate1, got "
                  f"{per_plate.get('plate1')}")

            # A dose that divides the contents exactly empties the tube, and
            # that is refused however generous the stock is.
            raises(ProtocolError,
                   lambda: v21.build(plate_dose_uL=available / n_wells,
                                     layout=TEST_LAYOUT, outdir=td, plot=False),
                   f"{n_wells} x {available / n_wells:g} out of {available:g} "
                   f"leaves nothing and must raise")


def test_v21_says_out_loud_that_it_is_not_a_balanced_pair():
    """Location 1 is empty, so the protocol's own premise no longer holds."""
    with _WithConfig(_p300_with_z(tube_rack_1p5={"work": 40.0})):
        import importlib
        import centrifuge_test_v2_1 as v21
        importlib.reload(v21)
        with tempfile.TemporaryDirectory() as td:
            m, _ = v21.build(layout=TEST_LAYOUT, outdir=td, plot=False)
            check(any("does NOT produce a balanced centrifuge pair" in w
                      for w in m.sim.report.warnings),
                  f"an unbalanced run must say so: {m.sim.report.warnings}")
            check(m.toggles["water_wells"] == [],
                  f"no water wells, got {m.toggles['water_wells']}")

            # Putting water back restores the pair, and the warning goes.
            m2, _ = v21.build(water_wells=((3, 5), (4, 6)),
                              layout=TEST_LAYOUT, outdir=td, plot=False)
            check(not any("balanced centrifuge pair" in w
                          for w in m2.sim.report.warnings),
                  "loading both plates should clear the warning")
            per_plate = {}
            for d in m2.dispenses:
                per_plate.setdefault(d["placement"], []).append(
                    d["target_volume_uL"])
            check(sum(per_plate["plate1"]) == sum(per_plate["plate2"]),
                  f"with both loaded the masses must match, got {per_plate}")

            # And an empty stock list is not a run at all.
            raises(ProtocolError,
                   lambda: v21.build(stock_wells=(), layout=TEST_LAYOUT,
                                     outdir=td, plot=False),
                   "a run that dispenses nothing should raise")


def test_centrifuge_protocol_checks_its_own_arithmetic():
    with _WithConfig(_p300_with_z(tube_rack_1p5={"work": 40.0})):
        import importlib
        import centrifuge_test
        importlib.reload(centrifuge_test)
        with tempfile.TemporaryDirectory() as td:
            # Nothing tracks liquid in a well, so the protocol must catch this.
            raises(ProtocolError,
                   lambda: centrifuge_test.build(
                       tube_preload_uL=0.0, stock_uL=100.0,
                       layout=TEST_LAYOUT, outdir=td, plot=False),
                   "withdrawing more than the tube holds should raise")
            # Exactly empty is refused too: the final aspiration would have
            # nothing above the tip and would draw air, and that dose lands
            # light with nothing in the run able to notice.
            raises(ProtocolError,
                   lambda: centrifuge_test.build(
                       tube_preload_uL=0.0, stock_uL=2 * 150.0,
                       plate_dose_uL=150.0,
                       layout=TEST_LAYOUT, outdir=td, plot=False),
                   "draining the tube to exactly zero should raise")
            # One microlitre of headroom is enough to be a real design.
            centrifuge_test.build(
                tube_preload_uL=1.0, stock_uL=2 * 150.0,
                plate_dose_uL=150.0,
                layout=TEST_LAYOUT, outdir=td, plot=False)
            raises(ProtocolError,
                   lambda: centrifuge_test.build(
                       well_a=(3, 5), well_b=(3, 5), layout=TEST_LAYOUT,
                       outdir=td, plot=False),
                   "using the same well twice should raise")

# ==========================================================================
# end to end
# ==========================================================================

def test_dogleg_when_both_ends_are_in_the_band():
    """Tube column 2 (X162) to column 3 (X150): both inside the band.

    Neither X-first nor Y-first is safe here, so the route has to step out of
    the band, move Y there, and come back.
    """
    m = _machine()
    m.startup()
    m.get_tip()
    m.goto(162.0, 223.0, op="probe")
    n = len(m.steps)
    m.goto(150.0, 187.0, op="probe")

    band = next(k for k in m.deck.keepouts if k.id == "tip_ejector")
    moves = [s for s in m.steps[n:] if s.kind == "move"]
    check(len(moves) == 3,
          f"a band-to-band traverse should be a 3-move dogleg, got {len(moves)}")
    offenders = [s for s in moves if "Y" in s.axes and band.contains_x(s.frm[0])]
    check(not offenders,
          f"the dogleg still moved Y inside the band: "
          f"{[(s.axes, s.frm) for s in offenders]}")
    check(any("Routed dogleg" in w for w in m.sim.report.warnings),
          f"the dogleg should be recorded as a warning, got "
          f"{m.sim.report.warnings}")


def test_full_protocol_and_manifest():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "protocols"))
    import accuracy_test

    with tempfile.TemporaryDirectory() as td:
        m, result = accuracy_test.build(
            effector_id="p300", name="unit_p300", layout=TEST_LAYOUT,
            outdir=td, plot=False
        )
        check(len(m.dispenses) == 20, f"expected 20 dispenses, got {len(m.dispenses)}")
        gpath = Path(result["gcode"])
        check(gpath.is_file(), "the gcode file should have been written")

        text = gpath.read_text()
        check("ACTUATOR_MOVE POS=23.279" in text,
              "the 30 uL aspirate POS should appear in the gcode")
        check("X256.6" in text and "Y218.5" in text,
              "tube A1 should appear in the gcode")

        man = json.loads(Path(result["manifest"]).read_text())
        check(len(man["provenance"]["config_sha256"]) == 4,
              "the manifest should hash all four config files")
        check(man["calibration"]["slope_mm_per_uL"] == 0.0533,
              "the manifest should record the calibration slope actually used")
        check(len(man["dispenses"]) == 20, "the manifest should list 20 dispenses")
        check(all("mass_dry_mg" in d for d in man["dispenses"]),
              "the manifest should leave balance columns empty and ready")
        check(man["uncalibrated"], "the manifest should record uncalibrated fields")

        csvp = gpath.with_name("unit_p300.balance.csv")
        check(csvp.is_file(), "the balance CSV should have been written")


def test_simulation_failure_writes_nothing():
    """A run that fails simulation must not leave a .gcode behind."""
    with tempfile.TemporaryDirectory() as td:
        m = Machine(effector="p300", name="should_not_exist",
                    layout=TEST_LAYOUT, strict=False)
        m.startup()
        m.get_tip()
        m.aspirate_uL(150.0)
        m.dispense("tubes", 0, 0)
        m.sim.report.errors.append("injected failure")
        try:
            m.finalize(outdir=td, plot=False)
        except ProtocolError:
            pass
        check(not (Path(td) / "should_not_exist.gcode").exists(),
              "no gcode should be written when simulation fails")


def test_imu_run_brackets_every_capture_in_one_file():
    """One program, one CSV - even though it is several captures.

    The board IMU cannot be commanded while it streams, so in gantry mode
    the capture must stop before every grip and a multi-cycle run is
    necessarily several captures. The run scope is what still makes it one
    file, so the pairing is checked rather than assumed.
    """
    m = _gripper_machine()
    m.startup()
    m.gripper_imu_run_begin("run_a")
    raises(ProtocolError, lambda: m.gripper_imu_run_begin("run_b"),
           "a second open run must refuse - captures would append to "
           "whichever the host recorded last")
    m.gripper_imu_start(label="cycle01")
    raises(ProtocolError, m.gripper_imu_run_end,
           "ending a run with a capture still open must refuse - the file "
           "would be copied before that capture had been merged into it")
    m.gripper_imu_stop()
    m.gripper_imu_run_end()
    raises(ProtocolError, m.gripper_imu_run_end,
           "closing a run twice must refuse")

    # ops are named "<effector>:<op>"; the prefix is not what is under test.
    ops = [s.op.split(":")[-1] for s in m.steps
           if getattr(s, "kind", "") == "macro"]
    check(ops.count("imu_run_begin") == 1, f"one run begin, got {ops}")
    check(ops.count("imu_run_end") == 1, f"one run end, got {ops}")
    check(ops.index("imu_run_begin") < ops.index("imu_start")
          < ops.index("imu_stop") < ops.index("imu_run_end"),
          f"the run must bracket the capture, got {ops}")


def test_station9_emits_one_run_and_a_capture_per_cycle():
    """The shape of the real program, at the cycle count that will be run."""
    import protocols.gripper_station9_test as prot
    m, _ = prot.build(cycles=3, layout=TEST_LAYOUT, outdir=tempfile.mkdtemp(),
                      plot=False)
    ops = [s.op.split(":")[-1] for s in m.steps
           if getattr(s, "kind", "") == "macro"]
    check(ops.count("imu_run_begin") == 1,
          f"exactly one run for the whole program, got "
          f"{ops.count('imu_run_begin')}")
    check(ops.count("imu_run_end") == 1,
          f"exactly one run end, got {ops.count('imu_run_end')}")
    check(ops.count("imu_start") == ops.count("imu_stop") == 3,
          f"one capture per cycle: {ops.count('imu_start')} starts, "
          f"{ops.count('imu_stop')} stops")
    # Every capture inside the run, and every grip outside a capture.
    begin, end = ops.index("imu_run_begin"), ops.index("imu_run_end")
    for i, op in enumerate(ops):
        if op in ("imu_start", "imu_stop", "imu_mark"):
            check(begin < i < end, f"{op} at {i} is outside the run")


def test_both_imus_are_captured_unless_spanning_is_asked_for():
    """PLATE_ONLY drops the board IMU, so it must not arrive by accident.

    A capture missing half its sensors is not visible in the gcode - it is
    one absent token - and shows up only as a CSV with no source=PCB rows
    after the run is over.
    """
    import protocols.gripper_station9_test as prot
    m, _ = prot.build(layout=TEST_LAYOUT, outdir=tempfile.mkdtemp(), plot=False)
    # kind must be filtered: a macro's guard dwell carries the macro's op
    # name, so op alone matches twice per capture.
    starts = [s for s in m.steps if s.kind == "macro"
              and s.op.endswith(":imu_start")]
    check(starts, "the default protocol should open a capture")
    for s in starts:
        check(not s.detail.get("plate_only"),
              "the DEFAULT mode must capture both IMUs - plate-only would "
              f"silently drop source=PCB: {s.detail}")
        check("PLATE_ONLY" not in s.detail["line"],
              f"and nothing should emit PLATE_ONLY: {s.detail['line']}")
    check(not m.toggles["plate_only"], "default must not be plate-only")

    sp, _ = prot.build(imu_mode="spanning", layout=TEST_LAYOUT,
                       outdir=tempfile.mkdtemp(), plot=False)
    check(sp.toggles["plate_only"],
          "spanning trades the board IMU away - that is the whole trade")


def test_imu_rate_is_set_and_is_one_rate_for_both_sensors():
    """An omitted rate meant the two sensors ran at different rates."""
    import protocols.gripper_station9_test as prot
    check(prot.IMU_RATE == 50,
          f"orientation work is specified at 50 Hz, got {prot.IMU_RATE}")
    m, _ = prot.build(layout=TEST_LAYOUT, outdir=tempfile.mkdtemp(), plot=False)
    starts = [s for s in m.steps if s.kind == "macro"
              and s.op.endswith(":imu_start")]
    check(starts, "the protocol should open a capture")
    for s in starts:
        check(s.detail.get("rate") == 50,
              f"the capture must carry its rate, got {s.detail}")
        check("RATE=50" in s.detail["line"],
              f"and must emit it, got {s.detail['line']!r}")


def _pos_at(m, i):
    """Where the gantry is at step i - macro steps carry no position, so it
    is the target (`to`) of the last move before them."""
    for s in reversed(m.steps[:i]):
        if s.kind == "move" and s.to is not None:
            return s.to
    return None


def _macro_ops(m):
    """The gripper macro ops in emission order, without their guard dwells."""
    return [s.op.split(":")[-1] for s in m.steps if s.kind == "macro"
            and s.op.startswith("gripper:")]


def test_trial_guard_policy():
    """Context writes get no guard; a burst's guard covers the burst."""
    g = eff_mod.load("gripper")
    check(g.guard_dwell_ms("trial") is None, "trial writes a file - no guard")
    check(g.guard_dwell_ms("trial_phase") is None, "phase - no guard")
    check(g.guard_dwell_ms("imu_mark") == 1500,
          "imu_mark keeps its guard: a mark still running when imu_stop "
          "merges is a lost mark")
    check(g.guard_dwell_ms("trial_grip") == g.guard_dwell_ms("grip"),
          "a trial grip is followed by a lift exactly like a grip")
    check(g.guard_dwell_ms("trial_burst", busy_ms=800) == 1500 + 800,
          "the board reads no queue while it streams, so the guard must "
          "cover the burst itself")


def test_offset_trials_sequence_per_trial():
    """Every trial: home, open, before_grip, GRIP, raised, lowered, RELEASE."""
    import protocols.gripper_offset_trials as prot
    m, _ = prot.build(trials=2, layout=TEST_LAYOUT, outdir=tempfile.mkdtemp(),
                      plot=False)
    ops = _macro_ops(m)
    check(ops.count("trials_begin") == 1 and ops.count("trials_end") == 1,
          f"one trial run per program, got {ops}")
    check("home" not in ops and "grip" not in ops and "release" not in ops,
          "every home, grip and release must go through the trial session - "
          "the plain macros skip the holding check and the grip_status")
    n = len(m.toggles["offsets_mm"]) * 2
    check(ops.count("trial") == n, f"{n} trials, got {ops.count('trial')}")
    # Cut the run into trials and check each one's order.
    cuts = [i for i, op in enumerate(ops) if op == "trial"] + [len(ops)]
    want = ["trial_home", "open", "trial_burst", "trial_grip",
            "trial_burst", "trial_burst", "trial_release"]
    for a, b in zip(cuts, cuts[1:]):
        seq = [op for op in ops[a:b] if op in set(want)]
        check(seq == want, f"trial order is {seq}, expected {want}")
    bursts = [s.detail["instant"] for s in m.steps
              if s.kind == "macro" and s.op == "gripper:trial_burst"]
    check(bursts[:3] == ["before_grip", "raised", "lowered"],
          f"bursts are before_grip, raised, lowered; got {bursts[:3]}")


def test_offset_trials_releases_at_the_release_height_with_a_declared_drop():
    import protocols.gripper_offset_trials as prot
    m, _ = prot.build(trials=1, offsets="0", layout=TEST_LAYOUT,
                      outdir=tempfile.mkdtemp(), plot=False)
    rel = [i for i, s in enumerate(m.steps)
           if s.kind == "macro" and s.op == "gripper:trial_release"]
    check(rel, "the trial must release")
    z = _pos_at(m, rel[0])[2]
    check(abs(z - 100.0) < 1e-6,
          f"release at the station's Z 100, got Z {z} - releasing at the "
          f"approach would drop the plate the whole lift")
    check(m.toggles["release_drop_mm"] == 7.0,
          "grip 107 to release 100 is a declared 7 mm fall")


def test_offset_trials_grip_depth_and_offsets_are_tunable():
    """The two parameters this protocol exists to tune."""
    import protocols.gripper_offset_trials as prot
    m, _ = prot.build(grip_z=109.0, offsets="-10,0,15", trials=1,
                      layout=TEST_LAYOUT, outdir=tempfile.mkdtemp(), plot=False)
    grips = [_pos_at(m, i) for i, s in enumerate(m.steps)
             if s.kind == "macro" and s.op == "gripper:trial_grip"]
    xs = sorted(round(pos[0], 3) for pos in grips)
    zs = {round(pos[2], 3) for pos in grips}
    x0 = m.toggles["station_xy"][0]
    check(xs == sorted(round(x0 + o, 3) for o in (-10, 0, 15)),
          f"grips at centre + offset, got X {xs}")
    check(zs == {109.0}, f"every grip at the tuned depth Z 109, got {zs}")


def test_offset_trials_refuses_geometry_that_reads_backwards():
    """+Z is DOWN. Each of these is the natural-looking mistake."""
    import protocols.gripper_offset_trials as prot
    kw = dict(layout=TEST_LAYOUT, outdir=tempfile.mkdtemp(), plot=False)
    raises(ProtocolError, lambda: prot.build(grip_z=88.0, **kw),
           "a grip ABOVE the approach (smaller Z) must refuse")
    raises(ProtocolError, lambda: prot.build(release_z=108.0, **kw),
           "a release BELOW the grip (larger Z) drives the plate into the "
           "deck and must refuse")
    raises(EffectorError, lambda: prot.build(open_ms=800, **kw),
           "an open past the 765 ms firmware clamp must refuse")
    raises(ProtocolError, lambda: prot.build(hold_s=6.0, **kw),
           "a grip held past 80% of tier 1 must refuse")


def test_offset_trials_order_is_offset_trials_order():
    """Same seed, same order as host/offset_trials.py's build_plan.

    The algorithm is restated here because the two files live in different
    repos; this pins that the restatement still shuffles identically.
    """
    import random
    import protocols.gripper_offset_trials as prot
    plan = prot.build_plan([0, 20, 40], 5, True, 7)
    ref = [{"offset_mm": o, "rep": r} for o in (0, 20, 40) for r in range(5)]
    random.Random(7).shuffle(ref)
    check([i["offset_mm"] for i in plan] == [i["offset_mm"] for i in ref],
          "the shuffle must match offset_trials for the same seed")
    check([i["trial"] for i in plan] == list(range(15)),
          "trial numbers follow run order after the shuffle, as the script")
    blocked = prot.build_plan([0, 20], 2, False, None)
    check([i["offset_mm"] for i in blocked] == [0, 0, 20, 20],
          "blocked order groups each offset")


def test_offset_trials_paces_the_hold_duty():
    """15 grips do not fit 120 s of hold per 300 s without waiting."""
    import protocols.gripper_offset_trials as prot
    m, _ = prot.build(layout=TEST_LAYOUT, outdir=tempfile.mkdtemp(), plot=False)
    cool = [s for s in m.steps if s.op == "duty_cooldown"]
    check(cool, "15 trials at the default settings need a duty cooldown")
    span = m.toggles["energised_span_hi_s"]
    budget = m.toggles["duty_budget_s"]
    check(span * 15 > budget,
          "the premise: 15 spans exceed one window's budget")
    tight, _ = prot.build(duty_margin=0.4, layout=TEST_LAYOUT,
                          outdir=tempfile.mkdtemp(), plot=False)
    check(sum(tight.toggles["duty_cooldowns_s"])
          > sum(m.toggles["duty_cooldowns_s"]),
          "a smaller budget must wait longer")


def test_offset_trials_every_trial_carries_a_seed():
    """Blocked order included - as offset_trials records one.

    The GRIPPER_TRIAL macro installed before 2026-10-21 rendered a bare
    "--seed" when SEED was missing; argparse rejected it, every trial
    failed, and the whole run's rows lost their trial tags. A SEED on every
    call is what offset_trials writes anyway, and it works on both macros.
    """
    import protocols.gripper_offset_trials as prot
    m, _ = prot.build(trials=1, layout=TEST_LAYOUT,
                      outdir=tempfile.mkdtemp(), plot=False)
    check(m.toggles["seed"] is not None, "a blocked run must still get a seed")
    lines = [s.detail["line"] for s in m.steps if s.kind == "macro"
             and s.op in ("gripper:trial", "gripper:trials_begin")]
    check(lines, "the program must open a run and start trials")
    for ln in lines:
        check(f"SEED={m.toggles['seed']}" in ln,
              f"every trial and the run banner carry the seed: {ln}")
    again, _ = prot.build(trials=1, seed=m.toggles["seed"], layout=TEST_LAYOUT,
                          outdir=tempfile.mkdtemp(), plot=False)
    check(again.toggles["seed"] == m.toggles["seed"],
          "passing the printed seed back reproduces it")


def test_offset_trials_no_plate_sends_plate_0():
    """--no-plate must reach the Pi; the default must not send it."""
    import protocols.gripper_offset_trials as prot
    kw = dict(trials=1, offsets="0", layout=TEST_LAYOUT, plot=False)
    for plate, want in ((False, True), (True, False)):
        m, _ = prot.build(plate_imu=plate, outdir=tempfile.mkdtemp(), **kw)
        begin = [s.detail["line"] for s in m.steps if s.kind == "macro"
                 and s.op == "gripper:trials_begin"]
        check(("PLATE=0" in begin[0]) == want,
              f"plate_imu={plate}: PLATE=0 {'missing' if want else 'sent'} "
              f"in {begin[0]}")


def test_trial_run_and_imu_capture_are_exclusive():
    """Both read the plate IMU over I2C; two readers would interleave."""
    m = _gripper_machine()
    m.startup()
    m.trials_begin("t")
    raises(ProtocolError, lambda: m.gripper_imu_start(label="x"),
           "an IMU capture during a trial run must refuse")
    m.trials_end()
    m.gripper_imu_start(label="x")
    raises(ProtocolError, lambda: m.trials_begin("t2"),
           "a trial run during an IMU capture must refuse")


# ==========================================================================

def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"running {len(tests)} tests\n")
    for t in tests:
        print(f"- {t.__name__}")
        try:
            t()
        except Exception as exc:  # a test that itself blows up is a failure
            FAILURES.append(f"{t.__name__} raised {type(exc).__name__}: {exc}")
            print(f"  ERROR {type(exc).__name__}: {exc}")
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"all {len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
