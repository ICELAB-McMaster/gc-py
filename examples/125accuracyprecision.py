from gcodepy.gcode import Gcode
import json
import random

# ----------------------------------------------------------------------
# Pipette accuracy / repeatability test  --  ISO 8655-style
#
#   4 test volumes x 5 replicates = 20 tubes.
#   Rack layout:  ROW = volume,  COLUMN = replicate.  Always cols 0-4.
#       row 0 : 12.50 uL  (10%  of nominal)  x 5 tubes
#       row 1 : 62.50 uL  (50%  of nominal)  x 5 tubes
#       row 2 : 93.75 uL  (75%  of nominal)  x 5 tubes
#       row 3 : 125.00 uL (100% of nominal)  x 5 tubes
#
#   ONE gcode file, run 3 TIMES on the SAME 20 rack positions.
#   Between runs:  weigh wet -> swap in fresh tubes -> fit a FRESH TIP -> re-run.
#   Result: 15 replicates per volume, spread over 3 tips. That satisfies
#   ISO 8655-6's minimum of 10 measurements per volume and >=1 tip change
#   per volume. No need to regenerate the gcode between runs.
#
#   Displacements come from the calibration curve refit over 10-140 uL:
#       d = 0.12698 * V + 0.0837      (V in uL, d in mm)
#   Controlled variable is plunger displacement; the gravimetric result is
#   what tells you the systematic (accuracy) and random (repeatability) error.
# ----------------------------------------------------------------------

name = "125ulPipetteAccuracy"

# ---- test points ---------------------------------------------------------
#            10%     50%     75%     100%   of 125 uL nominal
VOLUMES       = [12.50,  62.50,  93.75, 125.00]   # uL, for reporting only
displacements = [1.671,  8.020, 11.988, 15.956]   # mm of fluid travel
REPLICATES    = 5                                  # tubes per volume per run

N_VOL = len(displacements)

# ---- plunger / actuator scheme (mm) --------------------------------------
BLOWOUT_POS = 31.0                     # full dispense (all the way down) = blow-out
AIR_GAP     = 6.0                      # leading air cushion drawn in air
GAP_POS     = BLOWOUT_POS - AIR_GAP    # = 25.0 : plunger holds the air gap, tip in air, no fluid
TRAIL_GAP   = 1.0                      # mm of air drawn AFTER leaving the water (anti-drip)

# ---- rack geometry, MACHINE COORDS  (5 rows x 10 cols) --------------------
X0, Y0   = 186, 223                    # tube (row 0, col 0)
X_PITCH  = 12                          # column spacing
Y_PITCH  = 18                          # row spacing (advancing in -Y)
ROWS, COLS = 5, 10                     # 50 positions total

COL_OFFSET = 0                         # which column the replicate block starts at.
                                       # Same every run by design - fresh tubes, same holes.

WATER_X, WATER_Y = 0, 175              # water reservoir
PARK_X,  PARK_Y  = 350, 0              # park at end

# ---- wall wipe: transfer the residual droplet onto the tube side wall -----
WALL_WIPE = True                       # set False to restore the plain straight retract
WIPE_DX   = 3.2                        # mm sideways to contact inner wall (< tube inner radius; tune!)
WIPE_DZ   = 4.0                        # mm up-stroke dragging the droplet up the wall (< 15)

# ---- optional: shuffle the ORDER of the 20 dispenses.
#      Tube assignment (row=volume, col=replicate) is unchanged; only the
#      visiting sequence changes, which decorrelates any drift over the run
#      from volume. NOTE: one gcode file -> the same order is replayed in all
#      3 runs. Off = blocked, volume by volume.
SHUFFLE_WITHIN_RUN = False
SHUFFLE_SEED = 0

# ---- sanity checks --------------------------------------------------------
assert N_VOL == len(VOLUMES), "VOLUMES and displacements must be the same length"
assert N_VOL <= ROWS, f"{N_VOL} volumes needs {N_VOL} rows; rack has {ROWS}"
assert REPLICATES + COL_OFFSET <= COLS, (
    f"needs cols {COL_OFFSET}..{COL_OFFSET + REPLICATES - 1}; rack has {COLS}")
