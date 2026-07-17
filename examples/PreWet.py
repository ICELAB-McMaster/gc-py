from gcodepy.gcode import Gcode
import json

# ----------------------------------------------------------------------
# Pipette calibration-curve generator  --  ONE REPLICATE PER RUN
#   15 target volumes, dispensed once each (one full curve).
#   Set RUN = 1, run it; then RUN = 2, run it; then RUN = 3, run it.
#   Each run targets a fresh block of 15 tubes so the 3 runs tile 45 of
#   the 50 rack positions without overlap.
#   Controlled variable = plunger displacement (mm); volume is nominal
#   until this calibration produces the real uL/mm slope.
# ----------------------------------------------------------------------

name = "prewet"

# ---- plunger / actuator scheme (mm) --------------------------------------
BLOWOUT_POS = 31.0                     # full dispense (all the way down) = blow-out
AIR_GAP     = 6.0                      # trailing air gap  (was 6; 4 leaves 2 mm stroke margin at max vol)
GAP_POS     = BLOWOUT_POS - AIR_GAP    # = 27.0 : plunger holds the 4 mm gap, tip in air, no fluid

WATER_X, WATER_Y = 0, 175              # water reservoir
PARK_X,  PARK_Y  = 350, 0              # park at end

# ---- machine params from json --------------------------------------------
with open("examples/parameters.json", "r") as f:
    parameters = json.load(f)
for key, values in parameters.items():
    globals()[key] = values            # provides feedspeed, feedspeedhold, ...

# ---- generate -------------------------------------------------------------

g = Gcode(f"{name}.gcode")
g.home("Z")
g.home("XY")
g.file.write("HOME_ACTUATOR\n")
g.file.write(f"ACTUATOR_MOVE POS={BLOWOUT_POS}\n")           # prime: reference plunger at blow-out
                                                            # so the first gap re-arm is a true 4 mm draw

g.travel_absolute((WATER_X, g.get_y(), g.get_z()), feedrate=feedspeed)
g.travel_absolute((g.get_x(), WATER_Y, g.get_z()), feedrate=feedspeed)
g.wait_finish()

for i in range(3):
    g.file.write(f"ACTUATOR_MOVE POS={GAP_POS}\n")          # re-arm 4 mm air gap (tip in air)
    g.dwell(1)
    g.travel((0, 0, 45), feedrate=feedspeed)                # descend into water
    g.wait_finish()
    aspirate_pos = round(GAP_POS - 20, 3)                 # retract to draw fluid
    g.file.write(f"ACTUATOR_MOVE POS={aspirate_pos}\n")
    g.dwell(1)                                              # let meniscus settle
    g.travel((0, 0, -45), feedrate=feedspeed)               # withdraw
    g.file.write(f"ACTUATOR_MOVE POS={aspirate_pos-1}\n")
    g.wait_finish()
    g.file.write(f"ACTUATOR_MOVE POS={BLOWOUT_POS}\n")

g.close()
print("done")