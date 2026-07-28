from gcodepy.gcode import Gcode
import json

# ======================================================================
# 300 uL pipette -- accuracy / repeatability test generator
#
#   4 test volumes x 5 replicates = 20 tubes.
#   Rack layout:  ROW = volume,  COLUMN = replicate (5 cols starting at
#   X186,Y223).  ONE gcode file, run 4 TIMES on the SAME 20 positions.
#   Between runs: weigh wet -> swap fresh tubes -> fresh tip -> re-run.
#   -> 15 replicates per volume across 3 runs.  No RUN index; every
#   execution targets the same 20 tubes.
#
#   Displacements from the DENSITY-method calibration curve, refit over the
#   8-320 uL metering window at 23 C:
#       d = 0.05338 * V + 0.1218      (V in uL, d in mm)
#   evaluated at 10/50/75/100% of the 300 uL nominal.
#
#   Z CONVENTION: absolute, +Z is DOWN (Z=0 home, larger Z = lower).
# ======================================================================

#T 23*C
# rho 0.997538
# Z 1.0032 uL/mg 23C at 101.3 kPa
# Fractions 0.1, .5, .75, 1
#Fit window 8-320uL

name = "300ulPipetteAccuracy"

# ---------------- stage toggles ------------------------------------------
GET_TIP   = True        # pick up a fresh tip before starting
PREWET    = True        # condition the tip in water before dispensing
WALL_WIPE = True        # wipe residual droplet onto the tube side wall
TEMP_LOG  = True        # emit LOG_TEMPS calls into the gcode stream

# ---------------- actuator / plunger scheme (mm) --------------------------
BLOWOUT_POS      = 30.0                    # full dispense (bottomed) = blow-out
AIR_GAP          = 5.0                     # trailing air gap held above the liquid
GAP_POS          = BLOWOUT_POS - AIR_GAP   # = 25.0  plunger armed w/ air gap, tip in air
POST_ASP_BACKOFF = 1.0                     # extra retract in air after leaving water (anti-drip)
STROKE_MARGIN    = 1.0                     # keep this far off the POS=0 hard stop

# ---------------- 4 ISO test volumes -> displacements (density method) ----
#            10%     50%     75%     100%   of 300 uL nominal
VOLUMES       = [30.00, 150.00, 225.00, 300.00]   # uL, for reporting only
displacements = [1.722, 8.123, 12.123, 16.123]   # mm of fluid travel
REPLICATES    = 5                                  # tubes per volume per run
N_VOL = len(displacements)

# ---------------- prewet --------------------------------------------------
PREWET_CYCLES  = 3                                   # >= 3
PREWET_DISP    = 20.0                                # mm of WATER drawn per cycle
PREWET_ASP_POS = BLOWOUT_POS - PREWET_DISP           # = 10.0  (measured from BOTTOMED)

# ---------------- Z heights, absolute (+Z = DOWN) -------------------------
Z_HOME     = 0.0
Z_TRAVEL   = 5.0        # safe traverse height (tip hangs below once picked up)
Z_TIP_PICK = 57.5       # press nosecone into the tip in the tip rack
Z_WATER    = 40.0       # tip submerged in the reservoir
Z_TUBE     = 45.0       # <-- TUNE: dispense height inside the microcentrifuge tube
WIPE_DZ    = 4.0        # <-- TUNE: up-stroke dragging the droplet up the wall
WIPE_DX    = 3.2        # <-- TUNE: sideways to contact inner wall (< tube inner radius)

# ---------------- XY stations --------------------------------------------
TIP_X,   TIP_Y   = 218, 309     # tip rack pickup position
WATER_X, WATER_Y = 0,   175     # water reservoir
PARK_X,  PARK_Y  = 350, 0       # park at end

# ---------------- tube rack, machine coords (5 rows x 10 cols) ------------
X0, Y0     = 186, 223           # tube (row 0, col 0) -- first replicate of first volume
X_PITCH    = 12                 # column spacing (X decreases with column = replicate)
Y_PITCH    = 18                 # row spacing    (Y decreases with row = volume)
ROWS, COLS = 5, 10              # 50 positions; this run uses rows 0-3, cols 0-4
COL_OFFSET = 0                  # replicate block starts at column 0 (X186). Same every run.

# ---------------- feedrates ----------------------------------------------
TIP_PRESS_FEED = 600            # slow, controlled press onto / pull off the tip rack
WIPE_FEED      = 1500           # wall wipe

