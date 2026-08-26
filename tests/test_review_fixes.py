"""Regression tests for the defects an adversarial review found and confirmed.

Each test reproduces the exact failure the reviewer demonstrated, so the fix
cannot quietly regress. Numbered to match the review report.

Run with pytest, or directly:  python tests/test_review_fixes.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "protocols"))

from lab import config as cfgmod
from lab.deck import Deck, DeckError
from lab.effector import EffectorError, LabError, Pipette, VolumeError
from lab.machine import Machine, ProtocolError
from lab.simulate import SimulationError

FAILURES: list[str] = []


def check(cond, msg):
    if not cond:
        FAILURES.append(msg)
        print(f"  FAIL  {msg}")


def raises(exc, fn, msg):
    try:
        fn()
    except exc:
        return
    except Exception as e:
        FAILURES.append(f"{msg} (raised {type(e).__name__}: {e})")
        print(f"  FAIL  {msg} (raised {type(e).__name__}: {e})")
        return
    FAILURES.append(f"{msg} (nothing raised)")
    print(f"  FAIL  {msg} (nothing raised)")


def _machine(effector="p300", **kw):
    return Machine(effector=effector, name="review_fix", **kw)


# --------------------------------------------------------------------------
# 1. eject_tip approached the ejector at whatever X it was left at, and the
#    op-name whitelist then suppressed the keep-out check. Reachable with
#    --shuffle --eject-tip: a 104 mm TIPPED Y traverse at X162, unchecked.
# --------------------------------------------------------------------------

def test_1_eject_approach_is_routed_and_checked():
    import accuracy_test

    with tempfile.TemporaryDirectory() as td:
        m, _ = accuracy_test.build(effector_id="p300", name="rf_eject",
                                   eject_tip=True, shuffle=True, seed=0,
                                   outdir=td, plot=False)
    band = next(k for k in m.deck.keepouts if k.id == "tip_ejector")
    lane_x = float(m.deck.station("tip_eject").spec["lane_x"])

    # Any Y move inside the band must either be exempt, and therefore at the
    # lane X, or not be inside the band at all.
    bad = [
        s for s in m.steps
        if s.kind == "move" and "Y" in s.axes and band.contains_x(s.frm[0])
        and not s.detail.get("keepout_exempt")
    ]
    check(not bad,
          f"{len(bad)} unexempt Y move(s) inside the keep-out band: "
          f"{[(s.op, s.frm) for s in bad[:3]]}")

    exempt = [s for s in m.steps if s.detail.get("keepout_exempt")
              and s.kind == "move" and "Y" in s.axes]
    check(exempt, "the ejector engagement should still be exempt")
    check(all(abs(s.frm[0] - lane_x) < 1e-6 for s in exempt),
          f"only the lane X {lane_x} may be exempt; got "
          f"{[s.frm[0] for s in exempt]}")


def test_1b_exemption_cannot_be_spoofed_by_op_name():
    """Passing op='eject_tip' to an ordinary move must not buy an exemption."""
    m = _machine()
    m.startup()
    m.get_tip()
    m.goto(155.0, 300.0, op="eject_tip")
    # 61: past the band's safe-Z of 60, still clear of the tipped stage limit
    # of 62.5, so only the keep-out rule can fire.
    m._move_z(61.0, 600, "eject_tip")
    raises(SimulationError,
           lambda: m._move_xy(155.0, 250.0, 6000, "eject_tip", order="Y"),
           "an op named eject_tip should not be exempt without the step flag")


# --------------------------------------------------------------------------
# 2. get_tip() ignored consumed_tips, so a second pickup drove the bare
#    nosecone into an empty rack hole and then set tip_fitted = True anyway.
# --------------------------------------------------------------------------

def test_2_second_pickup_does_not_reuse_a_hole():
    m = _machine()
    m.startup()
    first = m.get_tip()
    m.eject_tip()
    second = m.get_tip()
    check(first != second,
          f"the second pickup reused position {second}")
    check(second in m.sim.consumed_tips and first in m.sim.consumed_tips,
          "both pickups should be recorded as consumed")


def test_2b_explicit_reuse_is_refused():
    m = _machine()
    m.startup()
    row, col = m.get_tip()
    m.eject_tip()
    raises(ProtocolError, lambda: m.get_tip(row=row, col=col),
           "picking an already-consumed position should raise")


def test_2c_consumption_works_inward_from_the_exit_edge():
    """The exit path stays clear only if columns go edge-first."""
    m = _machine()
    m.startup()
    p = m.deck.placement("tips")
    xs = p.col_x_values()
    picked = []
    for _ in range(4):
        picked.append(m.get_tip())
        m.eject_tip()
    got = [xs[c] for _, c in picked]
    check(got == sorted(got),
          f"columns should be consumed inward from the low-X edge, got X {got}")


# --------------------------------------------------------------------------
# 3. Measuring z.stage_surface alone switched the Z check ON for bare moves
#    and left it OFF for the deeper tipped ones, with a warning naming the
#    wrong field.
# --------------------------------------------------------------------------

def _config_without(*paths):
    """Temp LAB_CONFIG copy with the given deck z keys set to null."""
    import copy, os, shutil, tempfile
    d = Path(tempfile.mkdtemp())
    for name in ("deck.json", "labware.json", "layout.json"):
        shutil.copy(cfgmod.config_path(name), d / name)
    shutil.copytree(cfgmod.config_path("effectors"), d / "effectors")
    deck = json.loads((d / "deck.json").read_text())
    for key in paths:
        deck["z"][key] = None
    (d / "deck.json").write_text(json.dumps(deck))
    lw = json.loads((d / "labware.json").read_text())
    lw["labware"]["tiprack_300_96"]["tip"]["length"] = None
    (d / "labware.json").write_text(json.dumps(lw))
    return d


def test_3_tipped_z_limit_gap_is_reported_separately():
    """With the measurements REMOVED, bare and tipped must warn separately.

    The defect was that measuring z.stage_surface alone switched the check on
    for bare moves and left it off for the deeper tipped ones, under a single
    deduped warning that named the wrong field.
    """
    import os
    d = _config_without("stage_surface_with_p300_tip")
    old = os.environ.get("LAB_CONFIG")
    os.environ["LAB_CONFIG"] = str(d)
    try:
        m = _machine()
        m.startup()
        m.get_tip()
        m.aspirate_uL(150.0)
        m.dispense("tubes", 0, 0)
        ws = m.sim.report.warnings
        tipped = [w for w in ws if "TIPPED" in w]
        check(tipped, "the tipped Z-limit gap should be reported on its own")
        check(any("tip.length" in w for w in tipped),
              f"the tipped warning should name tip.length; got {tipped}")
    finally:
        if old is None:
            os.environ.pop("LAB_CONFIG", None)
        else:
            os.environ["LAB_CONFIG"] = old


def test_3b_limit_returns_what_is_missing():
    import os
    d = _config_without("stage_surface")
    old = os.environ.get("LAB_CONFIG")
    os.environ["LAB_CONFIG"] = str(d)
    try:
        m = _machine()
        limit, missing = m.sim.effective_z_limit(tip_fitted=False)
        check(limit is None and missing == ["deck z.stage_surface"],
              f"bare case should report only the surface; got {missing}")
        limit, missing = m.sim.effective_z_limit(tip_fitted=True)
        check(limit is None and len(missing) == 2,
              f"tipped case should report both missing fields; got {missing}")
    finally:
        if old is None:
            os.environ.pop("LAB_CONFIG", None)
        else:
            os.environ["LAB_CONFIG"] = old


def test_3c_limits_are_now_actually_enforced():
    """With everything measured, both limits resolve and no gap is reported."""
    m = _machine()
    bare, missing_bare = m.sim.effective_z_limit(tip_fitted=False)
    tipped, missing_tip = m.sim.effective_z_limit(tip_fitted=True)
    check(bare == 114.75 and not missing_bare,
          f"bare limit should be 114.75, got {bare} missing {missing_bare}")
    check(tipped == 62.5 and not missing_tip,
          f"tipped limit should be 62.5, got {tipped} missing {missing_tip}")
    check(m.sim.tip_length() == 52.25,
          f"tip length should be 52.25, got {m.sim.tip_length()}")

    m.startup()
    m.get_tip()
    m.aspirate_uL(150.0)
    m.dispense("tubes", 0, 0)
    gaps = [w for w in m.sim.report.warnings if "cannot check absolute crash" in w]
    check(not gaps, f"no Z-envelope gap should remain: {gaps}")


# --------------------------------------------------------------------------
# 4. Traverse clearance tested only the destination point, so a move that
#    swept across labware but ended elsewhere passed, and labware that was
#    never a destination was never checked at all.
# --------------------------------------------------------------------------

def test_4_traverse_clearance_uses_the_swept_segment():
    """A rim above travel height must stop a traverse that sweeps over it."""
    import copy
    lw = copy.deepcopy(cfgmod.load_labware().data)
    # A rim at Z 3 is ABOVE the travel height of 5 (+Z is down), so anything
    # traversing over this rack at Z 5 would hit it.
    lw["labware"]["tube_rack_0p65_5x10"]["z"]["top"] = 3.0

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        for name in ("deck.json", "layout.json"):
            (d / name).write_text(cfgmod.config_path(name).read_text())
        (d / "effectors").mkdir()
        for p in cfgmod.config_path("effectors").glob("*.json"):
            (d / "effectors" / p.name).write_text(p.read_text())
        (d / "labware.json").write_text(json.dumps(lw))

        import os
        old = os.environ.get("LAB_CONFIG")
        os.environ["LAB_CONFIG"] = str(d)
        try:
            m = _machine()
            m.startup()
            m.get_tip()

            # The Y leg down the X=186 column ends at the rack's own corner
            # well, but it is the SWEEP across Y 399 -> 223 that matters: it
            # crosses the rack with a 3.0 rim while travelling at Z 5.0.
            # Endpoint-only checking passed this.
            raises(SimulationError, lambda: m.goto(186.0, 223.0, op="probe"),
                   "a traverse sweeping over a rack rim above travel height "
                   "should be blocked")

            # And the reverse case: a move that ends nowhere near the rack but
            # sweeps straight across it on the way.
            m2 = _machine(strict=False)
            m2.startup()
            m2.goto(186.0, 100.0, op="probe")   # below the rack in Y
            n = len(m2.sim.report.errors)
            m2.goto(0.0, 100.0, op="probe")     # X leg passes under it, clear
            check(len(m2.sim.report.errors) == n,
                  "a traverse that misses the rack should not be flagged")
        finally:
            if old is None:
                os.environ.pop("LAB_CONFIG", None)
            else:
                os.environ["LAB_CONFIG"] = old


# --------------------------------------------------------------------------
# 5. max_volume_uL omitted pos_min, so check_volume accepted volumes that
#    check_pos then rejected.
# --------------------------------------------------------------------------

def test_5_max_volume_respects_pos_min():
    import copy
    from lab.config import ConfigFile

    spec = copy.deepcopy(cfgmod.load_effector_spec("p300"))
    spec.data["actuator"]["pos_min"] = 6.0
    e = Pipette(spec)

    check(abs(e.max_volume_uL - 316.6604) < 1e-3,
          f"with pos_min 6.0 the max should be ~316.66, got {e.max_volume_uL}")
    raises(VolumeError, lambda: e.check_volume(320.0),
           "320 uL should be refused once pos_min eats the headroom")
    # every volume check_volume accepts must survive the full stroke
    v = e.window[0]
    while v <= e.max_volume_uL:
        e.backoff_pos(v)
        v = round(v + 1.0, 6)


def test_5b_table_reports_only_legal_positions():
    import copy
    spec = copy.deepcopy(cfgmod.load_effector_spec("p300"))
    spec.data["actuator"]["pos_min"] = 6.0
    e = Pipette(spec)
    raises(LabError, lambda: e.table([320.0]),
           "table() should refuse to report a POS check_pos would reject")


# --------------------------------------------------------------------------
# 6. aspirate_uL committed fluid_uL before the backoff and never invalidated
#    _last_aspirate, so a mid-primitive raise made the next dispense record a
#    stroke that was never commanded.
# --------------------------------------------------------------------------

def test_6_failed_aspirate_emits_nothing_and_leaves_no_stale_record():
    import copy, os
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        for name in ("deck.json", "labware.json", "layout.json"):
            (d / name).write_text(cfgmod.config_path(name).read_text())
        (d / "effectors").mkdir()
        for p in cfgmod.config_path("effectors").glob("*.json"):
            (d / "effectors" / p.name).write_text(p.read_text())
        spec = json.loads((d / "effectors" / "p300.json").read_text())
        spec["actuator"]["pos_min"] = 6.0
        (d / "effectors" / "p300.json").write_text(json.dumps(spec))

        old = os.environ.get("LAB_CONFIG")
        os.environ["LAB_CONFIG"] = str(d)
        try:
            m = _machine()
            m.startup()
            m.get_tip()
            m.aspirate_uL(150.0)
            m.dispense("tubes", 0, 0)
            before = m.g.line_count()
            raises(LabError, lambda: m.aspirate_uL(320.0),
                   "320 uL should be refused with pos_min 6.0")
            check(m.g.line_count() == before,
                  f"the refused aspiration emitted {m.g.line_count() - before} "
                  f"gcode line(s); it should emit none")
            check(m.fluid_uL == 0.0,
                  f"state says {m.fluid_uL} uL is held after a refused aspiration")
            check(m._last_aspirate is None,
                  f"a stale aspirate record survived: {m._last_aspirate}")
        finally:
            if old is None:
                os.environ.pop("LAB_CONFIG", None)
            else:
                os.environ["LAB_CONFIG"] = old


def test_6b_dispense_clears_the_aspirate_record():
    m = _machine()
    m.startup()
    m.get_tip()
    m.aspirate_uL(150.0)
    m.dispense("tubes", 0, 0)
    check(m._last_aspirate is None,
          "one aspiration feeds exactly one dispense; the record should clear")


# --------------------------------------------------------------------------
# 7. simulate() appended into the same Report every call, so an
#    inspect-then-finalize caller tripled every whole-run line.
# --------------------------------------------------------------------------

def test_7_simulate_is_idempotent():
    m = _machine()
    m.startup()
    m.get_tip()
    m.aspirate_uL(150.0)
    m.dispense("tubes", 0, 0)
    first = len(m.simulate().warnings)
    second = len(m.simulate().warnings)
    third = len(m.simulate().warnings)
    check(first == second == third,
          f"warning count grew across calls: {first}, {second}, {third}")


# --------------------------------------------------------------------------
# 8. The five error types shared no base, so a protocol could only catch one.
# --------------------------------------------------------------------------

def test_8_all_errors_share_a_base():
    for exc in (EffectorError, VolumeError, DeckError, ProtocolError,
                SimulationError):
        check(issubclass(exc, LabError), f"{exc.__name__} should subclass LabError")


def test_8b_protocol_reports_a_deck_error_cleanly():
    import accuracy_test
    rc = accuracy_test.main(["--effector", "p300", "--tubes", "nope",
                             "--no-plot", "--outdir", tempfile.mkdtemp()])
    check(rc == 1, f"a bad placement name should exit 1, got {rc}")


# --------------------------------------------------------------------------
# 9. The calibration equation was read from free-text config that nothing
#    cross-checked against the coefficients actually used.
# --------------------------------------------------------------------------

def test_9_equation_is_derived_from_the_coefficients():
    e = Pipette(cfgmod.load_effector_spec("p300"))
    check(e.equation == "d_mm = 0.0533 * V_uL + 0.122",
          f"equation should come from slope/intercept, got {e.equation}")
    import copy
    spec = copy.deepcopy(cfgmod.load_effector_spec("p300"))
    spec.data["calibration"].pop("equation", None)
    e2 = Pipette(spec)
    check(e2.equation == e.equation,
          "dropping the config's equation text must not change the report")


# --------------------------------------------------------------------------
# 10. dispense emitted a full blow-out before a precondition check that could
#     raise, leaving a complete dispense in the buffer with no manifest row.
# --------------------------------------------------------------------------

def test_10_dispense_checks_wipe_config_before_moving():
    m = _machine()
    m.startup()
    m.get_tip()
    m.aspirate_uL(150.0)
    before = m.g.line_count()
    raises(ProtocolError,
           lambda: m.dispense("water", 0, 0, wall_wipe=True),
           "a wipe on labware with no wipe block should be refused")
    check(m.g.line_count() == before,
          f"the refused dispense emitted {m.g.line_count() - before} line(s)")
    check(m.fluid_uL == 150.0,
          "the fluid should still be held after a refused dispense")


# --------------------------------------------------------------------------
# 11. wall_wipe discarded its y argument and asserted nothing about its pose.
# --------------------------------------------------------------------------

def test_11_wall_wipe_requires_the_dispense_pose():
    m = _machine()
    m.startup()
    m.get_tip()
    p = m.deck.placement("tubes")
    raises(ProtocolError, lambda: m._wall_wipe(p, 186.0, 223.0),
           "a wipe from the wrong pose should be refused")
    check(not hasattr(m, "wall_wipe"),
          "wall_wipe should be private; a protocol must not call it directly")


# --------------------------------------------------------------------------
# 12. home("Z") recorded a Step with None coordinates into report.path.
# --------------------------------------------------------------------------

def test_12_no_none_coordinates_in_the_path():
    m = _machine()
    m.startup()
    m.get_tip()
    bad = [p for p in m.sim.report.path if any(v is None for v in p[:3])]
    check(not bad, f"report.path contains None coordinates: {bad[:3]}")


# --------------------------------------------------------------------------
# 13. Klipper macros fell back to hardcoded literals, so a mistyped or deleted
#     config entry emitted the built-in string instead of failing.
# --------------------------------------------------------------------------

def test_13_macros_must_be_declared():
    d = Deck.load()
    check(d.macro("actuator_move") == "ACTUATOR_MOVE POS=",
          "the declared macro should be returned")
    d.macros = {k: v for k, v in d.macros.items() if k != "actuator_move"}
    raises(DeckError, lambda: d.macro("actuator_move"),
           "a missing macro should raise, not silently use a literal")


# --------------------------------------------------------------------------
# 14. _move_z used a different tolerance from gcodepy, recording a Step for a
#     move that emitted no gcode.
# --------------------------------------------------------------------------

def test_14_no_phantom_z_steps():
    m = _machine()
    m.startup()
    n_lines, n_steps = m.g.line_count(), len(m.steps)
    m._move_z(m.g.get_z() + 1.2e-6, 6000, "probe")
    check(m.g.line_count() == n_lines and len(m.steps) == n_steps,
          f"a sub-tolerance Z move recorded {len(m.steps) - n_steps} step(s) "
          f"and {m.g.line_count() - n_lines} line(s)")


# --------------------------------------------------------------------------

def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"running {len(tests)} review-fix tests\n")
    for t in tests:
        print(f"- {t.__name__}")
        try:
            t()
        except Exception as exc:
            FAILURES.append(f"{t.__name__} raised {type(exc).__name__}: {exc}")
            print(f"  ERROR {type(exc).__name__}: {exc}")
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"all {len(tests)} review-fix tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