_min_pos = GAP_POS - max(displacements) - TRAIL_GAP
assert _min_pos >= 0, f"plunger would need POS={_min_pos:.3f} (< 0) at max volume"

# ---- machine params from json --------------------------------------------
with open("examples/parameters.json", "r") as f:
    parameters = json.load(f)
for key, values in parameters.items():
    globals()[key] = values            # provides feedspeed, feedspeedhold, ...


def tube_xy(vol_idx, rep_idx):
    """row = volume index, column = replicate index (+ block offset)."""
    row = vol_idx
    col = rep_idx + COL_OFFSET
    return X0 - col * X_PITCH, Y0 - row * Y_PITCH


def getWater(G, disp, feedspeed):
    """One independent aspiration: arm the air gap in air, descend into
    water, draw `disp` mm of fluid, withdraw, then draw a trailing air gap."""
    G.travel_absolute((WATER_X, G.get_y(), G.get_z()), feedrate=feedspeed)
    G.travel_absolute((G.get_x(), WATER_Y, G.get_z()), feedrate=feedspeed)
    G.wait_finish()
    G.file.write(f"ACTUATOR_MOVE POS={GAP_POS}\n")          # re-arm air gap (tip in air)
    G.dwell(1)
    G.travel((0, 0, 45), feedrate=feedspeed)                # descend into water
    G.wait_finish()
    aspirate_pos = round(GAP_POS - disp, 3)                 # retract to draw fluid
    G.file.write(f"ACTUATOR_MOVE POS={aspirate_pos}\n")
    G.dwell(1)                                              # let meniscus settle
    G.travel((0, 0, -45), feedrate=feedspeed)               # withdraw
    G.file.write(f"ACTUATOR_MOVE POS={round(aspirate_pos - TRAIL_GAP, 3)}\n")
    G.wait_finish()


# ---- build the dispense order --------------------------------------------
order = [(v, r) for v in range(N_VOL) for r in range(REPLICATES)]
if SHUFFLE_WITHIN_RUN:
    random.Random(SHUFFLE_SEED).shuffle(order)

# ---- generate -------------------------------------------------------------
outfile = name
g = Gcode(f"{outfile}.gcode")
g.home("Z")
g.home("XY")
g.file.write("HOME_ACTUATOR\n")
g.file.write(f"ACTUATOR_MOVE POS={BLOWOUT_POS}\n")           # prime: reference plunger at blow-out
g.travel((0, 0, 5), feedrate=feedspeed)

for vol_idx, rep_idx in order:
    disp = displacements[vol_idx]
    x, y = tube_xy(vol_idx, rep_idx)

    getWater(g, disp, feedspeed=feedspeed)                   # independent aspiration for THIS tube

    g.travel_absolute((g.get_x(), y, g.get_z()), feedrate=feedspeed)
    g.travel_absolute((x, g.get_y(), g.get_z()), feedrate=feedspeed)
    g.travel((0, 0, 15), feedrate=feedspeed)                 # descend to tube
    g.wait_finish()
    g.file.write(f"ACTUATOR_MOVE POS={BLOWOUT_POS}\n")        # full dispense + blow-out
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
print(f"done! -> {outfile}.gcode  "
      f"({N_VOL} volumes x {REPLICATES} reps = {len(order)} tubes, "
      f"cols {COL_OFFSET}-{COL_OFFSET + REPLICATES - 1})")
print("  Run this SAME file 3x. Each time: fresh tubes in the same 20 holes,")
print("  fresh tip, weigh dry before / wet after. -> 15 reps per volume, 3 tips.")
print()
print(f"{'vol_uL':>7} {'disp_mm':>8} {'asp_POS':>8} {'row':>4} {'col':>4} {'X':>5} {'Y':>5}")
for vol_idx, rep_idx in order:
    x, y = tube_xy(vol_idx, rep_idx)
    asp = round(GAP_POS - displacements[vol_idx], 3)
    print(f"{VOLUMES[vol_idx]:>7.2f} {displacements[vol_idx]:>8.3f} {asp:>8.3f} "
          f"{vol_idx:>4} {rep_idx + COL_OFFSET:>4} {x:>5} {y:>5}")