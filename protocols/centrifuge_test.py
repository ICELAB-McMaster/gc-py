"""Centrifuge mass-balance preparation across two 96-well plates.

Builds a diluted stock in the 1.5 mL tube. Each plate is loaded from **one
source only** - location 1 from the water reservoir, location 2 from the tube -
and the two plates receive equal mass, so they balance in a centrifuge.

    python protocols/centrifuge_test.py --effector p300

BEFORE RUNNING: the 1.5 mL tube must be **preloaded by hand with 50 uL**. The
run is not reproducible without it, and nothing in the library can check it -
there is no per-well liquid inventory anywhere, so source sufficiency is this
script's responsibility and the preload is recorded in the manifest.

Sequence, with the tube contents tracked:

    stage  action                                    volume   tube after
      1    water        -> plate 1, well A            100 uL      50
      2    water        -> plate 1, well B            100 uL      50
      3    water        -> 1.5 mL tube                250 uL     300
      4    mix at the tube BOTTOM, 5 cycles           200 uL     300
      5    1.5 mL tube  -> plate 2, well A            100 uL     200
      6    1.5 mL tube  -> plate 2, well B            100 uL     100

Both plates finish with two wells of 100 uL, so 200 uL each: equal mass from
different liquids, which is what makes them a valid centrifuge pair while
leaving each plate a single-source sample.

One source per plate also means the water doses and the stock doses never
interleave. Each source is visited in one consecutive run of stages, so the
effector crosses the deck twice rather than four times.

Every transfer is an **independent aspirate plus full blow-out**. Splitting one
200 uL load into two metered 100 uL dispenses would be fewer moves, but the
second dispense retains a liquid film the first did not, so one plate would land
a few uL light - and an even mass is the entire point of this protocol.

The two wells are diagonally opposite about the plate centre, so each plate is
internally balanced as well as matched to the other.

The run ends by ejecting the tip and returning home. The ejection forces a
mandatory G28 - the strip force can skip steps on an open-loop machine - so the
park that follows is only a Z move. Pass --no-eject-tip to leave the tip on.

Volumes are requested in microlitres. The millimetre displacement comes from the
pipette's stored calibration at generation time; no mm value is typed here.
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
# Diagonally opposite about the centre of an 8x12 plate, so the mass in each
# plate is centred as well as matched between plates.
WELL_A = (3, 5)          # D6
WELL_B = (4, 6)          # E7

PLATE_DOSE_uL = 100.0    # into each well, four times total
STOCK_uL = 250.0         # water added to the 1.5 mL tube
TUBE_PRELOAD_uL = 50.0   # already in the tube, put there by hand
TUBE_WELL = (0, 0)       # which position in the 1.5 mL holder

MIX_CYCLES = 5
MIX_VOLUME_uL = 200.0

PREWET_CYCLES = 3


def build(
    effector_id: str = "p300",
    name: str | None = None,
    water_plate: str = "plate1",
    stock_plate: str = "plate2",
    tube: str = "big_tubes",
    tube_well=TUBE_WELL,
    well_a=WELL_A,
    well_b=WELL_B,
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
    layout: str = "layout.json",
    outdir: str = ".",
    plot: bool = True,
    strict: bool = True,
):
    name = name or "centrifuge_test"
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

    plates = (water_plate, stock_plate)
    wells = (well_a, well_b)
    for pname in plates:
        p = m.deck.placement(pname)  # raises DeckError if not loaded
        for w in wells:
            p.check_well(*w)         # raises IndexError if out of range
    if water_plate == stock_plate:
        raise ProtocolError(
            f"water_plate and stock_plate are both {water_plate!r}. The point "
            f"of this protocol is one source per plate; two sources into one "
            f"plate is the old behaviour and there is no mass to balance "
            f"against."
        )
    if well_a == well_b:
        raise ProtocolError(
            "well_a and well_b are the same well; the second pair of doses is "
            "meant to land somewhere the first did not"
        )
    m.deck.placement(tube).check_well(*tube_well)

    # Nothing in the library tracks how much liquid is in a well, so the
    # arithmetic is checked here or not at all.
    # Only the stock plate draws on the tube now, so the tube supplies one
    # dose per well of one plate rather than one dose per plate.
    withdrawn = plate_dose_uL * len(wells)
    available = tube_preload_uL + stock_uL
    if withdrawn > available:
        raise ProtocolError(
            f"the tube would hold {available} uL "
            f"({tube_preload_uL} preloaded + {stock_uL} added) but this protocol "
            f"withdraws {withdrawn} uL from it. Raise the preload or the stock, "
            f"or lower the dose."
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
        "get_tip": do_tip, "get_tip_requested": get_tip,
        "prewet": do_prewet, "prewet_requested": prewet,
        "prewet_cycles_requested": prewet_cycles,
        "temp_log": temp_log,
        "eject_tip": do_eject, "eject_tip_requested": eject_tip,
        "plates": list(plates), "tube": tube,
        "water_plate": water_plate, "stock_plate": stock_plate,
        "well_a": well_name(*well_a), "well_b": well_name(*well_b),
        "plate_dose_uL": plate_dose_uL, "stock_uL": stock_uL,
        "tube_preload_uL": tube_preload_uL,
        "tube_contents_after_stock_uL": available,
        "tube_contents_at_end_uL": available - withdrawn,
        "mix_cycles": mix_cycles, "mix_volume_uL": mix_volume_uL,
    })

    m.note(f"{name}: centrifuge mass balance across {len(plates)} plates, "
           f"one source each")
    m.note(f"{water_plate} <- water only; {stock_plate} <- {tube} only")
    m.note(f"PRELOAD REQUIRED: {tube_preload_uL:g} uL already in {tube} "
           f"{well_name(*tube_well)} before this runs")
    m.note(f"each plate receives {len(wells)} x {plate_dose_uL:g} uL into "
           f"{well_name(*well_a)} and {well_name(*well_b)} = "
           f"{len(wells) * plate_dose_uL:g} uL")
    m.note(f"tube: {tube_preload_uL:g} + {stock_uL:g} = {available:g} uL, "
           f"mixed {mix_cycles}x{mix_volume_uL:g} uL, "
           f"{withdrawn:g} withdrawn, {available - withdrawn:g} left")
    m.note("independent aspirate + full blow-out per well, so every dose is "
           "handled identically")

    # ---- generate --------------------------------------------------------
    m.startup()
    m.log_temps("start")

    if do_tip:
        m.get_tip()
    if do_prewet:
        m.prewet(prewet_cycles)
    m.log_temps("prewet_done")

    # stages 1-2: water straight into both wells of the water plate. Doing
    # both here rather than one per plate keeps the water aspirations
    # consecutive, so the stock plate is never visited with water in the tip.
    for w in wells:
        m.log_temps(f"water_{water_plate}_{well_name(*w)}")
        m.aspirate_uL(plate_dose_uL)
        m.dispense(water_plate, *w)

    # stage 3: build the stock in the tube, which already holds the preload.
    # retract=False keeps the tip in the tube, because stage 4 mixes in the
    # same well - lifting to travel and descending again would be for nothing.
    m.log_temps("stock_to_tube")
    m.aspirate_uL(stock_uL)
    m.dispense(tube, *tube_well, retract=False)

    # stage 4: homogenise it deep in the tube, not at the surface
    m.log_temps("mix")
    m.mix(tube, *tube_well, cycles=mix_cycles, volume_uL=mix_volume_uL)

    # stages 5-6: the mixed stock into both wells of the stock plate
    for w in wells:
        m.log_temps(f"stock_{stock_plate}_{well_name(*w)}")
        m.aspirate_from(tube, *tube_well, volume_uL=plate_dose_uL)
        m.dispense(stock_plate, *w)

    m.log_temps("end")
    if do_eject and m.tip_fitted:
        # The ejection forces a mandatory G28, so the machine is already at the
        # home position; park() then only has to drop Z.
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
    print(f"  {t['water_plate']} <- water only, "
          f"{t['stock_plate']} <- {t['tube']} only, "
          f"{2 * t['plate_dose_uL']:g} uL each")
    print(f"  PRELOAD: {t['tube_preload_uL']:g} uL must already be in "
          f"{t['tube']} before this runs")
    print(f"  tube: {t['tube_preload_uL']:g} + {t['stock_uL']:g} = "
          f"{t['tube_contents_after_stock_uL']:g} uL -> mixed "
          f"{t['mix_cycles']}x{t['mix_volume_uL']:g} uL -> "
          f"{t['tube_contents_at_end_uL']:g} uL left")
    print()
    header = (f"{'#':>3} {'destination':>12} {'well':>5} {'source':>10} "
              f"{'vol_uL':>8} {'disp_mm':>8} {'asp_POS':>8} {'X':>8} {'Y':>8}")
    print(header)
    for d in m.dispenses:
        if d["placement"] == t["water_plate"]:
            source = "water"
        elif d["placement"] == t["stock_plate"]:
            source = "stock"
        else:
            source = "water"          # the tube itself, filled from water
        print(f"{d['order_index']:>3} {d['placement']:>12} {d['well']:>5} "
              f"{source:>10} "
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
    ap.add_argument("--no-eject-tip", dest="eject_tip", action="store_false",
                    help="skip the tip ejection. It runs by default, and it "
                         "forces a G28 afterwards, which is what returns the "
                         "machine home. NOTE the ejection routine has still "
                         "never been validated on hardware - dry-run it and "
                         "step it through at reduced feedrate the first time.")
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
            eject_tip=a.eject_tip, layout=a.layout, outdir=a.outdir,
            plot=a.plot, strict=a.strict,
        )
    except LabError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1

    summarise(m, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
