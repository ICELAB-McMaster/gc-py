# `lab` API reference

Every public function in the library: what it does, what it raises, and - the
part worth reading - what mistake it exists to prevent.

Regenerated from the source on 2026-09-08. If the code and this file disagree,
the code is right and this file is stale; `tests/test_lab.py` and
`tests/test_review_fixes.py` are the executable version of the same claims.

## Read this first

Three conventions run through everything, and all three read backwards at least
once.

**+Z is DOWN.** `Z = 0` is home, at the top of travel. A *larger* commanded Z is
physically *lower*. So "the effector must stay above Z 60" is written `z < 60`,
and the ejector's `Z145 -> Z137` is an 8 mm *upward* stroke. Every Z comparison
in the library is written in this frame; if one ever reads the natural way
round, it is a bug.

**Volumes are microlitres, never millimetres.** A protocol asks for
`aspirate_uL(150.0)`. The conversion to plunger travel happens in
`Pipette.displacement_mm` and nowhere else, from coefficients stored in
`config/effectors/*.json`. This is not style: the scripts this library replaced
had hardcoded millimetre tables that had drifted away from the equations printed
directly above them.

**A check that cannot be evaluated is not a pass.** Where a measurement is
missing, the simulator records an explicit `cannot check ...` warning and puts
it in the run manifest. Silence always means checked. One refinement worth
knowing: a null *commanded depth* stops the run, because descending somewhere
nobody measured is a crash; a null *crash limit* only warns, because not knowing
where a floor is means the check cannot be evaluated.

## The shape of it

```
config/                     data, no logic
  deck.json                 envelope, slots, stations, keep-outs, Z references
  labware.json              labware geometry and height - slot-independent
  layout.json               what is loaded where, right now
  layout_test.json          fixture for the test suite: everything at once
  effectors/<id>.json       one file per end effector

lab/
  config.py                 finds and hashes config; LAB_CONFIG overrides
  labware.py                well (row, col) -> machine (x, y)
  deck.py                   joins the three configs; stage, slots, keep-outs
  effector.py               Pipette / Gripper; uL -> mm lives here
  machine.py                the object a protocol drives
  simulate.py               every safety check, run as the gcode is generated
  logbook.py                run manifest + balance CSV

protocols/
  accuracy_test.py          ISO 8655 accuracy/repeatability, any pipette
  centrifuge_test.py        two plates, one source each, to equal mass
```

Data flows one way: `config` -> `labware`/`effector` -> `deck` -> `machine`,
with `simulate` observing every step `machine` produces and `logbook` recording
the result. Nothing below `machine` knows gcode exists.

## The shortest useful protocol

```python
from lab.machine import Machine

m = Machine(effector="p300", name="my_protocol")
m.startup()                              # home, prime the plunger, lift
m.get_tip()                              # next unused tip, edge-first
m.prewet(3)                              # only if the effector config enables it

m.aspirate_uL(150.0)                     # from the water station
m.dispense("plate1", row=3, col=5)       # a placement name from layout.json

m.aspirate_from("big_tubes", 0, 0, 100.0)   # or from a WELL
m.mix("big_tubes", 0, 0, cycles=5, volume_uL=200.0)

m.eject_tip()                            # forces a G28, which returns home
m.park()
result = m.finalize(outdir="out")        # simulates, THEN writes
```

`finalize()` runs the whole-run checks and refuses to write the `.gcode` if any
hard check fails, so a failed run leaves nothing behind to execute by accident.

## The per-labware depths

Every effector records up to four commanded Z values per labware, shallowest
first. They are separate numbers on purpose:

| key | meaning | p300 + 300 uL tip |
|---|---|---|
| `work` | the dispense depth, and the draw depth when `aspirate` is absent | plate 53.5, 1.5 mL tube 35.0, water 40.0 |
| `aspirate` | where a draw descends to. Optional; outranks `work` for `aspirate_uL` / `aspirate_from` | 1.5 mL tube 54.0 |
| `mix` | where `Machine.mix` cycles the plunger | 1.5 mL tube 54.0 |
| `bottom` | the well floor. A LIMIT, never a target | plate 60.8 |

A dispense wants to be *above* the liquid; a draw wants the orifice *under* it,
and the further the level falls the deeper that has to be. In the 1.5 mL tube
those are 19 mm apart, which is why one `work` could not serve both. The key is
opt-in: labware with a single sensible depth, like the water reservoir, declares
no `aspirate` and a draw uses `work`.

**A declared-but-null `aspirate` refuses the run.** Absent means "this labware
never needed the distinction"; present-and-null means "it does, and nobody has
measured it" - and falling back to `work` there would silently draw 19 mm
shallower than intended. `_aspirate_at` selects the key on *presence*, then
`Effector.z_for` raises on the null.

Aiming a mix at `bottom` would drive the tip into the floor, which is why it is
not the mix key.

## Errors

Every exception the library raises subclasses `lab.effector.LabError`, so a
protocol catches one class:

| class | raised when |
|---|---|
| `LabError` | base; catch this |
| `EffectorError` | the effector cannot do what was asked, or a depth is null |
| `VolumeError` | volume outside the calibration window or the available stroke |
| `DeckError` | the config cannot describe the deck, or a lookup missed |
| `ProtocolError` | the protocol asked for something illegal in sequence |
| `SimulationError` | a hard safety check failed; the gcode is not written |

---

## `lab/config.py`

Config discovery, loading and hashing. Every path resolves against the location of `config.py` itself (`_REPO_ROOT = <repo>/`, so `_DEFAULT_CONFIG_DIR = <repo>/config`), never against the working directory, so `python protocols/accuracy_test.py` behaves identically from the repo root, from `protocols/`, or from a scheduler. The whole module is data access: no geometry, no defaults, no repair of bad config. The one piece of policy it enforces is that every load records the SHA-256 of the exact bytes read, so a generated `.gcode` can be traced back to the config that produced it.

Layer position: this is the bottom. `lab/deck.py`, `lab/effector.py` and the tests all obtain their JSON through here; nothing here imports anything else from `lab/`.

### `config_dir()`

```python
def config_dir() -> Path
```

Returns the directory holding `deck.json`, `labware.json`, `layout.json` and `effectors/`.

- No parameters.
- **Returns:** an absolute `Path`. `$LAB_CONFIG` if that environment variable is set (expanded with `~` and resolved), otherwise `<repo>/config`.
- **Raises:** `FileNotFoundError` if `LAB_CONFIG` is set but does not name an existing directory. The message quotes the resolved path.

**Why it exists:** it is the single seam that lets a test point the whole library at a temp directory. Both test files use exactly this pattern - copy `config/*.json` plus `config/effectors/` to a temp dir, mutate one field, set `os.environ["LAB_CONFIG"]`, run, restore the old value in a `finally` (`tests/test_review_fixes.py` `_config_without`, `tests/test_lab.py` `_p300_with_z` / `_WithConfig`). Failing loudly on a bad `LAB_CONFIG` rather than silently falling back to `<repo>/config` is deliberate: a silent fallback would make a test that thinks it removed a measurement actually assert against the real bench.

**Note:** the env var is read on every call, not cached. `Deck.load()` therefore picks up a mid-process change, which is what makes `_WithConfig` work as a context manager - but it also means a long-lived object holding a `Path` from an earlier call can disagree with a later one.

### `repo_root()`

```python
def repo_root() -> Path
```

Returns the repository root, `Path(__file__).resolve().parent.parent`.

- No parameters. Returns an absolute `Path`. Raises nothing.

**Why it exists:** `Machine.git_commit()` and `Machine.git_dirty()` run `git rev-parse HEAD` / `git status` with `cwd=str(config.repo_root())` so the manifest records the commit of *this* library regardless of where the protocol was invoked from. Unlike `config_dir()`, this is not overridable - it is the code's location, not the data's.

### `config_path(*parts)`

```python
def config_path(*parts: str) -> Path
```

Joins path segments onto `config_dir()`.

- `*parts`: path segments, e.g. `config_path("effectors", "p300.json")`.
- **Returns:** a `Path`. Does not check existence.
- **Raises:** whatever `config_dir()` raises.

**Why it exists:** so callers never build config paths by string concatenation and never bake in `"config/"`. Used directly by the tests to enumerate the effector directory (`cfgmod.config_path("effectors").glob("*.json")`).

### `sha256_of(path)`

```python
def sha256_of(path: Path) -> str
```

Hex SHA-256 of a file's bytes.

- `path`: file to hash.
- **Returns:** 64-character lower-case hex digest.
- **Raises:** `OSError` / `FileNotFoundError` from `read_bytes` if the file is missing or unreadable.

**Why it exists:** provenance for a file that is not loaded as a `ConfigFile` (a calibration CSV, a reference `.gcode`).

**Note:** nothing in `lab/`, `protocols/`, `tools/` or `tests/` currently calls it - `ConfigFile` hashes inline in its constructor rather than delegating here. It is a public helper with no in-tree caller; do not assume it is exercised by the test suite.

### `class ConfigFile`

A loaded JSON config plus the SHA-256 of the bytes it came from. `__slots__ = ("path", "data", "sha256")`, so it holds no other state and accepts no extra attributes.

Attributes:

- `path` (`Path`): the file it was read from.
- `data` (`dict`): the parsed JSON.
- `sha256` (`str`): hex digest of the raw bytes, computed **before** decoding, so it is the hash of what is on disk byte for byte.

**Why it exists:** the hash and the data must not be able to drift apart. Reading the bytes once and deriving both from that single `read_bytes()` means the manifest's `provenance.config_sha256` cannot name a version of the file that was not the one used. `Machine.config_hashes()` assembles exactly four entries from these - `deck.json`, `labware.json`, the layout file under its own name, and `effectors/<id>.json` - and `tests/test_lab.py` asserts `len(man["provenance"]["config_sha256"]) == 4`.

#### `ConfigFile.__init__(path)`

```python
def __init__(self, path: Path) -> None
```

- `path`: the JSON file to load. No units.
- **Raises:** `FileNotFoundError` if `path` is not an existing file; the message also prints the current `config_dir()` and suggests setting `LAB_CONFIG`, because the usual cause is pointing at the wrong config tree rather than a genuinely missing file. `ValueError` (chained from `json.JSONDecodeError`) if the bytes are not valid JSON, naming the file. `UnicodeDecodeError` if the file is not UTF-8 - decoding is explicitly `raw.decode("utf-8")`, not locale-dependent, which matters on this Windows box where the ambient codepage is `cp1252`.

#### `ConfigFile.__getitem__(key)` / `get(key, default=None)` / `require(key)`

```python
def __getitem__(self, key: str) -> Any
def get(self, key: str, default: Any = None) -> Any
def require(self, key: str) -> Any
```

Top-level key access only - none of the three descends into nested dicts.

- `__getitem__` raises `KeyError` with the bare key name (`cfg["slots"]`).
- `get` returns `default` when absent.
- `require` raises `KeyError` naming the file: `"deck.json is missing required key 'z'"`.

**Why `require` exists:** the same failure with a filename attached. A bare `KeyError: 'z'` from deep inside `Deck.__init__` does not say which of four JSON files is short a key.

**Note:** `require` has no in-tree caller; `Deck.__init__` uses plain `d["feedrates"]` / `d["z"]` subscripting and so produces the less helpful message. Prefer `require` in new code.

#### `ConfigFile.__repr__()`

`<ConfigFile p300.json sha=1a2b3c4d>` - basename plus the first 8 hex characters of the hash. Useful in a traceback for telling two config trees apart.

### `load(*parts)`

```python
def load(*parts: str) -> ConfigFile
```

`ConfigFile(config_path(*parts))`. The generic loader the four named loaders below wrap. Raises whatever `ConfigFile.__init__` raises.

### `load_deck()`, `load_labware()`, `load_layout(name)`, `load_effector_spec(effector_id)`

```python
def load_deck() -> ConfigFile
def load_labware() -> ConfigFile
def load_layout(name: str = "layout.json") -> ConfigFile
def load_effector_spec(effector_id: str) -> ConfigFile
```

The four config kinds, named so a caller never spells a filename.

- `load_deck()` -> `config/deck.json`: the machine frame. Envelope, `z` (`stage_surface` 114.75, `stage_surface_with_p300_tip` 62.5, `slot_edge_with_p300_tip` 53.0, `travel` 5.0), the nine `slots`, `stations`, `keepouts`, `klipper_macros`, `coordinate_frame`.
- `load_labware()` -> `config/labware.json`: slot-independent geometry for every known labware.
- `load_layout(name)` -> `config/<name>`; `.json` is appended if absent, so `load_layout("layout_test")` and `load_layout("layout_test.json")` are the same call. This is the per-run file - the tests pass `TEST_LAYOUT = "layout_test.json"` for behaviour and reserve the no-argument form for the handful of tests that deliberately assert the shipped bench.
- `load_effector_spec(effector_id)` -> `config/effectors/<effector_id>.json`, e.g. `"p300"` -> the actuator limits, the calibration coefficients behind `d_mm = 0.0533 * V_uL + 0.122`, and `z_for_labware`.
- All four raise `FileNotFoundError` / `ValueError` per `ConfigFile.__init__`.

**Why one file per effector:** the per-labware working depths are a property of the *nosecone plus tip*, not of the labware, so they cannot live in `labware.json`. `sbs_96_wellplate` has `z.top: null` and a note pointing at `effectors/*.json -> z_for_labware`, where p300 carries plate `work` 53.5 and `bottom` 60.8. `tests/test_lab.py::test_well_bottom_crash_is_enforced_from_the_effector_table` pins that separation.

### `available_effectors()`

```python
def available_effectors() -> Dict[str, Path]
```

Every effector JSON on disk, keyed by id (the filename stem).

- No parameters.
- **Returns:** `{"p125": Path(...), "p300": Path(...)}`, insertion-ordered by sorted filename. Returns `{}` - not an error - if `config/effectors/` does not exist.
- Raises nothing beyond `config_dir()`'s `FileNotFoundError`.

**Why it exists:** so `--effector` is validated against what is actually installed and the error can list the real options. `lab/effector.py` uses it both for that check and for `list_effectors()`.

---

## `lab/labware.py`

Labware geometry and its placement on the deck. `labware.json` says what a rack *is* (rows, cols, pitch, well naming, height, approach order); `layout.json` says where it *is* (slot, station, or a measured anchor). This module joins the two and answers exactly one question: given a well, what are its machine coordinates in XY? No Z, no motion, no gcode. Z belongs to the effector table; motion belongs to `lab/machine.py`.

The load-bearing idea: **every placement, whatever its grid model, reduces to the same three vectors.**

```
xy(row, col) = origin + col * col_vec + row * row_vec
```

`origin` is the centre of well (0, 0) in machine coordinates; `col_vec` is the XY step per column index; `row_vec` the step per row index. All three are already-rotated, already-signed millimetres. The three grid models differ only in how those vectors are obtained, and after `build_placement` returns, nothing downstream can tell which model was used:

- **`pitch`** - `pitch_x`/`pitch_y` times integer `col_dir`/`row_dir` from `labware.json`, rotated by the placement's `rotation`, with the origin derived from a slot centre, a station, an `anchor_xy`, an `a1_from_center` override, or `a1_from_slot_edge` insets. Used by both tube holders and the 96-well plate.
- **`corners`** - three measured well centres are solved for pitch *and* rotation. Used by the tip rack, because the real ClipTip pitch is `100.0 / 11 = 9.090909...` in X and `63.7 / 7 = 9.1` in Y, not the nominal 9.00. Assuming 9.00 accumulates about 1.0 mm by column 12, which is enough to miss a tip entirely.
- **`single`** - a one-position station; both vectors are `(0, 0)`. Used by `reservoir_water`, whose stored `(70, 175)` is a dip point, not a trough centre.

Column senses are **per labware and must not be generalised.** `sbs_96_wellplate` and both tube holders have `col_dir: [-1, 0]` - increasing column index moves toward *lower* X, so A1 sits at the high-X, high-Y corner. The tip rack's solved `col_vec` runs the other way, toward *higher* X (origin X 18.0 to `along_col` X 118.0). `col_dir` on the plate was `[1, 0]` until 2026-09-08; that put A1 at the low-X end and displaced every well by the full 11 x 9 = 99 mm column span. It was caught only by the operator measuring plate1's A1 at (387, 399) against a prediction of (286.5, 396.5), and is now pinned by `tests/test_lab.py::test_plate_column_direction_runs_toward_low_x`.

### `_rotate(v, degrees)` (private)

```python
def _rotate(v: Vec2, degrees: float) -> Vec2
```

Rotates an XY vector counter-clockwise about the origin.

- `v`: `(x, y)` in mm.
- `degrees`: CCW degrees; reduced modulo 360.
- **Returns:** the rotated `(x, y)` in mm.
- Raises nothing.

0/90/180/270 are handled by exact integer swaps and sign flips rather than `cos`/`sin`, so a quarter-turn rack keeps its pitch to the last decimal instead of picking up `1e-16` dirt that then shows up in a coordinate diff. Other angles fall through to `math.radians` and trigonometry.

Protocol authors should never call this. It exists only for the `pitch` model inside `build_placement`.

### `well_name(row, col)`

```python
def well_name(row: int, col: int) -> str
```

0-indexed `(row, col)` to an `'A1'` style label. Row 0 is `A`, col 0 is `1`.

- `row`, `col`: 0-indexed integers, dimensionless.
- **Returns:** e.g. `well_name(1, 2) == "B3"`.
- **Raises:** `ValueError` if `row` is negative or past 25 ("row 26 has no single-letter name"). `col` is **not** range-checked here and is not checked against the labware at all - `well_name(0, 99)` cheerfully returns `"A100"`.

**Why it exists:** so the operator-facing label is generated in exactly one place. It is what the manifest, the CSV and the alignment report show, and it is the only string an operator can cross-check against a physical rack. Imported by `lab/machine.py`, both protocols and `tools/check_alignment.py`.

**Note:** because it validates nothing about the actual labware, a label is not evidence the well exists. `Placement.check_well` is the check.

### `parse_well(name)`

```python
def parse_well(name: str) -> Tuple[int, int]
```

Inverse of `well_name`: `'B3'` -> `(1, 2)`.

- `name`: a well label. Stripped and upper-cased first, so `" b3 "` works.
- **Returns:** `(row, col)`, both 0-indexed.
- **Raises:** `ValueError` if the string is shorter than 2 characters, does not start with a letter, or has a non-digit tail - so `"AA1"` and `"B"` are rejected. Multi-letter rows are not supported, matching `well_name`.

**Note:** `"A0"` parses to `(0, -1)` without complaint. A negative index is caught later by `Placement.check_well` (which requires `0 <= col`), so `Placement.well("A0")` does raise `IndexError` - but if you call `parse_well` directly, validate the result before using it as an index.

### `class Labware`

```python
@dataclass(frozen=True)
class Labware:
    id: str
    spec: Dict[str, Any]
```

Slot-independent geometry, straight out of `labware.json`. Frozen, and every accessor is a read-through to `spec`, so a `Labware` is a typed view over config rather than a parsed copy. Nothing here knows where the labware is.

`spec` is the raw dict, and anything without an accessor is read through it directly - which is how `avoid_traverse` is consumed (`lab/deck.py`: `p.labware.spec.get("avoid_traverse")`) and how the tests reach `a1_from_slot_edge` and `a1_from_center`.

#### `Labware.display_name`

`spec["display_name"]`, falling back to `id`. Operator-facing, e.g. `"300 uL ClipTip rack, 96 position"`.

#### `Labware.type`

`spec["type"]` or `"unknown"`. One of `tube_rack`, `tiprack`, `wellplate`, `reservoir` today. Descriptive; no code branches on it.

#### `Labware.grid`

The `grid` block, or `{"model": "single", "rows": 1, "cols": 1}` if absent. The default is what lets a station labware omit the block entirely and still resolve.

#### `Labware.rows` / `Labware.cols`

`int` of `grid["rows"]` / `grid["cols"]`, defaulting to 1. Dimensionless counts. 5 x 10 for the 0.65 mL holder, 8 x 12 for the tip rack and plate, 3 x 6 for the 1.5 mL holder.

**Why the counts matter more than they look:** the 1.5 mL holder's 3 x 6 is *inferred*, not stated by the operator, and 6 was chosen as the fail-safe. A 7th column would sit at X 132.50, which is 0.25 mm inside the location's low-X edge at 132.25, so an 11 mm tube there would overhang into slot 6 by 5.25 mm. If the holder really has 7 columns, asking for column 6 raises a clear `IndexError`; the opposite mistake would drive a tip into empty space over a slot boundary. `grid.rows` and `grid.cols` are both listed in that labware's `uncalibrated`.

#### `Labware.model`

`grid["model"]` or `"single"`. One of `"pitch"`, `"corners"`, `"single"`; `build_placement` raises on anything else.

#### `Labware.footprint`

```python
@property
def footprint(self) -> Optional[Vec2]
```

`(width_x_mm, depth_y_mm)` of the physical outline, or `None` if never measured. `sbs_96_wellplate` is `(127.76, 85.48)`; both tube holders and the tip rack are explicitly `null`.

**Why `Optional` and not a guess:** it is the input to `Placement.footprint_bounds`, which feeds the deck's overlap check. A guessed footprint would let that check report a pass it never earned.

#### `Labware.wipe`

The `wipe` block or `None`. For the 0.65 mL holder: `dx` 3.2 mm sideways to contact the inner wall, `dz` 4.0 mm dragging the droplet *up* (recall +Z is down, so the wipe moves toward a smaller Z), `feed` 1500, and `uncalibrated: true`. `dx` must be less than the tube inner radius.

