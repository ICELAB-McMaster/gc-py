"""Centrifuge balance test, version 2.1: hold after the mix, corner wells.

Derived from `centrifuge_test.py`, but **only location 2 is loaded**:

    1. nothing goes into location 1 - no water doses at all
    2. 150 uL of water goes into the 1.5 mL tube
    3. after mixing, the tip lifts clear and HOLDS for 30 s
    4. plate 2 is loaded at two CORNERS, 140 uL each
    5. the tube is preloaded by hand with 150 uL

    python protocols/centrifuge_test_v2_1.py --effector p300

BEFORE RUNNING: the 1.5 mL tube must be **preloaded by hand with 150 uL**.

Sequence, with the tube contents tracked:

    stage  action                                    volume   tube after
      1    water        -> 1.5 mL tube                150 uL     300
      2    mix at the tube BOTTOM, 5 cycles           200 uL     300
      3    lift to Z 10 and hold 30 s                    -       300
      4    1.5 mL tube  -> plate 2, H1                 140 uL     160
      5    1.5 mL tube  -> plate 2, A12                140 uL      20

**This run no longer balances a pair.** Location 1 is empty, so location 2's
280 uL has nothing opposite it. That is what was asked for, and the protocol
warns about it on every run rather than quietly dropping the word "balance"
from its own name - if the plate is going in a centrifuge, something of equal
mass has to go in the other side. Pass `--water-wells D6 E7` to put water back
into location 1 and restore the pair.

**The hold.** After the last mix stroke the tip stays at the mix depth and
then moves to **Z 10**, which with +Z down is 44 mm **higher** - clear of the
tube and of the liquid - and waits 30 s before the first draw pulls it back
down to Z 54. Nothing else moves; XY is untouched. A timed hold needs nothing
in printer.cfg and cannot leave the machine waiting forever, which is why this
is a `G4` dwell rather than a Klipper PAUSE.

**The volumes, in order.** Four numbers, and only the first is yours to do
by hand:

    150 uL   you put in the tube BY HAND before the run
    150 uL   the robot adds                     -> 300 in the tube
    280 uL   the robot draws back out, 140 per corner
     20 uL   left in the tube at the end

The 20 uL residual is what makes this legal. A tube cannot be drawn to exactly
zero: the last aspiration would have no liquid above the tip, would take air,
and would land light - with nothing in the run able to notice, since there is
no liquid inventory anywhere. `build()` refuses any combination that does not
leave a positive residual, which is why the dose is 140 and not 150.

20 uL is a thin margin. It assumes the two 140 uL draws take 140 uL each; if
they run over, the second one is the one that comes up short. Lower `--dose`
further if the balance readings suggest they do.

**The two corners.** "Highest X and lowest Y" is **H1** at (251.0, 336.0) and
"highest Y and lowest X" is **A12** at (152.0, 399.0). They are diagonally
opposite about the plate centre, so plate 2 is internally balanced the way
plate 1's D6/E7 pair is.

A12 sits at X 152, **inside the tip-ejector keep-out band X[148, 162]**, where
Y motion with a tip fitted is a hard error. It is still reachable - the router
takes the Y leg at a clear X and comes in along X - but that is the router
doing real work, not a coincidence, and it is why this protocol does not
hand-order its own moves.

Volumes are requested in microlitres. The millimetre displacement comes from
the pipette's stored calibration at generation time; no mm value is typed here.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lab.effector import LabError, Pipette
from lab.labware import well_name
from lab.machine import Machine, ProtocolError

# ---- design constants ---------------------------------------------------
# Plate 1 keeps the middle pair; plate 2 uses the two corners the operator
# asked for. Both pairs are diagonally opposite about the plate centre, so
# each plate is internally balanced as well as matched to the other.
# Location 1 gets nothing in this version; the tuple is kept so the water
# doses can be switched back on without restructuring the run.
PLATE1_WELLS = ()                        # nothing into location 1
PLATE2_WELLS = ((7, 0), (0, 11))         # H1 (high X, low Y), A12 (low X, high Y)

PLATE_DOSE_uL = 140.0    # into each well. 140 and not 150 so the tube keeps a
                         # residual - see the volume table above.
STOCK_uL = 150.0         # water ADDED to the 1.5 mL tube
TUBE_PRELOAD_uL = 150.0  # already in the tube, put there by hand
TUBE_WELL = (0, 0)

MIX_CYCLES = 5
MIX_VOLUME_uL = 200.0

PREWET_CYCLES = 3

# After the mix, lift the tip clear of the tube and let it sit. +Z is DOWN,
# so Z 10 is 44 mm ABOVE the Z 54 mix depth.
HOLD_Z = 10.0
HOLD_SECONDS = 30.0


def build(
    effector_id: str = "p300",
    name: str | None = None,
    water_plate: str = "plate1",
    stock_plate: str = "plate2",
    tube: str = "big_tubes",
    tube_well=TUBE_WELL,
    water_wells=PLATE1_WELLS,
    stock_wells=PLATE2_WELLS,
    plate_dose_uL: float = PLATE_DOSE_uL,
    stock_uL: float = STOCK_uL,
    tube_preload_uL: float = TUBE_PRELOAD_uL,
    mix_cycles: int = MIX_CYCLES,
    mix_volume_uL: float = MIX_VOLUME_uL,
    prewet_cycles: int = PREWET_CYCLES,
    get_tip: bool = True,
    prewet: bool = True,
    temp_log: bool = True,
    eject_tip: bool = True,
    hold_after_mix: bool = True,
    hold_z: float = HOLD_Z,
    hold_seconds: float = HOLD_SECONDS,
    layout: str = "layout.json",
    outdir: str = ".",
    plot: bool = True,
    strict: bool = True,
):
    name = name or "centrifuge_test_v2_1"
    m = Machine(effector=effector_id, name=name, layout=layout,
                strict=strict, temp_log=temp_log)

    eff = m.effector
    if not isinstance(eff, Pipette):
        raise ProtocolError(
            f"{effector_id} is a {eff.kind}; this protocol needs a pipette"
        )

    # ---- design checks, before a single line of gcode -------------------
    for v in (plate_dose_uL, stock_uL, mix_volume_uL):
        eff.check_volume(v)          # window + stroke, including the backoff

    if not stock_wells:
        raise ProtocolError(
            "stock_wells is empty, so this run would draw nothing and dispense "
            "nothing. There is no run to generate."
        )
    if water_wells and water_plate == stock_plate:
        raise ProtocolError(
            f"water_plate and stock_plate are both {water_plate!r}. One source "
            f"per plate is the point; two into one leaves nothing to balance "
            f"against."
        )
    targets = [(stock_plate, stock_wells)]
    if water_wells:
        targets.append((water_plate, water_wells))
    for pname, wells in targets:
        p = m.deck.placement(pname)  # raises DeckError if not loaded
        for w in wells:
            p.check_well(*w)         # raises IndexError if out of range
        if len(set(wells)) != len(wells):
            raise ProtocolError(
                f"{pname} lists the same well twice: "
                f"{[well_name(*w) for w in wells]}. Each dose is meant to land "
                f"somewhere the last did not."
            )

    # The protocol is named for a mass balance, and with one plate loaded
    # there is no longer a pair. That is a legitimate thing to ask for, so it
    # warns rather than refuses - but it must not pass in silence.
    if not water_wells:
        m.sim.warn(
            "unbalanced_pair",
            f"only {stock_plate} is loaded ({len(stock_wells)} x "
            f"{plate_dose_uL:g} = {len(stock_wells) * plate_dose_uL:g} uL); "
            f"nothing goes into {water_plate}. This run does NOT produce a "
            f"balanced centrifuge pair - put something of equal mass in the "
            f"opposite position before spinning it.",
        )
    elif len(water_wells) != len(stock_wells):
        raise ProtocolError(
            f"{water_plate} gets {len(water_wells)} doses and {stock_plate} "
            f"gets {len(stock_wells)}; the plates would not balance."
        )
    m.deck.placement(tube).check_well(*tube_well)

    # Nothing in the library tracks how much liquid is in a well, so the
    # arithmetic is checked here or not at all.
    withdrawn = plate_dose_uL * len(stock_wells)
    available = tube_preload_uL + stock_uL
    if withdrawn >= available:
        # >= not >: a design that drains the tube to exactly zero looks like it
        # closes, but the final aspiration has nothing above the tip, takes
        # air, and lands light with nothing able to notice at run time.
        raise ProtocolError(
            f"the tube would hold {available} uL "
            f"({tube_preload_uL} preloaded + {stock_uL} added) but this "
            f"protocol withdraws {withdrawn} uL from it, leaving "
            f"{available - withdrawn} uL. The last dose needs liquid above the "
            f"tip, so some must remain. Raise the preload or the stock, or "
            f"lower the dose."
        )
    if mix_volume_uL > available:
        raise ProtocolError(
            f"the mix stroke is {mix_volume_uL} uL but the tube only holds "
            f"{available} uL; the tip would draw air"
        )

    do_tip = get_tip and eff.needs_tip
    do_prewet = prewet and eff.prewet_enabled
    do_eject = eject_tip and eff.can_eject_tip

    m.toggles.update({
        "protocol_version": "2.1",
        "get_tip": do_tip, "get_tip_requested": get_tip,
        "prewet": do_prewet, "prewet_requested": prewet,
        "prewet_cycles_requested": prewet_cycles,
        "temp_log": temp_log,
        "eject_tip": do_eject, "eject_tip_requested": eject_tip,
        "hold_after_mix": hold_after_mix,
        "hold_z": hold_z, "hold_seconds": hold_seconds,
        "plates": [water_plate, stock_plate], "tube": tube,
        "water_plate": water_plate, "stock_plate": stock_plate,
        "water_wells": [well_name(*w) for w in water_wells],
        "stock_wells": [well_name(*w) for w in stock_wells],
        "plate_dose_uL": plate_dose_uL, "stock_uL": stock_uL,
        "tube_preload_uL": tube_preload_uL,
        "tube_contents_after_stock_uL": available,
        "tube_contents_at_end_uL": available - withdrawn,
        "mix_cycles": mix_cycles, "mix_volume_uL": mix_volume_uL,
    })

    m.note(f"{name}: version 2.1")
    if water_wells:
        m.note(f"{water_plate} <- water only; {stock_plate} <- {tube} only")
    else:
        m.note(f"{stock_plate} <- {tube} only; {water_plate} gets NOTHING - "
               f"this run is not a balanced pair")
    m.note(f"PRELOAD REQUIRED: {tube_preload_uL:g} uL already in {tube} "
           f"{well_name(*tube_well)} before this runs")
    m.note(f"{stock_plate}: {len(stock_wells)} x {plate_dose_uL:g} uL = "
           f"{len(stock_wells) * plate_dose_uL:g} uL into "
           f"{', '.join(well_name(*w) for w in stock_wells)} "
           f"(opposite corners)")
    if water_wells:
        m.note(f"{water_plate}: {len(water_wells)} x {plate_dose_uL:g} uL into "
               f"{', '.join(well_name(*w) for w in water_wells)}")
    m.note(f"tube: {tube_preload_uL:g} + {stock_uL:g} = {available:g} uL, "
           f"mixed {mix_cycles}x{mix_volume_uL:g} uL, "
           f"{withdrawn:g} withdrawn, {available - withdrawn:g} left")
    if hold_after_mix:
        m.note(f"after the mix the tip lifts to Z {hold_z:g} and holds "
               f"{hold_seconds:g} s")

    # ---- generate --------------------------------------------------------
    m.startup()
    m.log_temps("start")

    if do_tip:
        m.get_tip()
    if do_prewet:
        m.prewet(prewet_cycles)
    m.log_temps("prewet_done")

    # stages 1-2: water into both wells of the water plate
    for w in water_wells:
        m.log_temps(f"water_{water_plate}_{well_name(*w)}")
        m.aspirate_uL(plate_dose_uL)
        m.dispense(water_plate, *w)

    # stage 3: top the tube up. retract=False keeps the tip in the tube,
    # because stage 4 mixes in the same well.
    m.log_temps("stock_to_tube")
    m.aspirate_uL(stock_uL)
    m.dispense(tube, *tube_well, retract=False)

    # stage 4: homogenise deep in the tube, not at the surface. retract=False
    # when a hold follows, so the tip goes straight from the mix depth to the
    # hold height instead of bouncing off travel height on the way.
    m.log_temps("mix")
    m.mix(tube, *tube_well, cycles=mix_cycles, volume_uL=mix_volume_uL,
          retract=not hold_after_mix)

    # stage 5: lift clear of the tube and wait. The mix leaves the tip empty,
    # so nothing is held while it sits, and XY does not move.
    if hold_after_mix:
        m.log_temps("hold")
        m.hold(hold_seconds, z=hold_z)

    # stages 6-7: the mixed stock into the two corners of the stock plate
    for w in stock_wells:
        m.log_temps(f"stock_{stock_plate}_{well_name(*w)}")
        m.aspirate_from(tube, *tube_well, volume_uL=plate_dose_uL)
        m.dispense(stock_plate, *w)

    m.log_temps("end")
    if do_eject and m.tip_fitted:
        # The ejection forces a mandatory G28, so the machine is already home;
        # park() then only has to drop Z.
        m.eject_tip()
    m.park()

    return m, m.finalize(outdir=outdir, plot=plot)


def summarise(m: Machine, result) -> None:
    eff = m.effector
    t = m.toggles
    print(f"done -> {result['gcode']}")
    print(f"        {result['manifest']}")
    if result.get("plot"):
        print(f"        {result['plot']}")
    print(f"        {result['lines']} gcode lines, "
          f"{result['dispenses']} dispenses")
    print()
    print(f"  {eff.id}  {eff.equation}   "
          f"window {eff.window[0]}-{eff.window[1]} uL")
    print(f"  toggles: " + "  ".join(
        f"{k}={v}" for k, v in t.items() if isinstance(v, bool)))
    print()
    stock_total = len(t["stock_wells"]) * t["plate_dose_uL"]
    if t["water_wells"]:
        print(f"  {t['water_plate']} <- water only, "
              f"{t['stock_plate']} <- {t['tube']} only, "
              f"{stock_total:g} uL each")
    else:
        print(f"  {t['stock_plate']} <- {t['tube']} only, "
              f"{stock_total:g} uL. {t['water_plate']} gets NOTHING, so this "
              f"is NOT a balanced pair")
    print(f"  PRELOAD: {t['tube_preload_uL']:g} uL must already be in "
          f"{t['tube']} before this runs")
    print(f"  tube: {t['tube_preload_uL']:g} + {t['stock_uL']:g} = "
          f"{t['tube_contents_after_stock_uL']:g} uL -> mixed "
          f"{t['mix_cycles']}x{t['mix_volume_uL']:g} uL -> "
          f"{t['tube_contents_at_end_uL']:g} uL left")
    if t.get("hold_after_mix"):
        print(f"  after the mix: lift to Z {t['hold_z']:g} and hold "
              f"{t['hold_seconds']:g} s (+Z is DOWN, so that is "
              f"{54.0 - t['hold_z']:g} mm above the mix depth)")
    print()
    header = (f"{'#':>3} {'destination':>12} {'well':>5} {'source':>8} "
              f"{'vol_uL':>8} {'disp_mm':>8} {'asp_POS':>8} {'X':>8} {'Y':>8}")
    print(header)
    for d in m.dispenses:
        source = "stock" if d["placement"] == t["stock_plate"] else "water"
        print(f"{d['order_index']:>3} {d['placement']:>12} {d['well']:>5} "
              f"{source:>8} "
              f"{d['target_volume_uL']:>8.1f} {d['displacement_mm']:>8.3f} "
              f"{d['aspirate_pos']:>8.3f} {d['x']:>8g} {d['y']:>8g}")

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
    ap.add_argument("--effector", default="p300", choices=_eff.available(),
                    help="which end effector, i.e. config/effectors/<id>.json")
    ap.add_argument("--name", default=None, help="output basename")
    ap.add_argument("--water-plate", default="plate1", dest="water_plate",
                    help="layout placement loaded from the water reservoir")
    ap.add_argument("--stock-plate", default="plate2", dest="stock_plate",
                    help="layout placement loaded from the mixed tube stock")
    ap.add_argument("--tube", default="big_tubes",
                    help="layout placement holding the 1.5 mL tube")
    ap.add_argument("--dose", type=float, default=PLATE_DOSE_uL,
                    dest="plate_dose_uL", help="uL into each well")
    ap.add_argument("--stock", type=float, default=STOCK_uL, dest="stock_uL",
                    help="uL of water added to the tube")
    ap.add_argument("--preload", type=float, default=TUBE_PRELOAD_uL,
                    dest="tube_preload_uL",
                    help="uL already in the tube by hand before the run")
    ap.add_argument("--mix-cycles", type=int, default=MIX_CYCLES,
                    dest="mix_cycles")
    ap.add_argument("--mix-volume", type=float, default=MIX_VOLUME_uL,
                    dest="mix_volume_uL")
    ap.add_argument("--prewet-cycles", type=int, default=PREWET_CYCLES,
                    dest="prewet_cycles")
    ap.add_argument("--layout", default="layout.json",
                    help="layout file in the config dir; the per-run bench")
    ap.add_argument("--outdir", default=".")
    ap.add_argument("--no-tip", dest="get_tip", action="store_false")
    ap.add_argument("--no-prewet", dest="prewet", action="store_false")
    ap.add_argument("--no-temp-log", dest="temp_log", action="store_false")
    ap.add_argument("--no-plot", dest="plot", action="store_false")
    ap.add_argument("--no-hold", dest="hold_after_mix", action="store_false",
                    help="skip the post-mix hold")
    ap.add_argument("--hold-z", type=float, default=HOLD_Z, dest="hold_z",
                    help="commanded Z to hold at. +Z is DOWN, so a SMALLER "
                         "number is higher (default 10.0)")
    ap.add_argument("--hold-seconds", type=float, default=HOLD_SECONDS,
                    dest="hold_seconds", help="how long to hold")
    ap.add_argument("--no-eject-tip", dest="eject_tip", action="store_false",
                    help="skip the tip ejection. It runs by default, and it "
                         "forces a G28 afterwards, which is what returns the "
                         "machine home. NOTE the ejection routine has still "
                         "never been validated on hardware.")
    ap.add_argument("--lenient", dest="strict", action="store_false",
                    help="collect every simulation error instead of raising on "
                         "the first. Still refuses to write the gcode.")
    a = ap.parse_args(argv)

    try:
        m, result = build(
            effector_id=a.effector, name=a.name,
            water_plate=a.water_plate, stock_plate=a.stock_plate,
            tube=a.tube, plate_dose_uL=a.plate_dose_uL, stock_uL=a.stock_uL,
            tube_preload_uL=a.tube_preload_uL, mix_cycles=a.mix_cycles,
            mix_volume_uL=a.mix_volume_uL, prewet_cycles=a.prewet_cycles,
            get_tip=a.get_tip, prewet=a.prewet, temp_log=a.temp_log,
            eject_tip=a.eject_tip, hold_after_mix=a.hold_after_mix,
            hold_z=a.hold_z, hold_seconds=a.hold_seconds,
            layout=a.layout, outdir=a.outdir, plot=a.plot, strict=a.strict,
        )
    except LabError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1

    summarise(m, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
