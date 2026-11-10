"""Fill the first two rows of a 96-well plate from the water: 100 uL, then 200 uL.

    python protocols/plate_two_rows.py

One tip for the whole run, then ejected:

    home
    pick up a tip, prewet it in the water
    row A, all 12 wells: aspirate 100 uL of water, dispense, one well at a time
    row B, all 12 wells: aspirate 200 uL of water, dispense
    eject the tip (which re-homes), park

Each well is its own aspirate and full blowout, as every protocol here does:
no load ever feeds two wells, so no well gets a retained film the others did
not. That is 24 trips to the water for 3.6 mL in total.

The plate is `plate1`, slot 1, because its A1 is measured. `plate2`'s A1 is
still INFERRED from plate1's offset - pass `--plate plate2` only after jogging
to it and confirming.

THE TIP EJECTION HAS NEVER BEEN VALIDATED ON HARDWARE
(deck.stations.tip_eject.validated is false). It is on here because the run
asks for it; the slicer warns, and `--no-eject` leaves the tip on and parks.
Watch the first one with a hand on the stop.
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
PLATE = "plate1"
ROW_DOSES_uL = ((0, 100.0), (1, 200.0))     # (row index, uL) - row A, row B
PREWET_CYCLES = 3


def build(
    effector_id: str = "p300",
    name: str | None = None,
    plate: str = PLATE,
    row_doses=ROW_DOSES_uL,
    prewet: bool = True,
    prewet_cycles: int = PREWET_CYCLES,
    eject_tip: bool = True,
    temp_log: bool = True,
    layout: str = "layout.json",
    outdir: str = ".",
    plot: bool = True,
    strict: bool = True,
):
    name = name or "plate_two_rows"
    m = Machine(effector=effector_id, name=name, layout=layout,
                strict=strict, temp_log=temp_log)
    eff = m.effector
    if not isinstance(eff, Pipette):
        raise ProtocolError(
            f"{effector_id} is a {eff.kind}; this protocol needs a pipette"
        )

    # ---- design checks, before a single line of gcode -------------------
    p = m.deck.placement(plate)              # raises DeckError if not loaded
    cols = p.labware.cols
    wells = []
    for row, dose in row_doses:
        eff.check_volume(dose)               # window + stroke + backoff
        for col in range(cols):
            p.check_well(row, col)
            wells.append((row, col, dose))
    total = sum(d for _r, _c, d in wells)

    do_prewet = prewet and eff.prewet_enabled
    do_eject = eject_tip and eff.can_eject_tip
    m.toggles.update({
        "plate": plate, "wells": len(wells), "total_uL": total,
        "row_doses": [[well_name(r, 0)[0], d] for r, d in row_doses],
        "prewet": do_prewet, "eject_tip": do_eject,
        "eject_tip_requested": eject_tip,
    })
    m.note(f"{name}: {plate} rows "
           + ", ".join(f"{well_name(r, 0)[0]} x{cols} @ {d:g} uL"
                       for r, d in row_doses)
           + f" = {total:g} uL from water, one tip")
    if eject_tip and not do_eject:
        m.sim.warn("no_eject", f"{eff.id} cannot eject a tip; it stays on")

    # ---- generate --------------------------------------------------------
    m.startup()
    m.log_temps("start")
    if eff.needs_tip:
        m.get_tip()
    if do_prewet:
        m.prewet(prewet_cycles)

    for row, col, dose in wells:
        m.log_temps(f"{plate}_{well_name(row, col)}")
        m.aspirate_uL(dose)
        m.dispense(plate, row, col)

    m.log_temps("end")
    if do_eject and m.tip_fitted:
        m.eject_tip()            # forces its own G28, so the machine is home
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
    for row, dose in t["row_doses"]:
        print(f"  row {row}: 12 wells x {dose:g} uL")
    print(f"  {t['wells']} wells, {t['total_uL']:g} uL of water in total, "
          f"into {t['plate']}")
    print(f"  prewet {'on' if t['prewet'] else 'off'}; tip "
          f"{'EJECTED (unvalidated routine)' if t['eject_tip'] else 'left on'}")
    report = result["report"]
    if report.warnings:
        print()
        print(f"{len(report.warnings)} warning(s):")
        for w in report.warnings:
            print(f"  - {w}")


def main(argv=None) -> int:
    from lab import effector as _eff

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--effector", default="p300", choices=_eff.available())
    ap.add_argument("--name", default=None)
    ap.add_argument("--plate", default=PLATE,
                    help="placement to fill (default plate1, whose A1 is "
                         "measured; plate2's is inferred)")
    ap.add_argument("--no-prewet", dest="prewet", action="store_false")
    ap.add_argument("--no-eject", dest="eject_tip", action="store_false",
                    help="leave the tip on and park instead of ejecting")
    ap.add_argument("--no-temp-log", dest="temp_log", action="store_false")
    ap.add_argument("--layout", default="layout.json")
    ap.add_argument("--outdir", default=".")
    ap.add_argument("--no-plot", dest="plot", action="store_false")
    ap.add_argument("--lenient", dest="strict", action="store_false")
    a = ap.parse_args(argv)
    try:
        m, result = build(effector_id=a.effector, name=a.name, plate=a.plate,
                          prewet=a.prewet, eject_tip=a.eject_tip,
                          temp_log=a.temp_log, layout=a.layout,
                          outdir=a.outdir, plot=a.plot, strict=a.strict)
    except LabError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    summarise(m, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