`Machine.dispense(..., wall_wipe=True)` checks this **before** moving, and raises `ProtocolError` if it is `None` - pinned by `tests/test_review_fixes.py::test_10_dispense_checks_wipe_config_before_moving`, whose defect was a full blow-out emitted before the precondition check. `reservoir_water` and the 1.5 mL holder have no wipe block, so a wipe there is refused.

#### `Labware.height_mm`

```python
@property
def height_mm(self) -> Optional[float]
```

Physical height of the labware **above the stage**, in mm, contents included if known. Returns `spec["height_with_contents_mm"]` when present, otherwise `spec["height_mm"]`, otherwise `None`.

- 0.65 mL holder: 35.0 (empty). 1.5 mL holder: 45.0 (empty). `sbs_96_wellplate`: `None` - unmeasured.
- Raises nothing; `None` means unmeasured.

**Why a height and not a commanded Z:** a height is both frame-independent and effector-independent. The commanded Z at which the tool would strike the labware depends on which nosecone is fitted and whether it has a tip on, so it is derived per run: `Simulator.labware_top_z` computes `z_stage_surface - height` bare and subtracts the tip length again when tipped. `tests/test_lab.py::test_holder_heights_convert_to_commanded_z` pins the arithmetic: 35 mm tops out at Z 79.75 bare and Z 27.5 with a 300 uL tip (114.75 - 35 = 79.75; 79.75 - 52.25 = 27.5), and travel Z 5.0 must be a *smaller* number than 27.5 to be physically above it. The same test pins `max_labware_height() == 57.5` - the tallest labware that still clears travel height with a tip fitted (114.75 - 52.25 - 5.0). The 0.65 mL holder therefore has 22.5 mm of headroom for tubes and caps; the 1.5 mL holder has 12.5 mm.

The physical constraint encoded here: taller than 57.5 mm and travel height *strikes it*, and because `height_mm` on the plate is `None`, traverse clearance over the plates reports "cannot check" rather than passing.

#### `Labware.height_is_empty_only`

```python
@property
def height_is_empty_only(self) -> bool
```

`True` when `height_mm` is set but `height_with_contents_mm` is not - i.e. the number we have is the empty rack and the tubes and caps standing in it are unaccounted for. `True` for both tube holders today. Takes no parameters, raises nothing.

**Why it exists:** it turns a missing measurement into a warning that reaches the operator instead of an assumption. `tests/test_lab.py::test_empty_height_is_flagged_as_incomplete` runs a real dispense over the 0.65 mL holder and asserts the run warns that contents are unaccounted for - the third project convention, that a check which cannot be evaluated is not a pass.

#### `Labware.approach_order`

```python
@property
def approach_order(self) -> Optional[str]
```

Preferred XY axis order when approaching a well on this labware, upper-cased, or `None` for the default X-first.

- **Returns:** `"YX"` for both tube holders; `None` for the plate (deliberately unset - a plate has no caps) and the reservoir.
- Raises nothing.

`"YX"` means reach the Y position first, **while still clear in X**, then come in along X. The physical constraint: the tube caps make Y motion *across* the holder difficult, while X motion passes between cap rows. `lab/machine.py` honours it per labware; the keep-out router can still override it, and says so when it does. Pinned by `test_tube_holder_is_approached_y_first`, `test_goto_well_honours_approach_order` (whose defect was dropping the `prefer` argument outright) and `test_aspirate_from_reaches_the_well`, all of which assert the first two move legs are `["Y", "X"]`.

#### `Labware.uncalibrated`

```python
@property
def uncalibrated(self) -> List[str]
```

Copy of `spec["uncalibrated"]` - dotted field names this labware admits it does not know, e.g. `["wipe.dx", "wipe.dz", "z.work"]` for the 0.65 mL holder, `["grid.a1_corner", "consume_order.exit_edge"]` for the tip rack. Empty list if absent. Returns a fresh list, so mutating it does not touch the config.

Aggregated by `Deck.uncalibrated()` into `Machine.uncalibrated()` and into the manifest, and printed by the one-liner in `CLAUDE.md`. Read this before treating any value from this labware as known.

#### `Labware.z(key)`

```python
def z(self, key: str) -> Optional[float]
```

A nominal Z from the labware's own `z` block.

- `key`: e.g. `"top"`, `"work"`, `"bottom"`, `"approach"`, `"pick"`.
- **Returns:** the value as `float`, or `None` if the block or the key is missing.
- Raises nothing - a missing `z` block yields `None`, not `KeyError`.

**Note:** every `z` value in the shipped `labware.json` is currently `null`, by design. Absolute Z is effector-dependent, so the real numbers live in `effectors/*.json -> z_for_labware` (p300: plate `work` 53.5 / `bottom` 60.8; 1.5 mL tube `work` 35.0 / `mix` 45.0, `bottom` still unmeasured). Do not reach for `Labware.z` when you want a depth to command - it will hand you `None`. It is a placeholder for stage-referenced nominals that would only become useful if a labware-local Z were ever measured.

### `class Placement`

```python
@dataclass
class Placement:
    name: str
    labware: Labware
    origin: Vec2
    col_vec: Vec2
    row_vec: Vec2
    mount: str
    rotation: float = 0.0
    slot: Optional[int] = None
    station: Optional[str] = None
    spec: Dict[str, Any] = field(default_factory=dict)
```

One piece of labware at a definite spot on the deck: geometry joined to placement, resolved down to the three vectors. Built by `build_placement`, collected by `Deck` into `deck.placements[name]`. Not frozen, but treat it as immutable - `Deck` validates seating and occupancy at construction, and assigning to `origin` afterwards bypasses all of it.

Fields: `name` is the layout's placement name (`"tips"`, `"big_tubes"`, `"plate1"`), and the string a protocol passes to `Machine.dispense`/`aspirate_from`. `origin` is well (0, 0) in machine mm. `mount` is `"slot"`, `"station"` or `"fixed"`. `rotation` is degrees, already baked into the vectors. `slot` / `station` are whichever applies. `spec` is the layout entry.

**Note:** `spec` is not byte-identical to `layout.json`. `Deck.__init__` injects a private `_slot_pitch` key into a shallow copy of each slot-mounted entry before calling `build_placement`, so `spec["_slot_pitch"] == [135.5, 93.25]` is present on every slot-mounted placement. Harmless today because nothing serializes `spec` wholesale; do not assume it round-trips.

#### `Placement.xy(row, col)`

```python
def xy(self, row: int, col: int) -> Vec2
```

The machine XY of a well centre. The whole point of the module.

- `row`, `col`: 0-indexed integers, dimensionless.
- **Returns:** `(x_mm, y_mm)` in machine coordinates, each rounded to 4 decimals - so a coordinate diff shows a real move, not float noise.
- **Raises:** `IndexError` via `check_well` if the well is outside the labware's `rows` x `cols`.

Real values from the shipped bench: `d.placement("plate1").xy(0, 0) == (387.0, 399.0)`; `tips.xy(0, 11) == (118.0, 399.2)` and `tips.xy(7, 0) == (18.0, 335.5)` - the solve closes exactly on the two far measured corners; `big_tubes.xy(0, 0) == (252.5, 305.5)`, the operator's direct measurement. In the test fixture, the 0.65 mL holder's A1 is `(256.6, 218.5)`.

Rounding is applied after the multiply-add, not to the vectors, so the tip rack's irrational `100/11` pitch accumulates in full precision and only the emitted coordinate is rounded.

#### `Placement.well(name)`

```python
def well(self, name: str) -> Vec2
```

`xy(*parse_well(name))`. Same return and rounding; raises `ValueError` for a malformed label and `IndexError` for a well outside the labware. Convenience for operator-facing code; index-driven code should use `xy`.

#### `Placement.check_well(row, col)`

```python
def check_well(self, row: int, col: int) -> None
```

Raises `IndexError` unless `0 <= row < labware.rows` and `0 <= col < labware.cols`. Returns `None` on success.

**Why it exists / what mistake it prevents:** this is the bound between "a coordinate you can compute" and "a well that physically exists". The arithmetic in `xy` is perfectly happy to extrapolate column 6 of a 6-column holder and hand you a plausible number 20 mm past the last tube, over the slot boundary. The message names the placement, the labware id and the actual shape, e.g. `"big_tubes: well (row=0, col=6) is outside tube_rack_1p5 which is 3 x 6"`. It is also what makes the 1.5 mL holder's fail-safe 6-column choice work.

#### `Placement.all_wells()`

```python
def all_wells(self) -> List[Tuple[int, int]]
```

Every `(row, col)` in row-major order - 96 for the plate, 50 for the 0.65 mL holder, 18 for the 1.5 mL holder. Raises nothing. For sweeping a whole piece of labware (resolve-everything checks, exhaustive envelope tests) without re-deriving the shape.

#### `Placement.corner_wells()`

```python
def corner_wells(self) -> List[Tuple[int, int]]
```

The four extreme index pairs: `[(0, 0), (0, cols-1), (rows-1, 0), (rows-1, cols-1)]`. Raises nothing. Note these are *index* corners, which are geometric corners only because the two vectors are linearly independent - true for every current labware.

#### `Placement.well_bounds()`

```python
def well_bounds(self) -> Tuple[float, float, float, float]
```

`(x_min, x_max, y_min, y_max)` in mm over **well centres**, computed from the four corner wells.

- Raises `IndexError` for a degenerate 0-row or 0-col labware (via `xy`).
- **These are centres, not the physical outline** - no tube radius, no rack body. A tube whose centre is inside the bounds still overhangs by half its diameter. `tests/test_lab.py` does that arithmetic explicitly when it checks the 1.5 mL holder for overhang, using `tube_d = 11.0` and comparing `x - tube_d / 2` against the location's low-X edge. For the outline, use `footprint_bounds`.

Used to confirm each plate fits its slot cell: `x0 >= cx - px/2 and x1 <= cx + px/2` and the same in Y, with `px, py = d.slot_pitch()`.

#### `Placement.grid_center()`

```python
def grid_center(self) -> Vec2
```

Midpoint of `well_bounds`, rounded to 4 decimals. `(68.0, 367.35)` for the tip rack - which is 3.0 mm in X and 2.35 mm in Y from slot 3's centre `(65.0, 365.0)`. That 3.0 mm X offset is what confirmed the `x' = 420 + x` frame rather than a rack sitting a column off. Raises nothing beyond `well_bounds`.

Also the reference point for `footprint_bounds`, and used by `Deck` for slot seating reports.

#### `Placement.footprint_bounds()`

```python
def footprint_bounds(self) -> Optional[Tuple[float, float, float, float]]
```

Physical outline bounds in mm as `(x_min, x_max, y_min, y_max)`, taken as `grid_center()` plus/minus half the footprint - or **`None` if the footprint was never measured**.

- Raises nothing.
- Today only `sbs_96_wellplate` has a footprint (127.76 x 85.48), so this returns `None` for the tip rack, the reservoir and both tube holders.

**Why `None` rather than a fallback to the well bounds:** it keeps the overlap check honest. `Deck` and `Simulator` both branch on `None` and report "cannot check" instead of silently passing - a fabricated outline would make two racks look non-overlapping when nobody has measured either.

**Note:** the outline is centred on the *well grid* centre, which assumes the grid is centred on the body. That holds for an SBS plate (the `grid_centered_on_footprint` note works it out: 14.38 + 11*9/2 = 63.88 = 127.76/2), but if a footprint is ever added for a labware whose grid is off-centre, this will place the box wrong.

#### `Placement.col_x_values()` / `Placement.row_y_values()`

```python
def col_x_values(self) -> List[float]
def row_y_values(self) -> List[float]
```

The X of every column taken along row 0, and the Y of every row taken along column 0. Both in mm, both `xy`-rounded. Raise `IndexError` only for a degenerate labware.

For the 1.5 mL holder: columns at 252.5, 232.5, 212.5, 192.5, 172.5, 152.5 (20 mm apart, descending X); rows at 305.5, 275.5, 245.5 (30 mm apart, descending Y).

**Why they exist:** keep-out and routing logic reasons in whole columns, not wells. `lab/machine.py` calls `col_x_values()` in three places for exactly that, and the tests use them to check which columns fall inside the tip-ejector band `X[148, 162]` - the 0.65 mL holder's columns 8 and 9 (which no protocol uses; before the +70 correction the band appeared to cover columns 2 and 3, which the accuracy protocol does use), and plate2's columns 10 and 11 at X 161 and 152, the *high* indices, because the column sense runs toward lower X.

**Note:** these only describe row 0 / column 0. Under a non-axis-aligned rotation or a rotated corner solve, "the X of column c" varies by row and these lists become a single slice through the grid rather than the whole story. Every current placement has `rotation: 0` and an axis-aligned solve.

#### `Placement.__repr__()`

`<Placement tips tiprack_300_96 slot origin=(18.0, 399.2) 8x12>` - name, labware id, mount, origin and shape.

### Building placements from `layout.json`

#### `_pitch_vectors(labware)` (private)

```python
def _pitch_vectors(labware: Labware) -> Tuple[Vec2, Vec2]
```

`(col_vec, row_vec)` in mm from the labware's `pitch_x`/`pitch_y` and its integer `col_dir`/`row_dir`, **before** rotation.

- **Raises:** `KeyError` on `grid["pitch_x"]` or `grid["pitch_y"]` if the labware declares `model: "pitch"` without them.
- Defaults: `col_dir` `[1, 0]`, `row_dir` `[0, -1]`.

The `col_dir` default is the trap. Every labware in the shipped config that uses this model overrides it to `[-1, 0]`, and the plate defect of 2026-09-08 was exactly this value left at the default. If you add a `pitch` labware, state `col_dir` and `row_dir` explicitly and verify with `tools/check_alignment.py`. Not for protocol authors.

#### `_corner_vectors(labware, corners)` (private)

```python
def _corner_vectors(labware, corners) -> Tuple[Vec2, Vec2, Vec2]
```

Solves origin, `col_vec` and `row_vec` from three measured well centres. Returns `(origin, col_vec, row_vec)` in mm.

- `corners`: the placement's `corners` block. Needs `origin`, `along_col` and `along_row`, each `[x_mm, y_mm]`. `origin` is well (0, 0); `along_col` is the far well in the same row; `along_row` the far well in the same column.
- The solve is a plain division by span: `col_vec = (along_col - origin) / (cols - 1)`, `row_vec = (along_row - origin) / (rows - 1)`. Because the vectors come from measured points, **rotation and pitch fall out together** - no rotation parameter is needed or accepted.
- **Raises:** `KeyError` naming the missing key if any of the three is absent. `ValueError` if the labware has fewer than 2 rows or 2 cols (division by zero otherwise). `ValueError` if the optional `corners.solved` cross-check fails.

For the tip rack: origin (18.0, 399.2), `along_col` (118.0, 399.2), `along_row` (18.0, 335.5), giving `col_vec` (100/11, 0) = (9.0909..., 0) and `row_vec` (0, -63.7/7) = (0, -9.1). `tests/test_lab.py` asserts both to 1e-9.

**Why the model exists:** the nominal 9.00 mm pitch is wrong. Real is 9.0909 in X, and 9.00 accumulates about 1.0 mm by column 12 - enough to miss a tip, with no crash to tell you.

**Why `corners.solved` exists:** it holds the solve to what the measurement is supposed to produce. If present it may carry `col_pitch`, `row_pitch` (checked against `math.hypot` of each vector to 1e-6) and `grid_center` (checked to 1e-4). The shipped values are `col_pitch: 9.090909090909092`, `row_pitch: 9.1`, `grid_center: [68.0, 367.35]`. The failure message says which number disagrees and tells you to re-measure or delete `corners.solved` - deliberately refusing to pick a winner. The comment in the source names the reason: a stored number nothing checks is how the old scripts' own displacement tables drifted away from the equations printed above them.

**Note:** the three corners fix the grid but *not* which corner is A1. `labware.json` records `grid.a1_corner: "unconfirmed"` and lists it in `uncalibrated`; it is an open question in `layout.json`. Coordinates from this placement are trustworthy; the well *names* attached to them are not yet.

#### `_resolve_origin(entry, labware, deck, name, needs_grid, col_vec, row_vec)` (private)

```python
def _resolve_origin(entry, labware, deck, name, needs_grid,
                    col_vec=(0.0, 0.0), row_vec=(0.0, 0.0)) -> Vec2
```

Answers "where is well (0, 0)?" for the `pitch` and `single` models. Returns `(x_mm, y_mm)`.

- `entry`: the layout placement dict, as amended by `Deck` with `_slot_pitch`.
- `labware`: for the labware-level fallbacks and the grid span.
- `deck`: the **raw `deck.json` dict**, not a `Deck` object - it needs `deck["slots"]` and `deck["stations"]`. Passing the wrong one is the easy mistake here.
- `needs_grid`: `False` for a single-position labware (the slot centre is the answer); `True` for a grid, which enables the A1-offset paths.
- `col_vec`, `row_vec`: mm, already rotated, needed only by the centred-grid span branch.

Precedence, first match wins:

1. **`entry["anchor_xy"]`** - an explicitly measured machine coordinate. Always wins, so pinning a coordinate is always available as an escape hatch.
2. **`entry["station"]`** - `deck["stations"][name]["xy"]`. Raises `KeyError` naming the placement and the unknown station. This is how `water` resolves to `(70, 175)`: the stored value is a *dip point*, 5.0 mm in X and 3.5 mm in Y off slot 9's centre, which is precisely why it is a station and not slot-mounted.
3. **`entry["slot"]`** - looks up `deck["slots"][str(slot)]["center"]`; raises `KeyError` for an unknown slot. Then:
   - if `needs_grid` is `False`, the slot centre *is* the origin;
   - **`a1_from_center`** on the entry or the labware - the slot centre plus a stored `[dx, dy]` in mm. The plate uses `[51.0, 34.0]`, so slot 1's centre (336, 365) gives A1 at (387, 399), the operator's 2026-09-08 measurement. It is applied to *every* placement of that labware, on the theory that how the plate seats against a location's retaining features is a property of the plate, not of the location - which is why plate2's (251, 399) is inferred and still an open question. This override takes precedence over the centred-grid derivation below, which would have given (+49.5, +31.5);
   - **`a1_from_slot_edge`** on the entry or the labware - insets from the **location edges** rather than from a measured machine coordinate. The location cell is the slot centre expanded by half the slot pitch, so the pitch must be passed in via the private `entry["_slot_pitch"]` that `Deck.__init__` injects; without it, `ValueError` ("Deck must pass it in"). `x_edge`/`y_edge` are `"high"` or `"low"` (default `"high"`) and name which edge each inset is measured from: high means `centre + pitch/2 - inset`, low means `centre - pitch/2 + inset`. Result rounded to 4 decimals. The 1.5 mL holder uses `x_inset` 15.25, `y_inset` 13.125 from the high edges: slot 5's centre (200, 272) with the mean pitch (135.5, 93.25) gives A1 at (200 + 67.75 - 15.25, 272 + 46.625 - 13.125) = (252.50, 305.50), the measured value, and `tests/test_lab.py` recomputes exactly that expression rather than hardcoding it. The insets were *calibrated from* that measurement, not from the verbal description "13 mm down from the top and 14 mm in from the very left" - which predicted (253.75, 305.625), right to 0.125 mm in Y and 1.25 mm out in X. That same measurement is what settled "the very left" as the *high*-X edge; the low-X reading would have put A1 at X 146.25, 106 mm away. **Caveat carried in the config:** these insets were calibrated in slot 5 and the slot X pitch is irregular (136 between slots 1-2, 135 between 2-3, mean 135.5), so a cell derived from the mean pitch is up to 0.5 mm out in another slot. Re-measure A1 if the holder moves rather than trusting the model to transfer;
   - **`grid_centered_on_footprint`** - only if the labware declares it. Origin is the slot centre minus half the grid span, `span = (cols-1)*col_vec + (rows-1)*row_vec`. Note it centres on the *well span*, not the footprint, so the declaration is an assertion that those coincide;
   - otherwise **`ValueError`**, refusing to guess: "Cannot guess where well A1 is - add anchor_xy or a1_from_center to the placement."
4. Nothing at all - `ValueError`: "has no anchor_xy, no slot and no station - there is no way to know where it is."

**Why the refusals matter more than the branches:** every raise here is a coordinate the library declined to invent. A guessed A1 does not crash - the tube pitch is 12 mm and a plausible-looking origin lands the tip inside *some* tube, in the wrong column, with the balance readings still looking reasonable. That is the failure mode the whole module is shaped around.

**Note:** the `if not pitch` guard on `_slot_pitch` is falsy for `[0, 0]` as well as for a missing key, so a single-slot deck (pitch 0, 0 from `Deck._raw_slot_pitch`) would report "the slot pitch was not supplied" rather than the truth. Not reachable with the nine-slot deck.

#### `build_placement(entry, labware, deck)`

```python
def build_placement(entry: Dict[str, Any],
                    labware: Labware,
                    deck: Dict[str, Any]) -> Placement