# ---------------- temperature logging ------------------------------------
SENSOR_ACTUATOR  = "temperature_sensor actuator"   # EBB42 TH0
SENSOR_CHAMBER   = "temperature_sensor chamber"    # Manta
TEMP_LOG_EVERY_N = 1                               # emit LOG_TEMPS every N tubes

# ---------------- optional run-order shuffle ------------------------------
# NOTE: one gcode file -> the same order replays in all 3 runs.
SHUFFLE_WITHIN_RUN = False
SHUFFLE_SEED       = 0

# ---------------- machine params from json --------------------------------
with open("examples/parameters.json", "r") as f:
    parameters = json.load(f)
for key, values in parameters.items():
    globals()[key] = values         # provides feedspeed, feedspeedhold, ...

# ---------------- sanity checks -------------------------------------------
assert N_VOL == len(VOLUMES), "VOLUMES and displacements must be the same length"
assert N_VOL <= ROWS, f"{N_VOL} volumes needs {N_VOL} rows; rack has {ROWS}"
assert REPLICATES + COL_OFFSET <= COLS, (
    f"needs cols {COL_OFFSET}..{COL_OFFSET + REPLICATES - 1}; rack has {COLS}")
_min_asp = GAP_POS - max(displacements)
assert _min_asp >= STROKE_MARGIN, (
    f"max displacement {max(displacements)} mm drives POS to {_min_asp}; "
    f"keep >= {STROKE_MARGIN} mm off the hard stop")
assert PREWET_ASP_POS >= STROKE_MARGIN, "prewet stroke exceeds available travel"
assert PREWET_DISP <= max(displacements) or PREWET_DISP <= 24.0, "prewet draw sanity"


# ======================================================================
# primitives
# ======================================================================

def actuator(G, pos, settle=0.0):
    """Command the plunger to an absolute POS (mm) and optionally settle."""
    G.file.write(f"ACTUATOR_MOVE POS={round(pos, 3)}\n")
    if settle:
        G.dwell(settle)


def log_temps(G, tag):
    """Ask Klipper to timestamp actuator + chamber temps into klippy.log."""
    if TEMP_LOG:
        G.file.write(f"LOG_TEMPS TAG={tag}\n")


def move_z(G, z_abs, feedrate):
    """Absolute Z via a relative delta (gcodepy tracks position for us)."""
    dz = round(z_abs - G.get_z(), 4)
    if abs(dz) > 1e-6:
        G.travel((0, 0, dz), feedrate=feedrate)


def move_xy(G, x, y, feedrate):
    """Traverse X first, then Y -- never diagonally."""
    if abs(x - G.get_x()) > 1e-6:
        G.travel_absolute((x, G.get_y(), G.get_z()), feedrate=feedrate)
    if abs(y - G.get_y()) > 1e-6:
        G.travel_absolute((G.get_x(), y, G.get_z()), feedrate=feedrate)


def goto_station(G, x, y, feedrate):
    """Lift to travel height, then traverse. The only safe way to move XY."""
    move_z(G, Z_TRAVEL, feedrate)
    move_xy(G, x, y, feedrate)


def tube_xy(vol_idx, rep_idx):
    """row = volume index, column = replicate index (+ block offset)."""
    row = vol_idx
    col = rep_idx + COL_OFFSET
    return X0 - col * X_PITCH, Y0 - row * Y_PITCH


# ======================================================================
# operations
# ======================================================================

def startup(G):
    """Home everything and reference the plunger at blow-out."""
    G.home("Z")
    G.home("XY")
    G.file.write("HOME_ACTUATOR\n")
    actuator(G, BLOWOUT_POS)        # bottomed: no vacuum while pressing on a tip
    move_z(G, Z_TRAVEL, feedspeed)
    G.wait_finish()


def get_tip(G):
    """Press the nosecone into a tip in the rack and lift it out."""
    goto_station(G, TIP_X, TIP_Y, feedspeed)
    G.wait_finish()
    move_z(G, Z_TIP_PICK, TIP_PRESS_FEED)   # slow press -- seats the ClipTip collar
    G.wait_finish()
    G.dwell(1)
    move_z(G, Z_TRAVEL, TIP_PRESS_FEED)     # slow lift -- pull the tip free of the rack
    G.wait_finish()


