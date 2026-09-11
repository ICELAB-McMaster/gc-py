# Bench observations

What actually happened when a generated run met the hardware. Separate from
[LAB.md](LAB.md) (how the library works) and [API.md](API.md) (what each
function does), because this is *data about runs*, and it accumulates.

One entry per run worth remembering. Record the measurement, not the
conclusion: a displacement in millimetres is what the machine did, a volume in
microlitres is what a calibration says that means, and the two are not the same
claim. Where a number is derived, say what from.

---

## 2026-09-11 — centrifuge_test with latex microbeads

First hardware run of `protocols/centrifuge_test.py`. The suspension handled
was **latex microbeads**, not the water the protocol's own notes describe.

**Observed:** at the end of the test, a plunger displacement of **roughly
8.5 mm** was collected from the latex microbeads.

**What that is in microlitres:** ≈ **157 µL**, from the p300's stored
calibration, `V = (8.5 - 0.122) / 0.0533`.

Treat that conversion as indicative rather than measured. The calibration was
fitted **gravimetrically against water** over 8-320 µL; a latex microbead
suspension differs in density and viscosity, so the curve is being applied
outside what it was fitted on. The displacement is the hard number here. If the
recovered volume matters, weigh it - the balance CSV exists for exactly this.

**Losses.** Less was recovered than was put in. The operator attributes the
shortfall to two mechanisms:

* liquid retained in the **filter**, and
* **evaporation**.

Neither is quantified, and nothing in the library models either. A run that
ends short is not evidence of a metering error until those two are accounted
for.

**Still open from this run:**

* Which vessel the 8.5 mm was drawn from - the record does not say, and the
  plates and the tube imply very different expected volumes.
* The expected figure to compare 157 µL against, so the shortfall can be
  attributed rather than asserted.
* A split between filter retention and evaporation. Running the same protocol
  with a capped, unfiltered control would separate them.

**Related, same week:** a 1.5 mL tube left overnight after a run had its liquid
reachable only at **Z 58.5**, against a configured draw depth of Z 54. That
bounds the tube's interior floor to **(58.5, 62.5]** - the tip reached 58.5
without contact, and Z 62.5 is the stage with a tip fitted. It also means a
residual left overnight is not a starting point for another run without topping
the tube back up. `p300.z_for_labware.tube_rack_1p5.bottom` is still `null`, so
the well-floor crash check for that tube only warns.
