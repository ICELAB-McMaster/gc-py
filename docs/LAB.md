# The `lab` layer

> Function-by-function reference: [API.md](API.md).
> What actually happened on the bench: [RUNS.md](RUNS.md).

Everything the pipetting robot knows about itself lives in `config/`. Everything
it knows how to *do* lives in `lab/`. A protocol script is then just the
procedure, with no coordinates and no millimetres in it.

```
config/
  deck.json            machine envelope, slot centres, stations, keep-out zones
  labware.json         labware GEOMETRY (rows, cols, pitch) - slot-independent
  layout.json          what is loaded WHERE right now - edit this per run
  effectors/
    p125.json          one file per end effector
    p300.json
    gripper.json       placeholder, refuses to generate motion

lab/
  config.py            path resolution (LAB_CONFIG or ./config) + SHA-256 hashing
  labware.py           well (row, col) -> machine (x, y)
  deck.py              joins deck + labware + layout; stations and keep-outs
  effector.py          Pipette / Gripper; volume -> millimetres lives here
  machine.py           the primitives a protocol calls
  simulate.py          bounds, keep-outs and state checking
  logbook.py           run manifest + balance CSV

protocols/
  accuracy_test.py     ISO 8655 accuracy/repeatability, any pipette
  centrifuge_test.py   two plates to equal mass, one liquid source each
  centrifuge_test_v2_1.py  location 2 only, corner wells, post-mix hold

tools/compare_gcode.py compare two .gcode files by motion, not bytes
tests/test_lab.py      regression tests; pins every known-good coordinate
```

## Writing a protocol

```python
from lab.machine import Machine

m = Machine(effector="p300", name="my_protocol")

m.startup()                       # home Z, home XY, home actuator, prime, lift
m.get_tip()                       # picks the column at the exit edge
m.prewet()                        # only if the effector config enables it

m.aspirate_uL(150.0)              # microlitres. Always microlitres.
m.dispense("tubes", row=0, col=0) # a placement name from layout.json

m.eject_tip()
m.park()

result = m.finalize(outdir="out") # simulates, THEN writes
```

`finalize()` runs the whole-run checks and refuses to write the `.gcode` if any
hard check fails. It also writes `<name>.manifest.json` and
`<name>.balance.csv`.

Run a built-in protocol:

```bash
python protocols/accuracy_test.py --effector p300 --outdir out
python protocols/centrifuge_test.py --effector p300 --outdir out
```

## The centrifuge balance test

`protocols/centrifuge_test.py` prepares two 96-well plates with **equal mass**
so they can sit opposite each other in a centrifuge, while giving each plate a
**single liquid source** so the samples stay unmixed.

The deck it expects: tips in slot 3, a 96-well plate in slots 1 and 2, the
1.5 mL tube holder in slot 5, water at the station near slot 9.

| stage | action | volume | tube after |
|---|---|---|---|
| 1 | water → plate 1, D6 | 150 µL | 50 |
| 2 | water → plate 1, E7 | 150 µL | 50 |
| 3 | water → 1.5 mL tube | 300 µL | 350 |
| 4 | mix at Z 54, 5 cycles | 200 µL | 350 |
| 5 | tube → plate 2, D6 | 150 µL | 200 |
| 6 | tube → plate 2, E7 | 150 µL | 50 |

**The tube is preloaded by hand with 50 µL before the run.** Nothing in the
library tracks liquid in a well, so source sufficiency is the protocol's own
job: `build()` refuses a design that withdraws more than the tube holds, and
refuses one that empties it *exactly* — the final draw needs liquid above the
tip or it takes air, and no check at run time could notice.

Each plate finishes with two wells of 150 µL, 300 µL per plate. The two wells
are diagonally opposite about the plate centre, so each plate is internally
balanced as well as matched to the other.

Three design points that are easy to get backwards:

* **Every transfer is an independent aspirate plus full blow-out.** One 300 µL
  load split into two metered 150 µL dispenses would be fewer moves, but the
  second dispense retains a film the first did not, and one plate would land a
  few µL light. Equal mass is the entire point.
* **Draw and dispense use different depths in the tube** — `aspirate` 54.0 and
  `work` 35.0. A dispense wants to be above the liquid; a draw wants the orifice
  under it, and the further the level falls the deeper that has to be.
* **Z 54 is 1 mm past the Z 53.0 raised-edge limit.** Legal only because the mix
  and the draw are XY-stationary inside slot 5 and lift to travel height before
  any XY move. The simulator hard-fails any XY leg at that depth.

`--water-plate` and `--stock-plate` name which plate gets which source; both go
into the manifest, because a balance reading is not interpretable without
knowing which liquid a plate held.