def prewet(G, cycles):
    """Wet the tip wall so the first real aspiration isn't wetting dry plastic."""
    goto_station(G, WATER_X, WATER_Y, feedspeed)
    G.wait_finish()
    actuator(G, BLOWOUT_POS, settle=0.5)        # start bottomed, in air
    move_z(G, Z_WATER, feedspeed)               # submerge
    G.wait_finish()
    for _ in range(cycles):
        actuator(G, PREWET_ASP_POS, settle=1)   # draw water up the wall
        actuator(G, BLOWOUT_POS,   settle=1)    # expel it back into the reservoir
    move_z(G, Z_TRAVEL, feedspeed)              # leave empty and bottomed
    G.wait_finish()


def aspirate(G, disp):
    """One independent aspiration of `disp` mm of fluid. Returns the POS used."""
    goto_station(G, WATER_X, WATER_Y, feedspeed)
    G.wait_finish()
    actuator(G, GAP_POS, settle=1)              # arm the air gap while still in air
    move_z(G, Z_WATER, feedspeed)               # descend into water
    G.wait_finish()
    asp = round(GAP_POS - disp, 3)
    actuator(G, asp, settle=1)                  # draw fluid, let meniscus settle
    move_z(G, Z_TRAVEL, feedspeed)              # withdraw
    actuator(G, asp - POST_ASP_BACKOFF)         # back off in air -- pulls the drop up
    G.wait_finish()
    return asp


def dispense(G, x, y):
    """Full dispense + blow-out into the tube, then wipe the droplet off."""
    goto_station(G, x, y, feedspeed)
    move_z(G, Z_TUBE, feedspeed)
    G.wait_finish()
    actuator(G, BLOWOUT_POS, settle=1)          # expels backoff air + fluid + air gap
    if WALL_WIPE:
        G.travel_absolute((x + WIPE_DX, G.get_y(), G.get_z()), feedrate=WIPE_FEED)  # to the wall
        move_z(G, Z_TUBE - WIPE_DZ, WIPE_FEED)                                      # drag up the wall
        G.travel_absolute((x, G.get_y(), G.get_z()), feedrate=WIPE_FEED)            # off the wall
    move_z(G, Z_TRAVEL, feedspeed)
    G.wait_finish()


def park(G):
    goto_station(G, PARK_X, PARK_Y, feedspeed)
    move_z(G, Z_HOME, feedspeed)
    G.wait_finish()


# ======================================================================
# generate
# ======================================================================

order = [(v, r) for v in range(N_VOL) for r in range(REPLICATES)]
if SHUFFLE_WITHIN_RUN:
    import random
    random.Random(SHUFFLE_SEED).shuffle(order)

g = Gcode(f"{name}.gcode")

startup(g)
log_temps(g, "start")

if GET_TIP:
    get_tip(g)
if PREWET:
    prewet(g, PREWET_CYCLES)
log_temps(g, "prewet_done")

for i, (vol_idx, rep_idx) in enumerate(order):
    disp = displacements[vol_idx]
    x, y = tube_xy(vol_idx, rep_idx)

    if i % TEMP_LOG_EVERY_N == 0:
        log_temps(g, f"v{VOLUMES[vol_idx]:g}_r{rep_idx + 1}")

    aspirate(g, disp)       # independent aspiration for THIS tube
    dispense(g, x, y)

log_temps(g, "end")
park(g)
g.close()

# ---------------- console summary ----------------------------------------
print(f"done! {name}.gcode  "
      f"({N_VOL} volumes x {REPLICATES} reps = {len(order)} tubes, cols "
      f"{COL_OFFSET}-{COL_OFFSET + REPLICATES - 1})")
print("  Run this SAME file 3x: fresh tubes in the same 20 holes, weigh dry")
print("  before / wet after. -> 15 replicates per volume.")
print(f"  tip={GET_TIP}  prewet={PREWET}({PREWET_CYCLES}x)  wipe={WALL_WIPE}  templog={TEMP_LOG}")
print(f"  blowout={BLOWOUT_POS}  air_gap={AIR_GAP}  gap_pos={GAP_POS}  "
      f"min_asp_POS={round(GAP_POS - max(displacements), 3)}")
print()
print(f"{'vol_uL':>7} {'disp_mm':>8} {'asp_POS':>8} {'row':>4} {'col':>4} {'X':>6} {'Y':>6}")
for vol_idx, rep_idx in order:
    x, y = tube_xy(vol_idx, rep_idx)
    asp = round(GAP_POS - displacements[vol_idx], 3)
    print(f"{VOLUMES[vol_idx]:>7.2f} {displacements[vol_idx]:>8.3f} {asp:>8.3f} "
          f"{vol_idx:>4} {rep_idx + COL_OFFSET:>4} {x:>6} {y:>6}")