```

Turns one `layout.json` `placements[]` entry into a resolved `Placement`. The module's only public constructor, and the single point where a grid model is dispatched.

- `entry`: the placement dict. Reads `name` (default `labware.id`), `mount` (default `"fixed"`), `rotation` (degrees, default 0), `slot`, `station`, `corners`, and the origin keys above. The whole dict is stored as `Placement.spec`.
- `labware`: the catalogue entry, which supplies the model.
- `deck`: the **raw `deck.json` dict** - `deck["slots"]` and `deck["stations"]`, not a `Deck` object.
- **Returns:** a fully resolved `Placement`; `origin`, `col_vec` and `row_vec` are final and rotation is already applied.
- **Raises:**
  - `KeyError` if the labware's model is `"corners"` but the placement supplies no `corners` block - the block belongs to the placement, because it is a measurement of *this rack in this slot*, which is also why moving the tip rack means re-measuring rather than editing a slot number;
  - `ValueError` if a `"corners"` placement also sets a non-zero `rotation`: "rotation is meaningless with the 'corners' model - the measured corners already encode any rotation." Applying both would double-count it. The returned `Placement` has `rotation=0.0` forced;
  - `ValueError` for an unknown model: `"{labware.id}: unknown grid model {model!r}"`;
  - anything `_corner_vectors`, `_pitch_vectors` or `_resolve_origin` raises.

Dispatch order is `corners`, then `single`, then `pitch`, with anything else rejected.

**Note:** for the `single` model, `rotation` is stored on the `Placement` but has no effect - both vectors are `(0, 0)`, so there is nothing to rotate. A non-zero rotation on a single-position station is silently inert rather than an error. Harmless for a dip point; misleading if someone later gives a station a grid.

**Note:** `deck` is the raw dict, so `build_placement` cannot see `Deck`'s validation. Seating checks (`_check_recorded_slot_center`), duplicate-name detection and slot-occupancy checks all live in `Deck.__init__` *after* this returns. Building a `Placement` directly skips them; go through `Deck.load()` unless you are writing a geometry test.

---

## `lab/effector.py`

The end-effector layer: identity, the calibration curve, plunger-position arithmetic, and the per-effector Z table. One JSON file per physical effector in `config/effectors/` (`p300.json`, `p125.json`, `gripper.json`), one class per `kind` value. Adding a new effector is a new JSON file; adding a new *kind* of effector is a new class here plus an entry in `_KINDS`. Nothing else in the library changes.

Two responsibilities sit here rather than anywhere else, and both are deliberate:

**uL -> mm conversion is exclusive to `Pipette`.** Protocols request microlitres (`aspirate_uL(150.0)`); the only code that multiplies by a slope is `Pipette.displacement_mm`, reading `calibration.slope` / `calibration.intercept` out of the effector JSON. The scripts this library replaced carried hardcoded displacement tables (`[1.722, 8.123, 12.123, 16.123]` for the p300) alongside a curve written in a comment, and the two drifted apart - `config/effectors/p300.json -> calibration.supersedes` records both. Because every mm value is now derived at call time from the coefficients in use, a displacement cannot claim to be a volume it is not. The corollary is a hard rule: **no mm value is ever typed into a protocol.**

**The Z table is per-effector, not per-labware.** `labware.json` stores a height above the stage; the commanded Z at which *this* effector works in *that* labware depends on what is fitted to the nosecone. The bare p300 nosecone contacts the stage at Z 114.75 and the same stage with a 300 uL tip at Z 62.5, so a fitted tip moves the reference point 52.25 mm. The same 1.5 mL tube therefore needs a different commanded Z with a tip on than without, which is why `z_for_labware` lives in the effector file. Remember throughout that **+Z is DOWN**: Z 0 is home at the top, and a larger number is physically lower.

### `class LabError(Exception)`

Base for every error this library raises. Subclasses, all defined across the package but rooted here:

```
LabError
  EffectorError        (this module)  effector cannot do what was asked
    VolumeError        (this module)  volume outside what can be metered
  DeckError            lab/deck.py
  ProtocolError        lab/machine.py
  SimulationError      lab/simulate.py
```

**Why it exists:** a protocol can wrap its body in `except LabError as exc: print(exc)` and show the operator a sentence instead of a traceback. The messages are written for that - they name the file and key to edit (`config/effectors/p300.json -> z_for_labware`), not the Python frame. `tests/test_lab.py` pins the contract that an out-of-envelope `goto()` surfaces as a `LabError`, so catching the one class is genuinely sufficient.

### `class EffectorError(LabError)`

Raised when the effector cannot perform the request: unknown effector id, unknown `kind`, a placeholder loaded for motion, a missing or unmeasured Z entry, an unsupported calibration model, an inconsistent air gap, or a plunger POS outside its legal band.

### `class VolumeError(EffectorError)`

Raised only by `Pipette.check_volume`, for a volume outside the fitted calibration window or beyond the available plunger stroke. Separate from `EffectorError` so a protocol sweeping a volume series can catch just the volume case and skip a point.

---

### `class Effector`

`Effector(cfg: config.ConfigFile)`

Base class: identity, raw config access, and the Z table. `kind = "generic"`; not registered in `_KINDS`, so it is never instantiated by `load()` directly.

Attributes set in `__init__`:

| attribute | source | notes |
|---|---|---|
| `cfg` | the `ConfigFile` | keeps `path`, `data`, `sha256` |
| `spec` | `cfg.data` | the parsed JSON dict; escape hatch for fields with no accessor |
| `id` | `spec["id"]` | raises `KeyError` if absent |
| `display_name` | `spec["display_name"]`, default `id` | |
| `sha256` | `cfg.sha256` | hash of the exact bytes loaded |

**Why `sha256` is carried:** `lab/machine.py` writes it into the run manifest as `effectors/p300.json: <sha>`, so a generated `.gcode` can be traced back to the precise calibration file that produced it. A recalibration that changes a slope changes the hash, and old output stays attributable.

### `Effector.z_for(labware_id: str, key: str = "work") -> float`

Absolute commanded Z, in millimetres, for this effector at this labware.

- `labware_id` (str): a labware id as it appears in `labware.json` and as a key of `spec["z_for_labware"]`, e.g. `"sbs_96_wellplate"`, `"tube_rack_1p5"`, `"tiprack_300_96"`.
- `key` (str): which depth. See the three-key convention below.

Returns the Z as a float (larger = physically lower). Raises `EffectorError` in three distinct cases, each with a different fix:

1. no entry for that labware - message lists the known ids and names the file to edit;
2. entry exists but lacks that key - message lists the keys present;
3. the key is present but `null` - "it has never been measured. Measure it and put it in the config."

**Why it exists / what it prevents:** case 3 is the point. A missing measurement stored as `null` must refuse, not fall back to a plausible default, because descending to a depth nobody measured is a crash. `tests/test_lab.py` blanks out `tube_rack_1p5.work` in a copied config and asserts that `aspirate_from` then raises rather than picking a depth.

**The three-key convention, shallowest first.** For the p300, from `config/effectors/p300.json`:

| key | `sbs_96_wellplate` | `tube_rack_1p5` | meaning |
|---|---|---|---|
| `work` | 53.5 | 35.0 | the dispense / aspirate depth. What `dispense`, `aspirate` and `prewet` command. |
| `mix` | (absent) | 45.0 | where `Machine.mix` cycles - 10 mm deeper than `work`, so a mix reaches under the liquid it is stirring instead of skimming the surface. |
| `bottom` | 60.8 | `null` | the vessel floor. A **CRASH LIMIT, never a target.** `lab/simulate.py` fails any move deeper than it. |

Because +Z is down, the ordering constraint is `work < mix < bottom` as numbers. The plate dispenses 7.3 mm above its floor (60.8 - 53.5). `tests/test_lab.py::test_plate_depths_are_measured_and_ordered` pins 53.5 and 60.8, asserts `work < bottom`, and asserts more than 5 mm of clearance.

`bottom` is allowed to be `null` where `work` and `mix` are not: an unknown *limit* only means a check cannot be evaluated, so `Simulator` emits `cannot check well-bottom crash for tube_rack_1p5` and carries on; an unknown *commanded depth* is a refusal. Both halves are pinned by `tests/test_lab.py`.

Two entries do not use these keys at all. `tiprack_300_96` carries `approach: 47.0` and `pick: 59.0` and **no `work`**, so a bare `z_for("tiprack_300_96")` raises; `Machine.get_tip` asks for `"approach"` and `"pick"` explicitly. `reservoir_water` carries only `work: 40.0`.

Note that plate `work` 53.5 is 0.5 mm deeper than the 53.0 tipped traverse limit over raised location edges. That is legal - the limit forbids *crossing a location boundary* at that depth, not being at it inside a well.

**Note:** the "no Z key" message filters candidates with `if not k.endswith('note')`, which hides `note` and `gap_pos_note` but still advertises `superseded` (the p300 tip rack records `superseded: 57.5`) as if it were a usable key. Cosmetic only.

### `Effector.z_is_uncalibrated(labware_id: str) -> bool`

True when `spec["z_for_labware"][labware_id]["uncalibrated"]` is truthy - the value exists and will be commanded, but was derived or guessed rather than measured. Currently true for the p300 at `tube_rack_0p65_5x10` (45.0, "was marked `<-- TUNE` in the source and was never tuned") and `tube_rack_1p5`, and for both p125 entries.

**Note:** nothing in `lab/`, `protocols/` or `tools/` calls this method. The same information reaches the operator by a different route - `Effector.uncalibrated()` feeding the run manifest - so this is a convenience predicate awaiting a caller, not dead-but-load-bearing code.

### `Effector.needs_tip -> bool` (property)

`spec["tips"]["required"]`, default `False`. True for the p300, false for the p125. Read by `Machine.get_tip` (returns early when false) and by the guard that refuses to aspirate with no tip fitted.

### `Effector.tip_labware -> Optional[str]`

`spec["tips"]["labware"]` - `"tiprack_300_96"` for the p300, `None` for the p125. `Machine.get_tip` compares it against the placement it was handed and raises if they disagree, so a p300 cannot pick a tip out of a rack it was not calibrated against.

### `Effector.can_eject_tip -> bool`

`spec["tips"]["eject_supported"]`, default `False`. True for the p300. Gates `Machine.eject_tip` and the `--eject-tip` path in the protocols. Independent of the deck-side gate: `deck.stations.tip_eject.validated` is `false`, the routine has never run on hardware, and it stays off by default.

### `Effector.is_placeholder -> bool`

`spec["status"] == "placeholder"`. True for `gripper.json`, where every numeric field is `null`. `load()` refuses a placeholder unless asked not to.

### `Effector.uncalibrated() -> List[str]`

Returns the effector's own `uncalibrated` list, each entry prefixed with the effector id, e.g. `["p300.z_for_labware.tube_rack_0p65_5x10.work", "p300.z_for_labware.tube_rack_1p5.bottom"]`. `Machine` concatenates it with `Deck.uncalibrated()` so one call enumerates everything unmeasured in a run. This is a flat list maintained by hand in the JSON, not derived from the `uncalibrated: true` flags - the two can disagree if someone edits one and not the other.

### `Effector.feedrate(name: str, default: Optional[int] = None) -> Optional[int]`

Per-effector feedrate override in mm/min, from `spec["feedrates"]`. The p300 sets `tip_press: 600`; the p125 and gripper set none. Returns `int` or `None`. Callers use it as an override on the deck default: `eff.feedrate("tip_press") or self.deck.feedrate("tip_press")`. **Why per-effector:** the pressing speed that seats a 300 uL tip is a property of that tip and nosecone, not of the machine.

### `Effector.__repr__`

`<Pipette p300>`. For logs and test failure messages.

---

### `class Pipette(Effector)`

`Pipette(cfg: config.ConfigFile)` - `kind = "pipette"`. An air-displacement pipette: plunger POS arithmetic plus a linear volume calibration.

Plunger geometry is expressed in **POS**, a single-axis position in mm on the actuator. POS *decreases* as the plunger retracts and liquid is drawn up; `blowout_pos` is bottomed (full dispense plus blow-out) and `pos_min` is the hard stop. Legal band, used by `check_pos`, is `pos_min + stroke_margin <= POS <= blowout_pos`.

Fields read from `spec["actuator"]`, with p300 / p125 values:

| attribute | p300 | p125 | meaning |
|---|---|---|---|
| `blowout_pos` | 30.0 | 31.0 | plunger bottomed; full dispense plus blow-out |
| `air_gap` | 5.0 | 6.0 | trailing air gap, armed **in air** before descending |
| `gap_pos` | 25.0 | 25.0 | POS with the gap armed and no liquid; must equal `blowout_pos - air_gap` |
| `post_asp_backoff` | 1.0 | 1.0 | extra retract in air after leaving the water, anti-drip |
| `stroke_margin` | 1.0 | 1.0 | keep POS at least this far off the hard stop |
| `pos_min` | 0.0 | 0.0 | the hard stop |
| `settle_s` | 1.0 | 1.0 | dwell after a plunger move, seconds |
| `decimals` | 3 | 3 | rounding for every emitted POS and mm value |

From `spec["calibration"]`: `slope` (mm per uL), `intercept` (mm), `window` (a `(lo, hi)` tuple from `window_uL`). p300: 0.0533 / 0.122, window `(8.0, 320.0)`. p125: 0.1274 / 0.049, window `(10.0, 140.0)`. From the top level: `nominal_uL` (300.0 / 125.0).

`__init__` raises `EffectorError` twice:

1. `calibration.model != "linear_mm_per_uL"` - refuses rather than silently applying a linear fit to a curve that was not fitted linearly.
2. `gap_pos != blowout_pos - air_gap` - "the air gap is not what it claims to be." Three numbers in the config encode two degrees of freedom; the redundancy is checked at load so a typo cannot make every subsequent aspirate off by the difference.

It raises `KeyError` (not `EffectorError`) if `actuator`, `calibration`, `calibration.slope`, `calibration.window_uL` or `nominal_uL` are absent.

### `Pipette.equation -> str` (property)

`"d_mm = 0.0533 * V_uL + 0.122"` for the p300 - built from `self.slope` and `self.intercept`, the coefficients actually in use.

**Why it exists:** the config carries its own free-text `equation` string that nothing cross-checks, and in the scripts this replaced exactly that kind of duplicate drifted from its own table. Reports (`lab/logbook.py`, both protocols' banners) use this derived property, so the printed curve can never disagree with the arithmetic. `tests/test_review_fixes.py::test_9_equation_is_derived_from_the_coefficients` deletes the config's `equation` key and asserts the reported string is unchanged.

### `Pipette.displacement_mm(volume_uL: float) -> float`

Plunger travel in millimetres for a volume in microlitres: `round(slope * V + intercept, decimals)`.

- `volume_uL` (float, microlitres).

Returns mm, rounded to `decimals` (3). Raises `VolumeError` via `check_volume` before computing anything.

Real p300 values, pinned in `tests/test_lab.py::test_p300_calibration`: 30 uL -> 1.721 mm, 150 -> 8.117, 225 -> 12.114, 300 -> 16.112. p125: 12.5 -> 1.642, 62.5 -> 8.012, 93.75 -> 11.993, 125 -> 15.974.

**Why it exists / what it prevents:** this is the single conversion point named in the module docstring and in `CLAUDE.md`. Anything that needs a millimetre calls this; nothing else does the multiplication. That is what stops a displacement drifting out of step with the volume it claims to be.

### `Pipette.volume_uL(displacement_mm: float) -> float`

Inverse of the calibration, `(d - intercept) / slope`, rounded to 4 decimals. For reporting a measured or emitted stroke back as a volume.

**Note:** deliberately does *not* validate - it takes no `check_volume` path and will happily invert a displacement outside the fitted window. Fine for reporting; do not use it to decide whether a stroke is legal. It currently has no callers in the repo.

### `Pipette.aspirate_pos(volume_uL: float) -> float`

The plunger POS that holds this volume with the air gap already armed: `gap_pos - displacement_mm(V)`.

- `volume_uL` (float, microlitres).

Returns POS in mm, rounded to `decimals`. Raises `VolumeError` (from `displacement_mm`) or `EffectorError` (from `check_pos`, message ends `while aspirating <V> uL`).

p300: 30 uL -> 23.279, 150 -> 16.883, 225 -> 12.886, 300 -> 8.888.

**Why it starts from `gap_pos` and not `blowout_pos`:** the air gap is armed in air *before* descending into liquid, so the plunger is already at 25.0 when the draw begins. Measuring the stroke from `blowout_pos` would over-aspirate by the whole `air_gap`. (Contrast `Machine.mix`, which starts bottomed and so validates `blowout_pos - d` instead - mixing carries no air gap.)

### `Pipette.backoff_pos(volume_uL: float) -> float`

POS after the anti-drip retract: `aspirate_pos(V) - post_asp_backoff`. Validated by `check_pos` with `what="post-aspirate backoff at <V> uL"`.

**Why it is a method and not a subtraction at the call site:** this is the POS the machine actually ends up at, and it is the deepest point of the plunger stroke, so it is the one that must be checked. Every consumer - `Machine._aspirate_at`, `Pipette.table` - goes through here, which means no reported or emitted POS can be one `check_pos` would reject. `tests/test_review_fixes.py::test_5b_table_reports_only_legal_positions` pins that for `table()`.

### `Pipette.max_volume_uL -> float` (property)

Largest volume the plunger has stroke for, in microlitres:

```
gap_pos - pos_min - stroke_margin - intercept - post_asp_backoff
--------------------------------------------------------------- 
                        slope