### Version 2.1

`protocols/centrifuge_test_v2_1.py` loads **location 2 only**. The tube is
preloaded by hand with 150 uL, the robot adds 200 uL of water, mixes, lifts
clear and holds for 30 s, then puts 140 uL into each of two **corners**.

| | plate 1 | plate 2 |
|---|---|---|
| wells | none | **H1** (251.0, 336.0), **A12** (152.0, 399.0) |
| source | - | the mixed tube stock |
| total | 0 | 2 x 140 = 280 uL |

The two corners are diagonally opposite about the plate centre, so the plate
is internally balanced - `Placement.grid_center()` is the midpoint of the
pair, and a test asserts it rather than pinning coordinates.

**This run does not produce a balanced pair on its own**, because location 1
is empty - and that is deliberate. The operator's reason (2026-09-11): the two
well plates have **different tare weights**, so matching their dispensed
volumes would not have balanced them anyway. Balance by weighing the loaded
plates, not by matching what went into them. The protocol still says so on
every run, because "no counterweight yet" and "no counterweight needed" look
identical in a gcode file. `--water-wells D6 E7` restores the pair if wanted.

**The tube arithmetic.** 150 preloaded plus 200 added is 350, and 2 x 140
draws 280, leaving **70 uL**. A tube can never be drawn to exactly zero - the
last aspiration would have no liquid above the tip - and `build()` refuses any
combination that does not leave a positive residual. 70 uL is comfortable; at
350 in the tube the dose could go back to 150 and still leave 50.

**A12 is at X 152, inside the tip-ejector band X[148, 162]**, where Y motion
with a tip fitted is a hard error. It is reachable because `_route_xy` takes
the Y leg at a clear X (252.5, coming off the tube) and then crosses the band
in X, which is harmless. That is the router earning its place; do not
hand-order moves into that column.

**The preload is 200 uL, not 150.** 150 preloaded plus 150 added is 300, and
drawing 2 x 150 out of 300 empties the tube exactly - the last aspiration
would have no liquid above the tip. `build()` refuses that, so 200 gives the
same 300 uL of usable stock plus the 50 uL that has to stay behind anyway.

**The hold.** `Machine.hold(seconds, z=...)` moves Z and then dwells, and
touches nothing else - XY does not move, which is what keeps it clear of the
travel-height rule. `mix(..., retract=False)` leaves the tip at the mix depth
so the hold lifts **straight from Z 54 to Z 10**, 44 mm up, instead of
bouncing off travel height on the way.

Read that Z carefully: **+Z is down, so Z 10 is higher than Z 54 and higher
than the Z 5 travel height is not**. Z 10 is 5 mm *below* travel. It is clear
of the tube and of the liquid, which is all the hold needs. The Z move is
checked like any other, so an unsafe depth is refused rather than held at.

A timed hold needs nothing in `printer.cfg` and cannot leave the machine
waiting forever, which is why it is a `G4` dwell rather than a Klipper
`PAUSE`. `--hold-z`, `--hold-seconds` and `--no-hold` are on the CLI.

## Rules the library enforces so a protocol cannot break them

| rule | where |
|---|---|
| Volumes are requested in µL; mm come from stored coefficients | `Pipette.displacement_mm` |
| A volume outside the calibration window is refused | `Pipette.check_volume` |
| The whole stroke, backoff included, is validated before anything is emitted | `Machine.aspirate_uL` |
| Plunger POS stays in `[pos_min + stroke_margin, blowout_pos]` | `Pipette.check_pos` |
| `goto()` is the only public XY move, and it lifts to travel height first | `Machine.goto` |
| Y motion never happens inside a keep-out band | `Machine._route_xy` |
| A fitted tip is never routed over the tip rack | `Deck.no_fly_rects` |
| A traverse must clear every loaded labware along the whole swept path | `Simulator._check_traverse_clearance` |
| Tips are consumed edge-first, and a used position is never revisited | `Machine._next_free_tip_col` |
| After a tip pickup, the exit is X-first over already-consumed positions | `Machine._exit_tip_box` |
| Every tip ejection is followed by `G28` | `Simulator._pending_eject_home` |
| A hold moves Z, dwells, and never moves XY | `Machine.hold` |
| No dispense without an aspirate; no aspirate without a tip | `Machine.aspirate_uL` / `dispense` |
| Nothing is written to disk until simulation passes | `Machine.finalize` |

Every error the library raises subclasses `lab.effector.LabError`, so a protocol
catches that one class and prints the message instead of a traceback.

