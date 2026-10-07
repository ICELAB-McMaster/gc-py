# gc-py

Python generates gcode for a Klipper pipetting robot in a self-driving lab. The
output drives real hardware into real glassware, so a wrong number here does not
crash - it dispenses into the wrong tube and the balance readings still look
plausible.

`docs/LAB.md` orients; `docs/API.md` documents every function;
`docs/RUNS.md` logs what happened on the bench. Two project
subagents exist: **lab-explainer** for "why do these numbers disagree" and doc
work, **lab-engineer** for code changes.

## Three conventions that read backwards

Get any of these the natural way round and you produce something fluent and wrong.

1. **+Z is DOWN.** `Z=0` is home at the top; a *larger* commanded Z is physically
   *lower*. "Stay above Z 60" is written `z < 60`. The ejector's `Z145 -> Z137` is
   an 8 mm *upward* stroke. When writing a Z comparison, put the physical meaning
   in a comment on the spot.
2. **Volumes are microlitres, never millimetres.** `aspirate_uL(150.0)`. The
   conversion happens only in `Pipette.displacement_mm`, from coefficients in
   `config/effectors/*.json`. **Never type a mm value into a protocol.**
3. **A check that cannot be evaluated is not a pass.** Missing a measurement means
   `self.warn(key, "cannot check ... because <field> is unmeasured")` and skip -
   never a silent early return.

## The X frame: +70

The X travel limit was increased by 70 mm. The machine homes at max X, so the same
physical endstop is now called 420 where it was 350, and **every physical point
gained 70 in X**. Anything written before that change is 70 mm low today. Evidence
and corrections are in `config/deck.json` -> `coordinate_frame`.

It was invisible because the tube pitch is 12 mm and 70 mm is six columns minus
2 mm: a shifted well lands 2 mm from a real well, inside the tube, wrong column,
no crash. Verify frames with `tools/check_alignment.py`, never by eye. Y and Z
were never shifted.

## Machine facts

Envelope X 0-420, Y 0-580, Z 0-270. X homes at 420. Travel Z 5.0.

Measured 2026-08-26, referenced to the **p300 bare nosecone**:

| | Z |
|---|---|
| bare nosecone contacts the stage | 114.75 |
| same stage, 300 uL tip fitted | 62.5 |
| tip length (derived) | 52.25 |
| raised location edges, tipped traverse limit | 53.0 |
| tallest labware that clears travel height with a tip | 57.5 mm |

Slots are 3x3, row-major: **1-3 front** (Y 365), 4-6 middle (Y 272), **7-9 back**
(Y 178.5); X descends within a row (336 / 200 / 65). Loaded now: tip rack slot 3,
96-well plates in slots 1 and 2, 1.5 mL tube holder slot 5, water at the station
near slot 9. `config/layout_test.json` is the fixture the suite uses, so tests do
not move when the bench does.

The stage surface exists only over the slot area, which is why the ejector's
Z 145 at Y 89 is legal - it hangs past the front edge.

Both tube holders need a **Y-first approach**: their caps make Y motion across the
holder difficult, so the effector reaches its Y while still clear in X.

The tip ejection routine has **never been validated on hardware**
(`deck.stations.tip_eject.validated: false`) and is off by default.

## The gripper

A Parallax Feedback 360 servo on a rack and pinion, over CAN, driven through the
`GRIPPER_*` macros in the SDL_Gcode repo. Three things that bite:

1. **It has no position.** Feedback has failed, so motion is **timed** and
   gripping is a current-controlled stall that **expires after ~140 s**.
2. **`GRIPPER_PICK` / `GRIPPER_PLACE` must never run here.** They emit relative
   Z with a normal printer's sign; +Z is DOWN on this machine, so their "lift"
   drives into the part. The library refuses them by name.
3. **An IMU capture including the board sensor blocks commands.** The board runs
   that capture as a blocking loop and *queues* anything it receives. Only a
   `--plate-only` capture can span a grip. See `capture_concurrency` in
   `config/effectors/gripper.json`.

Plate geometry is still unmeasured, so `gripper_plate_test.py` refuses; station
9 is measured and `gripper_station9_test.py` runs.

## Running things

```bash
python tests/test_lab.py            # 93 tests
python tests/test_review_fixes.py   # 22 tests, one per confirmed past defect
python tests/smoke_geometry.py      # resolve every placement and coordinate

python protocols/accuracy_test.py --effector p300 --outdir out --no-plot
python tools/check_alignment.py out/*.gcode          # does it land on the deck?
python tools/compare_blocks.py old.gcode new.gcode   # diff by block, not line
```

Any change to motion, geometry or safety logic runs all of it. Read the warnings,
not just the pass line - a clean run still reports what it could not check.

Before treating any value as known:

```bash
python -c "import sys;sys.path.insert(0,'.');from lab.deck import Deck;d=Deck.load();[print(' -',u) for u in d.uncalibrated()];[print(' ?',q) for q in d.open_questions]"
```

## Environment

Windows. `python` is 3.14 and cannot import `gcodepy` directly, but
`lab/__init__.py` bootstraps `src/` onto `sys.path`, so the commands above work.
`py -3.12` has the editable install - use it only for the legacy `examples/`
scripts, and `git checkout -- '*.gcode'` afterwards since they overwrite the
committed reference output.

Three traps that have already cost time here:

- **Bash heredocs mangle Python.** Use the Write and Edit tools for files.
- **`write_text` truncates before it encodes.** One non-ASCII character in a
  `cp1252` context leaves the file at zero bytes. This destroyed `docs/LAB.md`.
- **An inverted slice yields `""`, and `str.replace("", new)` inserts between
  every character.** This destroyed `lab/simulate.py`. Anchor on unique strings.

## Layout

```
config/     data only, no logic. deck / labware / layout / effectors
lab/        config -> labware+effector -> deck -> machine; simulate observes
protocols/  the procedure - no coordinates, no millimetres
tools/      comparison and verification
src/gcodepy the gcode writer (forked)
examples/   the original scripts this replaced; reference, still runnable
```

Seams worth not breaking: geometry is slot-independent and placement is per-run;
labware stores a **height above the stage**, not a commanded Z; one JSON per end
effector; nothing reaches disk until simulation passes; `goto()` is the only
public XY move.

## Commits

Config and code go in separate commits, so a wrong slot position can be reverted
without losing library work. Subject is `area: what changed`, lower case. The body
says what was wrong and what the evidence was, with real numbers. Do not squash.
End with:

```
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```
