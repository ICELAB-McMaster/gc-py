"""ISO 8655-style gravimetric accuracy / repeatability test.

One script for every pipette. Which pipette is a command-line argument; nothing
below is specific to one of them.

    python protocols/accuracy_test.py --effector p300
    python protocols/accuracy_test.py --effector p125 --no-prewet

Experimental design, unchanged from the scripts this replaces:

* 4 test volumes x 5 replicates = 20 tubes. **Row = volume, column = replicate.**
* ONE gcode file, run 3x against the SAME 20 rack positions. Between runs:
  weigh wet, put fresh tubes in the same holes, fit a FRESH TIP, re-run. That
  gives 15 replicates per volume across 3 tips, which satisfies ISO 8655-6's
  minimum of 10 measurements per volume and at least one tip change per volume.
* Independent aspiration per tube. Never one aspirate feeding two dispenses.
* The air gap is armed IN AIR before descending; the trailing backoff is drawn
  after leaving the water, as an anti-drip measure.
* Dispense is always full travel to blow-out, expelling backoff air, fluid and
  air gap together.

Volumes are requested in microlitres. The millimetre displacement is computed
from the pipette's stored calibration coefficients at generation time - no mm
value is typed into this file, which is what stops the two drifting apart.
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lab.effector import LabError, Pipette
from lab.labware import well_name
from lab.machine import Machine, ProtocolError

# ISO 8655-6 test points, as fractions of nominal. A property of the standard,
# not of the pipette, so both share them.
TEST_FRACTIONS = (0.10, 0.50, 0.75, 1.00)
REPLICATES = 5
COL_OFFSET = 0  # replicate block starts at column 0. Same every run, by design.


def build(
    effector_id: str,
    name: str | None = None,
    tubes: str = "tubes",
    replicates: int = REPLICATES,
    col_offset: int = COL_OFFSET,
    fractions=TEST_FRACTIONS,
    get_tip: bool = True,
    prewet: bool = True,
    wall_wipe: bool = True,
    temp_log: bool = True,
    temp_log_every: int = 1,
    eject_tip: bool = False,
    shuffle: bool = False,
    seed: int = 0,
    outdir: str = ".",
    plot: bool = True,
    strict: bool = True,
):
    name = name or f"{effector_id}_accuracy"
    m = Machine(effector=effector_id, name=name, strict=strict, temp_log=temp_log)

    eff = m.effector
    if not isinstance(eff, Pipette):
        raise ProtocolError(
            f"{effector_id} is a {eff.kind}; this protocol needs a pipette"
        )

    volumes = eff.fractions(list(fractions))
    rack = m.deck.placement(tubes)

    # ---- design checks, before a single line of gcode -------------------
    if len(volumes) > rack.labware.rows:
        raise ProtocolError(
            f"{len(volumes)} volumes need {len(volumes)} rows; "
            f"{rack.labware.id} has {rack.labware.rows}"
        )
    if replicates + col_offset > rack.labware.cols:
        raise ProtocolError(
            f"needs columns {col_offset}..{col_offset + replicates - 1}; "
            f"{rack.labware.id} has {rack.labware.cols}"
        )
    for v in volumes:
        eff.check_volume(v)  # window + stroke, including the backoff

    order = [(vi, r) for vi in range(len(volumes)) for r in range(replicates)]
    if shuffle:
        random.Random(seed).shuffle(order)

    # Record what will actually happen, not what was merely requested: an
    # effector that does not take tips silently skips get_tip, and a manifest
    # claiming get_tip=True for a run that never picked one is a lie.
    do_tip = get_tip and eff.needs_tip
    do_prewet = prewet and eff.prewet_enabled
    do_eject = eject_tip and eff.can_eject_tip

    m.toggles.update({
        "get_tip": do_tip, "get_tip_requested": get_tip,
        "prewet": do_prewet, "prewet_requested": prewet,
        "wall_wipe": wall_wipe,
        "temp_log": temp_log, "temp_log_every": temp_log_every,
        "eject_tip": do_eject, "eject_tip_requested": eject_tip,
        "shuffle_within_run": shuffle,
        "shuffle_seed": seed, "col_offset": col_offset,
        "replicates": replicates, "fractions": list(fractions),
        "volumes_uL": volumes,
    })
    m.note(f"{name}: {len(volumes)} volumes x {replicates} replicates "
           f"= {len(order)} tubes, {eff.id}")
    m.note(f"calibration {eff.equation} over {eff.window[0]}-{eff.window[1]} uL")
    m.note("run this SAME file 3x: fresh tubes in the same holes, fresh tip, "
           "weigh dry before / wet after")

    # ---- generate --------------------------------------------------------
    m.startup()
    m.log_temps("start")

    if do_tip:
        m.get_tip()
    if do_prewet:
        m.prewet()
    m.log_temps("prewet_done")

    for i, (vol_idx, rep_idx) in enumerate(order):
        volume = volumes[vol_idx]
        row = vol_idx
        col = rep_idx + col_offset

        if temp_log_every and i % temp_log_every == 0:
            m.log_temps(f"v{volume:g}_r{rep_idx + 1}")

        m.aspirate_uL(volume)                       # independent, for THIS tube
        m.dispense(tubes, row=row, col=col, wall_wipe=wall_wipe)

    m.log_temps("end")
    if do_eject and m.tip_fitted:
        m.eject_tip()
    m.park()

    return m, m.finalize(outdir=outdir, plot=plot)


def summarise(m: Machine, result) -> None:
    eff = m.effector
    print(f"done -> {result['gcode']}")
    print(f"        {result['manifest']}")
    if result.get("plot"):
        print(f"        {result['plot']}")
    print(f"        {result['lines']} gcode lines, "
          f"{result['dispenses']} dispenses")
    print()
    print(f"  {eff.id}  {eff.equation}   "
          f"window {eff.window[0]}-{eff.window[1]} uL   "
          f"max {eff.max_volume_uL} uL")
    print(f"  blowout={eff.blowout_pos}  air_gap={eff.air_gap}  "
          f"gap_pos={eff.gap_pos}  backoff={eff.post_asp_backoff}  "
          f"margin={eff.stroke_margin}")
    print(f"  toggles: " + "  ".join(
        f"{k}={v}" for k, v in m.toggles.items()
        if isinstance(v, bool)))
    print()
    header = (f"{'vol_uL':>8} {'disp_mm':>8} {'asp_POS':>8} {'backoff':>8} "
              f"{'well':>5} {'row':>4} {'col':>4} {'X':>7} {'Y':>7}")
    print(header)
    for d in m.dispenses:
        print(f"{d['target_volume_uL']:>8.2f} {d['displacement_mm']:>8.3f} "
              f"{d['aspirate_pos']:>8.3f} {d['backoff_pos']:>8.3f} "
              f"{d['well']:>5} {d['row']:>4} {d['col']:>4} "
              f"{d['x']:>7g} {d['y']:>7g}")

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
    ap.add_argument("--effector", required=True, choices=_eff.available(),
                    help="which end effector, i.e. config/effectors/<id>.json")
    ap.add_argument("--name", default=None, help="output basename")
    ap.add_argument("--tubes", default="tubes",
                    help="layout placement holding the tube rack")
    ap.add_argument("--replicates", type=int, default=REPLICATES)
    ap.add_argument("--col-offset", type=int, default=COL_OFFSET)
    ap.add_argument("--outdir", default=".")
    ap.add_argument("--no-tip", dest="get_tip", action="store_false")
    ap.add_argument("--no-prewet", dest="prewet", action="store_false")
    ap.add_argument("--no-wipe", dest="wall_wipe", action="store_false")
    ap.add_argument("--no-temp-log", dest="temp_log", action="store_false")
    ap.add_argument("--no-plot", dest="plot", action="store_false")
    ap.add_argument("--eject-tip", action="store_true",
                    help="eject the tip at the end. OFF by default: the "
                         "ejection routine has never been validated on "
                         "hardware, and the 3-run workflow fits a fresh tip "
                         "by hand between runs anyway.")
    ap.add_argument("--shuffle", action="store_true",
                    help="shuffle the visiting order only; tube assignment is "
                         "unchanged. One gcode file means the same order "
                         "replays in all 3 runs.")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--lenient", dest="strict", action="store_false",
                    help="collect every simulation error instead of raising on "
                         "the first. Still refuses to write the gcode.")
    a = ap.parse_args(argv)

    try:
        m, result = build(
            effector_id=a.effector, name=a.name, tubes=a.tubes,
            replicates=a.replicates, col_offset=a.col_offset,
            get_tip=a.get_tip, prewet=a.prewet, wall_wipe=a.wall_wipe,
            temp_log=a.temp_log, eject_tip=a.eject_tip, shuffle=a.shuffle,
            seed=a.seed, outdir=a.outdir, plot=a.plot, strict=a.strict,
        )
    except LabError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1

    summarise(m, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