The keep-out exemption is carried **per step**, not per operation: only the
waypoints that actually engage the ejector are exempt, so the approach and
retract that surround them are checked like any other traverse. Naming an
operation `eject_tip` buys nothing.

## Never fly over the tip rack

The tips stand upright well above the rack body, and a fitted tip hangs 52.25 mm
below the nosecone. So crossing the rack with a tip on shears it off, and
**travelling higher does not help** - it is not a Z problem. The rack carries
`avoid_traverse: true`, and two things act on it:

* `Machine._route_xy` routes around it. It is candidate-based: the preferred
  axis order, then the other order, then X doglegs, then Y doglegs, and it takes
  the first whose every leg is clear.
* `Simulator` *fails* a tipped crossing rather than warning, so a hand-rolled
  move cannot bypass the router.

Y doglegs earn their place on their own. When both ends of a move sit inside the
rack's Y span - dispensing into plate 1 straight after a pickup is exactly that -
no axis order and no X dogleg can get there. The only route is Y clear of the
rack, X across, Y back.

This was found on hardware. The water station at X 70 sits *underneath* the rack
in X, so every unrouted approach to water from a high Y ran straight through it:
six colliding legs in one run, all at travel height.

## Conventions that read backwards

**+Z is DOWN.** `Z=0` is home, at the top. A *larger* commanded Z is physically
*lower*. So "the effector must be above Z 60" means `z < 60`, and the ejector
stroke `Z145 → Z137` is 8 mm *upward*.

**A check that cannot be evaluated is not a pass.** Where a measurement is
missing — `z_stage_surface`, a tip length, a labware rim — the simulator emits
an explicit `cannot check ...` warning and records it in the manifest, rather
than staying quiet. The bare and tipped Z envelopes warn separately, because
measuring only `z_stage_surface` would switch the check on for bare moves while
leaving it off for the deeper, more dangerous tipped ones.

## The X frame: everything pre-dating the limit change is +70

The X travel limit was increased by 70 mm. The machine homes at max X, so the
same physical endstop is now called 420 where it used to be 350 - and every
physical point gained 70 in X. Coordinates written before that change are 70 mm
too low today.

`config/deck.json` records this under `coordinate_frame`, with the corrections
applied. What moved:

| | pre-shift | current |
|---|---|---|
| tube rack anchor (well A1) | X 186 | **X 256** [1] |
| water dip point | X 0 | **X 70** |
| park | X 350 | **X 420** (= X home) |

Four independent checks agree, which is why this is treated as settled rather
than as a guess:

* tube rack centre 132 + 70 = 202 against slot 8 at 200 - 2.0 mm
* water 0 + 70 = 70 against slot 9 at 65 - 5.0 mm
* park 350 + 70 = 420 is exactly X home; park at X 350 in a 0-350 machine
  always meant "go home in X"
* unshifted, the tube rack (X 78-186) straddles the slot 8/9 cell seam and is
  seated in neither. Shifted (X 148-256) it fits entirely inside slot 8.

[1] The tube anchor has since been **measured directly** as (256.6, 218.5) and
that is what `layout.json` carries. The extra (+0.6, -4.5) is measurement
correcting an estimate; the frame shift is still exactly 70. The measured anchor
seats the rack 4.8 mm from slot 8's centre, against the estimate's 8.7 mm.

Not affected: every Y coordinate, every Z height, and the tip rack corners -
those were measured after the change. `Deck.slot_seating_report()` is the check
that keeps this honest: a slot-mounted labware whose grid centre sits more than
half a slot pitch from its slot centre is reported on every run.

**The generated gcode is not comparable to anything produced before this
correction.** Old files are in the old frame; diff with
`tools/compare_gcode.py` and expect a uniform +70 in X.

## Slot numbering

Row-major, three rows of three. **Slots 1-3 are the front row** (largest Y,
365), 4-6 the middle (272), **7-9 the back** (smallest Y, 178.5). Within a row
X descends, so the first of the three is highest X.

Currently loaded (`config/layout.json`, the centrifuge bench): tip rack in
**slot 3**, 96-well plates in **slots 1 and 2**, the 1.5 mL tube holder in
**slot 5**, water at the station near **slot 9**. The 0.65 mL holder that the
accuracy protocol uses is off the deck; `config/layout_test.json` is the
fixture that keeps the test suite independent of whatever is loaded today.

## The Z envelope, measured 2026-08-26

Three measurements, and the whole absolute-Z model follows from them:

| measured | value |
|---|---|
| bare p300 nosecone contacts the main stage | Z **114.75** |
| same stage, 300 uL tip fitted | Z **62.5** |
| traverse limit over a location corner, tip fitted | Z **53.0** |