```

p300: `(25.0 - 0.0 - 1.0 - 0.122 - 1.0) / 0.0533 = 429.2308 uL`. p125: `(25.0 - 0.0 - 1.0 - 0.049 - 1.0) / 0.1274 = 180.1491 uL`. Both pinned in `tests/test_lab.py`. With the stock configs `pos_min` is 0.0 and these sit above the calibration windows (320 and 140), so the window is what actually binds - the stroke limit matters when `pos_min` is nonzero or a window is widened.

**Why it subtracts both `pos_min` and `post_asp_backoff`:** two separate past defects, one test each.

- **`post_asp_backoff`.** The old script asserted `gap_pos - d >= margin`, which validates the *aspirate* POS - a position the machine passes through and then leaves, because it immediately retracts a further 1.0 mm in air. The constrained position is the one *after* the backoff. `tests/test_lab.py::test_max_volume_accounts_for_backoff` computes the backoff-ignoring bound and asserts the real limit is more than 15 uL below it.
- **`pos_min`.** The floor `check_pos` enforces is `pos_min + stroke_margin`, not `stroke_margin` alone. Omitting `pos_min` made `check_volume` accept volumes that `check_pos` then rejected mid-primitive - a raise after gcode had already been emitted. `tests/test_review_fixes.py::test_5_max_volume_respects_pos_min` sets `pos_min = 6.0`, asserts the max drops to ~316.66, and then walks every volume from the window floor to that max through `backoff_pos` to prove the two checks now agree exactly.

The rule this encodes: **`check_volume` must accept only volumes whose entire stroke, backoff included, survives `check_pos`.** If you touch either, run both tests.

### `Pipette.check_volume(volume_uL: float) -> None`

Two-stage validation; returns `None`, raises `VolumeError`.

1. Outside `window` (with a 1e-9 tolerance) - "The curve was never fitted there; the displacement would be an extrapolation." For the p300 that rejects 5 uL and 400 uL, both pinned in `tests/test_lab.py::test_volume_window`.
2. Above `max_volume_uL` - "needs more stroke than there is", naming the margin off the hard stop.

The window check comes first, so a volume that fails both reports the calibration reason - the more useful one, since widening the window is a measurement job while stroke is a mechanical fact.

### `Pipette.check_pos(pos: float, what: str = "move") -> None`

Validates a single plunger POS against `[pos_min + stroke_margin, blowout_pos]`, both with 1e-9 tolerance. Returns `None`, raises `EffectorError` naming `what`, the offending POS, and the bound it broke (with `pos_min` and `stroke_margin` spelled out so the operator knows which config field to look at).

- `pos` (float, mm on the plunger axis).
- `what` (str): a phrase describing the operation, interpolated into the message. Callers pass e.g. `"aspirating 150.0 uL"`, `"mixing 200.0 uL"`.

**Why it exists:** `pos_min + stroke_margin` keeps the plunger off its hard stop - crashing a stepper into a mechanical stop loses position silently and every subsequent volume is wrong. The upper bound catches a POS past blowout, which would be commanding travel the actuator does not have. Called by `aspirate_pos`, `backoff_pos`, `prewet` positions via the machine, and directly by `Machine.mix` for the top of its stroke.

Note the p125's `stroke_margin` of 1.0 is not measured - its old script defined no margin and only asserted `POS >= 0`. The config records that 1.0 was copied from the p300 and that this can only *tighten* the check, never loosen it.

### `Pipette.prewet_enabled -> bool`

`spec["prewet"]["enabled"]`, default `False`. True for the p300, false for the p125 (whose old script performs no prewet at all). `Machine.prewet` raises `ProtocolError` if called when this is false - "enable it there rather than calling prewet() anyway."

### `Pipette.prewet_cycles -> int`

`spec["prewet"]["cycles"]`, default 0. p300: 3. The default when `Machine.prewet(cycles=None)`.

### `Pipette.prewet_min_cycles -> int`

`spec["prewet"]["min_cycles"]`, default 0. p300: 3. A **floor**, not a default: `Machine.prewet` refuses fewer than this even when the caller asks explicitly. Prewetting fewer times than the calibration was fitted with changes the delivered volume, so the protocol is not allowed to shorten it silently.

### `Pipette.prewet_asp_pos -> float`

The POS the plunger draws to on each prewet cycle. Returns `spec["prewet"]["asp_pos"]` when present and non-null, otherwise derives `blowout_pos - prewet.displacement_mm`. For the p300 both routes give 10.0 (30.0 - 20.0).

**Why measured from `blowout_pos` and not `gap_pos`:** prewetting happens with no air gap armed - the plunger starts bottomed, draws, and pushes back. That is a different reference from `aspirate_pos`, and the config says so explicitly (`asp_pos_note: "measured from BOTTOMED, not from gap_pos"`).

**Note:** the fallback branch does `float(p["displacement_mm"])` unguarded, so on an effector whose `prewet` block has neither `asp_pos` nor `displacement_mm` - the p125, which has only `enabled: false` - this raises `KeyError` rather than a `LabError`. Unreachable today because `Machine.prewet` checks `prewet_enabled` first, but it is one refactor away from being a raw `KeyError` in front of the operator.

### `Pipette.table(volumes: List[float]) -> List[Dict[str, float]]`

One dict per volume with keys `volume_uL`, `displacement_mm`, `aspirate_pos`, `backoff_pos`. Raises whatever `displacement_mm` / `aspirate_pos` / `backoff_pos` raise on the first bad volume.

**Why `backoff_pos` and not `aspirate_pos - post_asp_backoff`:** going through the validating method means the table cannot print a POS the machine would refuse to move to. A report that shows a legal-looking number for an illegal move is worse than an exception. Used by `tests/smoke_geometry.py` over `fractions([0.10, 0.50, 0.75, 1.00])`.

### `Pipette.fractions(fracs: List[float]) -> List[float]`

Test volumes as fractions of `nominal_uL`, each rounded to 4 decimals. `fractions([0.1, 0.5, 0.75, 1.0])` gives `[30.0, 150.0, 225.0, 300.0]` on the p300 and `[12.5, 62.5, 93.75, 125.0]` on the p125.

**Why it exists:** the ISO accuracy-and-precision points are defined as fractions of nominal, so the same protocol runs unchanged on either pipette and lands on that pipette's correct volumes. `protocols/accuracy_test.py` builds its volume series through this. The values it returns are not themselves validated - `displacement_mm` does that later - and note that the 10% p125 point shifts about 1.8% of its target versus the old hardcoded table, which is recorded in `p125.json -> calibration.note` precisely because it is the ISO point where it matters.

---

### `class Gripper(Effector)`

`Gripper(cfg: config.ConfigFile)` - `kind = "gripper"`. **Not built.** `gripper.json` has `status: "placeholder"` and every numeric field `null`, so `load("gripper")` refuses unless `allow_placeholder=True`.

Attributes: `open_pos`, `closed_pos`, `grip_pos` (raw from `spec["actuator"]`, all `None` today, deliberately *not* coerced with `float()` so the class can be constructed for inspection); `carries_labware` (bool, default `True`); `held` (`Optional[str]`, initialised `None`).

**Why the class exists at all:** so `load()` has something to dispatch to and `available()` lists the file. That is the whole of the integration - it has no motion methods, `lab/deck.py` does not reference effectors, and `lab/simulate.py` validates plunger POS only for `Pipette`, so a gripper POS would reach disk unvalidated. `tests/test_lab.py::test_gripper_is_a_placeholder` pins both halves: the refusal for motion, and the successful dispatch to this class under `allow_placeholder=True`.

**Why `held` is here and not on `Machine`:** a gripper changes the deck *layout* at run time - picking a plate empties a slot and fills the effector - which a pipette never does. Naming the state now marks where that tracking will have to live. Nothing writes `held` today, and nothing reads `carries_labware`; both are declarations of intent. `gripper.json -> todo_before_use` lists what building this actually requires (measure the positions, add `grip()`/`release()`/`move_labware()` to `Machine`, teach `Simulator` to validate a gripper POS and track held labware, decide whether it drives `ACTUATOR_MOVE` or its own macro).

### `Gripper.require_ready() -> None`

Raises `EffectorError` listing whichever of `open_pos`, `closed_pos`, `grip_pos` are still `None`, naming the file to measure them into. Returns `None` when all three are set.

**Note:** no caller exists yet. It is the guard the future `grip()` / `release()` primitives are expected to call first, so that motion generation cannot begin from placeholder numbers.

---

### `load(effector_id: str, allow_placeholder: bool = False) -> Effector`

The only way a protocol should obtain an effector. Loads `config/effectors/<effector_id>.json` and returns an instance of the class registered for its `kind`.

- `effector_id` (str): the file stem, e.g. `"p300"`. Must appear in `config.available_effectors()`.
- `allow_placeholder` (bool): permit an effector whose `status` is `"placeholder"`. **Pass `True` only to inspect the file, never to generate motion.**

Returns a `Pipette` or `Gripper` (`_KINDS = {"pipette": Pipette, "gripper": Gripper}`). Raises `EffectorError` for an unknown id (listing what is available), an unrecognised `kind` (telling you to add a class to this module), or a placeholder without the override. Constructor validation - the calibration model, the air-gap identity - fires here too, so a bad config is caught at load rather than at the first aspirate.

**Why the placeholder gate:** the failure mode being blocked is generating a full gcode file from `null`-derived numbers and only discovering it at the machine. `tests/test_lab.py::test_unknown_effector` and `::test_gripper_is_a_placeholder` cover both refusals.

### `available() -> List[str]`

Sorted ids of every effector JSON on disk - `["gripper", "p125", "p300"]` today. Discovery only; says nothing about whether an id is usable (`gripper` is listed and will not load for motion).

Used as the `choices` for the `--effector` argument in `protocols/accuracy_test.py` and `protocols/centrifuge_test.py`, so adding a file to `config/effectors/` makes it selectable on the command line with no code change. That is the "one JSON per end effector" seam, pinned by `tests/test_lab.py::test_one_json_per_effector`.

Respects the `LAB_CONFIG` environment variable through `config.config_dir()`, which is how the tests point the whole library at a copied config tree.

---

## `lab/deck.py`

The resolved deck: the machine envelope, the 3x3 slot grid, the fixed stations,
the keep-out bands, and everything currently loaded. `Deck.load()` reads
`config/deck.json` + `config/labware.json` + `config/layout.json`, resolves every
placement to machine coordinates at construction time, and refuses to build a deck
whose config contradicts itself. Everything downstream (`lab/machine.py` for
routing, `lab/simulate.py` for checking, `lab/logbook.py` for the manifest) asks
this object rather than carrying coordinates of its own, which is why a protocol
can say `deck.placement("plate1").well("A1")` and never contain a literal.

Two things about this module are worth holding in mind before reading any single
method. First, **+Z is DOWN**: every Z here is a commanded Z, `Z=0` is home at the
top, and "higher" always means a *smaller* number. Second, the module makes a hard
distinction between *derived* and *stored* geometry. The slot pitch, the stage
region, the slot cells and the bare-nosecone edge limit are all computed from the
slot table and from single measurements, specifically so they cannot drift out of
step with their source. Where the config does duplicate a coordinate for human
convenience (`slot_center` in a placement), `__init__` holds the copy to the
original and raises if they disagree.

---

### `class DeckError(LabError)`

```python
class DeckError(LabError)
```

Raised for a deck the config cannot describe: an unknown placement, station, slot,
macro or feedrate name; a layout referencing labware that is not in
`labware.json`; two placements with the same name; two placements claiming the same
slot; a placement whose `slot_center` copy disagrees with `deck.json`.

**Why:** it subclasses `LabError` (from `lab/effector.py`) along with
`EffectorError`, `VolumeError`, `ProtocolError` and `SimulationError`, so a
protocol can wrap a whole run in one `except LabError` and still get a specific
type when it wants one. `tests/test_review_fixes.py::test_8_all_errors_share_a_base`
pins that.

Note the asymmetry that comes out of `lab/labware.py`: config errors *inside* a
placement (a missing `corners` block, a stale `corners.solved`, an unresolvable
A1) surface as `KeyError` / `ValueError` from `build_placement`, not as
`DeckError`. Only the deck-level integrity checks raise `DeckError`.

---

### `class Axis`

```python
@dataclass(frozen=True)
class Axis:
    name: str
    min: float
    max: float
    home: float
```

One kinematic axis, read straight from `deck.json` -> `axes`. Values in mm.
Current deck: `x = Axis("x", 0.0, 420.0, 420.0)`, `y = Axis("y", 0.0, 580.0, 0.0)`,
`z = Axis("z", 0.0, 270.0, 0.0)`.

**Why:** X homes at its *maximum* (420) and counts down toward 0, so `home` cannot
be assumed equal to `min`. Keeping `home` as a stored field rather than deriving it
is what keeps that fact in the data. The Z axis here is the *kinematic* limit only
(0-270); the real Z limit is effector-dependent and much shallower, and lives in
`lab/simulate.py` (`effective_z_limit`) built from `z_stage_surface` and the tip
length.

#### `Axis.contains(self, v: float, tol: float = 1e-6) -> bool`

Is `v` (mm) inside `[min, max]`, allowing `tol` mm of slack at each end?
Returns `bool`. Raises nothing.

**Why the tolerance:** commanded coordinates are the result of arithmetic on
measured values (pitch solves, half-pitch expansions, rounding to 4 dp), so an
exact endpoint like X 420 or X 0 can land a fraction of a micron outside. Without
the slack, a legal move to the home position would intermittently read as an
envelope violation. `Machine._route_problem` uses `deck.x.contains` /
`deck.y.contains` to reject dogleg waypoints, so this is on the routing hot path.

---

### `class KeepOut`

```python
@dataclass(frozen=True)
class KeepOut:
    id: str
    x_range: Tuple[float, float]
    forbids: str
    safe_when_z_below: Optional[float]
    whitelist: Tuple[str, ...]
    description: str = ""
    tipped_is_error: bool = True
```

An X band in which Y motion collides with a mechanism unless the effector is high
enough. There is exactly one on the current deck, from `deck.json` -> `keepouts`:

- `id` `"tip_ejector"`
- `x_range` `(148.0, 162.0)` mm - the ejector lane X 155 plus/minus 7.0
- `forbids` `"y_motion"`
- `safe_when_z_below` `60.0` - a commanded Z, stated for the p300 with **no tip**
- `whitelist` `("eject_tip",)`

**Why the field is named `safe_when_z_below`:** because +Z is down, a *smaller*
commanded Z is physically *higher*, so the band is safe when `z < 60.0`. The name
is deliberately phrased in the commanded-Z direction rather than as "safe above",
which would read correctly in English and evaluate backwards in code.

**The physical constraint the band encodes:** the 14 mm width is carried over from
a previously documented band centred on X 85 and is flagged in
`deck.json` -> `uncalibrated` as unverified at this location. The lane X 155 itself
is operator-confirmed and already in the post-`+70` frame. `x_range` is the single
source of truth - do not re-derive it from `lane_x`.

**Note:** `tipped_is_error` is never populated from config (`Deck.__init__` builds
`KeepOut` without it) and is never read anywhere in the repo. The behaviour it
names is hard-coded in `Simulator._check_keepout`, which fails *any* tipped Y move
through the band regardless: `safe_when_z_below` was measured bare, a fitted tip
reaches 52.25 mm lower, and the tipped limit was never measured. The field is dead
and a config author setting it will see no effect.

#### `KeepOut.contains_x(self, x: float) -> bool`

Is this single X coordinate (mm) inside the band, inclusive of both edges?
Returns `bool`. Raises nothing.

**Why:** this is the test for a **Y** move, because a Y move happens at one fixed
X. `Deck.keepouts_hit_by_y_move` is the only caller.

#### `KeepOut.spans_x(self, x0: float, x1: float) -> bool`

Does a straight X move from `x0` to `x1` (mm, either order) enter the band at all?
Returns `bool`. Raises nothing.

**Why:** the interval test, as opposed to `contains_x`'s point test. It normalises
the direction itself, so callers do not have to know whether the move is toward
higher or lower X - relevant on a machine whose X counts down from home.

#### `KeepOut.z_is_safe(self, z: float) -> bool`

Is a commanded `z` (mm) high enough to clear this mechanism?
Returns `z < safe_when_z_below`, and **`False` when `safe_when_z_below` is `None`**.

**Why the `None` case returns `False`:** an unmeasured safe height is not a pass.
A band with no measured clearance is treated as never safe, so a missing
measurement blocks the move instead of silently permitting it. This is the same
rule as convention 3 in `CLAUDE.md`, applied to a boolean instead of a warning.

---

### `class Station`

```python
@dataclass(frozen=True)
class Station:
    name: str
    xy: Vec2
    z: Optional[float]
    spec: Dict[str, Any] = field(default_factory=dict)
