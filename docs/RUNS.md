# Bench observations

What actually happened when a generated run met the hardware. Separate from
[LAB.md](LAB.md) (how the library works) and [API.md](API.md) (what each
function does), because this is *data about runs*, and it accumulates.

One entry per run worth remembering. Record the measurement, not the
conclusion: a displacement in millimetres is what the machine did, a volume in
microlitres is what a calibration says that means, and the two are not the same
claim. Where a number is derived, say what from.

---

## 2026-10-06 - gripper bring-up at station 9

First hardware run of `protocols/gripper_station9_test.py`. Two failures,
neither in the motion.

**Attempt 1: every GRIPPER_* came back `Unknown command`.**
`gripper_macros.cfg` had never been included from `printer.cfg`. The
significant part is what Klipper did next: it **carried on**. The gantry ran
the whole sequence - traverse, descend to Z 107, lift, hold, lower, retreat -
with the gripper inert, and the log reads like a completed run.

Fixed by installing it (`[include gripper_macros.cfg]`, with
`shell_command.cfg` for the `gcode_shell_command` extra). Guarded by emitting
`GRIPPER_PING` and `GRIPPER_STATUS` **before `G28`**, so the failure now
arrives with the machine parked. Nothing in a gcode file can force Klipper to
abort on an unknown command; this only moves when it is noticed.

**Attempt 2: the gripper worked, the IMU wrapper did not.**

    pong from node 0 in 7.0 ms
    n/a deg   13 mA  pulse 0 us  idle  [homed,powered]
    home: done -> 0.0 deg, mean 133 mA, peak/extra 1234
    open: done -> 0.0 deg, mean  92 mA, peak/extra  157

Then `FileNotFoundError: /home/admin/imu/..._01.csv.marks.csv`.
`/home/admin/imu` did not exist. Three faults in `host/imu_session.py`, all
mine:

* nothing created the parent directory - and `gripctl.py imu --csv` does not
  either, so the forked capture had already died for the same reason,
* the fork happened **before** the write that failed, leaving an orphaned
  child and a state file naming a dead pid,
* `_append_mark` assumed the file existed, so every later mark and the stop
  raised their own traceback on top of the first.

Fixed: the directory and the marks header are created **before** the fork, a
`--settle` check confirms the capture survived its first moment, the child's
output is kept at `<csv>.log`, and a mark never raises - it records the
timestamp and warns if the capture has gone.

**Note `status` reports `n/a deg`.** That is the expected reading, not a
fault: the feedback output has failed, so there is no position to report.

**Still open:** nothing has yet confirmed the IMU records across a pick,
because attempt 2 never got a live capture. Run the single-cycle file and
check `<csv>.log` is empty and the CSV has rows before trying 15.

---

## 2026-10-06 (later) - the board cannot command and stream at once

Third attempt. `imu_session 2026-10-06.2` confirmed in the log, the directory
fix landed, `GRIPPER_IMU_START` and the first mark both succeeded - and then
`GRIPPER_GRIP` never returned. No `Command {gripper} finished`, where every
earlier gripper command had one.

The tell was the ordering: `PING`, `STATUS`, `HOME` and `OPEN` all succeeded,
and every one of them ran **before** the capture started. The first command
after it hung.

`board/gripnode.py` explains it. `imu_read()` streams in a blocking loop, and
`drain()` documents the consequence: *"a motion in progress polls that flag
and the queue is not read until the motion returns"*. ESTOP, ABORT and STOP
set the flag immediately; everything else is queued, up to 8 deep.

So the grip was never refused - it was **queued**, and would have fired when
the capture finished. The 30 s `gripctl` timeout is what stopped that from
happening, which was luck rather than design: a queued grip firing minutes
late, with the gantry elsewhere, is worse than a timeout.

The sequence was wrong, not the code. A capture can only span **gantry**
motion. The protocol now grips first, captures the lift/hold/lower, stops,
then releases - and the two transients it can no longer see are taken from
the board's own telemetry via `--log --csv`, which writes current against
time for the grip and the release.

