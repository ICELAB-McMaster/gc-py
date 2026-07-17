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

RUN  = 1                               # <-- change to 2, then 3, on each rerun
name = "200ulPipetteCurve15"

# ---- plunger / actuator scheme (mm) --------------------------------------
BLOWOUT_POS = 31.0                     # full dispense (all the way down) = blow-out
AIR_GAP     = 6.0                      # trailing air gap  (was 6; 4 leaves 2 mm stroke margin at max vol)
GAP_POS     = BLOWOUT_POS - AIR_GAP    # = 27.0 : plunger holds the 4 mm gap, tip in air, no fluid

# ---- 15 displacements, dense at the low end (mm of fluid travel) ----------
displacements = [0.4, 0.7, 1.0, 1.4, 1.8, 2.4, 3.0, 4.0,
                 5.0, 7.0, 9.0, 12.0, 16.0, 20.0, 24.0]
N_VOL    = len(displacements)          # 15 -> one replicate = one run

# ---- rack geometry, MACHINE COORDS  (5 rows x 10 cols, your x/y lists) -----
X0, Y0   = 186, 223                     # tube (row 0, col 0)
X_PITCH  = 12                          # column spacing
Y_PITCH  = 18                          # row spacing (advancing in -Y)
ROWS, COLS = 5, 10                     # 50 positions total

WATER_X, WATER_Y = 0, 175              # water reservoir
PARK_X,  PARK_Y  = 350, 0              # park at end

# ---- wall wipe: transfer the residual droplet onto the tube side wall -----
WALL_WIPE = True                       # set False to restore the plain straight retract
WIPE_DX   = 3.2                        # mm sideways to contact inner wall (< tube inner radius; tune!)
WIPE_DZ   = 4.0                        # mm up-stroke dragging the droplet up the wall (< 15)

# ---- optional: shuffle the within-run volume order (decorrelates any short
#      drift over the ~15-dispense run from volume). Off = deterministic. -----
SHUFFLE_WITHIN_RUN = False
SHUFFLE_SEED = 0

# ---- machine params from json --------------------------------------------
with open("examples/parameters.json", "r") as f:
    parameters = json.load(f)
for key, values in parameters.items():
    globals()[key] = values            # provides feedspeed, feedspeedhold, ...

# ---- build this run's point order ----------------------------------------
order = list(range(N_VOL))
if SHUFFLE_WITHIN_RUN:
    import random
    random.Random(SHUFFLE_SEED + RUN).shuffle(order)   # per-run seed -> reproducible

# global tube index -> (row, col); run k occupies tubes [(k-1)*15 .. k*15-1]
assert RUN * N_VOL <= ROWS * COLS, "RUN too high: would exceed rack capacity"


def tube_xy(point_number):
    """point_number = 0..14 within this run -> machine (x, y)."""
    idx = (RUN - 1) * N_VOL + point_number
    row, col = idx // COLS, idx % COLS
    return X0 - col * X_PITCH, Y0 - row * Y_PITCH


def getWater(G, disp, feedspeed):
    """One independent aspiration: arm the 6 mm air gap in air, descend into
    water, draw `disp` mm of fluid, then withdraw."""
    G.travel_absolute((WATER_X, G.get_y(), G.get_z()), feedrate=feedspeed)
    G.travel_absolute((G.get_x(), WATER_Y, G.get_z()), feedrate=feedspeed)
    G.wait_finish()
    G.file.write(f"ACTUATOR_MOVE POS={GAP_POS}\n")          # re-arm 4 mm air gap (tip in air)
    G.dwell(1)
    G.travel((0, 0, 45), feedrate=feedspeed)                # descend into water
    G.wait_finish()
    aspirate_pos = round(GAP_POS - disp, 3)                 # retract to draw fluid
    G.file.write(f"ACTUATOR_MOVE POS={aspirate_pos}\n")
    G.dwell(1)                                              # let meniscus settle
    G.travel((0, 0, -45), feedrate=feedspeed)               # withdraw
    G.file.write(f"ACTUATOR_MOVE POS={aspirate_pos-1}\n")
    G.wait_finish()


# ---- generate -------------------------------------------------------------
outfile = f"{name}_run{RUN}"
g = Gcode(f"{outfile}.gcode")
g.home("Z")
g.home("XY")
g.file.write("HOME_ACTUATOR\n")
g.file.write(f"ACTUATOR_MOVE POS={BLOWOUT_POS}\n")           # prime: reference plunger at blow-out
                                                            # so the first gap re-arm is a true 4 mm draw
g.travel((0, 0, 5), feedrate=feedspeed)

for point_number in order:
    disp = displacements[point_number]
    x, y = tube_xy(point_number)

    getWater(g, disp, feedspeed=feedspeed)                  # independent aspiration for THIS point

    g.travel_absolute((g.get_x(), y, g.get_z()), feedrate=feedspeed)
    g.travel_absolute((x, g.get_y(), g.get_z()), feedrate=feedspeed)
    g.travel((0, 0, 15), feedrate=feedspeed)            # descend to tube
    g.wait_finish()
    g.file.write(f"ACTUATOR_MOVE POS={BLOWOUT_POS}\n")       # full dispense + blow-out
    g.dwell(1)

    if WALL_WIPE:                                       # wipe residual droplet onto the tube side wall
        g.travel((WIPE_DX, 0, 0), feedrate=1500)   # move tip across to contact the inner wall
        g.travel((0, 0, -WIPE_DZ), feedrate=1500)  # drag the droplet up the wall (-Z = up)
        g.travel((-WIPE_DX, 0, 0), feedrate=1500)  # pull the tip back off the wall
        g.travel((0, 0, -(15 - WIPE_DZ)), feedrate=1500)  # finish retract to travel height
    else:
        g.travel((0, 0, -15), feedrate=feedspeed)       # retract

    g.wait_finish()

g.travel_absolute((PARK_X, PARK_Y, 0), feedrate=feedspeed)
g.close()

# ---- console summary ------------------------------------------------------
print(f"done! RUN {RUN} -> {outfile}.gcode  ({N_VOL} tubes)")
print(f"{'vol_uL':>7} {'disp_mm':>7} {'asp_POS':>7} {'X':>5} {'Y':>5}")
for point_number in order:
    x, y = tube_xy(point_number)
    asp = round(GAP_POS - displacements[point_number], 3)
    print(f"{displacements[point_number]:>7g} {asp:>7g} {x:>5} {y:>5}")