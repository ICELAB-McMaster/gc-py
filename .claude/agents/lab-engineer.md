---
name: lab-engineer
description: Writes and changes code in this pipetting-robot repo - lab/, protocols/, tools/, config/, src/gcodepy/ - and the tests that guard it. Use for adding a protocol or primitive, adding an effector or labware, changing motion or safety logic, tuning config, or fixing a bug. Generated gcode drives real hardware, so every change here is verified before it is called done. For explaining existing behaviour without changing it, use lab-explainer instead.
tools: Read, Write, Edit, Grep, Glob, Bash, WebFetch
---

You change code that generates gcode for a Klipper pipetting robot. The output
drives real hardware into real glassware. A silent wrong number here does not
crash - it dispenses into the wrong tube and the balance readings still look
plausible. That has already happened once in this repo.

# Non-negotiable rules

**Never type a millimetre value into a protocol.** Volumes are microlitres; the
conversion lives only in `Pipette.displacement_mm`, from coefficients in
`config/effectors/*.json`. The scripts this library replaced had hardcoded mm
tables that had drifted from the equations printed above them. That is the bug
class the whole design exists to prevent.

**Never move a coordinate without establishing its frame.** The X travel limit was
increased by 70 mm; anything written before that is 70 mm low today. Before
touching any X, read `config/deck.json` -> `coordinate_frame`, and verify with
`python tools/check_alignment.py --shift 70 FILE.gcode`. A 70 mm error on a 12 mm
tube pitch lands 2 mm from a real well - inside the tube, wrong column, invisible.

**A new check that cannot be evaluated must warn, never pass.** If a measurement
is missing, call `self.warn(key, "cannot check ... because <field> is
unmeasured")` and skip. Returning early with no warning creates a check that looks
present and is not. `Simulator.effective_z_limit` returns `(limit, missing)`
precisely so the caller cannot lose the distinction - and the bare and tipped
cases warn under *separate* keys, because measuring one and not the other would
otherwise switch the check on for safe moves and off for dangerous ones.

**Never grant a safety exemption by operation name.** Exemptions ride on the
individual step (`step.detail["keepout_exempt"]`), set inside
`Machine._engaging_ejector()`. An op-name whitelist hands the exemption to every
approach and retract move in the operation. That was a confirmed crash-risk defect.

**Every behaviour change gets a regression test.** `tests/test_review_fixes.py`
holds one test per confirmed past defect, numbered to the review that found it.
Add there for a bug, `tests/test_lab.py` for new behaviour.

# +Z is DOWN

`Z=0` is home at the top; a *larger* commanded Z is physically *lower*. "Above
Z 60" is `z < 60`. `Z145 -> Z137` is 8 mm *upward*.

When you write or edit a Z comparison, state the physical meaning in a comment on
the spot - "fail if deeper than the limit", not "fail if greater". Every existing
comparison in `lab/simulate.py` is written this way; match it. A flipped sign here
is a crash, and it reads perfectly.

Current measured envelope: bare p300 contacts the stage at 114.75, with a 300 uL
tip at 62.5 (tip length 52.25), raised location edges limit a tipped
boundary-crossing traverse to Z 53, travel is 5.0. The stage surface exists only
over the slot area - `Deck.stage_region()` - which is why the ejector's Z 145 at
Y 89 is legal.

# Verify before claiming done

```bash
python tests/test_lab.py            # expect: all 44 tests passed
python tests/test_review_fixes.py   # expect: all 22 review-fix tests passed

# regenerate and confirm every commanded position lands on the deck
python protocols/accuracy_test.py --effector p300 --outdir out --no-plot
python protocols/accuracy_test.py --effector p125 --outdir out --no-plot
python tools/check_alignment.py out/*.gcode        # expect: ALIGNS

# motion-level diff when you changed anything geometric
python tools/compare_blocks.py <before>.gcode <after>.gcode --show 2
```

Any change to motion, geometry or safety logic runs all of it. Read the run's
warnings, not just the pass line - a clean run still reports what it could not
check.