`Machine._gripper_macro` refuses a gripper command while a session is open,
so this cannot be written again.

---

## 2026-09-11 - centrifuge_test with latex microbeads

First hardware run of `protocols/centrifuge_test.py`. The suspension handled
was **latex microbeads**, not the water the protocol's own notes describe.

**Observed at the end of the test:**

| | |
|---|---|
| recovered, plates and tube pooled | **~8.5 mm** of plunger displacement |
| left in each plate | **~70 uL** |
| evaporation | **~80 uL over ~16 hours** |
| exposed on the stage | the water reservoir and both plates |

**8.5 mm in microlitres:** ~ **157 uL**, from `V = (8.5 - 0.122) / 0.0533`.
Indicative, not measured: the p300 curve was fitted gravimetrically against
**water** over 8-320 uL, and a latex microbead suspension differs in density
and viscosity. The displacement is the hard number. Weigh it if the volume
matters - the balance CSV exists for this.

### The balance, and what it says

The run puts 2 x 150 = **300 uL into each plate** and leaves **50 uL** in the
tube (350 loaded, 300 drawn). So 650 uL is on the deck when it finishes, and
157 uL came back:

    plates   2 x ~70  =  140 uL
    tube               ~ 17 uL
    -------------------------
    pooled             ~157 uL  = 8.5 mm

**The missing ~490 uL is very likely still in the plates, not lost.** A draw
from `sbs_96_wellplate` uses its `work` depth, **Z 53.5**, because that plate
declares no `aspirate` key. The plate floor is **Z 60.8**. So the orifice stops
**7.3 mm above the bottom of the well**, and everything under it is
unreachable:

| well ID | uL per mm of depth | stranded below Z 53.5 | reachable of 300 uL |
|---|---|---|---|
| 6.2 mm | 30.2 | 220 uL | 80 uL |
| **6.4 mm** | **32.2** | **235 uL** | **65 uL** |
| 6.6 mm | 34.2 | 250 uL | 50 uL |

At a nominal 6.4 mm bore that predicts **65 uL recoverable per plate** against
the **~70 uL** measured. The arithmetic closes on dead volume without needing
the filter to have absorbed anything.

**So the filter suspicion is probably wrong, and worth dropping unless it is
measured.** A 300 uL tip's filter plug is a few millimetres of porous
material; it cannot hold hundreds of microlitres. Evaporation is likewise
accounted for and small - ~80 uL over 16 hours, shared across the reservoir
and both plates, is nowhere near the shortfall.

The well bore above is a **nominal SBS figure, not a measurement of this
plate**: `sbs_96_wellplate` carries no internal geometry, so there is no
liquid-height model anywhere in the config. The agreement is strong enough to
act on and not strong enough to quote.

### What to do about it

Recovering a plate needs its own draw depth, the same way the 1.5 mL tube got
one. `p300.z_for_labware.sbs_96_wellplate` has `work` 53.5 and `bottom` 60.8
and nothing between them. Measure the depth at which a tip can draw a 96-well
plate down without touching the floor and add it as `aspirate` - a value in the
high 50s would leave a millimetre or two of clearance. Until then, any
plate recovery is capped at roughly a fifth of what was dispensed.

### Still open

* A split between filter retention and evaporation is no longer the question -
  dead volume dominates both. If the filter is still suspected, weigh a dry
  tip against a used one.
* The ~70 uL per plate is read as *what came out*, which is what makes the
  pooled 157 uL close. If it meant ~70 uL still sitting in each plate
  **after** recovery, the balance does not close and something else is going
  on - worth confirming which was meant.
* `p300.z_for_labware.tube_rack_1p5.bottom` and an `aspirate` depth for the
  plate are both still unmeasured.

**Related, same week:** a 1.5 mL tube left overnight after a run had its liquid
reachable only at **Z 58.5**, against a configured draw depth of Z 54. That
bounds the tube's interior floor to **(58.5, 62.5]** - the tip reached 58.5
without contact, and Z 62.5 is the stage with a tip fitted. It also means a
residual left overnight is not a starting point for another run without topping
the tube back up.