Everything else is derived, so there is one source of truth per number:

* **tip length = 114.75 - 62.5 = 52.25 mm.** That is what
  `labware.json` `tiprack_300_96.tip.length` holds.
* **raised edges, bare reference = 53.0 + 52.25 = 105.25.** The edges stand
  62.5 - 53.0 = **9.5 mm** above the stage floor.

Where each limit applies:

| move | Z | tip | clearance |
|---|---|---|---|
| travel | 5.0 | yes | **48.0 mm** below the edge limit |
| water submerge | 40.0 | yes | 22.5 mm above the stage floor |
| tube dispense | 45.0 | yes | 17.5 mm above the stage floor |
| tip approach / pick | 47.0 / 59.0 | no | 67.8 / 55.8 mm above the stage |
| ejector descend / strip | 145.0 / 137.0 | yes | off-stage, see below |

`z_travel = 5.0` is now **verified rather than assumed** - it clears the raised
edges by 48 mm with the longest tip fitted.

Two subtleties the simulator handles:

**The stage surface only exists over the stage.** `Deck.stage_region()` derives
it from the slot centres expanded by half the slot pitch - X[-2.75, 403.75],
Y[131.88, 411.62]. The ejector works at Y 80-89, clear of the front edge, which
is the only reason its Z 145 is reachable at all. A test asserts that; if the
ejector ever moved onto the stage, Z 145 would be a 30 mm dig into it.

**The edge limit applies to crossing a boundary, not to depth as such.** Motion
staying inside one location is not over an edge, which is what keeps the wall
wipe legal: it works at Z 45 and 41 inside a tube but never leaves slot 8.

**The 114.75 was measured with the p300 nosecone.** A different pipette body
sits at a different height, so it does not transfer - the simulator warns when
the loaded effector is not the reference one, and p125's own stage contact is
still unmeasured.

## What is still unmeasured

| field | why it matters |
|---|---|
| each labware `z.top` | only matters if a rack stands HIGHER than the raised edges - the 53.0 edge limit is currently the binding traverse constraint |
| each labware `z.bottom` | stops a dispense driving into the vessel |
| each labware `footprint` | pairwise overlap, and rim-clipping on a traverse |
| `p300.z_for_labware.tube_rack_0p65_5x10.work` | Z 45 was never tuned as a dispense depth; it is safe, not necessarily right |
| `tiprack_300_96.consume_order.exit_edge` | which way the effector leaves the box |
| `tiprack_300_96.grid.a1_corner` | which corner is A1 - well names in the manifest |
| `keepouts.tip_ejector.x_range` | the +/-7 mm band half-width is carried over from the old X 85 mechanism |
| `p125.z_reference.stage_surface` | p125 borrows the p300 reference; its own is unknown |

The tip ejection routine has never been run on hardware, and `deck.json` says
so (`stations.tip_eject.validated: false`). It is off by default in the
accuracy protocol; `--eject-tip` turns it on. Dry-run it, then step it through
manually at reduced feedrate.

## Adding an end effector

Add `config/effectors/<id>.json` with a `kind` field. If the kind is one that
already has a class (`pipette`, `gripper`), that is the whole job:

```bash
python protocols/accuracy_test.py --effector <id>
```

A new *kind* also needs a class in `lab/effector.py` and an entry in `_KINDS`.

Set `"status": "placeholder"` while the numbers are still unmeasured — the
loader will refuse to generate motion with it, which is what `gripper.json`
does today.

## Adding labware

Geometry goes in `labware.json`, placement in `layout.json`. Two grid models:

* **`pitch`** — anchor well plus pitch and integer direction vectors. The
  placement supplies `anchor_xy` (or a slot, for a grid centred on its
  footprint). Use for regular racks. The tube holder uses this.
* **`corners`** — three measured well centres. Pitch *and* rotation fall out of
  the solve, so a rack whose real pitch is not nominal comes out right. The tip
  rack uses this: its actual pitch is 9.0909 × 9.10, and assuming 9.00 would
  accumulate 1.0 mm by column 12 — enough to miss a tip.

## Config file locations

Config resolves against the repo, not the working directory, so a protocol runs
from anywhere. Point `LAB_CONFIG` at another directory to override:

```bash
LAB_CONFIG=/path/to/other/config python protocols/accuracy_test.py --effector p300
```

## Comparing output against a previous run

Byte diffs are all noise — number formatting changed deliberately. Compare what
the machine actually does:

```bash
python tools/compare_gcode.py old.gcode new.gcode
```

It reports motion, actuator and structure separately, then lists the distinct
positions that appear in only one file.