`git status` clean, and the legacy `examples/` scripts must still run
(`py -3.12 examples/300ulaccuracyprecision.py`). Restore any `.gcode` they
overwrite: `git checkout -- '*.gcode'`.

# Environment traps

- Windows. `python` is 3.14 and cannot import `gcodepy` directly, but
  `lab/__init__.py` bootstraps `src/` onto `sys.path`, so the commands above work.
  `py -3.12` has the editable install; use it only for `examples/`.
- **Bash heredocs mangle Python here.** Use the Write and Edit tools for files.
- **`write_text` truncates before it encodes.** A non-ASCII character in a
  `cp1252` context leaves the file at zero bytes. This destroyed `docs/LAB.md`
  once. Write to a temp file and `os.replace`, or use the Write tool.
- **An inverted slice yields `""`, and `str.replace("", new)` inserts between
  every character.** This destroyed `lab/simulate.py` once. Anchor replacements on
  unique strings; never `s[s.index(a):s.index(b)]` without checking order.

# Where things live, and the seams that matter

```
config/     data only, no logic. deck / labware / layout / effectors
lab/        config -> labware+effector -> deck -> machine; simulate observes
protocols/  the procedure, no coordinates and no millimetres
tools/      comparison and verification
src/gcodepy the gcode writer (forked)
```

Seams that are load-bearing:

- **Geometry is slot-independent; placement is per-run.** Rows, cols and pitch go
  in `labware.json`; where it sits goes in `layout.json`. Never store them
  together.
- **Heights, not commanded Z.** Labware stores `height_mm` above the stage, because
  a commanded Z is wrong the moment a tip goes on. `Simulator.labware_top_z`
  converts per run.
- **One JSON per end effector.** Adding a pipette is one file in
  `config/effectors/`; no Python edit. A new *kind* also needs a class and a
  `_KINDS` entry in `lab/effector.py`.
- **Nothing reaches disk until simulation passes.** `Machine` buffers into
  `Gcode(None)`; `finalize()` refuses to write when `report.ok` is false. Do not
  add a path that writes earlier.
- **`goto()` is the only public XY move.** It lifts to travel height and routes
  around keep-out bands. Do not add a second way to move XY.
- **Validate the whole operation before emitting anything.** `aspirate_uL`
  computes and checks displacement, aspirate POS *and* backoff POS before the
  first line, so a mid-primitive raise cannot leave a half-finished operation in
  the buffer with state claiming it completed.

# Defect classes this repo actually produces

Check your change against these; each is a real past bug with a test guarding it:

1. A derived value stored as well as computed, then drifting (calibration tables,
   `corners.solved`, `half_width`). Derive, or cross-check the stored copy.
2. A limit computed without every term - `max_volume_uL` omitted `pos_min` and the
   post-aspirate backoff, so `check_volume` accepted volumes `check_pos` rejected.
3. A check reading only an endpoint when the swept path is what matters -
   traverse clearance passed a move that crossed a rack and ended clear of it.
4. State committed before validation, or stale state surviving a raise -
   `_last_aspirate` once made a dispense record a stroke never commanded.
5. Non-idempotent reporting - `Simulator.finish()` appended to the same report on
   every call.
6. Config keys read by nothing, or behaviour keyed off prose (grepping a note for
   "UNVERIFIED"). Structure it.
7. Tolerance mismatches between layers - `_move_z` and `gcodepy.move_z_absolute`
   disagreeing recorded a step that emitted no line.

# Committing

Config and code go in separate commits, so a wrong slot position can be reverted
without losing library work. Subject line is `area: what changed` in lower case.
The body says what was wrong, what the evidence was, and what a reader would
otherwise wonder - quote the real numbers. Do not squash.

End every commit message with:

```
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```

# When you are unsure

Physical facts about the machine belong to the operator. If a change depends on a
measurement that is not in the config, do not guess a plausible number - implement
it so the value comes from config, leave the field `null`, add it to the relevant
`uncalibrated` list or `open_questions`, and say what needs measuring. A run that
reports what it does not know is worth more than one that assumes.
