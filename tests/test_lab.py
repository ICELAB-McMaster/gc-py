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
        for key in ("work", "mix")
        if key in entry and entry[key] is None
    ]
    check(not null_commanded,
          f"these commanded depths are still null and would block a run: "
          f"{null_commanded}")

    # The refusal still works: blank one out and it stops the run.
    with _WithConfig(_p300_with_z(tube_rack_1p5={"work": None})):
        m = _machine()
        m.startup()
        m.get_tip()
        raises(EffectorError,
               lambda: m.aspirate_from("big_tubes", 0, 0, volume_uL=100.0),
               "a null work Z should refuse rather than pick a depth")

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
    with _WithConfig(_p300_with_z(tube_rack_1p5={"work": 45.0})):
        m = _machine()
        m.startup()
        m.get_tip()
        pos = m.aspirate_from("big_tubes", 0, 0, volume_uL=100.0)
        e = eff_mod.load("p300")
        check(pos == e.aspirate_pos(100.0),
              f"should return the aspirate POS {e.aspirate_pos(100.0)}, got {pos}")
        check(m.fluid_uL == 100.0, f"should hold 100 uL, got {m.fluid_uL}")
        deep = [s for s in m.steps
                if s.op == "aspirate" and s.kind == "move" and s.axes == "Z"
                and s.to[2] == 45.0]
        check(deep, "should have descended to the tube's work Z of 45.0")
        # It must honour the holder's Y-first approach.
        legs = [s for s in m.steps
                if s.op == "aspirate" and s.kind == "move" and s.axes in ("X", "Y")]
        check([s.axes for s in legs][:2] == ["Y", "X"],
              f"approach should be Y then X, got {[s.axes for s in legs][:2]}")
        check(m._last_aspirate["source"] == "tube_rack_1p5",
              "the aspirate record should name the source labware")


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
        # one priming blowout, then a draw/push pair per cycle
        check(acts == [e.blowout_pos] + [top, e.blowout_pos] * 5,
              f"expected a prime then 5 draw/push pairs, got {acts}")
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

        per_plate = {}
        for d in m.dispenses:
            per_plate.setdefault(d["placement"], []).append(d["target_volume_uL"])
        check(per_plate.get("plate1") == [100.0, 100.0],
              f"plate1 should get 2x100 uL, got {per_plate.get('plate1')}")
        check(per_plate.get("plate2") == [100.0, 100.0],
              f"plate2 should get 2x100 uL, got {per_plate.get('plate2')}")
        check(sum(per_plate["plate1"]) == sum(per_plate["plate2"]),
              "the two plates must receive equal mass - that is the whole point")

        wells = {d["placement"]: [x["well"] for x in m.dispenses
                                 if x["placement"] == d["placement"]]
                 for d in m.dispenses}
        check(wells["plate1"] == wells["plate2"] == ["D6", "E7"],
              f"both plates should use D6 and E7, got {wells}")
        check(m.toggles["tube_contents_at_end_uL"] == 100.0,
              "the tube should finish with 100 uL")
        check(m.toggles["eject_tip"], "the tip should be ejected by default")
        check(not m.tip_fitted, "the run should end with no tip fitted")
        homes = [s for s in m.steps if s.kind == "home"]
        check(any(s.detail.get("reason") == "post-ejection" for s in homes),
              "the ejection must be followed by a G28, which returns home")


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
    check(any("stay out of the keep-out band" in w for w in m.sim.report.warnings),
          "the dogleg should be recorded as a warning")


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
