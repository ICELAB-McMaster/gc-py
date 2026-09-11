# Bench observations

What actually happened when a generated run met the hardware. Separate from
[LAB.md](LAB.md) (how the library works) and [API.md](API.md) (what each
function does), because this is *data about runs*, and it accumulates.

One entry per run worth remembering. Record the measurement, not the
conclusion: a displacement in millimetres is what the machine did, a volume in
microlitres is what a calibration says that means, and the two are not the same
claim. Where a number is derived, say what from.

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
