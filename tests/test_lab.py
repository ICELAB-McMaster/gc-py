"""Regression tests. Run with pytest, or directly:  python tests/test_lab.py

The geometry tests pin coordinates that were verified three independent ways:
against the hardcoded constants in the old scripts, against the separate
hardcoded lists in examples/PipetteCurve.py, and against the XY actually
emitted into 300ulPipetteAccuracy.gcode. If one of these fails, the deck config
moved and somebody needs to know.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lab import effector as eff_mod
from lab.deck import Deck
from lab.effector import EffectorError, Pipette, VolumeError
from lab.machine import Machine, ProtocolError
from lab.simulate import SimulationError

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
    """The 0.65 mL tube holder: 12 mm in X, 18 mm in Y, 5 rows x 10 cols."""
    d = Deck.load()
    p = d.placement("tubes")

    check(p.labware.rows == 5 and p.labware.cols == 10,
          f"tube rack should be 5x10, got {p.labware.rows}x{p.labware.cols}")
    check(p.labware.grid["pitch_x"] == 12.0, "tube rack X pitch should be 12.0")
    check(p.labware.grid["pitch_y"] == 18.0, "tube rack Y pitch should be 18.0")
    check(p.xy(0, 0) == (186.0, 223.0), f"A1 should be (186, 223), got {p.xy(0,0)}")

    # Column index advances toward LOWER X; row index toward LOWER Y.
    expect_x = [186.0, 174.0, 162.0, 150.0, 138.0, 126.0, 114.0, 102.0, 90.0, 78.0]
    expect_y = [223.0, 205.0, 187.0, 169.0, 151.0]
    check(p.col_x_values() == expect_x, f"tube col X wrong: {p.col_x_values()}")
    check(p.row_y_values() == expect_y, f"tube row Y wrong: {p.row_y_values()}")
    check(p.grid_center() == (132.0, 187.0),
          f"tube grid centre should be (132, 187), got {p.grid_center()}")

    # The 20 positions the accuracy protocol actually visits, straight out of
    # 300ulPipetteAccuracy.gcode.
    check(p.xy(3, 4) == (138.0, 169.0), "last accuracy tube should be (138, 169)")


def test_tiprack_corner_solve():
    """Real pitch is 9.0909 / 9.10, not the nominal 9.00."""
    d = Deck.load()
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
    d = Deck.load()
    p = d.placement("tubes")
    raises(IndexError, lambda: p.xy(5, 0), "row 5 of a 5-row rack should raise")
    raises(IndexError, lambda: p.xy(0, 10), "col 10 of a 10-col rack should raise")


def test_keepout_covers_tube_columns():
    """The X155 ejector band sits over tube columns 2 and 3."""
    d = Deck.load()
    p = d.placement("tubes")
    band = next(k for k in d.keepouts if k.id == "tip_ejector")
    inside = [c for c in range(p.labware.cols) if band.contains_x(p.xy(0, c)[0])]
    check(inside == [2, 3],
          f"tube columns inside the X155 band should be [2, 3], got {inside}")


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


def test_gripper_is_a_placeholder():
    raises(EffectorError, lambda: eff_mod.load("gripper"),
           "the gripper placeholder should refuse to load for motion")
    g = eff_mod.load("gripper", allow_placeholder=True)
    check(g.kind == "gripper", "gripper should dispatch to the Gripper class")
    check(g.is_placeholder, "gripper should be flagged as a placeholder")


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

def test_keepout_blocks_low_y_move():
    """A Y move inside the band below Z 60 must fail."""
    m = _machine()
    m.startup()
    m.get_tip()
    # Park inside the band, drop below the safe Z, then try to move Y.
    m.goto(155.0, 300.0)
    m._move_z(65.0, 600, "unit_test")

    def bad_y():
        m._move_xy(155.0, 250.0, 6000, "unit_test", order="Y")

    raises(SimulationError, bad_y,
           "a Y move at X155 with Z 65 should be blocked by the keep-out band")


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
    m = _machine()
    m.startup()
    raises(SimulationError, lambda: m.goto(500.0, 100.0),
           "X 500 is outside the 0-420 envelope and should be blocked")


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
    check(any("stay out of the keep-out band" in w for w in m.sim.report.warnings),
          "the dogleg should be recorded as a warning")


def test_full_protocol_and_manifest():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "protocols"))
    import accuracy_test

    with tempfile.TemporaryDirectory() as td:
        m, result = accuracy_test.build(
            effector_id="p300", name="unit_p300", outdir=td, plot=False
        )
        check(len(m.dispenses) == 20, f"expected 20 dispenses, got {len(m.dispenses)}")
        gpath = Path(result["gcode"])
        check(gpath.is_file(), "the gcode file should have been written")

        text = gpath.read_text()
        check("ACTUATOR_MOVE POS=23.279" in text,
              "the 30 uL aspirate POS should appear in the gcode")
        check("X186" in text and "Y223" in text, "tube A1 should appear in the gcode")

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
        m = Machine(effector="p300", name="should_not_exist", strict=False)
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