```

A single fixed XY position on the machine that is not a slot. `xy` is mm; `z` is a
commanded Z or `None` when the station has no single meaningful Z. `spec` is the
whole raw `deck.json` station block, so per-station extras stay reachable without
this dataclass having to grow a field per station.

Current stations:

| name | xy | z | notes |
|---|---|---|---|
| `water` | `(70.0, 175.0)` | `None` | a **dip point**, not the trough centre. Submerge depth is per-effector (`effectors/*.json` -> `z_for_labware.reservoir_water.work`). Was X 0 pre-shift; `+70` applied. |
| `park` | `(420.0, 0.0)` | `0.0` | X 420 is X home; the station always meant "go home in X". Was X 350 pre-shift. |
| `tip_eject` | `(155.0, 89.0)` | `None` | the whole routine lives in `spec`: `lane_x` 155.0, `approach_y` 83.0, `engage_y` 88.0, `retract_y` 80.0, `z_below_hook` 145.0, `z_strip` 137.0, `rehome_after` true, `validated` **false**. |

**Why `z` is optional:** for `water` the working Z depends on which effector and
tip is fitted, and for `tip_eject` there are two Zs (145 then 137), so a single
number would be a lie in both cases. `park` genuinely has one: Z 0, home.

**Why `spec` is kept:** `tip_eject` carries seven routine-specific values and a
`validated: false` flag. Promoting each to a `Station` field would mean every other
station carrying six `None`s, and `Deck.uncalibrated()` reads `validated` out of
the raw dict anyway. `Z145 -> Z137` is an 8 mm **upward** stroke - that is the
strip.

---

### `class Deck`

```python
class Deck:
    def __init__(self, deck_cfg: config.ConfigFile,
                 labware_cfg: config.ConfigFile,
                 layout_cfg: config.ConfigFile) -> None
```

Resolves three config files into one queryable object. Parameters are
`ConfigFile`s (from `lab/config.py`), which carry both the parsed JSON and the
sha256 of the bytes it came from - the hashes go into the run manifest so a
generated `.gcode` can be traced back to the exact config.

Attributes after construction: `id`, `raw` (the whole deck dict), `x`/`y`/`z`
(`Axis`), `feedrates` (`{"travel": 6000, "hold": 3000, "tip_press": 600,
"wipe": 1500}`), `z_home` 0.0, `z_travel` 5.0, `z_stage_surface` 114.75,
`slots` (`{"1": (336.0, 365.0), ...}` keyed by **string**), `stations`,
`keepouts`, `macros`, `catalogue` (labware id -> `Labware`), `placements` (name ->
`Placement`), `open_questions`.

**Raises `DeckError`** during construction when: a layout placement names labware
absent from `labware.json`; two placements share a name; two placements claim the
same slot (`_check_slot_occupancy`); a placement's `slot_center` convenience copy
disagrees with `deck.json` (`_check_recorded_slot_center`). Propagates `KeyError`
and `ValueError` out of `lab/labware.py` for placement-level config faults.

**Why it resolves everything up front:** a placement that cannot be resolved is a
config bug, and the cheapest place to discover it is before a single line of gcode
exists. Nothing reaches disk until simulation passes, and nothing gets as far as
simulation if the deck will not build.

One implementation detail worth knowing: for slot-mounted labware dimensioned from
the location edges (`a1_from_slot_edge`, which `tube_rack_1p5` uses), `__init__`
injects `_slot_pitch` into a **copy** of the layout entry before calling
`build_placement`, because the cell size is the slot pitch and only the `Deck`
knows it. `lab/labware.py` raises if that injection is missing.

#### `Deck.load(cls, layout: str = "layout.json") -> Deck`

Classmethod. Loads `deck.json`, `labware.json` and the named layout from the
config directory (`config/`, or `$LAB_CONFIG`).

**Parameter:** `layout` - filename; `lab/config.load_layout` appends `.json` if
absent, so `"layout_test"` works.

**Returns** a fully resolved `Deck`. **Raises** `FileNotFoundError` for a missing
file, `ValueError` for invalid JSON, `DeckError` for the integrity failures above.

**Why the layout is a parameter:** `layout.json` is the per-run file - the
operator's actual bench, which changes between runs. Tests that assert *behaviour*
pass `layout_test.json`, a fixture holding every piece of labware at once; only the
handful of tests that deliberately assert the shipped bench call `Deck.load()` with
no argument. Today the shipped bench is `tips` slot 3, `big_tubes` (1.5 mL) slot 5,
`plate1` slot 1, `plate2` slot 2, `water` at the water station near slot 9. The
0.65 mL holder is **off** the deck, so `protocols/accuracy_test.py` refuses until
it is put back and re-added.

---

#### `Deck.placement(self, name: str) -> Placement`

The resolved placement with this layout name (`"tips"`, `"big_tubes"`, `"plate1"`,
`"plate2"`, `"water"`).

**Raises `DeckError`** listing every loaded placement name. **Why the listing:**
the failure mode is a protocol written against a different bench - naming `"tubes"`
when only `big_tubes` is loaded - and the fix is always "look at what is actually
on the deck", so the error carries it.

#### `Deck.placement_for_labware(self, labware_id: str) -> Placement`

The single placement using this labware id (e.g. `"tiprack_300_96"`).

**Raises `DeckError`** if nothing uses it (listing the labware ids in play), and
also if **more than one** placement does, naming them and telling the caller to ask
by name instead.

**Why the ambiguity is an error rather than a first-match:** the shipped bench has
two `sbs_96_wellplate` placements, `plate1` and `plate2`. Returning either one
would put liquid in a real plate, in the right kind of well, on the wrong plate -
exactly the class of mistake this repo exists to prevent. `Machine._tip_placement`
uses this as a *fallback* when the effector's declared tip placement name is not in
the layout, which is safe because only one tip rack is ever loaded.

#### `Deck.station(self, name: str) -> Station`

The named `Station`. **Raises `DeckError`** listing the known station names
(`park`, `tip_eject`, `water`).

#### `Deck.slot_center(self, slot: int) -> Vec2`

Centre `(x, y)` in mm of slot `1`-`9`. Takes an **int** and stringifies it
internally, because `deck.json` keys the slots as JSON strings.

**Raises `DeckError`** listing the known slots.

The layout that keeps reading backwards: slots **1-3 are the FRONT row** at the
*largest* Y (365), 4-6 the middle (272), **7-9 the BACK row** at the smallest Y
(178.5). Within each row X *descends*: 336 / 200 / 65. So slot 1 is
`(336, 365)` and slot 9 is `(65, 178.5)`. Per-slot centres are stored rather than
derived from a pitch because the real frame is irregular by about 1 mm (X pitch
136 then 135; Y pitch 93.0 then 93.5) and deriving them would erase that.

#### `Deck.macro(self, name: str) -> str`

The Klipper macro prefix declared under `deck.json` -> `klipper_macros`:
`actuator_move` -> `"ACTUATOR_MOVE POS="`, `home_actuator` ->
`"HOME_ACTUATOR"`, `log_temps` -> `"LOG_TEMPS TAG="`.

**Raises `DeckError`** listing the declared names, with the `"note"` key filtered
out of the listing (it is prose, not a macro).

**Why it must raise rather than default:** this is defect 13 in
`tests/test_review_fixes.py`. The code used to fall back to a hardcoded literal, so
a mistyped or deleted `klipper_macros` entry emitted the built-in string instead of
failing. The generated gcode looked right and addressed nothing - the macro has to
exist in `printer.cfg`, and the config is the only place that correspondence is
recorded. `test_13_macros_must_be_declared` deletes the entry and asserts the
raise.

#### `Deck.feedrate(self, name: str) -> int`

A deck-wide feedrate in mm/min: `travel` 6000, `hold` 3000, `tip_press` 600,
`wipe` 1500. **Raises `DeckError`** listing the known names.

**Why deck-level:** an effector may override any of these in its own JSON
(`Effector.feedrate`), and `Machine.get_tip` reads
`eff.feedrate("tip_press") or self.deck.feedrate("tip_press")` - effector first,
deck fallback. That way a gentler pipette lowers its own press speed in one place
without every effector having to restate all four.

---

#### `Deck.in_envelope(self, x: float, y: float, z: float) -> List[str]`

Checks a commanded point (mm) against the kinematic envelope X 0-420, Y 0-580,
Z 0-270.

**Returns** a list of human-readable violation strings, e.g.
`"Z 300.0 outside [0.0, 270.0]"`. **Empty list means fine.** Raises nothing.

**Why a list rather than a bool or an exception:** the caller decides severity.
`Simulator._check_move` turns any non-empty result into a hard failure, but it
wants all three violations in the message, not just the first, so the operator sees
the whole problem in one pass. Returning strings also keeps the axis bounds out of
the simulator.

**This is the loosest of the Z checks, not the operative one.** Z 270 is the
kinematic limit; the effector strikes the stage at Z 114.75 bare and Z 62.5 with a
300 uL tip. `Simulator.effective_z_limit` is the check that matters, and it warns
explicitly when it cannot be evaluated.

#### `Deck.no_fly_rects(self) -> List[Tuple[str, Tuple[float, float, float, float]]]`

Placements a traverse must **never** cross while a tip is fitted.

**Returns** `[(placement_name, (x_min, x_max, y_min, y_max))]` in mm. On the
shipped bench: `[("tips", (13.4545, 122.5455, 330.95, 403.75))]`. Raises nothing.

Selection is by the labware's `avoid_traverse` flag, read from the raw spec. Only
`tiprack_300_96` sets it. The rectangle is `Placement.footprint_bounds()` when a
footprint is measured; the tip rack has none, so it falls back to the outermost
well centres (X 18-118, Y 335.5-399.2) expanded by half the solved grid pitch
(9.0909/2 and 9.1/2), which approximates the rack body.

**The physical constraint, and why this is a separate mechanism from every Z
check:** the tips stand upright well above the rack body, and the fitted tip hangs
52.25 mm below the nosecone. Crossing the rack with a tip fitted shears the tip off
at **any** travel height. There is no Z you can retreat to - flying higher raises
the nosecone and the tip with it. So this is a routing constraint, not a clearance
constraint, and both consumers treat it as absolute: `Machine._route_xy` doglegs
around the rectangle (`_dogleg_x_candidates` / `_dogleg_y_candidates` pad it by
6 mm and try waypoints nearest the target first, recording `"dogleg via Y"` as a
warning), and `Simulator._check_move` hard-fails any tipped XY step whose swept box
intersects it. `op == "get_tip"` is exempt, because the pickup itself necessarily
happens over the rack.

Observed on hardware 2026-09-08: the water station at X 70 sits *underneath* this
rack in X, so every unrouted approach to water from high Y went straight through
it. That is what the routing exists for, and
`tests/test_lab.py::test_leaving_the_tip_box_goes_toward_x_zero_then_y` and
`test_router_doglegs_in_y_to_cross_the_tip_rack` pin it.

**Note:** the fallback expansion uses only `abs(col_vec[0])/2` for X and
`abs(row_vec[1])/2` for Y. For a rack rotated 90 degrees those components are zero,
so the box would not be expanded at all on the axis where the pitch actually lies,
understating the obstacle. The trailing `or 0.0` on both lines is a no-op (it can
only replace `0.0` with `0.0`). Neither matters for the current tip rack, whose
corner solve yields axis-aligned vectors, but a rotated no-fly labware would need
this generalised to the full vector magnitudes.

#### `Deck.segment_hits_rect(frm: Vec2, to: Vec2, rect: Tuple[float, float, float, float]) -> bool`

Staticmethod. Does an axis-aligned move from `frm` to `to` (mm) sweep through
`rect` (`x_min, x_max, y_min, y_max`)?

**Returns** `bool`; touching an edge exactly counts as a hit (the comparisons are
strict on the *separated* case, so equality is an intersection). Raises nothing.

**Why the swept box is the right model:** `Machine._move_xy` emits **one axis per
step** - X first then Y, or Y first then X, never diagonal (`deck.json` ->
`conventions.xy_order`). So the region a single step sweeps *is* the axis-aligned
box between its two endpoints, and this test is exact rather than conservative for
the moves the library can actually produce. If a diagonal move ever appeared, the
box would over-approximate it, which errs toward refusing a legal move rather than
permitting an illegal one.

**Why test the segment and not the destination:** the destination test misses the
common case entirely. A traverse that starts over the tip rack and ends at the
water reservoir crosses the whole rack while neither endpoint is on it. Same
reasoning drives `Simulator._check_traverse_clearance`.

#### `Deck.keepouts_hit_by_y_move(self, x: float) -> List[KeepOut]`

Which keep-out bands does a Y move at this fixed X (mm) enter? Filters to
`forbids == "y_motion"` and `contains_x(x)`. Returns a possibly-empty list; raises
nothing.

**Why X is the only argument:** a Y move happens at one X, so band membership is a
point test on X and the Y endpoints are irrelevant. Both callers pass the move's
*starting* X (`step.frm[0]` in the simulator, `frm[0]` in the router) - correct,
since a single step never changes X and Y together.

#### `Deck.keepouts_crossed_by_x_move(self, x0: float, x1: float) -> List[KeepOut]`

Which bands does a straight X move from `x0` to `x1` (mm) pass through? Returns a
possibly-empty list; raises nothing.

**Note:** nothing in the repo calls this today, and unlike
`keepouts_hit_by_y_move` it does **not** filter on `forbids`. That is arguably
right - the current band forbids `y_motion`, so an X move straight through it is
legal and this method would report it anyway - but a caller must therefore check
`k.forbids` itself. A protocol author should not need this; the router already
handles band avoidance.

---

#### `Deck.slot_seating_report(self) -> List[str]`

Does each slot-mounted labware actually sit in the slot it claims?

**Returns** one string per suspect placement, naming the claimed slot centre, the
resolved grid centre, the signed offset, the distance, and whether the layout
already flags it `slot_disputed`. **Empty means every claim checks out**, which is
the case on both the shipped and the test layout. Raises nothing.

For each placement with `slot is not None` **and** `mount == "slot"`, it compares
`Placement.grid_center()` against `slot_center(slot)` and reports when the radial
distance exceeds `max(px, py) / 2` = **67.75 mm** on the current frame. Placements
mounted `fixed` or at a `station` are skipped deliberately: `water` carries
`near_slot: 9` as a human reference, not a seating claim, because its stored
coordinate is a dip point `(70, 175)` rather than the trough centre.

**Why it exists:** a slot label is a claim about physical seating, and a claim
nothing checks can be wrong for months. This is also the check that catches a
coordinate-frame error. The `+70` X shift was confirmed exactly this way: the
0.65 mL tube rack unshifted spans X 78-186, straddling the slot 8/9 cell seam at
X 132.25 and seated in neither; shifted to X 148-256 it fits entirely inside slot
8's cell of `[132.25, 267.75] x [131.875, 225.125]`, which is where the operator
says it is. `Simulator` folds each line into its report as a warning prefixed
`"slot seating: "`, and `lab/logbook.py` records the whole list in the run
manifest.

**Note:** the local names `hw, hh` hold the **full** pitches `(135.5, 93.25)`, not
halves, and the threshold takes the larger of the two and halves it, applying
67.75 mm to both axes. The consequence is that the Y direction is checked loosely:
a placement misseated by up to 67.75 mm in Y passes, although the Y slot pitch is
only 93.25 mm. Tightening it to a per-axis test (`abs(dx) <= px/2 and
abs(dy) <= py/2`) would catch a row-off misseating that the radial test can let
through. Neither the shipped nor the test layout is anywhere near the threshold, so
this is latent rather than active.

#### `Deck.near_slots(self) -> Dict[str, int]`

Placements carrying a `near_slot` reference rather than a seating claim.

**Returns** `{placement_name: slot_number}` - `{"water": 9}` on both layouts.
Raises nothing.

**Why `near_slot` is a different key from `slot`:** it is documentation for a human
("the water trough is over by slot 9"). Keeping it separate is what lets
`slot_seating_report` be strict about the claims that *are* claims, and stops
`_check_slot_occupancy` treating a human note as an exclusive claim on slot 9. The
water dip point is 5.0 mm in X and 3.5 mm in Y from slot 9's centre - close enough
to be worth writing down, not close enough to be a seating.

#### `Deck.overlap_report(self) -> List[str]`

Pairwise footprint intersection between every pair of placements.

**Returns** a list of strings: `"OVERLAP: a footprint intersects b footprint"` for
a real collision, or `"cannot check a vs b: no footprint measured for ..."` naming
the placements that lack one. Raises nothing.

**Why the "cannot check" line instead of skipping:** an unmeasured footprint must
not read as a pass. Only `sbs_96_wellplate` has a measured footprint
(127.76 x 85.48 mm), so on the shipped bench every pair involving `tips`,
`big_tubes` or `water` comes back as a cannot-check line and only `plate1` vs
`plate2` is actually evaluated. `Simulator` promotes `OVERLAP` lines to errors and
everything else to warnings.

#### `Deck.uncalibrated(self) -> List[str]`

Every field the config **admits** was never tuned.

**Returns** dotted strings, mixing three sources: per-labware
(`f"{placement}({labware_id}).{field}"` from each `Labware.uncalibrated`), the deck
itself (`"deck.z.stage_surface (never measured)"` when unmeasured, plus each entry
of `deck.json` -> `uncalibrated`), and any station with `"validated": false`
(`"deck.stations.tip_eject (routine never validated on hardware)"`). Raises
nothing.

**Why it reads a structured list rather than grepping prose:** as the comment in
the source says, rewording a comment must not be able to make an uncalibrated field
silently look calibrated. The honesty of the whole system rests on these lists
being data. This is the method behind the one-liner in `CLAUDE.md` that you run
before treating any value as known, and `tests/smoke_geometry.py` prints it.

The `tip_eject` line is the important one: `validated: false` means the ejection
routine has **never been run on hardware**, has no working reference gcode
anywhere in the repo, and is off by default.

---

#### `Deck.stage_region(self) -> Optional[Tuple[float, float, float, float]]`

The `(x_min, x_max, y_min, y_max)` rectangle over which the stage surface exists,
in mm.

**Returns** `(-2.75, 403.75, 131.875, 411.625)` on the current slot table - the
slot centres (X 65-336, Y 178.5-365) expanded by half the slot pitch
(135.5/2 = 67.75 and 93.25/2 = 46.625). Returns `None` if the deck declares no
slots. Raises nothing.

**Why the stage surface only exists over the slot area, and why that matters:** the
stage is the plate the nine locations are cut into. It does not extend to the front
of the machine. Off that rectangle there is simply nothing beneath the effector at
stage depth, so the Z 114.75 stage-contact limit does not apply. That is precisely
what makes the tip ejector legal: it lives at X 155, Y 80-89, which is more than
40 mm in front of the stage's front edge at Y 131.875, and so the routine can
command **Z 145 and Z 137** - both far *below* the stage surface - without hitting
anything. `tests/test_lab.py::test_ejector_is_off_the_stage` asserts exactly that
for all three of `approach_y`, `engage_y` and `retract_y`, and
`Simulator._check_move` re-derives it rather than taking it on trust: it warns
`off_stage_depth` whenever a move goes past Z 114.75 off-stage, saying in the
warning that this is only safe if nothing is under the effector there.

**Why derived and not stored:** a stored rectangle would drift out of step with the
slot table the first time a slot centre was corrected. Deriving it means the frame
correction propagates.

**Note:** `x_min` comes out at -2.75, outside the X envelope minimum of 0, so in
practice every reachable X below 403.75 counts as on-stage. That is harmless and
arguably correct (the stage plate presumably does extend past the outermost slot
edge), but it does mean the region is a bounding box, not a measured outline: it
cannot represent a stage with a notch, and X 420 - the park/home column - reads as
off-stage.

#### `Deck.point_on_stage(self, x: float, y: float) -> bool`

Is `(x, y)` (mm) over the stage surface? Inclusive on all four bounds; **`False`
when there are no slots** (no stage region means nothing is on it). Raises nothing.

**Why:** it is the single gate on whether the stage-depth Z limits apply at all.
`Simulator._check_move` only computes `effective_z_limit` for on-stage points, and
only warns about off-stage depth otherwise. Getting this backwards would either
forbid the ejector entirely or permit driving the nosecone through the stage.

#### `Deck.slot_cell(self, x: float, y: float) -> Optional[str]`

Which stage location contains this point?

**Returns** the slot key as a **string** (`"8"`, matching `deck.json`'s keys), or
`None` if the point is off-stage. Where a point falls in the overlap of two
half-pitch cells - which happens because the real pitch is irregular - the
**nearest** centre wins. Raises nothing.

**Why it exists:** to tell a traverse that crosses a location boundary from motion
that stays inside one. The edges of every location are raised, standing
`62.5 - 53.0 = 9.5 mm` above the stage floor, so with a 300 uL tip fitted Z must be
**53.0 or lower-numbered** (physically higher) whenever the effector passes over a
location corner. But that limit applies only to *crossing*: the wall wipe works at
Z 45 and Z 41, deep inside a tube, and is perfectly legal because it never leaves
the cell the tube sits in. `Simulator._check_raised_edges` returns early exactly
when `cell_from is not None and cell_from == cell_to`, and
`tests/test_lab.py` asserts every wipe move resolves to cell `"8"`.

#### `Deck.z_slot_edge_bare` (property)

The **bare-nosecone** commanded Z at which the effector would strike a raised
location edge.

**Returns** `105.25` on the current deck; `None` if any of
`z.slot_edge_with_p300_tip`, `z.stage_surface_with_p300_tip` or `z.stage_surface`
is missing. Raises nothing.

Computed as `slot_edge_with_p300_tip + (stage_surface - stage_surface_with_p300_tip)`
= `53.0 + (114.75 - 62.5)` = `53.0 + 52.25` = `105.25`.

**Why the indirection:** the edge was measured *with a tip on* (Z 53.0), because
that is the configuration the operator could actually touch off. Converting it to a
bare-nosecone reference means the same single measurement covers both cases -
`Simulator._check_raised_edges` takes `z_slot_edge_bare` and subtracts the fitted
tip's length to get back to 53.0 for tipped moves, and uses 105.25 unchanged for
bare ones. That is also why the tip length is derived from the two stage
measurements rather than guessed independently:
`tiprack_300_96.tip.length = 52.25` comes from the same subtraction.
`tests/test_lab.py::test_stage_surface_and_tip_length` pins 105.25 and
`test_travel_height_is_now_verified_not_assumed` reconstructs 53.0 from it, then
checks travel Z 5.0 clears it by 48.0 mm.

**Returning `None` is not a pass.** The caller must warn: the simulator emits
`"cannot check the raised location edges: ... unmeasured"` and skips, per convention
3.

#### `Deck.slot_pitch(self) -> Tuple[float, float]`

Mean X and Y spacing between adjacent slot centres, in mm.

**Returns** `(135.5, 93.25)` today: X from `(336 - 65) / 2` and Y from
`(365 - 178.5) / 2`, over the *distinct* sorted coordinate values. Returns `0.0`
for an axis with fewer than two distinct values. Raises nothing.

**Why the mean, and the caveat it carries:** the real frame is irregular - 136 mm
between slots 1 and 2 but 135 between 2 and 3, and 93.0 then 93.5 in Y. The mean is
what defines the notional 135.5 x 93.25 mm **location cell**, which is what
`stage_region`, `slot_cell`, `slot_seating_report` and `a1_from_slot_edge` all work
against. The consequence is documented on `tube_rack_1p5.a1_from_slot_edge.caveat`:
its insets (15.25 mm in from the high-X edge, 13.125 mm down from the high-Y edge)
were calibrated in slot **5** against a mean-pitch cell, so a cell derived the same
way in another slot is up to 0.5 mm out. Re-measure A1 if that holder moves rather
than trusting the model to transfer exactly. On slot 5 the model reproduces the
measured A1 exactly: cell high-X edge `200 + 67.75 = 267.75`, minus 15.25 =
**252.50**; high-Y edge `272 + 46.625 = 318.625`, minus 13.125 = **305.50**.

#### `Deck.summary(self) -> str`

A multi-line human-readable dump: deck id, envelope (annotated `(+Z is DOWN)`),
`z_travel` and `z_home`, one line per placement (labware id, where it is mounted,
grid shape, and its well-centre X and Y extents), and one line per keep-out band.
Returns a string; raises nothing.

**Why:** the fastest way to see whether the config in front of you describes the
bench in front of you. `tests/smoke_geometry.py` prints it first, then the
displacement table, then `uncalibrated()` and `overlap_report()`. Note the
placement extents are **well-centre** bounds (`Placement.well_bounds`), not
footprints - the physical rim extends past them by an unmeasured amount.

---

### Private helpers

A protocol author should never call any of these; the first two run automatically
during `__init__` and the third is an internal ordering workaround.

#### `Deck._check_recorded_slot_center(self, entry, p) -> None`

Holds a placement's optional `slot_center` convenience copy to the real centre in
`deck.json`, to within 1e-6 mm. No-ops when the placement has no `slot_center`, no
slot, or an unknown slot. **Raises `DeckError`** naming both values and telling the
caller to fix whichever is stale or drop the key.

**Why:** a duplicated coordinate that nothing verifies will eventually disagree
with its source, and the copy is the one a human reads. Every slot-mounted
placement in the shipped layout carries one, so this check is live on four
placements today.

#### `Deck._check_slot_occupancy(self) -> None`

**Raises `DeckError`** if two placements claim the same slot, naming both.
Placements with `slot is None` are ignored, and `near_slot` is not consulted - so
`water` near slot 9 does not conflict with anything genuinely seated in slot 9.

#### `Deck._raw_slot_pitch(deck_raw) -> Tuple[float, float]`

Staticmethod. The same computation as `slot_pitch()`, but reading the **raw**
deck dict.

**Why it is duplicated:** `__init__` needs the pitch to inject `_slot_pitch` into
placements that use `a1_from_slot_edge`, and at that moment `self.slots` is
populated but the method is being called from inside the constructor's placement
loop; reading the raw config keeps the helper independent of construction order.
If you change the pitch definition, change **both** - they must agree or a
slot-edge-dimensioned A1 will resolve differently from the cell it is checked
against.

---

## `lab/machine.py`

`Machine` is the one object a protocol drives. It owns the resolved deck, the loaded end effector, the gcode buffer, the live machine state (homed / tip fitted / fluid held / plunger POS), and the `Simulator` that checks every step as it is generated. A protocol asks for wells and microlitres; this module turns them into commanded X/Y/Z and `ACTUATOR_MOVE POS=` lines and refuses anything the geometry does not allow.

Three properties hold across the whole module and are worth stating once:

- **+Z is DOWN.** `Z=0` is home at the top. `deck.z_travel` is `5.0`, the safest place the effector can be; the plate floor at `60.8` is the most dangerous. Every "lift" in this file is a move to a *smaller* Z.
- **Nothing reaches disk until `finalize()` runs the simulator clean.** `Gcode` is constructed with `None` as its path, so the run lives in a buffer. A refused primitive is meant to emit no lines at all, which is why the validation in `_aspirate_at`, `mix` and `dispense` happens before the first motion.
- **A precondition failure is a `ProtocolError` (subclass of `LabError`); a geometry failure is a `SimulationError` (also `LabError`).** A protocol catches `LabError` and prints the message. `tests/test_review_fixes.py::test_8_all_errors_share_a_base` pins that.

---

### Construction

#### `Machine(effector, name, layout="layout.json", strict=True, temp_log=True)`

Loads the deck and the end effector, builds the simulator, and opens an in-memory gcode buffer whose home position is `(deck.x.home, deck.y.home, deck.z.home)` = `(420.0, 0.0, 0.0)`.

- `effector` (str): effector id, i.e. `config/effectors/<id>.json`. `"p300"` or `"p125"` today. Raises `EffectorError` for an unknown id, an unknown `kind`, or a config marked `status: placeholder`.
- `name` (str): run name. Becomes `<name>.gcode`, `<name>.manifest.json`, `<name>.balance.csv`, `<name>.toolpath.png`.
- `layout` (str): which layout file in the config dir describes what is physically loaded. Defaults to `layout.json`; the tests pass `layout_test.json`, which is the only layout that still carries the 0.65 mL `"tubes"` placement.
- `strict` (bool): passed to `Simulator`. `True` means the first hard violation raises `SimulationError` at the moment the offending step is recorded, so the traceback points at the protocol line that caused it. `False` collects errors into the report instead and lets `finalize()` refuse at the end.
- `temp_log` (bool): whether `log_temps()` emits anything.

Raises `DeckError` for a layout that cannot be resolved (unknown labware id, two placements in one slot, a `slot_center` copy that disagrees with `deck.json`), `EffectorError` as above.

**Why it exists / what it prevents:** it is the only place config is read, so every number in a run is traceable to a file whose SHA-256 lands in the manifest. Two warnings are raised here rather than later, because a run that is about to be generated is the last moment anyone is looking: every entry in `layout.json -> open_questions` becomes a warning (five of them today, including plate2's *inferred* A1 at `(251, 399)`), and an effector config carrying a `z_conflict` block warns that it has an unresolved crash risk. Neither blocks the run - they are things the operator has to read in the report.

**Note:** a warning is recorded once per key (`Simulator.warn` dedupes), so nothing here can spam a loop.

---

### Recording layer

Everything that emits gcode goes through `_record`, which is what feeds the simulator. A protocol author never calls these.

#### `xyz() -> tuple[float, float, float]`

Current commanded position `(X, Y, Z)` in mm, read back out of the gcode writer. Public and side-effect-free; useful in a protocol only for assertions.

#### `_record(**kw) -> Step` *(private)*

Builds a `Step` with the current tip state, held volume and gcode line number, appends it to `self.steps`, and hands it to `Simulator.check()` - which is where a `SimulationError` is raised in strict mode. If `self._keepout_exempt` is set it stamps `detail["keepout_exempt"] = True` on the step. Never call it from a protocol.

#### `_engaging_ejector()` *(private, context manager)*

Sets `_keepout_exempt` for the duration of the `with` block so the steps recorded inside carry `detail["keepout_exempt"]`.

**Why it exists:** the keep-out band `tip_ejector` spans X `[148.0, 162.0]` and forbids Y motion there. The ejector lane is at X `155.0`, inside its own band - the engagement is physically required to break the rule. The exemption therefore has to be granted to *specific waypoints*, not to the operation. `Simulator._check_keepout` only honours it when **both** the step carries the flag **and** `step.op` is in the band's `whitelist` (`["eject_tip"]`). Marking by operation name alone was a confirmed defect: the approach and retract traverses around the engagement are ordinary moves and inherited the exemption, which allowed an unchecked 104 mm tipped Y traverse at X 162. `tests/test_review_fixes.py::test_1b_exemption_cannot_be_spoofed_by_op_name` pins the fix by passing `op="eject_tip"` to a hand-rolled move and requiring it to still fail.

#### `_raw(line, op, **detail) -> Step` *(private)*

Writes one literal gcode line and records it as `kind="macro"`. Used for the Klipper macros. A protocol should use `note()` instead; anything else it might want to emit raw belongs in `deck.json -> klipper_macros`.

#### `_require_homed(what)` *(private)*

Raises `ProtocolError` naming `what` if `startup()` has not run. Guards `goto()`, `get_tip()` and `eject_tip()`.

#### `_pipette() -> Pipette` *(private)*

Returns `self.effector` if it is a `Pipette`, else raises `ProtocolError` naming the actual `kind`. Every liquid primitive calls it, so loading the gripper and calling `aspirate_uL` fails with a sentence rather than an `AttributeError`.

---

### Motion

#### `_move_z(z, feedrate, op, labware=None)` *(private)*

Absolute Z move to `z` mm at `feedrate` (mm/min). Remember `z` larger means physically lower.

- `labware` (str or None): the labware id whose Z table this depth came from. It is passed straight through to the `Step`, and `Simulator._check_move` uses it to look up that labware's `bottom` crash limit. **Pass it whenever the depth came from `effector.z_for(...)`** - omitting it silently disables the well-floor check for that move.

Returns nothing. Emits nothing and records nothing when the delta rounds to zero at 6 decimal places, using the same tolerance as `gcodepy.move_z_absolute`. That match is deliberate: a mismatched tolerance recorded a `Step` for a move that emitted no line, which desynchronised step indices from gcode lines (`test_review_fixes.py::test_14_no_phantom_z_steps`).

#### `_move_xy(x, y, feedrate, op, labware=None, order="XY")` *(private)*

Moves to `(x, y)` mm **one axis at a time**, in the order given by the characters of `order` - `"XY"`, `"YX"`, or a single `"X"` / `"Y"` to move only that axis. Each axis is a separate `Step` and therefore separately checked; an axis whose delta is within 1e-6 is skipped.

**Why one axis at a time:** the simulator's swept-region checks (`Deck.segment_hits_rect`, traverse clearance) assume the swept area is the axis-aligned box between the endpoints, which is only true for single-axis legs. `deck.json -> conventions.xy_order` states the same rule ("Never diagonal. Enforced by the library."). A protocol must not call this: it does no routing and no travel-height lift, so it will happily drive Y through a keep-out band and let the simulator catch it. That is exactly how the tests provoke failures.

#### `goto(x, y, op="goto", feedrate=None, prefer=None)`

Lift to travel height, then traverse to `(x, y)`. **The only public XY move.**

- `x`, `y` (mm, machine frame): envelope is X 0-420, Y 0-580.
- `op` (str): label carried on every `Step` this produces; it appears in error messages and in the toolpath plot, and it is what `_LOW_XY_OK` and the keep-out whitelist match against.
- `feedrate` (mm/min or None): defaults to `deck.feedrate("travel")` = 6000.
- `prefer` (`"XY"`, `"YX"` or None): axis-order preference. `None` means `"XY"`.

Raises `ProtocolError` if not homed, if `prefer` is not one of the two strings, or if no route is clear. Returns nothing.

**Why it exists:** it enforces the two invariants a hand-written move gets wrong. First, the lift - a traverse below travel height is the crash mode that costs a tip, and `Simulator._check_move` hard-fails any XY step whose start or end Z is not exactly `deck.z_travel` (5.0) unless `op` is in `_LOW_XY_OK` = `{"wall_wipe", "eject_tip"}`. Second, the lift is **conditional on XY actually changing**: lifting unconditionally made a dispense followed by a mix in the same well climb to Z 5 and come straight back down to Z 45 for nothing.

**Note:** the lift is emitted before `_route_xy` can raise, so a `goto` that is refused for being unreachable can leave one Z line in the buffer if the effector was not already at travel height. In practice every primitive ends at travel height, which is why `test_lab.py::test_envelope` sees zero lines emitted by a refused `goto(500, 100)`.

#### `goto_placement(placement, row, col, op="goto") -> (x, y)`

Traverse to a well, honouring that labware's `approach_order`.

- `placement` (str): placement name from the layout - `"tips"`, `"big_tubes"`, `"plate1"`, `"plate2"`, `"water"` (and `"tubes"` in the test layout).
- `row`, `col` (int, 0-based): `row=0, col=0` is A1.

Returns the resolved `(x, y)` in mm. Raises `DeckError` for an unknown placement name and `IndexError` for a well outside the grid.

**Why it exists:** it is the bridge from protocol language to coordinates, and it is where the per-labware Y-first rule gets applied. Both tube holders declare `approach_order: "YX"` because their caps make Y motion across the holder difficult - the effector must reach its Y while still clear in X. A plate declares nothing, so the default X-first applies.

#### `goto_well(placement, row, col, op="goto") -> (x, y)`

Alias for `goto_placement`, kept because protocols call it.

**Why it exists:** it used to be a near-duplicate that did not pass `prefer`, so it silently ignored the Y-first rule both tube holders need. Two ways to reach a well is one too many. `test_lab.py::test_goto_well_honours_approach_order` pins that the legs come out `["Y", "X"]`.

#### `goto_station(name, op=None)`

Traverse to a fixed station's XY. `name` is a key in `deck.json -> stations`: `"water"` `(70.0, 175.0)`, `"park"` `(420.0, 0.0)`, `"tip_eject"` `(155.0, 89.0)`. Raises `DeckError` for an unknown name. `op` defaults to `"goto:<name>"`.

**Note:** it does not pass a `prefer`, so a station approach always starts from the X-first preference. That is currently harmless (no station sits on labware that declares `"YX"`) and the router still overrides the preference on geometry - the tip-rack no-fly around the water station at X 70 is resolved by `_route_xy`, not by a preference.

#### `_route_xy(x, y, feedrate, op, labware=None, prefer=None)` *(private, and the heart of the module)*

Traverses to `(x, y)` on a path that is actually clear, emitting the legs itself via `_move_xy`. Raises `ProtocolError` naming the first obstacle if nothing works.

There are two obstacle classes and **neither can be solved by travelling higher**:

- **Keep-out bands** forbid *Y* motion at certain X. `tip_ejector` covers X `[148.0, 162.0]` and is safe only when commanded Z is *below* `60.0` (i.e. physically above it) - and only bare. With a tip fitted it is an unconditional error, because `safe_when_z_below` was stated for the bare nosecone and the tipped equivalent was never measured. A pure *X* move straight through a band is fine.
- **No-fly labware** (`labware.json -> avoid_traverse`, true only for `tiprack_300_96`) forbids motion of *either* axis across it while a tip is fitted. The tips stand upright well above the rack body and the fitted tip hangs 52.25 mm below the nosecone, so crossing shears the tip off at any travel height.

The algorithm is **candidate-based**: rather than reason about which single axis order survives, it builds a list of complete routes and takes the first whose *every* leg is clear.

1. **Degenerate cases first.** If both axes are already at target, return. If only one axis needs to move, try that single leg on its own and take it if `_route_problem` says it is clear.
2. **Build candidates, in order:**
   - the **preferred order** - `[(x, cur_y), (x, y)]` for `"XY"`, `[(cur_x, y), (x, y)]` for `"YX"`;
   - the **other order**;
   - **X doglegs**: for each candidate X from `_dogleg_x_candidates`, the three-leg route `[(via, cur_y), (via, y), (x, y)]` - step sideways to a clear X, do the whole Y move there, come back in X;
   - **Y doglegs**: for each candidate Y from `_dogleg_y_candidates`, `[(cur_x, via), (x, via), (x, y)]` - step out in Y, cross in X at that Y, then come back in Y.
3. **Take the first clear candidate.** If it is not the preferred order, record a `route_override:<op>:<label>` warning quoting the preference, the problem that blocked it, and what was routed instead. Then emit the legs, deciding each leg's axis by which coordinate changed.
4. **If nothing is clear**, raise `ProtocolError` quoting `first_problem` - the reason the *preferred* route failed, which is the most useful one to show.

**Why Y doglegs are needed on their own:** an X dogleg only helps when there is a clear X to do the Y move at. It cannot help at all when both the start and the end lie *inside* the no-fly rectangle's Y span, because then every X leg between them crosses the rack no matter which X the Y move happens at. That is not a corner case - it is the normal situation straight after a tip pickup. `_exit_tip_box` leaves the effector at X 8.9, clear of the rack in X but still at the rack's Y (335.5-399.2), and plate1 D6 sits at Y 372, also inside that span, on the far side in X. The only way across is to step *out in Y* first (to `y1 + pad` = 403.75 or `y0 - pad` = 329.6), cross in X there, then come back in Y. `test_lab.py::test_router_doglegs_in_y_to_cross_the_tip_rack` pins exactly this: three legs, none crossing the box, and a `"dogleg via Y"` warning in the report. `test_lab.py::test_hand_rolled_move_over_the_tip_rack_is_refused` pins the other half - the simulator catches a bypass that skips the router.

The mirror case for X doglegs is a band-to-band move: 0.65 mL tube column 2 at X 162 to column 3 at X 150, both inside `[148, 162]`. Neither axis order is safe, so the route has to step out of the band, move Y there, and come back (`test_lab.py::test_dogleg_when_both_ends_are_in_the_band`, three moves plus a `"Routed dogleg"` warning).

**The preference always yields, loudly.** `approach_order` is an ease-of-motion instruction from the operator, not a safety constraint, so an obstacle overrides it - but never silently.

#### `_route_problem(legs) -> str | None` *(private)*

Returns a sentence explaining why a sequence of `(frm, to)` legs is unsafe, or `None` if clear. Checks, per leg: the destination is inside the envelope; the leg does not sweep a no-fly rect (only when `tip_fitted` - `no_fly_rects()` is empty otherwise); and, for any leg that changes Y, that the Y motion's X is not inside a keep-out band (an error unconditionally when tipped, and when bare only if travel Z is not safe - at Z 5.0 versus `safe_when_z_below` 60.0 it always is).

It evaluates against `deck.z_travel`, not the current Z, because `goto()` guarantees the lift has already happened before routing.

#### `_dogleg_x_candidates(cur_x, target_x) -> list[float]` *(private)*
#### `_dogleg_y_candidates(cur_y, target_y) -> list[float]` *(private)*

Candidate dogleg coordinates in mm, deduped, clamped to the envelope, sorted nearest the *target* first (so the shortest detour is tried first). Both use a 6.0 mm pad. The X version draws candidates from both no-fly rect edges and keep-out band edges; the Y version only from no-fly rect Y edges - correctly, since keep-outs constrain the X at which Y motion happens, which is an X-dogleg problem.

**Note:** the `cur_x` / `cur_y` parameters are accepted and never used; ordering is by distance to the target only.

---

### Actuator

#### `actuator(pos, settle=0.0, op="actuator") -> float`

Commands the plunger to an absolute POS and returns the rounded value actually emitted.

- `pos` (mm of plunger travel, in the effector's own POS frame - **not** a Z and **not** a volume): rounded to `actuator.decimals` (3 for both pipettes) and, for a `Pipette`, checked by `Pipette.check_pos`, which raises `EffectorError` if it is below the floor `pos_min + stroke_margin` (0.0 + 1.0 = 1.0 for p300) or above `blowout_pos` (30.0).
- `settle` (seconds): a dwell emitted after the move. 0 or negative is a no-op.

Emits `ACTUATOR_MOVE POS=<pos>` at fixed width, from `deck.macro("actuator_move")` - the macro string is never defaulted, so a mistyped `klipper_macros` entry raises `DeckError` instead of emitting a plausible-looking line that addresses nothing (`test_review_fixes.py::test_13_macros_must_be_declared`). Updates `self.pos`.

**Why the fixed-width format matters:** the value is quantized exactly once, at `decimals`. Rounding into a variable and then subtracting from it compounds two quantizations; the comment records that 2 dp would inject 0.63 percent error at the 10 percent ISO point, and 4 dp is below the actuator's step size.

Protocols normally reach this through `aspirate_uL`, `dispense`, `mix` and `prewet` rather than calling it directly - those are the paths that convert microlitres to a POS through the calibration.

#### `dwell(seconds, op="dwell")`

Emits a dwell of `seconds`. Guarded: a zero or negative settle is a no-op, not an exception, so callers can pass a config value straight through without checking it.

#### `wait()`

Emits `wait_finish` (Klipper `M400`). Records no `Step`. Used after every motion that must physically complete before the next one starts - descents into liquid, the tip press, every leg of the ejection.

#### `log_temps(tag)`

Emits `LOG_TEMPS TAG=<tag>`, or nothing at all when the machine was constructed with `temp_log=False`. `tag` is a free-text label that lands in the Klipper log so a temperature reading can be tied back to a step. The macro name comes from config; `DeckError` if it is missing.

#### `note(text)`

Writes `; <text>` into the gcode, appends to `self.notes`, and records a `Step`. The manifest carries the same list, so a note is the intended way for a protocol to leave a human-readable marker in both artefacts at once.

---

### Operations

#### `home(op="home")`

Homes Z first, then XY, then the actuator via `deck.macro("home_actuator")`, and sets `self.homed = True`. Two separate `Step`s for the axis groups.

**Why Z first:** the effector must be at the top before X or Y move, and X homes at max (420) - a long traverse.

#### `startup()`

Home everything, reference the plunger at blow-out, lift to travel height, wait.

Calls `home()`, then for a pipette commands `actuator(blowout_pos)` (30.0 for p300), then `_move_z(deck.z_travel)` = 5.0, then `wait()`.

**Why blow-out first:** bottomed means no vacuum in the tip cone. Referencing the plunger anywhere else would leave the pipette holding a partial vacuum while the nosecone is later pressed onto a tip, which draws air (or liquid) past the seal as the tip seats.

#### `park()`

Traverse to the `park` station `(420.0, 0.0)` - X home - then move Z to the station's `z` (0.0, i.e. fully up) or `deck.z_home` if the station declares none, then wait. Call it at the end of a run so the deck is clear for the operator.

#### `get_tip(row=0, col=None, placement="tips") -> (row, col)`

Press the nosecone into a tip, lift it out, then leave the box in X. Returns the `(row, col)` actually picked.

- `row` (int, 0-based): rack row. 8 rows in `tiprack_300_96`.
- `col` (int, 0-based, or None): `None` means "the next unused column working inward from the configured exit edge" via `_next_free_tip_col` - the only choice whose X exit path is guaranteed clear.
- `placement` (str): placement name to take tips from, cross-checked against the effector's own `tips.labware` by `_tip_placement`.

Raises `ProtocolError` if not homed, if the effector's `tips.required` is false (p125), if a tip is already fitted, if the named placement holds the wrong labware, if the requested position was already consumed this run, or if every column in the row is used. Raises `IndexError` for a position outside the grid. Raises `SimulationError` if the exit path crosses an occupied position.

Sequence: `goto` the well honouring the rack's `approach_order`; wait; descend to the rack's `approach` Z (47.0 for p300, or travel height if the effector declares none) at the tip-press feedrate 600; descend to `pick` Z 59.0; wait; dwell 1 s; set `tip_fitted` and `current_tip` and record the pickup with the simulator; lift back to travel Z 5.0; wait; `_exit_tip_box`.

**Why the already-consumed check is a hard refusal:** pressing into an empty rack hole drives the *bare* nosecone down to Z 59.0 with nothing to stop it. This was a confirmed defect - `get_tip` ignored `consumed_tips`, so a second pickup did exactly that and then set `tip_fitted = True` anyway. `test_review_fixes.py` tests 2, 2b and 2c pin the fix.

**Note:** `pick` 59.0 comes from a direct rack measurement and supersedes `Z_TIP_PICK = 57.5` in `examples/300ulaccuracyprecision.py`; the effector config keeps 57.5 under `superseded` with an open question about whether it was a different tip type.

#### `_tip_placement(placement) -> Placement` *(private)*

Resolves the rack to use, preferring the effector's own `tips.labware` (`"tiprack_300_96"` for p300) over the caller's placement name. If the named placement does not exist it falls back to `deck.placement_for_labware(want)`; if it exists but holds different labware, it raises `ProtocolError` telling the operator to name the right placement or fix `tips.labware`.

**Why:** routing by placement name alone would send a second effector that declares a different rack to the wrong rack, and the resulting error would then tell the operator to add the wrong rack to its Z table.

#### `_next_free_tip_col(p, row, exit_edge) -> int` *(private)*

First unused column, working inward from the exit edge (columns sorted by their X value, reversed when the edge is `high_x`). Raises `ProtocolError` when the row is exhausted.

**Why that order:** the effector leaves the box across every column between the one it picked and the edge, so the edge column has to go first and the box must empty toward the middle. `test_review_fixes.py::test_2c_consumption_works_inward_from_the_exit_edge` picks four tips and asserts their X values come out sorted.

#### `_has_z(labware_id, key) -> bool` *(private)*

Whether the effector's `z_for_labware[labware_id][key]` is present and non-null. Used only so `get_tip` can skip the approach move when no `approach` Z is configured, without going through `z_for` (which raises on a missing key by design).

#### `_tip_exit(p) -> (edge, col)` *(private)*

Reads `labware.json -> tiprack_300_96.consume_order.exit_edge` and returns the edge name plus the column index at that edge. Accepts `"low_x"`/`"min_x"` and `"high_x"`/`"max_x"`.

The shipped value is `"unconfirmed"`. In that case it assumes the low-X edge (column 0 at X 18.0) **and warns**, saying that if the real exit is the high-X edge then every pickup in the run exits the wrong way. It does not refuse, because the exit-path check in `_exit_tip_box` is what actually keeps this safe - that check fails loudly whichever edge turns out to be right.

#### `_exit_tip_box(p, row, col, edge)` *(private)*

Records a note-kind `Step` listing the positions the X exit will cross, hands it to `Simulator.check_tip_exit` (which fails on any crossed position not already consumed), then moves X one full column pitch **plus the labware's `consume_order.exit_clearance_mm`** beyond the outermost well centre - clamped into the envelope - and waits. A negative clearance raises `ProtocolError`, since it would steer the exit back into the rack.

One pitch beyond the outermost centre clears every tip *position*, the nearest being a full pitch away, without needing the rack's physical outline, which is not measured. But a centre-to-centre gap is not a wall-to-wall gap: the fitted tip and the standing tips are each several mm wide, so one pitch left only 4.55 mm between X 8.909 and the rack body's low-X edge at 13.45. `exit_clearance_mm` is 2.0 on `tiprack_300_96` as of 2026-09-10, putting the low-X target at **6.909** and the gap at 6.55 mm. The number lives in `labware.json`, not here, so widening it is a config edit; `test_leaving_the_tip_box_goes_toward_x_zero_then_y` recomputes the target from `col_vec` and that field rather than pinning the literal.

**Why it is not redundant with the following `goto`:** it guarantees the effector is out of the box *before* `_route_xy` makes any decision that could put Y first. `test_lab.py::test_leaving_the_tip_box_goes_toward_x_zero_then_y` asserts the first tipped leg is an X move that ends below the box's low-X edge, and that the next `goto` then starts with Y at that cleared X.

#### `eject_tip()`

Hook the tip collar on the ejector and pull **up** to strip it, then re-home. Raises `ProtocolError` if not homed, if the effector's `tips.eject_supported` is false, or if no tip is fitted.

All values come from `deck.json -> stations.tip_eject`: `lane_x` 155.0, `approach_y` 83.0, `engage_y` 88.0, `retract_y` 80.0, `z_below_hook` 145.0, `z_strip` 137.0, `rehome_after` true, `validated` **false**.

The choreography, with Z directions spelled out:

1. **Warn.** `validated` is false, so a warning says the routine has never been run on hardware and must be dry-run and then stepped through manually at reduced feedrate.
2. **Lift to travel height** (Z 5.0). Physically the top.
3. **Route to `(155.0, 83.0)`** through `_route_xy` at travel feedrate, then wait. Deliberately *routed*, not hand-ordered: the lane X 155 is itself inside the keep-out band `[148, 162]`, so the Y leg cannot happen at the lane X. The router works that out from the geometry and picks Y-first or a dogleg, which is exactly the required "Y first, then X". Hand-ordering this was the original defect.
4. **Descend to Z 145.0** at the tip-press feedrate 600, then wait. That is 140 mm *lower* than travel, below the hook. It is legal only because `(155, 89)` is off the stage region - the stage surface exists only over the slot area, and the ejector hangs past the front edge. The simulator asserts this rather than taking it on trust, and warns that the depth is past the stage surface at an off-stage point. `test_lab.py::test_ejector_is_off_the_stage` pins it for all three Y values.
5. **Enter `_engaging_ejector()`.** Only the steps inside carry the keep-out exemption.
6. **Y 83.0 -> 88.0** at Z 145, slow, `order="Y"`. This is the lateral move that brings the tip collar *under* the hook. It is a Y move at X 155, inside the band, at Z 145 - illegal for any other step, which is the whole point of the exemption.
7. **Z 145.0 -> 137.0**, slow, then wait. **This is the strip: 8 mm UPWARD**, because +Z is down and 137 is a smaller number than 145. The collar is trapped under the hook and the nosecone pulls out from under the tip. `tip_fitted` and `current_tip` are cleared immediately after.
8. **Z 137.0 -> 145.0** - back *down*, clear of the hook - then **Y 88.0 -> 80.0** to retract out of the lane, both still exempt.
9. **Exit the exemption. Lift to travel height** (Z 5.0) and wait. Then record a `kind="note"` step with `detail={"final": True}`.
10. **Mandatory re-home** when `rehome_after` is true: `G28 ALL`, recorded with `reason: "post-ejection"`, then wait.

**Why the "ejection complete" marker goes after the lift rather than at the strip:** `Simulator.check` arms `_pending_eject_home` when it sees that final marker, and then hard-fails the *next* `move` or `actuator` step that is not a home. Marking earlier would make the lift itself trip the check.

**Why the re-home is not optional:** the strip force can skip steps on an open-loop machine, so the commanded position is no longer trustworthy afterward. `Simulator.finish` also errors if the whole run ends on an ejection with no `G28` after it. `test_lab.py::test_eject_is_followed_by_home` and `test_ejection_passes_the_envelope_checks` pin both halves.

**Why the exemption rides on the step, not the operation name:** see `_engaging_ejector` above. Steps 3 and 9 use `op="eject_tip"` too, but carry no flag, so they are checked as the ordinary traverses they are. `test_review_fixes.py::test_1_eject_approach_is_routed_and_checked` asserts that every exempt Y move happens at exactly X 155.0 and that no unexempt Y move happens inside the band at all.

#### `goto_reservoir(station="water")`

Traverse to a reservoir station's XY. Thin wrapper on `goto_station` with `op="reservoir"`.

#### `prewet(cycles=None, station="water")`

Wet the tip wall so the first real aspiration is not wetting dry plastic.

- `cycles` (int or None): `None` uses the effector's configured `prewet.cycles` (3 for p300). A value below `prewet.min_cycles` (also 3) raises `ProtocolError` - the minimum exists because a partial prewet is worse than none: it leaves the wall unevenly wetted.
- `station` (str): station name; the labware id behind it is resolved through `_station_labware` and `placement_for_labware`.

Raises `ProtocolError` if the effector is not a pipette or has `prewet.enabled` false (p125), and `EffectorError` if the reservoir's `work` Z is unmeasured.

Sequence: `goto_station`; wait; bottom the plunger at `blowout_pos` with a 0.5 s settle; descend to the reservoir `work` Z (40.0 for p300, tip submerged); wait; then `cycles` times draw to `prewet.asp_pos` (10.0 = `blowout_pos` 30.0 minus `prewet.displacement_mm` 20.0, measured **from bottomed**, not from `gap_pos`) with a 1 s settle and push back to `blowout_pos` with a 1 s settle; lift to travel Z; wait. Records `toggles["prewet_cycles"]` for the manifest.

#### `_station_labware(station) -> str` *(private)*

Reverse-maps a station name to the labware id of the placement sitting there. Raises `ProtocolError` if no placement claims that station - the library genuinely cannot know what is at a station nobody declared.

#### `_require_empty_tip(what) -> Pipette` *(private)*

Shared preconditions for anything that draws liquid: the effector is a pipette; a tip is fitted if the effector requires one; and `fluid_uL` is zero.

**Why holding fluid is a refusal, not a top-up:** every destination gets its own independent aspiration. Stacking two draws into one tip would mean the second dispense's volume is not the one the calibration was fitted for, and the balance reading would still look plausible. `what` is the caller's name, so the message says which primitive refused.

#### `_aspirate_at(x, y, labware_id, volume_uL, op="aspirate", prefer=None, z_key="work")` *(private)*

The aspiration sequence itself, shared by both public forms. Returns the aspirate POS.

**The depth first.** When the effector's entry for this labware contains an `aspirate` key, `z_key` switches from `"work"` to `"aspirate"` - selected on the key being *present*, so a null one refuses at `z_for` rather than falling back to the shallower dispense depth. An explicit `z_key` argument overrides both, which is how `mix` reaches its own depth. See **The per-labware depths**.

Every ordering guarantee lives here, which is why the station and well forms delegate instead of each repeating it:

1. **Validate the whole stroke before emitting anything.** `displacement_mm(volume_uL)` (raises `VolumeError` outside the calibration window - p300 `[8.0, 320.0]` uL - or if the stroke exceeds `max_volume_uL`), `aspirate_pos`, `backoff_pos`, and `z_for(labware_id, z_key)` (raises `EffectorError` if that depth is null). Discovering at the backoff that the plunger has run out of travel would leave a half-finished aspiration in the buffer with the state claiming it completed. `test_review_fixes.py::test_6_failed_aspirate_emits_nothing_and_leaves_no_stale_record` pins zero lines emitted, `fluid_uL` still 0, and `_last_aspirate` still `None` after a refusal.
2. Clear `_last_aspirate`, so a stale record from an earlier aspiration can never survive into the next dispense.
3. `goto(x, y, prefer=prefer)`; wait.
4. **Arm the air gap in air**, before descending: `actuator(gap_pos)` = 25.0 for p300, which is `blowout_pos` 30.0 minus `air_gap` 5.0, with `settle_s` = 1.0 s.
5. Descend to the work Z; wait.
6. `actuator(aspirate_pos)` with the settle, then set `fluid_uL`.
7. Lift to travel Z.
8. **Only now**, if `post_asp_backoff` is non-zero (1.0 mm for p300), `actuator(backoff_pos)` - the anti-drip retract, which must happen after the tip is clear of the liquid or it just draws more water.
9. Wait, then store `_last_aspirate` with the volume, displacement, both POS values and the source labware id, for the dispense to fold into its manifest row.

#### `aspirate_uL(volume_uL, station="water") -> float`

One independent aspiration from a **station**. Returns the plunger POS used.

- `volume_uL` (microlitres, always - never millimetres): converted by `Pipette.displacement_mm` from `slope` 0.0533 and `intercept` 0.122 for p300, `0.1274` / `0.049` for p125.
- `station` (str): defaults to `"water"` at `(70.0, 175.0)`.

Raises `ProtocolError` (not a pipette / no tip / already holding fluid / no placement at that station), `VolumeError` (outside the window or beyond the stroke), `EffectorError` (unmeasured depth or an illegal POS).

#### `aspirate_from(placement, row, col, volume_uL) -> float`

One independent aspiration from a **well**. Returns the plunger POS used. Honours the placement's `approach_order`, so drawing from `"big_tubes"` approaches Y-first.

**Why both forms exist:** the station form reverse-maps a station name to labware, and a slot-mounted rack has no station. `test_lab.py::test_aspirate_from_reaches_the_well` pins the descent to the tube's work Z, the `["Y", "X"]` approach, and that `_last_aspirate["source"]` names the labware.

#### `mix(placement, row, col, cycles, volume_uL, z_key="mix")`

Homogenise a well by cycling the plunger deep in it. Returns nothing.

- `cycles` (int): number of draw/push pairs. Below 1 raises `ProtocolError`.
- `volume_uL` (microlitres): the stroke per cycle, converted through the calibration.
- `z_key` (str): which per-labware depth to cycle at. Defaults to `"mix"`.

Raises `ProtocolError` (not a pipette / no tip / already holding fluid / `cycles < 1`), `VolumeError`, `EffectorError` (illegal top-of-stroke POS, or a null `mix` depth).

**The three depths, and why they are three separate numbers.** Per effector, per labware, shallowest first: `work` is the dispense/aspirate depth, `mix` is deeper so the stroke reaches *under* the liquid rather than skimming its surface, and `bottom` is the vessel floor - **a crash limit the machine must never reach, so it is the wrong thing to aim at.** For p300: the 1.5 mL tube is `work` 35.0 / `mix` 45.0 (10 mm deeper) / `bottom` still null; the plate is `work` 53.5 / `bottom` 60.8, which puts a dispense 7.3 mm above the floor. Larger is lower throughout. `test_lab.py::test_mix_cycles_at_the_mix_depth` asserts the mix descends to the `mix` Z and that the `bottom` Z appears in no move at all.

Sequence: `goto` honouring `approach_order`; wait; **bottom the plunger** at `blowout_pos` with a 0.5 s settle, *skipped* when `self.pos` is already there (the common dispense-then-mix case, where emitting a move to where the plunger already is would be noise); descend to the mix Z; wait; then `cycles` times draw up to `top` and push back to `blowout_pos`, each with `settle_s`; lift to travel Z; wait. `toggles["mix_cycles"]` and `toggles["mix_volume_uL"]` are recorded.

`top` is `blowout_pos - displacement_mm(volume_uL)`, checked by `check_pos` before anything is emitted: the plunger draws *up* from bottomed, so the top of the stroke is the position that can run out of travel.

**Why it starts and ends bottomed:** the tip holds no liquid between cycles and carries none away, so a mix is volume-neutral and `fluid_uL` stays 0 throughout.

#### `dispense(placement, row, col, wall_wipe=None, retract=True) -> dict`

Full dispense to blow-out, then optionally wipe the droplet off the wall. Returns the manifest row for this dispense.

- `placement`, `row`, `col`: the destination well.
- `wall_wipe` (bool or None): `None` means "wipe if this labware has a `wipe` block". `True` on labware with no `wipe` block raises `ProtocolError`. Only `tube_rack_0p65_5x10` has one today (`dx` 3.2 mm, `dz` 4.0 mm, feed 1500 - both marked uncalibrated).
- `retract` (bool): when `True` (default) the tip lifts to travel height after the dispense. **`retract=False` leaves the tip at the working depth.** Use it only when the very next operation is in the *same* well - a mix, typically - where lifting to Z 5 and descending again is pure wasted motion. It stays safe either way, because `goto()` lifts to travel height before any XY move, so the tip cannot be dragged sideways while down.

Raises `ProtocolError` if `fluid_uL` is zero (nothing aspirated), if the effector is not a pipette, or on the wipe-config mismatch; `EffectorError` for an unmeasured `work` depth; `IndexError` for a bad well.

Sequence: settle the wipe question **before any motion**; `goto` honouring `approach_order`; descend to the `work` Z; wait; `actuator(blowout_pos)` with the settle - a full blow-out, so the tip is empty; zero `fluid_uL`; optional `_wall_wipe`; optional lift; wait; build the record.

**Why the wipe check happens first:** raising after the blow-out would leave a complete dispense in the buffer with no manifest row to account for it - the mass would be on the balance and absent from the data. `test_review_fixes.py::test_10_dispense_checks_wipe_config_before_moving` pins zero lines emitted and `fluid_uL` still 150.0 after a refused dispense.

The returned dict is appended to `self.dispenses` and becomes one row of the manifest and one row of `<name>.balance.csv`: `order_index`, `placement`, `labware`, `slot`, `well` (`well_name(row, col)`, e.g. `"D6"`), `row`, `col`, `x`, `y`, `z`, `target_volume_uL`, everything from `_last_aspirate` (`volume_uL`, `displacement_mm`, `aspirate_pos`, `backoff_pos`, `source`), and `mass_dry_mg` / `mass_wet_mg` / `temp_C` left `None` for the operator to fill in from the balance. `_last_aspirate` is then cleared - **one aspiration feeds exactly one dispense** (`test_review_fixes.py::test_6b_dispense_clears_the_aspirate_record`).

#### `_wall_wipe(p, x, y)` *(private)*

Step to the inner wall, drag the droplet **up**, then pull back off. `dx`, `dz` and `feed` come from the labware's `wipe` block.

It first asserts its starting pose: if the effector is not at exactly `(x, y, z_work)` it raises `ProtocolError` quoting both poses. Then `X -> x + dx` (3.2 mm sideways, less than the tube inner radius, to contact the wall), `Z -> z_work - dz` (**`-dz` is UP** - that is the drag that pulls the droplet off), then `X -> x` back off the wall.

**Private, and it must stay private.** This is the one place that moves XY below travel height, which is why `"wall_wipe"` is in `_LOW_XY_OK` and so exempt from the travel-height check. Calling it from anywhere but the bottom of a dispense would traverse the deck at well depth with that check suppressed. The pose assertion is what makes that unreachable. `test_review_fixes.py::test_11_wall_wipe_requires_the_dispense_pose` pins both the refusal and the absence of any public `wall_wipe` attribute.

It stays legal against the raised-edge rule (Z 53.0 tipped) only because it never leaves the stage location the tube sits in - the edge check compares `slot_cell` at both endpoints and returns early when they match. `test_lab.py::test_wall_wipe_inside_one_location_is_still_allowed` asserts every wipe leg stays inside slot 8.

---

### Finalize

#### `simulate() -> Report`

Runs the whole-run checks and returns the `Report`: `errors`, `warnings`, `steps`, `path`, and `ok` (no errors). Whole-run checks are the ones that can only be evaluated at the end - an ejection with no `G28` after it (error), fluid still held (warning), a tip still fitted and never ejected (warning), slot-seating offsets, and footprint overlaps (`OVERLAP` is an error, "cannot check" a warning).

Idempotent: `Simulator.finish` returns the same `Report` on every call after the first. It used to append into the same report each time, so an inspect-then-finalize caller tripled every whole-run line and the manifest recorded the pile-up (`test_review_fixes.py::test_7_simulate_is_idempotent`).

#### `finalize(outdir=".", plot=True) -> dict`

Run the whole-run checks, then write the artefacts. **Or refuse to.**

- `outdir` (str): directory, resolved and created if absent.
- `plot` (bool): whether to render `<name>.toolpath.png`. Returns `None` for the plot path when matplotlib is not installed, rather than failing the run.

Raises `ProtocolError` carrying the full report text if `report.ok` is false - **and writes nothing**. That is the seam the whole library is built around: a run that fails simulation never becomes a `.gcode` file, so a bad number cannot reach the hardware through this path. `test_lab.py::test_simulation_failure_writes_nothing` pins it by injecting an error into a non-strict run.

On success it dumps the buffered gcode to `<outdir>/<name>.gcode`, writes the manifest and balance CSV via `lab.logbook.write_manifest`, optionally renders the plot, and returns `{"gcode", "manifest", "plot", "report", "lines", "dispenses"}` - paths as strings, `lines` the gcode line count, `dispenses` the count of manifest rows.

**Note:** the check order means `outdir` is created even when the run is then refused, so a failed run can leave an empty directory behind.

---

### Provenance

These exist so a `.gcode` file can be traced back to the exact configuration and commit that produced it. `lab.logbook` calls all four when building the manifest.

#### `config_hashes() -> dict[str, str]`

SHA-256 of each config file actually used, keyed by name: `deck.json`, `labware.json`, the layout file's own basename (so `layout_test.json` is not silently reported as `layout.json`), and `effectors/<id>.json`. `test_lab.py::test_full_protocol_and_manifest` asserts all four are present.

#### `git_commit() -> str | None`

`git rev-parse HEAD` in the repo root, 10 s timeout. Returns `None` on any failure - not installed, not a repo, timed out. Provenance must never be able to break a run.

#### `git_dirty() -> bool | None`

`True` if `git status --porcelain` produced any output, `False` if clean, `None` if it could not be determined. A `True` here means the committed config is not the config that generated the file, which is the one thing that makes a hash misleading.

#### `uncalibrated() -> list[str]`

Every field the config itself admits was never tuned - the deck's list concatenated with the effector's. Today that includes `keepouts.tip_ejector.x_range` (the 14 mm band width is carried over from a different location and is still unverified), `stations.tip_eject` (routine never validated on hardware), `z_for_labware.tube_rack_0p65_5x10.work`, `z_for_labware.tube_rack_1p5.bottom`, and the plate's unmeasured `height_mm`.

**Why it is structured rather than grepped:** these come from explicit `uncalibrated` arrays in the JSON, not from parsing prose notes, so rewording a comment cannot make an uncalibrated field silently look calibrated. The list goes into the manifest, so a data file always carries the caveats of the run that produced it.

---

## `lab/simulate.py`

Dry-run checking. Nothing here talks to hardware or Moonraker, and nothing here writes a file: the `Simulator` is fed every `Step` as `Machine._record` creates it, and raises `SimulationError` on the first hard violation before the gcode buffer ever reaches disk (`Machine.finalize` refuses to dump when `Report.ok` is false - pinned by `test_simulation_failure_writes_nothing`).

Two conventions govern every comparison in the module:

* **+Z is DOWN.** A larger commanded Z is physically lower, so "must stay above the rim" is written `z < rim` and a *limit* is exceeded when `z > limit`.
* **A check that cannot be evaluated is not a pass.** Where a needed measurement is missing (tip length, `deck z.stage_surface`, a labware height, a footprint, a well bottom), the simulator records an explicit `cannot check ...` warning naming the missing field and skips only that check. It never returns silently. This is why a clean run still prints warnings, and why the operator is told to read them and not just the pass line.

The related distinction, which shows up in two different places:

* A **commanded depth** that is null stops the run. `Effector.z_for("tube_rack_1p5", "work")` raises `EffectorError` if the value is `None`, because the machine is about to be told to go somewhere and there is no number to send.
* A **crash limit** that is null only warns. `Simulator._effector_z` returns `None` instead of raising, so `tube_rack_1p5.bottom: null` in `config/effectors/p300.json` produces `cannot check well-bottom crash for tube_rack_1p5` and the run continues. Not knowing where the floor is does not make the dispense wrong; refusing to run because a limit is unmeasured would just stop work on labware whose floor nobody has probed yet.

### `class SimulationError(LabError)`

```python
SimulationError(step: Step, message: str)
```

Raised by `Simulator.fail` when `strict=True`. Carries `.step` (the offending `Step`) and `.message`, and formats itself with everything needed to find the fault without re-running:

```
step 41 [dispense/move]: Z 61.0 is past the plate1 well bottom 60.8 for p300
  at (387.0, 399.0, 5.0) -> (387.0, 399.0, 61.0)  POS 25.0 -> 25.0  tip=True  fluid=100.0 uL
```

Subclasses `LabError`, so a protocol can catch that one class and print the message instead of a traceback (`test_8_all_errors_share_a_base` pins this for all five error types). Why it exists: an exception that only said "crash" would send the operator back through a 2000-line gcode file; the step index, op, both endpoints, plunger POS, tip state and held fluid identify the generating call site directly.

### `@dataclass class Step`

One generated action plus the machine state it produced. Fields:

- `i` (int) - step index, monotonic per run, used in every error message.
- `op` (str) - the protocol-level operation that produced it (`"dispense"`, `"get_tip"`, `"eject_tip"`, `"wall_wipe"`, `"probe"`, ...). Checks key off this, so it is load-bearing, not a label.
- `kind` (str) - one of `move | actuator | home | dwell | macro | note`. Selects which checks run.
- `frm`, `to` (`(x, y, z)` in mm, or `None`) - commanded start and end. Both are needed: the swept-segment checks are exactly the ones that endpoint-only checking got wrong.
- `axes` (str) - which axis letters this step commands, e.g. `"X"`, `"Y"`, `"Z"`. Drives `moves_y` / `moves_xy`.
- `pos_before`, `pos_after` (mm) - plunger POS, for actuator steps.
- `tip` (bool) - was a tip fitted at this step. Every absolute Z limit in the module shrinks by the tip length when this is true.
- `fluid_uL` (uL) - fluid held, for the end-of-run check and error context.
- `feedrate` (mm/min), `labware` (labware id, not placement name), `gline` (gcode line number at emission).
- `detail` (dict) - per-step flags. Only `keepout_exempt` (set by `Machine._engaging_ejector`) and `final` (set on the last ejector step) are read.

Why `detail["keepout_exempt"]` is per-step and not per-op: see `_LOW_XY_OK` below.

#### `Step.moves_y` / `Step.moves_xy` (properties)

`"Y" in self.axes` and `"X" in self.axes or "Y" in self.axes`. Return bool. They exist so the checks read off what the step actually commands rather than comparing coordinates, which would treat a zero-length X leg as an XY traverse.

### `_LOW_XY_OK` (module constant)

`{"wall_wipe", "eject_tip"}` - the only operations permitted to move XY somewhere other than travel height, exempting them from the travel-height rule and from `_check_traverse_clearance`. `wall_wipe` works inside a well by definition; the ejector engagement happens around Z 137-145, 140 mm down. Note what is *not* in this exemption: the keep-out band. That exemption is granted per step through `detail["keepout_exempt"]`, so the approach and retract moves that surround an ejection cannot inherit it from the operation name they share. `test_1` (in `test_review_fixes.py`) pins this: a step merely *named* `eject_tip` without the flag is still refused a keep-out crossing.

### `@dataclass class Report`

Accumulated result. `errors` (list of str), `warnings` (list of str), `steps` (int, highest step index + 1), and `path` - a list of `(x, y, z, op)` tuples for every step with a fully resolved `to`, which is what `plot_path` draws.

#### `Report.ok` (property)

`not self.errors`. `Machine.finalize` writes gcode only when this is true. Warnings never block.

#### `Report.text() -> str`

One header line (`simulation: N steps, E error(s), W warning(s)`) then every error prefixed `ERROR` and every warning prefixed `warning`. Raises nothing. It is the body of the `ProtocolError` that `finalize` raises on refusal, so the operator sees the whole picture rather than the first failure only.

---

### `class Simulator`

```python
Simulator(deck: Deck, effector: Effector, strict: bool = True)
```

Incremental checker. `deck` supplies geometry, `effector` supplies the per-labware Z table and plunger limits. `strict=True` (the default, and what `Machine` uses unless told otherwise) makes `fail` raise immediately; `strict=False` collects errors and keeps going, which is how tests exercise several failures in one run. Either way the errors land in `report.errors` and gate the write.

State it keeps: `report`, `_warned` (dedup keys), `consumed_tips` (set of `(row, col)` positions already taken from the rack), `_pending_eject_home` / `_eject_step` (the post-ejection G28 obligation), and `_finished` (makes `finish` idempotent).

### `Simulator.warn(key: str, message: str) -> None`

Records `message` in `report.warnings` the first time `key` is seen and does nothing thereafter. Returns `None`, raises nothing. Why: every warning in this module fires from inside a per-step check, so a 20-dispense loop would emit the same "the 0.65 mL holder height is the EMPTY holder" line 200 times and bury the other findings. Choosing the key is the real design decision - see `_check_move`, where the Z-envelope key includes the tip state (`z_limit:tipped` vs `z_limit:bare`) because the tipped case can be unresolvable while the bare case is fine, and collapsing them would swallow the more dangerous one as a duplicate.

Protocol authors do call this: `Machine.startup` forwards layout `open_questions` through it.

### `Simulator.fail(step: Step, message: str) -> None`

Appends `step N [op]: message` to `report.errors` and raises `SimulationError(step, message)` if `strict`. Returns `None` in non-strict mode. Every hard check funnels through here so that strict and non-strict runs cannot diverge in what they consider a failure.

### `Simulator.tip_length() -> Optional[float]`

Tip length in mm, or `None` if it cannot be resolved. Looks up `effector.tip_labware` (`"tiprack_300_96"` for p300) in `deck.catalogue` and reads `spec["tip"]["length"]`. Currently 52.25 mm, itself derived in `labware.json` from `deck z.stage_surface` 114.75 minus `stage_surface_with_p300_tip` 62.5 - measured once, not guessed twice. Raises nothing; `None` is the signal that the tipped variant of every absolute Z check is unevaluable. Pinned at 52.25 by `test_3c_limits_are_now_actually_enforced`.

### `Simulator.effective_z_limit(tip_fitted: bool, clearance: float = 0.0) -> Tuple[Optional[float], List[str]]`

The deepest legal commanded Z over the stage, plus a list of the measurements missing if it cannot be computed.

- `tip_fitted` (bool) - whether a tip is on. With +Z down a fitted tip extends the reference point downward, so the maximum commanded Z **shrinks** by the tip length the instant a tip goes on: 114.75 bare, 62.5 tipped for p300 (`test_3c`).
- `clearance` (mm) - optional safety margin subtracted from the limit. No caller passes a non-zero value today.
- Returns `(limit_mm, [])` when resolvable, or `(None, ["deck z.stage_surface", "tiprack_300_96 tip.length"])` when not. Raises nothing.

Why the `missing` list is returned rather than logged: measuring `z.stage_surface` alone would switch the check on for bare moves and leave it off for the deeper, more dangerous tipped ones with nothing saying so. That was a real defect, and `test_3b_limit_returns_what_is_missing` pins the two cases separately (bare reports one missing field, tipped reports two).

It also emits the `stage_ref:<id>` warning when `deck z.stage_surface_reference` (`"p300"`) is not the loaded effector's id: a different pipette body sits at a different height, so every absolute Z limit derived from 114.75 is borrowed rather than measured, and the run is told to measure that effector's own stage contact.

### `Simulator.check(step: Step) -> None`

The single entry point. `Machine._record` calls it on every step. Dispatches by `step.kind` to `_check_move` or `_check_actuator`, maintains the post-ejection homing obligation, and appends fully-resolved destinations to `report.path`. Raises `SimulationError` (via `fail`) on the first hard violation in strict mode.

The post-ejection rule, in order: a `home` step clears the obligation; any `move` or `actuator` step while the obligation stands is a hard failure ("a tip ejection was not followed by G28"); and a step whose `op` is `eject_tip` with `detail["final"]` sets it. The physical failure: the strip force can skip steps on an open-loop machine, so after an ejection the commanded position is no longer trustworthy and every subsequent coordinate is fiction. `deck.json` states this as `rehome_after: true`, `"G28 is mandatory, not optional"`.

`report.path` only takes steps whose `to` has no `None` component - `home("Z")` used to record `(None, None, 0.0)` and poison the plot (`test_12_no_none_coordinates_in_the_path`).

### `Simulator._check_move(step: Step) -> None` (private)

All geometric checks for a `move` step, in the order they run. A protocol author never calls this; it is reached through `check`.

1. **Envelope.** `deck.in_envelope(x, y, z)` against X 0-420, Y 0-580, Z 0-270. Prevents commanding a coordinate the kinematics cannot reach, which on Klipper is a mid-run abort with the tip somewhere unknown.
2. **Off-stage depth (warning).** The stage surface exists only over the slot area (`deck.stage_region()`, derived as the slot centres expanded by half the slot pitch). Off it there is nothing to hit at stage depth, which is exactly what makes the ejector's Z 145 at Y 89 legal - it hangs past the front edge. When a step commands a Z past `z_stage_surface` at a point outside the region, this records the `off_stage_depth` warning stating the assumption rather than staying quiet, so anything *other* than the ejector doing it shows up in the report. `test_ejector_is_off_the_stage` pins the ejector's three Y waypoints (83 / 88 / 80 at X 155) as genuinely off-stage instead of taking it on trust.
3. **Raised location edges** - `_check_raised_edges`, below.
4. **Tip-aware absolute Z.** When the destination is on the stage, `effective_z_limit(step.tip)` is compared against `z`: deeper than the limit is a hard failure naming the tip state. Prevents driving the nosecone or the tip into the stage floor. Unresolvable means the `z_limit:tipped` / `z_limit:bare` warning, which states plainly that only the kinematic limit and the per-labware Z were checked.
5. **Travel-height rule.** For any XY-moving step whose `op` is not in `_LOW_XY_OK`, both `frm[2]` and `to[2]` must equal `deck.z_travel` (5.0). Anything else is a hard failure: "Every traverse must be at travel height; this is the crash mode that costs a tip." A traverse at well depth drags the tip through rims, caps and edge pieces for the whole length of the move.
6. **No-fly labware crossing.** For any XY-moving step with a tip fitted whose `op` is not `get_tip`, `deck.segment_hits_rect` is tested against every `deck.no_fly_rects()` entry (labware flagged `avoid_traverse: true` - the 300 uL tip rack). Crossing one is a hard failure, deliberately *not* a Z comparison: the tips stand upright above the rack body and the fitted tip hangs below the nosecone, so the tip shears off at any travel height and flying higher does not help. The only fix is to route around, which `Machine._route_xy` does. `labware.json` records the hardware observation behind this: the water station at X 70 sits underneath the rack in X, so every unrouted approach to water from high Y went straight through it.
7. **Keep-out bands.** For a step that moves Y, `deck.keepouts_hit_by_y_move(step.frm[0])` gives the bands that X sits inside, and each goes to `_check_keepout`.
8. **Swept-segment traverse clearance** - `_check_traverse_clearance`, below.
9. **Well bottom from the effector table.** When the step names a labware, `_effector_z(labware, "bottom")` is the crash limit and `z` deeper than it is a hard failure ("Z 61.0 is past the plate1 well bottom 60.8 for p300"). The limit lives in the *effector* file, not `labware.json`, because the commanded Z at which a tip touches a floor depends on the tip: the same plate bottoms out at a different number bare than with a 300 uL tip fitted. For `sbs_96_wellplate`, p300 records `work: 53.5` and `bottom: 60.8`, so a dispense sits 7.3 mm off the floor. A labware in the catalogue with no measured `bottom` gets the `bottom:<labware>` warning naming the file to add it to - the null-crash-limit case.

### `Simulator._check_raised_edges(step: Step) -> None` (private)

The edges of each stage location stand about 9.5 mm above the stage floor (`62.5 - 53.0`). The limit is `deck.z_slot_edge_bare`, which is the stored with-tip measurement `slot_edge_with_p300_tip: 53.0` plus the tip length, so 105.25 bare and back to 53.0 with a tip fitted. Exceeding it is a hard failure naming both cells.

Two exemptions, both physical rather than cosmetic:

- **Nothing on the stage was swept.** If the axis-aligned box between `frm` and `to` misses `deck.stage_region()` entirely, no stage feature is in reach. This is what makes the ejector legal at Y 80-89.
- **Motion stayed inside one location.** `deck.slot_cell` for `frm` and `to` returning the same cell means no boundary was crossed, and it is boundaries the raised edges sit on. This is what keeps the wall wipe legal: it works at tube depth but never leaves the cell the tube sits in. The same reasoning is why plate `work` 53.5 being 0.5 mm deeper than the 53.0 edge limit is fine - the rule forbids crossing a boundary at that depth, not being there.

Unmeasured inputs warn under `slot_edge` (the deck fields) or `slot_edge_tip` (tip length) and skip.

### `Simulator._check_keepout(step: Step, k: KeepOut, z: float) -> None` (private)

Applies one keep-out band to a Y-moving step. The shipped band is `tip_ejector`, X[148, 162] (the lane at X 155 plus or minus 7), safe only when commanded `Z < 60` - that is, physically above the mechanism.

Order matters:

1. `detail["keepout_exempt"]` **and** `op` in the band's `whitelist` returns early. Both are required. The flag is set only by `Machine._engaging_ejector` around the waypoints that actually engage the ejector, so the surrounding approach and retract are checked like any other traverse.
2. **A tip fitted is an unconditional failure.** `safe_when_z_below: 60.0` was stated for the bare effector; a fitted tip reaches 52.25 mm lower and the real limit was never measured, so a tipped traverse through the band is an error rather than a comparison against a number that does not apply.
3. Otherwise `k.z_is_safe(z)` must hold. Note `KeepOut.z_is_safe` returns `False` when `safe_when_z_below` is `None`, so an undeclared limit forbids the motion rather than permitting it.

**Note:** the band's `x_range` width is itself flagged in `deck.json -> uncalibrated` ("width unverified; lane X 155 itself is confirmed and post-shift"), so this check is only as wide as an unverified 14 mm carried over from the old X 85 location.

**Note:** `KeepOut.tipped_is_error` is never read - neither `Deck.__init__` (which does not populate it from config) nor this method consults it. The tipped-is-an-error behaviour is hardcoded here. Harmless today, misleading if someone sets it in config expecting an effect.

### `Simulator._check_traverse_clearance(step: Step) -> None` (private)

Does the whole swept segment clear every loaded placement? For each placement it computes `labware_top_z(p, step.tip)` and tests the axis-aligned box between `frm` and `to` for overlap with the placement's bounds; overlap at a Z at or below the top is a hard failure.

Why the swept segment and not the destination: a traverse that starts over the rack and ends at the reservoir crosses the whole rack, and labware that is never a move endpoint would never be checked at all. That was a real defect; `test_4_traverse_clearance_uses_the_swept_segment` reproduces it by giving the tube rack a 60 mm contents height (commanded top `114.75 - 60 - 52.25 = 2.5`, above travel height 5.0) and asserting the Y leg from the tip rack down into the rack is refused - and that a move which misses the rack is not. `Machine._move_xy` emits one axis per step, which is what makes the box a valid model of the sweep.

Bounds preference: `p.footprint_bounds()` (the measured physical outline) if available, else `p.well_bounds()` plus the `footprint:<name>` warning, because a rim extends past the outermost well centres and well centres alone would miss a path that clips the edge of the rack.

Two warnings alongside:

- `ztop:<name>` when the top is unknown, carrying the reason from `labware_top_z`.
- `contents:<name>` when `p.labware.height_is_empty_only` - the stored `height_mm` is the empty holder and the real height with tubes and caps in it is greater by an unmeasured amount. The message quotes `max_labware_height()` as the threshold above which travel height is struck with a tip on. `test_empty_height_is_flagged_as_incomplete` pins that a run over the 0.65 mL holder produces it.

### `Simulator._effector_z(labware_id: str, key: str) -> Optional[float]` (private)

Reads `effector.spec["z_for_labware"][labware_id][key]` as a float, or returns `None` if the labware, the key, or the value is absent. Raises nothing - and that is the whole point. `Effector.z_for` raises `EffectorError` on a null because it is being asked for a depth to command; this returns `None` because it is being asked for a crash limit to compare against, and an unknown limit becomes a `cannot check` warning instead of stopping the run. Not for protocol use; ask `Effector.z_for` for a target.

### `Simulator.labware_top_z(p, tip_fitted: bool) -> Tuple[Optional[float], str]`

Commanded Z at which the effector would touch this placement's top.

- `p` (`Placement`) - a resolved placement.
- `tip_fitted` (bool).
- Returns `(z_mm, "")`, rounded to 4 dp, or `(None, reason)` where `reason` is the ready-made `cannot check ...` warning text. Raises nothing.

Computed as `z_stage_surface - height_mm`, minus the tip length when tipped. Derived from a physical height above the stage rather than stored as a Z because a stored Z is wrong the moment a tip goes on: the 35 mm 0.65 mL holder tops out at commanded Z **79.75** bare and **27.5** with a 300 uL tip (`test_holder_heights_convert_to_commanded_z`, which also checks travel height 5.0 clears 27.5). A height is frame- and effector-independent; a commanded Z is neither.

Public because tests and tools use it; a protocol has no reason to.

### `Simulator.max_labware_height() -> float`

Tallest anything can be, in mm above the stage, and still clear travel height with a tip fitted: `z_stage_surface - tip_length - z_travel` = `114.75 - 52.25 - 5.0` = **57.5 mm**, pinned by `test_holder_heights_convert_to_commanded_z`. Used in the `contents:<name>` warning to tell the operator the number the unmeasured contents height must not exceed.

**Note:** the `or 0.0` fallbacks mean an unmeasured `stage_surface` or tip length silently yields a nonsense figure (`-5.0` with neither measured) rather than `None`. It is only ever interpolated into a warning string, so nothing acts on it, but the message would be quietly wrong in exactly the configuration where the reader most needs to distrust it.

### `Simulator._check_actuator(step: Step) -> None` (private)

Plunger POS bounds for a `Pipette`. `pos_after` below `pos_min + stroke_margin` (0.0 + 1.0 = 1.0 mm for p300) is a hard failure - that margin keeps the plunger off the POS=0 hard stop, which it would otherwise ram. Above `blowout_pos` (30.0) is a hard failure - past full dispense plus blow-out there is no stroke left. These duplicate `Pipette.check_pos` on purpose: the effector validates when asked to compute a POS, and the simulator validates the POS that was actually commanded, whatever path produced it.

**Note:** the method returns silently when `pos_after` is `None` and when the effector is not a `Pipette`. The first is benign (non-actuator steps carry no POS), but the second means a `Gripper`'s actuator steps have no bound checked at all and no `cannot check` warning is recorded - the one place in the module that departs from the stated principle. A `Gripper` is not built yet, so nothing is at risk today.

### `Simulator.record_tip_pickup(step: Step, row: int, col: int) -> None`

Adds `(row, col)` to `consumed_tips`. `row`/`col` are zero-based rack indices. Returns `None`, raises nothing. It is the bookkeeping that makes `check_tip_exit` and `Machine.get_tip`'s refusal to reuse a hole possible: without it a second pickup drove the bare nosecone into an already-empty hole and then set `tip_fitted = True` anyway (`test_2_second_pickup_does_not_reuse_a_hole`, `test_2b_explicit_reuse_is_refused`).

### `Simulator.check_tip_exit(step, row: int, col: int, crossed: Sequence[Tuple[int, int]]) -> None`

After a pickup, the X exit must cross only positions already consumed.

- `step` - the recording step, for the error message.
- `row`, `col` (int) - the position just picked.
- `crossed` - the `(row, col)` positions the exit path passes over, computed by `Machine`.

Hard failure if any crossed position still holds a tip. The physical failure: passing over an occupied position shears the fresh tip straight off the nosecone. The remedy is in the message - consume columns starting at the exit edge and work inward, which `test_2c_consumption_works_inward_from_the_exit_edge` pins.

### `Simulator.finish(held_fluid: float, tip_fitted: bool) -> Report`

Whole-run checks, then the finished `Report`. `held_fluid` in uL and `tip_fitted` are the end-of-run machine state (`Machine.simulate` passes its own). Raises nothing - `finish` never raises even in strict mode, because these are conditions of the finished run and the caller is about to inspect the report.

- Errors: an unfulfilled post-ejection G28 obligation ("the run ends on a tip ejection with no G28 after it"), and every `OVERLAP` line from `deck.overlap_report()` - two placements whose measured footprints intersect means the bench cannot physically be as the layout describes.
- Warnings: fluid still held at the end; a tip still fitted and never ejected when the effector supports ejection; every line from `deck.slot_seating_report()` (a labware whose resolved grid centre is more than half a slot pitch from the slot it claims); and the non-`OVERLAP` lines from `overlap_report()`, which are its own `cannot check X vs Y: no footprint measured` entries.

Idempotent via `_finished`: a caller that inspects the report and then calls `finalize()` would otherwise get every whole-run line duplicated, and the manifest would record the pile-up. `test_7_simulate_is_idempotent` calls `simulate()` three times and asserts the warning count does not move.

### `plot_path(report: Report, out_path: str, title: str = "toolpath") -> Optional[str]`

Renders the toolpath to a PNG and returns the path written, or `None` if matplotlib is not installed or `report.path` is empty. `out_path` is a filesystem path; `title` labels the figure. Raises nothing on the missing-dependency route - plotting is a convenience and must never be the reason a run fails to produce gcode, which is why `Machine.finalize` treats the `None` as a normal outcome (and every protocol offers `--no-plot`).

Two panels, both fighting the sign convention so a human can read them:

- XY path with points coloured by Z, equal aspect, colourbar labelled `Z (mm, +Z down)` and a title saying "larger Z is LOWER".
- Z against step index with the Y axis **inverted**, so down on the plot is down in space.

Forces the `Agg` backend before importing pyplot, so it works headless.

---

## `lab/logbook.py`

The run manifest: a JSON sidecar written next to every generated `.gcode`, plus a balance-entry CSV. Two jobs. First, **gravimetric traceability** - `mass_dry_mg`, `mass_wet_mg` and `temp_C` are written out empty, one row per dispense, so balance readings join to the run on tube ID at the balance instead of being transcribed by hand into a spreadsheet nobody can later tie to a specific gcode file. Second, **reproducibility** - the git commit of the generating code, the SHA-256 of every config file consumed, the calibration coefficients actually used, and every field the config admits is uncalibrated, all captured at generation time. The simulator's warnings are copied in too, so the file records what could not be checked and not just what passed.

### `build_manifest(machine: "Machine", report: Report) -> Dict[str, Any]`

Assembles the manifest as a plain dict. `machine` is a `Machine` after the run is generated; `report` is the finished `Report` from `Simulator.finish`. Returns a JSON-serialisable dict; raises nothing of its own (it will propagate a `KeyError` if an effector spec is missing `calibration`, which `Pipette.__init__` would already have rejected). Separated from the writing so tests and tools can inspect the structure without touching disk.

Contents worth knowing:

- `schema_version` (1) and `generated_utc` (ISO 8601, seconds precision).
- `effector` - id, kind, display name, `nominal_uL`.
- `calibration` - populated only for a `Pipette`, otherwise `{}`. Carries `equation` from `Pipette.equation`, which is **derived from the coefficients in use**, not the free-text `equation` string in the JSON: that duplicate is exactly the kind that drifted from its own table in the scripts this library replaces (`test_9_equation_is_derived_from_the_coefficients`). Also `slope_mm_per_uL` (0.0533 for p300), `intercept_mm` (0.122), `window_uL` ([8.0, 320.0]), the gravimetric conditions (`method`, `temperature_C`, `water_density_g_per_mL`, `z_factor_uL_per_mg`, `pressure_kPa`), and every plunger constant that shaped the run (`blowout_pos`, `air_gap`, `gap_pos`, `post_asp_backoff`, `stroke_margin`, `max_volume_uL`).
- `provenance` - `config_dir`, `git_commit`, `git_dirty`, and `config_sha256` for all four files consumed (`deck.json`, `labware.json`, the layout by its real filename, and `effectors/<id>.json`). `test_full_protocol_and_manifest` asserts all four are hashed. `git_commit` and `git_dirty` are `None` when git cannot be reached, so a manifest is still written outside a checkout - but a `git_dirty: true` is the flag that the commit alone does not describe the code that ran.
- `deck` - id, `z_travel`, `z_home`, `z_stage_surface`, feedrates.
- `layout` - one entry per placement with the resolved geometry (`origin`, `col_vec`, `row_vec`, `rows`, `cols`) as well as the labels (`slot`, `station`, `mount`, `rotation`). The vectors are what actually produced the coordinates, so a well can be recomputed from the manifest without re-resolving the config.
- `open_questions`, `near_slots`, `slot_seating` - the deck's own admissions of what is unresolved or is a human reference rather than a seating claim.
- `toggles` (`temp_log`, and `prewet_cycles` / `mix_cycles` / `mix_volume_uL` if those ran), `notes`, `uncalibrated` (deck plus effector).
- `simulation` - step count, errors, warnings verbatim.
- `gcode_lines` and `dispenses` - the full dispense records, each already carrying `x`, `y`, `z`, `target_volume_uL`, the aspirate record, and the three empty balance columns.

### `_CSV_COLUMNS` (module constant)

The balance CSV's column order: `order_index, placement, labware, slot, well, row, col, x, y, z, target_volume_uL, displacement_mm, aspirate_pos, backoff_pos, mass_dry_mg, mass_wet_mg, temp_C`. Fixed and explicit so the file the operator types into keeps the same shape between runs, and so `extrasaction="ignore"` can drop any extra key a dispense record grows without breaking the writer.

### `write_manifest(machine: "Machine", report: Report, outdir: Path) -> Path`

Writes `<name>.manifest.json` and, when there are dispenses, `<name>.balance.csv` into `outdir`, creating it (with parents) if needed. Returns the path of the JSON. Raises `OSError` on an unwritable directory.

- JSON: `indent=2`, `encoding="utf-8"` explicit. Note the project trap about `write_text` truncating before it encodes on a `cp1252` default - the explicit encoding here is what keeps a non-ASCII character in a config note from producing a zero-byte manifest.
- CSV: `newline=""` (so `csv` controls line endings on Windows rather than doubling them), `utf-8`, header row, then one row per `machine.dispenses` entry. The three balance columns are written as empty cells, ready to fill in - `test_full_protocol_and_manifest` checks both files appear for a 20-dispense run and that every dispense carries `mass_dry_mg`.

Called only from `Machine.finalize`, and only **after** `report.ok` has been confirmed, so a manifest never describes a run whose gcode was refused.

---
