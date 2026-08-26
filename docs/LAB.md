# The `lab` layer

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

Run the built-in protocol:

```bash
python protocols/accuracy_test.py --effector p300 --outdir out
```

## Rules the library enforces so a protocol cannot break them

| rule | where |
|---|---|
| Volumes are requested in µL; mm come from stored coefficients | `Pipette.displacement_mm` |
| A volume outside the calibration window is refused | `Pipette.check_volume` |
| The whole stroke, backoff included, is validated before anything is emitted | `Machine.aspirate_uL` |
| Plunger POS stays in `[pos_min + stroke_margin, blowout_pos]` | `Pipette.check_pos` |
| `goto()` is the only public XY move, and it lifts to travel height first | `Machine.goto` |
| Y motion never happens inside a keep-out band | `Machine._route_xy` |
| A traverse must clear every loaded labware along the whole swept path | `Simulator._check_traverse_clearance` |
| Tips are consumed edge-first, and a used position is never revisited | `Machine._next_free_tip_col` |
| After a tip pickup, the exit is X-first over already-consumed positions | `Machine._exit_tip_box` |
| Every tip ejection is followed by `G28` | `Simulator._pending_eject_home` |
| No dispense without an aspirate; no aspirate without a tip | `Machine.aspirate_uL` / `dispense` |
| Nothing is written to disk until simulation passes | `Machine.finalize` |

Every error the library raises subclasses `lab.effector.LabError`, so a protocol
catches that one class and prints the message instead of a traceback.

The keep-out exemption is carried **per step**, not per operation: only the
waypoints that actually engage the ejector are exempt, so the approach and
retract that surround them are checked like any other traverse. Naming an
operation `eject_tip` buys nothing.

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

## What is still unmeasured

Every absolute-Z crash check is currently inert. A clean run reports this; the
manifest records it. Before trusting generated gcode on hardware:

| field | why it matters |
|---|---|
| `deck.z.stage_surface` | with `tip.length`, the whole tip-aware Z envelope |
| `tiprack_300_96.tip.length` | how far a fitted tip extends below the nosecone |
| each labware `z.top` | traverse clearance — the crash mode that costs a tip |
| each labware `z.bottom` | stops a dispense driving into the vessel |
| each labware `footprint` | pairwise overlap, and rim-clipping on a traverse |
| `p300.z_for_labware.tube_rack_0p65_5x10.work` | the Z it descends to 20× a run |
| `tiprack_300_96.consume_order.exit_edge` | which way the effector leaves the box |
| `tiprack_300_96.grid.a1_corner` | which corner is A1 — well names in the manifest |
| `keepouts.tip_ejector.x_range` | the band width at X155 is carried over from X85 |

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
