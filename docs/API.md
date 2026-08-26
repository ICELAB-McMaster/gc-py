# `lab` API reference

Every public function in the library, what it does, what it raises, and - the
part worth reading - what mistake it exists to prevent.

Generated from the source on 2026-08-26. If the code and this file disagree,
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
it in the run manifest. Silence always means checked.

## The shape of it

```
config/                     data, no logic
  deck.json                 envelope, slots, stations, keep-outs, Z references
  labware.json              labware geometry and height - slot-independent
  layout.json               what is loaded where, right now
  effectors/<id>.json       one file per end effector

lab/
  config.py                 finds and hashes config; LAB_CONFIG overrides
  labware.py                well (row, col) -> machine (x, y)
  deck.py                   joins the three configs; stage, slots, keep-outs
  effector.py               Pipette / Gripper; uL -> mm lives here
  machine.py                the object a protocol drives
  simulate.py               every safety check, run as the gcode is generated
  logbook.py                run manifest + balance CSV
```

Data flows one way: `config` -> `labware`/`effector` -> `deck` -> `machine`,
with `simulate` observing every step `machine` produces and `logbook` recording
the result. Nothing below `machine` knows gcode exists.

## The shortest useful protocol

```python
from lab.machine import Machine

m = Machine(effector="p300", name="my_protocol")
m.startup()                        # home Z, home XY, home actuator, prime, lift
m.get_tip()                        # next unused tip, edge-first
m.prewet()                         # only if the effector config enables it
m.aspirate_uL(150.0)               # microlitres, always
m.dispense("tubes", row=0, col=0)  # a placement name from layout.json
m.eject_tip()
m.park()
result = m.finalize(outdir="out")  # simulates, THEN writes
```

`finalize()` runs the whole-run checks and refuses to write the `.gcode` if any
hard check fails, so a failed run leaves nothing behind to run by accident.

## Errors

Every exception the library raises subclasses `lab.effector.LabError`, so a
protocol catches one class:

| class | raised when |
|---|---|
| `LabError` | base; catch this |
| `EffectorError` | the effector cannot do what was asked |
| `VolumeError` | volume outside the calibration window or the available stroke |
| `DeckError` | the config cannot describe the deck, or a lookup missed |
| `ProtocolError` | the protocol asked for something illegal in sequence |
| `SimulationError` | a hard safety check failed; the gcode is not written |

---

## `lab/config.py`

Config discovery, loading and hashing. Every path resolves against the location of `config.py` itself (`_REPO_ROOT = Path(__file__).resolve().parent.parent`, so the repo root, and `_DEFAULT_CONFIG_DIR = <repo>/config`), never against the working directory, so `python protocols/accuracy_test.py` behaves the same from the repo root, from `protocols/`, or from a cron job with an arbitrary cwd. The other half of the module's job is provenance: a loaded config carries the SHA-256 of the exact bytes it came from, so a generated `.gcode` can always be traced back to the deck geometry that produced it.

The four files this module knows about are `deck.json` (envelope, slot centres, stations, keep-outs, Z references), `labware.json` (slot-independent geometry), `layout.json` (what is loaded right now) and `effectors/<id>.json` (one file per end effector).

### `config_dir() -> Path`

Returns the directory holding `deck.json` / `labware.json` / `layout.json` / `effectors/`.

- Parameters: none.
- Returns: the `LAB_CONFIG` environment variable if set (after `expanduser()` and `resolve()`), otherwise `<repo>/config`.
- Raises: `FileNotFoundError` if `LAB_CONFIG` is set but does not point at a directory. It fails loudly rather than silently falling back to the in-repo config, because silently generating gcode from the wrong deck is the failure mode that matters.
- Why: it is the single override point for running against an alternate deck. `tests/test_review_fixes.py` uses it heavily: it copies the real config into a temp dir, nulls out one measured Z or one geometry field, points `LAB_CONFIG` at the copy, and checks that the library refuses to emit motion. The env var is re-read on every call and nothing is cached, so a test that flips `LAB_CONFIG` mid-process affects only loads made after the flip.

### `repo_root() -> Path`

Returns the repository root (the parent of `lab/`).

- Parameters: none.
- Returns: `Path` to the repo root. Never raises.
- Why: `Machine.git_commit()` and `Machine.git_dirty()` run `git rev-parse HEAD` and `git status --porcelain` with `cwd=str(config.repo_root())`. Without this the provenance block in the run manifest would describe whatever repository the operator's shell happened to be sitting in, or nothing at all.

### `config_path(*parts: str) -> Path`

Joins path components onto `config_dir()`.

- `*parts`: path components, e.g. `config_path("effectors", "p300.json")`.
- Returns: the joined `Path`. Does not check existence; propagates `FileNotFoundError` from `config_dir()` if `LAB_CONFIG` is bad.

### `sha256_of(path: Path) -> str`

Hex SHA-256 of a file's bytes.

- `path`: file to hash.
- Returns: 64-character lowercase hex digest.
- Raises: whatever `read_bytes()` raises (`FileNotFoundError`, `PermissionError`).
- **Note:** no caller in the repo uses this. `ConfigFile.__init__` hashes inline off the bytes it already read, which avoids a second read and guarantees the hash matches the parsed data. Treat `sha256_of` as a convenience for hashing something that is not a `ConfigFile` (a gcode file, say), and prefer `ConfigFile.sha256` for configs.

### `class ConfigFile`

A loaded JSON config plus the hash of the bytes it came from. Uses `__slots__ = ("path", "data", "sha256")`, so you cannot attach extra attributes to an instance (`AttributeError`).

Attributes:

- `path: Path` - where it was read from. `lab/logbook.py` reports `machine.deck.deck_cfg.path.parent` as the run's `config_dir`, and `Machine.config_hashes()` uses `Path(layout_cfg.path).name` as the manifest key, so a non-default layout file is named in the manifest rather than being flattened to "layout.json".
- `data: Any` - the parsed JSON (a dict for every config in this repo).
- `sha256: str` - hex digest of the raw bytes.

Why the class exists rather than a bare `json.load`: the hash and the data cannot drift apart, because both come from one `read_bytes()`. `Machine.config_hashes()` emits four entries (`deck.json`, `labware.json`, the layout file's real name, and `effectors/<id>.json`) into the manifest's provenance block. A gcode file plus that manifest fully determines the geometry it was generated from.

### `ConfigFile.__init__(self, path: Path) -> None`

Reads, hashes and parses one JSON config.

- `path`: file to load.
- Raises: `FileNotFoundError` if `path` is not a file, with a three-line message that names the file, the current `config_dir()` and the `LAB_CONFIG` override - written that way because "file not found" on a path resolved relative to a module is otherwise very hard for an operator to act on. `ValueError` (chained from `json.JSONDecodeError`) if the bytes are not valid JSON, naming the file and the parse position. A file that is not valid UTF-8 raises `UnicodeDecodeError` from `raw.decode("utf-8")` and is not wrapped.

### `ConfigFile.__getitem__(self, key: str) -> Any`

Indexes into `data`. Raises the plain `KeyError` from the dict. Used for keys that must exist, e.g. `labware_cfg["labware"]` in `Deck.__init__`.

### `ConfigFile.get(self, key: str, default: Any = None) -> Any`

`data.get`. Used for optional top-level blocks, e.g. `layout_cfg.get("placements", [])` and `layout_cfg.get("open_questions", [])`, so a layout with no placements loads and produces an empty deck rather than an exception.

### `ConfigFile.require(self, key: str) -> Any`

Like `__getitem__` but raises `KeyError(f"{self.path.name} is missing required key {key!r}")`, i.e. the message names the file.

- `key`: top-level key that must be present.
- Returns: `data[key]`.
- Raises: `KeyError` when absent.
- **Note:** no caller uses this; every reader currently uses `[]` or `.get`. It is the better choice for required keys (a bare `KeyError: 'axes'` does not say which of four JSON files is missing `axes`), so prefer it in new code.

### `ConfigFile.__repr__(self) -> str`

`<ConfigFile deck.json sha=1a2b3c4d>` - filename plus the first 8 hex characters of the hash. Enough to tell two configs apart in a traceback or a log line without dumping the whole file.

### `load(*parts: str) -> ConfigFile`

Loads `config_dir()/<parts...>` as a `ConfigFile`. Raises everything `ConfigFile.__init__` raises. The generic form; prefer the four named loaders below so the filenames live in one place.

### `load_deck() -> ConfigFile`

Loads `deck.json`. No parameters.

### `load_labware() -> ConfigFile`

Loads `labware.json`. No parameters.

### `load_layout(name: str = "layout.json") -> ConfigFile`

Loads a layout file, appending `.json` if `name` does not already end in it.

- `name`: layout filename or bare stem. `load_layout("bench_2026_08")` and `load_layout("bench_2026_08.json")` are the same call.
- Returns: `ConfigFile`. Raises `FileNotFoundError` / `ValueError` as above.
- Why: layout is the per-run file (the current one is `id: "accuracy_bench_2026_08"`), so it is the one config an operator is expected to swap. Accepting the stem means `Deck.load(layout="...")` can be driven from a CLI argument without the caller remembering the extension.

### `load_effector_spec(effector_id: str) -> ConfigFile`

Loads `effectors/<effector_id>.json`.

- `effector_id`: file stem, e.g. `"p300"`, `"p125"`, `"gripper"`.
- Returns: `ConfigFile`. Raises `FileNotFoundError` if there is no such file.
- Why: absolute working Z and the volume-to-displacement coefficients are effector properties, not labware properties - `p300` is `d = 0.0533*V + 0.122` and `p125` is `d = 0.1274*V + 0.049`, and a fitted 300 uL tip hangs 52.25 mm below the nosecone. Keeping one file per effector is what lets `labware.json` leave every absolute `z` null and point at `effectors/*.json -> z_for_labware`.

### `available_effectors() -> Dict[str, Path]`

Every effector JSON on disk, keyed by file stem.

- Parameters: none.
- Returns: `{stem: path}` from `sorted(dir.glob("*.json"))`; an empty dict if `config_dir()/effectors` does not exist. Never raises for a missing directory.
- Why: it makes the effector set discoverable instead of hardcoded. `lab/effector.py` calls it twice: `load()` uses it to reject an unknown id with the actual list of available ones plus the filename to create, and `available()` returns `sorted(...)` for CLI help. Adding a pipette is a new JSON file, not a code change.

## `lab/labware.py`

Labware geometry and its placement on the deck. Geometry (rows, cols, pitch, well naming, physical height) is slot-independent and lives in `labware.json`; *where* a piece of labware sits lives in `layout.json`. This module joins the two and answers exactly one question: given a well, what are its machine coordinates?

### The three vectors, and the two grid models

Every placement, whatever it was measured or declared as, collapses to the same three vectors:

```
xy(row, col) = origin + col * col_vec + row * row_vec
```

`origin` is the machine XY of well `(0, 0)`, `col_vec` is the mm displacement per unit column index, and `row_vec` the mm displacement per unit row index. All the model-specific work happens once, in `build_placement`, and everything downstream (`Placement.xy`, the bounds and centre helpers, the simulator's traverse-clearance sweep, `tools/check_alignment.py`) sees only the vectors. That is why a 96-position tip rack solved from three measured corners, a 5x10 tube holder built from a nominal pitch, and a single-point water reservoir all cost the same two multiplies and two adds, and why nothing downstream contains an `if model ==` branch.

**`pitch`** - the labware states `pitch_x`, `pitch_y` and integer direction vectors `col_dir` / `row_dir`; the placement states where A1 is. The directions are per-labware on purpose: `tube_rack_0p65_5x10` has `col_dir: [-1, 0]` (column index runs toward *lower* X) while `sbs_96_wellplate` has `col_dir: [1, 0]`. There is no global convention to hardcode. Optional `rotation` (degrees CCW) is applied to both vectors.

**`corners`** - the placement supplies three *measured well centres* (`origin`, `along_col`, `along_row`) and the module divides out the index spans to recover pitch and rotation together. For the loaded tip rack: `origin (18.0, 399.2)`, `along_col (118.0, 399.2)`, `along_row (18.0, 335.5)` over 12 columns and 8 rows gives `col_vec = (100.0/11, 0) = (9.090909..., 0.0)` and `row_vec = (0, -63.7/7) = (0.0, -9.1)`.

The corners model exists because the real tip pitch is **9.0909, not the nominal 9.00**. The error per column is only 0.09 mm, but it accumulates: `11 * 0.0909 = 1.0 mm` of X error by the last column, which is enough to miss a tip entirely. A pitch model with `pitch_x: 9.0` would place column 0 perfectly and column 11 at X 117.0 instead of 118.0. Measuring three corners also picks up any rack rotation for free, and the same measurement independently confirmed the coordinate frame: the solved grid centre X 68.0 against slot 3's centre X 65.0 is a 3.0 mm offset, consistent with a rack that is already in the post-shift frame, which is why the tip rack is on the `unaffected` list of the `+70` X correction while the tube rack's `anchor_xy` had to be moved from X 186 to X 256.

There is also a **`single`** model (`reservoir_water`): one position, `col_vec` and `row_vec` both `(0.0, 0.0)`, so the same arithmetic returns the origin for the only legal well.

**Note:** `labware.json`'s `grid_models` documentation block now lists a third name, `a1_from_slot_edge`, alongside `pitch` and `corners`. It is not a grid model as far as this code is concerned - `build_placement` accepts only `pitch`, `corners` and `single`, and raises `ValueError: unknown grid model 'a1_from_slot_edge'` for anything else. It is an *anchoring* strategy read by `_resolve_origin`. The `tube_rack_1p5` entry does the right thing (`grid.model: "pitch"` with `a1_from_slot_edge` as a sibling of `grid`); do not copy the doc block's framing into a new labware's `model` field.

### `_rotate(v: Vec2, degrees: float) -> Vec2` (private)

Rotates a vector CCW about the origin, special-casing 0/90/180/270 as exact integer swaps before falling back to `math.cos`/`math.sin`.

The special cases are not an optimisation. `math.cos(math.radians(90))` is `6.12e-17`, so the generic path would give a 12.0 mm column vector a Y component of `7.3e-16`, and `col_x_values()` would start reporting `255.99999999999997` instead of `256.0` - which then breaks the exact-equality assertions in `tests/test_lab.py` and makes diffs of generated gcode unreadable. Called only from the `pitch` branch of `build_placement`. Protocol authors should never call it.

### `well_name(row: int, col: int) -> str`

0-indexed `(row, col)` to an `'A1'` style label; row 0 is `A`, col 0 is `1`.

- `row`, `col`: 0-based indices (not mm).
- Returns: e.g. `well_name(1, 2) == "B3"`.
- Raises: `ValueError` if `row` is negative or `>= 26` ("row {row} has no single-letter name") - there is no `AA` naming.
- Why: the 1-based-in-the-name, 0-based-in-code off-by-one is centralised here instead of being re-derived at each call site. It is the labelling used in the run manifest and by `tools/check_alignment.py` when it maps an old gcode coordinate back onto a named well, so a bug here would silently mislabel provenance rather than crash.
- **Note:** `col` is not validated. `well_name(0, -1)` returns `"A0"`, and `parse_well("A0")` round-trips it back to `(0, -1)`, which only fails later at `check_well`. Also relevant: `tiprack_300_96.grid.a1_corner` is `"unconfirmed"` in `labware.json` - the three measured corners fix the grid but not which physical corner is called A1, so tip well *names* in a manifest are not yet trustworthy even though the coordinates are.

### `parse_well(name: str) -> Tuple[int, int]`

`'B3'` to `(1, 2)`.

- `name`: well label; whitespace-stripped and upper-cased first, so `" b3 "` works.
- Returns: `(row, col)`, both 0-based.
- Raises: `ValueError` if shorter than 2 characters, if the first character is not alphabetic, or if the remainder is not all digits. A non-ASCII alphabetic first character (`"A1"` typed with a Cyrillic A) passes the `isalpha()` check and then raises `ValueError` from `string.ascii_uppercase.index` with a less helpful message.
- Why: lets a protocol say `p.well("A1")` instead of `p.xy(0, 0)`. It does *not* bound-check against the actual labware - that is `Placement.check_well`'s job, reached via `Placement.xy`.

### `class Labware` (frozen dataclass)

Slot-independent geometry, straight out of `labware.json`. Fields: `id` (the key in the JSON's `labware` map, e.g. `"tiprack_300_96"`) and `spec` (the raw JSON subtree, kept whole so keys this class does not model are still reachable, as `Machine` does for `consume_order`).

Frozen because `Deck.catalogue` holds one instance per labware type and shares it with every `Placement` of that type. If a placement could mutate it, moving one rack would silently change the geometry of another. All the accessors below are read-only views over `spec` with defaults, so a partially specified labware loads instead of raising at import time - the library's convention is to let unmeasured fields be `None` and refuse to emit *motion* later, not to refuse to *load*.

### `Labware.display_name -> str`

`spec["display_name"]`, falling back to `id`. Human-facing strings only, e.g. `"300 uL ClipTip rack, 96 position"`.

### `Labware.type -> str`

`spec["type"]`, default `"unknown"`. One of `tube_rack`, `tiprack`, `wellplate`, `reservoir` in the current config. Categorical only; nothing dispatches motion on it.

### `Labware.grid -> Dict[str, Any]`

The raw `grid` block, or `{"model": "single", "rows": 1, "cols": 1}` if the labware has none. The default means a labware entry that forgot its grid behaves as a single-position station rather than crashing with a `KeyError` deep inside `_pitch_vectors`.

### `Labware.rows -> int` / `Labware.cols -> int`

`int(grid["rows"])` / `int(grid["cols"])`, default 1 each. `tube_rack_0p65_5x10` is 5 x 10, `tiprack_300_96` is 8 x 12, `tube_rack_1p5` is 3 x 7, `reservoir_water` is 1 x 1. These are the bounds `Placement.check_well` enforces and the spans `_corner_vectors` divides by, so an incorrect count corrupts the solved pitch rather than just the range check: `tube_rack_1p5` lists `grid.rows` and `grid.cols` in its own `uncalibrated` array precisely because they were derived from what fits a location, not stated by the operator.

### `Labware.model -> str`

`grid["model"]`, default `"single"`. See the grid-models section above for the three values `build_placement` accepts.

### `Labware.footprint -> Optional[Vec2]`

Physical outline `(width_mm, height_mm)`, or `None` when the labware has no `footprint` key or it is `null`.

Only `sbs_96_wellplate` has one today: `[127.76, 85.48]`, the standard SBS outline. Both tube holders and the tip rack are `null`, which is why `Placement.footprint_bounds()` returns `None` for every currently loaded placement and the simulator downgrades its traverse-clearance check to well centres with a warning.

### `Labware.wipe -> Optional[Dict[str, Any]]`

The wall-wipe parameters, or `None`.

For `tube_rack_0p65_5x10`: `{"dx": 3.2, "dz": 4.0, "feed": 1500, "uncalibrated": true}`. Consumed by `Machine`'s wall wipe, which steps `dx` sideways to contact the inner wall, moves `z_work - dz` (which is 4.0 mm *upward*, since +Z is down) to drag the droplet up, then returns. The physical constraint encoded in `dx`: it must be less than the tube inner radius, or the tip is driven into the wall rather than brushed against it. Both `dx` and `dz` are listed in the labware's `uncalibrated` array.

### `Labware.height_mm -> Optional[float]`

Physical height above the stage in mm, contents included when known: returns `height_with_contents_mm` if set, otherwise `height_mm`, otherwise `None`.

- Returns: `35.0` for the 0.65 mL holder, `45.0` for the 1.5 mL holder, `None` for the tip rack, the SBS plate and the reservoir.
- Why a height and not a commanded Z: a height above the stage is independent of both the coordinate frame and the fitted effector, whereas a commanded Z is not. Travel Z is 5.0, so with a 300 uL tip fitted the orifice rides `114.75 - 52.25 - 5.0 = 57.5` mm above the stage; anything taller than 57.5 mm is struck at travel height. At 35.0 mm the 0.65 mL holder has 22.5 mm of headroom for tubes and caps, and the 1.5 mL holder has 12.5 mm. Storing `35.0` survives a change of pipette; storing "Z 57.5 is fine" would not.
- **Note:** nothing calls this property yet. `lab/simulate.py`'s traverse-clearance check still reads `p.labware.z("top")`, which is `null` for every labware in the config, so it emits `cannot check traverse clearance over tubes ... its z.top rim height is unmeasured` even though that holder's height *is* measured at 35.0. `labware.json`'s own `top_note` for that rack says z.top is `"Superseded by height_mm"`. Wiring the simulator to `height_mm` (minus the effector's reach) is the outstanding change; until then the measured heights are documentation, not a check.

### `Labware.height_is_empty_only -> bool`

`True` when `height_mm` is set but `height_with_contents_mm` is `null`, i.e. only the empty holder was measured.

`True` for both tube holders today. It exists so a clearance check can distinguish "35 mm and that is the whole story" from "35 mm empty, plus an unmeasured amount of tube and cap", which are very different things to plan a traverse against. Also unused so far.

### `Labware.approach_order -> Optional[str]`

Preferred XY axis order when approaching a well in this labware: `"YX"`, `"XY"`, or `None` if unspecified. Upper-cased on read, so `"yx"` in JSON works.

- Returns: `"YX"` for both tube holders, `None` for everything else.
- Why: this encodes a real obstruction. `"YX"` means reach the Y position first while still clear of the holder in X, then come in along X. The tube caps make Y motion *across* the holder difficult, while X motion passes between cap rows. `Machine.goto` is passed this as `prefer=p.labware.approach_order` from `goto_well`, `get_tip` and `dispense`; it validates the value (`ProtocolError` if it is not `"XY"` or `"YX"`) and the keep-out router may still override it, saying so when it does. Set this on the labware, not in a protocol, so every protocol that touches the rack inherits it.

### `Labware.uncalibrated -> List[str]`

Dotted names of fields that are not yet trusted, e.g. `["wipe.dx", "wipe.dz", "z.work"]` for the 0.65 mL holder and `["grid.a1_corner", "consume_order.exit_edge"]` for the tip rack. Returns a fresh `list` (a copy, so a caller cannot append into the shared spec); `[]` when the key is absent.

`Deck.uncalibrated()` aggregates these across placements, `Machine.uncalibrated()` adds the effector's, and the result lands in the run manifest and in `protocols/accuracy_test.py`'s pre-run printout. The point is that "we do not know this yet" is a first-class, machine-readable value that follows the geometry into every artefact, instead of a comment someone has to remember to read.

### `Labware.z(self, key: str) -> Optional[float]`

One nominal Z from the labware's `z` block, in mm of commanded Z (larger = physically lower).

- `key`: e.g. `"top"`, `"work"`, `"bottom"`, `"approach"`, `"pick"`.
- Returns: the value as `float`, or `None` if the `z` block is missing, the key is absent, or the value is `null`. Never raises on an unknown key.
- Why `None` everywhere in the current config: absolute Z is effector-dependent, because a fitted tip hangs 52.25 mm below the nosecone, so the real working Z lives in `effectors/*.json` under `z_for_labware` and is looked up by `Machine`. These fields stay null rather than being filled with a plausible guess. The only current consumer is the simulator's clearance check, which treats `None` as "cannot check" and warns.

### `class Placement` (mutable dataclass)

One piece of labware at a definite spot on the deck: the resolved product of a `labware.json` entry and a `layout.json` entry. Built by `build_placement`, keyed by name in `Deck.placements`.

Fields:

- `name: str` - the layout's `name`, e.g. `"tubes"`, `"tips"`, `"water"`. `Deck` rejects duplicates.
- `labware: Labware` - the shared geometry object.
- `origin: Vec2` - machine XY (mm) of well `(0, 0)`.
- `col_vec: Vec2` - mm displacement per unit column index, rotation already applied.
- `row_vec: Vec2` - mm displacement per unit row index, rotation already applied.
- `mount: str` - `"slot"`, `"station"` or `"fixed"` (the default when the layout omits it). `Deck.slot_seating_report` only checks placements with `mount == "slot"`; a `station` mount carries `near_slot` as a human reference, not a seating claim.
- `rotation: float` - degrees CCW, default 0.0. Informational on the resolved object, since it is already baked into the two vectors; forced to `0.0` for the corners model.
- `slot: Optional[int]` - slot number if slot-mounted (front row 1-3 at Y 365, middle 4-6 at Y 272, back 7-9 at Y 178.5, X descending within a row).
- `station: Optional[str]` - station name if station-mounted, e.g. `"water"`.
- `spec: Dict[str, Any]` - the raw layout entry, so notes and unmodelled keys stay reachable (`Deck` reads `near_slot` and `slot_disputed` out of it).

Not frozen, unlike `Labware`, but treat it as read-only: mutating `origin` after construction bypasses every consistency check in `_corner_vectors` and in `Deck.__init__`.

**Note:** `spec` is not identical to the layout file. `Deck.__init__` injects a private `_slot_pitch` key into a shallow copy of the entry before calling `build_placement` (because `a1_from_slot_edge` needs the cell size), so `spec["_slot_pitch"] == [135.5, 93.25]` is present on every slot-mounted placement. Harmless today because nothing serializes `spec` wholesale, but do not assume `spec` round-trips to `layout.json`.

### `Placement.xy(self, row: int, col: int) -> Vec2`

The machine XY of one well.

- `row`, `col`: 0-based indices.
- Returns: `(x, y)` in mm, each rounded to 4 decimal places. Rounding matters because the tip rack's pitch is `100.0/11`, so column 11 computes to `118.00000000000001` and would otherwise print that way in gcode and in every diff.
- Raises: `IndexError` via `check_well` for an out-of-range index.
- Real values: `tubes.xy(0, 0) == (256.0, 223.0)`, `tubes.xy(0, 9) == (148.0, 223.0)`, `tubes.xy(4, 0) == (256.0, 151.0)`; `tips.xy(0, 0) == (18.0, 399.2)`, `tips.xy(7, 11) == (118.0, 335.5)`.

### `Placement.well(self, name: str) -> Vec2`

`parse_well` then `xy`. `p.well("B3")` for `p.xy(1, 2)`.

- `name`: `'A1'` style label.
- Returns: `(x, y)` in mm.
- Raises: `ValueError` for a malformed name, `IndexError` for a well the labware does not have.
- **Note:** for the tip rack, well names are provisional - `grid.a1_corner` is `"unconfirmed"`, so `tips.well("A1")` is a well-defined *coordinate* (18.0, 399.2) but not a confirmed *label*. Index-based access is the honest interface there until the corner is confirmed.

### `Placement.check_well(self, row: int, col: int) -> None`

Bounds check. Raises `IndexError` naming the placement, the indices, the labware id and its actual dimensions (`"tubes: well (row=5, col=0) is outside tube_rack_0p65_5x10 which is 5 x 10"`); returns `None` otherwise.

Why the loud message: an off-grid index does not produce an error on this machine, it produces a coordinate. Well `(0, 10)` of the tube rack would resolve to X 136, one clean pitch step past the last real column and into the next location, and the tip would be driven into the stage or an edge piece. Called by `xy`, and directly by `Machine` before it commits to a move.

### `Placement.all_wells(self) -> List[Tuple[int, int]]`

Every `(row, col)` pair, row-major (all of row 0 first). 50 entries for the tube rack, 96 for the tip rack. Returns a fresh list; never raises.

Note the ordering: many protocols on this deck want *column*-major iteration (row index selects volume, column index selects replicate, per the tube-rack layout notes), and the tip rack must be consumed column by column. Use `col_x_values()` or your own loop for those; `all_wells()` is for exhaustive passes such as tests and a stage tour.

### `Placement.corner_wells(self) -> List[Tuple[int, int]]`

The four extreme wells: `[(0, 0), (0, cols-1), (rows-1, 0), (rows-1, cols-1)]`.

For a 1x1 labware such as `reservoir_water` all four entries are `(0, 0)`; for a single-row or single-column labware they contain duplicates. Harmless, because the only consumer takes a min and a max.

### `Placement.well_bounds(self) -> Tuple[float, float, float, float]`

`(x_min, x_max, y_min, y_max)` in mm over **well centres**, not the physical outline.

- Returns: `tubes -> (148.0, 256.0, 151.0, 223.0)`; `tips -> (18.0, 118.0, 335.5, 399.2)`; `water -> (70.0, 70.0, 175.0, 175.0)`.
- Why centres, and why it matters: the rim of a rack extends past the outermost well centre by an unmeasured amount, so these bounds *understate* the obstacle. The simulator prefers `footprint_bounds()` and only falls back to this with an explicit warning that a path clipping the rim will not be caught. `Deck.slot_seating_report` uses the bounds to check a slot claim: the tube grid spans X 148-256, Y 151-223, entirely inside slot 8's cell of `[132.25, 267.75] x [131.75, 225.25]`.

### `Placement.grid_center(self) -> Vec2`

Centre of `well_bounds()`, each component rounded to 4 dp.

- Returns: `tubes -> (202.0, 187.0)`, `tips -> (68.0, 367.35)`, `water -> (70.0, 175.0)`.
- Why: this is the number that catches a wrong slot label or a wrong coordinate frame. `Deck.slot_seating_report` compares it against the claimed slot centre and complains when the offset exceeds half a slot pitch. It is also the evidence in the frame audit: the tube grid centre `132 + 70 = 202` against slot 8 at 200 is 2.0 mm, whereas unshifted it straddles the slot 8/9 seam at X 132.25 and is seated in neither.
- Because the grid is a parallelogram, its bounding-box centre equals its true centroid, so this agrees exactly with the centroid `_corner_vectors` validates against `corners.solved.grid_center` - the two are not two different definitions that could disagree under rotation.

### `Placement.footprint_bounds(self) -> Optional[Tuple[float, float, float, float]]`

Physical outline bounds in mm, or `None` if the footprint was never measured.

- Returns: `(cx - w/2, cx + w/2, cy - h/2, cy + h/2)` around `grid_center()`, or `None`. `None` for all three currently loaded placements, since only `sbs_96_wellplate` has a footprint and it is not loaded.
- Why `None` and not an estimate: it keeps the overlap and clearance checks honest. `Deck`'s overlap check and the simulator both report "cannot check" and warn, rather than passing on a guess. A fabricated footprint would turn a missing measurement into a silent green light.
- **Note:** it assumes the footprint is centred on the grid centre, which is only true for labware whose grid genuinely is centred on its outline. `sbs_96_wellplate` declares `grid_centered_on_footprint: true` and satisfies it (`14.38 + 11*9/2 = 63.88 = 127.76/2`), but nothing here checks that declaration before centring. If someone measures and adds a footprint for the tip rack, whose well span of 100 x 63.7 mm is much smaller than its box and not obviously concentric with it, this would return mis-centred bounds that the simulator's clearance check would then trust. Gate on `grid_centered_on_footprint` or store an explicit outline offset before adding footprints to off-centre labware.

### `Placement.col_x_values(self) -> List[float]`

X coordinate (mm) of each column, taken along row 0.

- Returns: `tubes -> [256.0, 244.0, 232.0, 220.0, 208.0, 196.0, 184.0, 172.0, 160.0, 148.0]`; `tips -> [18.0, 27.0909, 36.1818, 45.2727, 54.3636, 63.4545, 72.5455, 81.6364, 90.7273, 99.8182, 108.9091, 118.0]`.
- Why: the protocols on this deck are column-loops, and the tip rack's consume order is a column policy. `Machine._next_free_tip_col` works from this ladder to pick the first unused column counting inward from the exit edge, because after pickup the effector cannot pass over occupied tip positions without shearing the tip off, and the first move after pickup is in X. The tip list above is also the clearest single argument for the corners model: with a nominal 9.00 pitch the last entry would be 117.0.

### `Placement.row_y_values(self) -> List[float]`

Y coordinate (mm) of each row, taken along column 0.

- Returns: `tubes -> [223.0, 205.0, 187.0, 169.0, 151.0]` (descending, because `row_dir` is `[0, -1]`); `tips -> [399.2, 390.1, 381.0, 371.9, 362.8, 353.7, 344.6, 335.5]`.
- **Note on both of these:** each samples only one line of the grid (row 0, column 0 respectively). That is exact for an axis-aligned grid, which all three loaded placements are, but for a rotated `pitch` grid or a skewed corner solve the values along one edge are not the values for the whole column or row. Use `xy(row, col)` if you ever place something rotated.

### `Placement.__repr__(self) -> str`

`<Placement tubes tube_rack_0p65_5x10 slot origin=(256.0, 223.0) 5x10>` - name, labware id, mount, origin and dimensions, which is enough to spot a wrong frame or a wrong rack in a traceback.

### `_pitch_vectors(labware: Labware) -> Tuple[Vec2, Vec2]` (private)

Builds `(col_vec, row_vec)` from the labware's nominal pitch and direction vectors.

`col_dir` defaults to `[1, 0]` and `row_dir` to `[0, -1]`. Raises `KeyError` if `pitch_x` or `pitch_y` is missing. For `tube_rack_0p65_5x10`: `12.0 * [-1, 0] = (-12.0, 0.0)` and `18.0 * [0, -1] = (0.0, -18.0)`. Not for protocol authors; `build_placement` is the entry point.

### `_corner_vectors(labware: Labware, corners: Dict[str, Any]) -> Tuple[Vec2, Vec2, Vec2]` (private)

Solves origin, pitch **and** rotation from three measured well centres, and optionally verifies the solve against numbers recorded in the placement.

- `corners`: the placement's `corners` block. Requires `origin`, `along_col` and `along_row`, each `[x, y]` in mm of measured *well centre* (not box outline). Optional `solved` block with `col_pitch`, `row_pitch`, `grid_center`.
- Returns: `(origin, col_vec, row_vec)`; `col_vec = (along_col - origin) / (cols - 1)`, `row_vec = (along_row - origin) / (rows - 1)`.
- Raises: `KeyError` naming the missing corner key; `ValueError` if the labware has fewer than 2 rows or 2 columns (you cannot divide by a zero span); `ValueError` if `solved.col_pitch` or `solved.row_pitch` disagrees with the computed `math.hypot` by more than `1e-6`, or if `solved.grid_center` disagrees with the computed centroid by more than `1e-4`, in both cases telling the operator to re-measure or delete `corners.solved`.
- Why the cross-check: a stored number that nothing verifies is exactly how the old scripts' hardcoded displacement tables drifted away from the equations printed above them. Here the stored expectation is a tripwire, not documentation. The tolerances are set for float noise, not for measurement slop: the tip rack's row pitch computes to `9.099999999999998` against a stored `9.1`, which passes, while a genuinely stale entry (a 9.00 pitch left behind after re-measuring) fails at load.

### `_resolve_origin(entry, labware, deck, name, needs_grid, col_vec=(0,0), row_vec=(0,0)) -> Vec2` (private)

Answers "where is well (0, 0)?" by strict precedence.

- `entry`: the layout placement dict (as amended by `Deck` with `_slot_pitch`).
- `labware`: the geometry, consulted for fallback keys held on the labware rather than the placement.
- `deck`: the **raw** `deck.json` dict, needed for `deck["slots"]` and `deck["stations"]`.
- `name`: placement name, for error messages only.
- `needs_grid`: `False` for the `single` model, `True` for `pitch`. When `False`, a slot mount returns the bare slot centre.
- `col_vec`, `row_vec`: mm per index, already rotated, used only by the last fallback to halve the grid span.
- Returns: `(x, y)` in mm.

Precedence, in order:

1. `entry["anchor_xy"]` - an explicitly measured machine coordinate always wins. This is how `tubes` is anchored: `[256.0, 223.0]`, the `+70`-corrected value of the old scripts' X 186.
2. `entry["station"]` - the station's `xy` from `deck.json`. `water` resolves to `(70.0, 175.0)`, which is a *dip point* rather than a trough centre, and is kept as a station for exactly that reason. Raises `KeyError` for an unknown station name.
3. `entry["slot"]` - the slot centre from `deck.json`, then, if `needs_grid`:
   - `a1_from_center` (placement first, then labware): slot centre plus the stored offset.
   - `a1_from_slot_edge` (placement first, then labware): A1 dimensioned from the *location edges*. The cell is the slot centre expanded by half the slot pitch, so with `_slot_pitch == [135.5, 93.25]` the half-extents are `(67.75, 46.625)`. `x_edge: "high"` gives `cx + 67.75 - x_inset` and `"low"` gives `cx - 67.75 + x_inset`, same for Y with `y_edge`; both default to `"high"`. Result rounded to 4 dp. For `tube_rack_1p5` (`x_inset 14.0`, `y_inset 13.0`) this is `(cx + 53.75, cy + 33.625)`, matching the offsets recorded in `labware.json`; in slot 5 that is A1 at `(253.75, 305.625)` with columns running down to X 133.75, inside slot 5's cell.
   - Otherwise the labware must declare `grid_centered_on_footprint`, in which case A1 is the slot centre minus half the grid span.
   - Raises `KeyError` for an unknown slot; `ValueError` if `a1_from_slot_edge` is used but `_slot_pitch` was not injected ("Deck must pass it in"); `ValueError` if none of the above applies, refusing to guess where A1 is.
4. Nothing at all - `ValueError`: no `anchor_xy`, no slot, no station means there is no way to know where it is.

Why the precedence runs measured-before-derived: every derived form inherits error from the deck frame, and this deck's frame is known to be imperfect. The `a1_from_slot_edge` path is the weakest of them - **Note:** `Deck._raw_slot_pitch` computes the pitch as `(max - min) / (n - 1)`, i.e. a *mean*, while `deck.json` records the real spacing as irregular ("X pitch is 136/135 and Y pitch is 93.0/93.5 - irregular by ~1 mm", provenance unverified). An edge-dimensioned A1 therefore carries up to roughly 0.5 mm of frame error, varying by slot, on top of the operator's own `x_edge` uncertainty (`tube_rack_1p5` lists `a1_from_slot_edge.x_edge` as uncalibrated: if "the very left" is the low-X edge rather than the high-X edge, the whole grid mirrors). Replace it with a measured `anchor_xy` as soon as the rack is on the deck.

Also note that with `needs_grid=False` the slot branch returns the slot centre and ignores `a1_from_center` and `a1_from_slot_edge` entirely. That is correct for a one-position vessel, but it will silently ignore an offset someone adds to a `single` labware. And `entry.get("anchor_xy")` is a truthiness test, so an empty list falls through to the next rule rather than erroring.

Not for protocol authors.

### `build_placement(entry: Dict[str, Any], labware: Labware, deck: Dict[str, Any]) -> Placement`

Turns one `layout.json` `placements[]` entry into a resolved `Placement`. This is the module's public entry point and the only place the grid models are distinguished.

- `entry`: one element of the layout's `placements` array. Reads `name` (default: the labware id), `mount` (default `"fixed"`), `rotation` (degrees CCW, default 0), `slot`, `station`, and whichever anchoring keys apply. Stored whole on the result as `Placement.spec`.
- `labware`: the `Labware` from `Deck.catalogue` for `entry["labware"]`.
- `deck`: the **raw** `deck.json` dict, not a `Deck` object - it needs `deck["slots"]` and `deck["stations"]`. Passing the wrong one is the easy mistake here.
- Returns: a fully resolved `Placement` with `origin`, `col_vec` and `row_vec` filled in.
- Raises:
  - `KeyError` if the labware's model is `"corners"` but the placement has no `corners` block, naming both the placement and the labware.
  - `ValueError` if `rotation` is nonzero with the `"corners"` model. The measured corners already encode any rotation, so applying one on top double-counts it and rotates the rack away from where it was measured. The message says to set rotation to 0 and re-measure if the rack moved.
  - `ValueError` for any model other than `pitch`, `corners` or `single`.
  - Everything `_corner_vectors`, `_pitch_vectors` and `_resolve_origin` raise.
- Dispatch: `corners` solves the vectors and stores `rotation=0.0`; `single` resolves the origin with `needs_grid=False` and sets both vectors to `(0.0, 0.0)`; `pitch` builds nominal vectors, rotates them, then resolves the origin with `needs_grid=True` so the grid-span fallback can use the rotated vectors.
- Why it is a free function rather than a `Placement` classmethod: `Deck.__init__` calls it once per layout entry, and it is the seam the tests exercise directly - `tests/test_lab.py` asserts exact resolved coordinates, and `tests/test_review_fixes.py` builds deliberately broken entries to confirm each of the raises above actually fires. Nothing after this point can be model-specific, which is what keeps `Machine` and `simulate` free of geometry branches.

---

## `lab/effector.py`

End effectors: one JSON file per effector in `config/effectors/`, one class here per `kind`. The module owns three things and only three: effector identity and provenance (id, display name, config SHA-256), the per-effector Z table for each labware, and - for pipettes - the volume-to-plunger-displacement calibration plus every guard on plunger travel. It also defines `LabError`, the root of the whole library's exception tree, because `lab/deck.py`, `lab/simulate.py` and `lab/machine.py` all import it from here.

Adding an effector is a new JSON file. Adding an effector *kind* is a new JSON file plus a class here plus an entry in `_KINDS`. Nothing else in the library needs to change.

Two conventions apply throughout:

- **Volumes are always uL.** Millimetres are computed, never typed.
- **Plunger POS is a millimetre coordinate on the actuator axis** driven by the `ACTUATOR_MOVE POS=` macro, and it counts *down* as fluid is drawn in: `blowout_pos` (30.0 for p300) is the plunger bottomed out, and a smaller POS means more air displaced and more liquid held. It is a separate axis from the Cartesian Z and does not share Z's "larger is lower" sense in any physical way - but numerically it also has its dangerous end at the low numbers, since POS 0 is a hard stop.

### Exception hierarchy

```python
class LabError(Exception)
class EffectorError(LabError)
class VolumeError(EffectorError)
```

`LabError` is the base for every error the library raises deliberately. The full tree, gathered from across the package:

```
LabError                       lab/effector.py
├── EffectorError              lab/effector.py   effector cannot do what was asked
│   └── VolumeError            lab/effector.py   volume outside what can be metered
├── DeckError                  lab/deck.py       deck/labware/layout geometry
├── SimulationError            lab/simulate.py   a move the simulator rejects
└── ProtocolError              lab/machine.py    protocol sequencing (no tip fitted, etc.)
```

**Why it exists:** every message in this library is written to tell the operator what to fix and where, so a protocol script can do `except LabError as e: print(e); sys.exit(1)` and produce a one-line actionable failure instead of a traceback. `protocols/accuracy_test.py` does exactly that.

`VolumeError` is separated from `EffectorError` so a caller sweeping a range of volumes can catch "this volume is out of range" and skip the point, without also swallowing "this effector has no Z for that labware".

**Note:** the split between `VolumeError` and `EffectorError` is not clean along the axis you would expect. `check_volume` raises `VolumeError`, but `check_pos` raises plain `EffectorError` even when the cause is a volume that needs too much stroke. In practice `check_volume`'s stroke test fires first and gives the better message, so `except VolumeError` usually suffices - but do not rely on it.

**Note:** malformed or incomplete config is *not* under `LabError`. `lab/config.py` raises `FileNotFoundError` for a missing file and `ValueError` for bad JSON, and `Pipette.__init__` raises bare `KeyError` if `actuator.blowout_pos`, `actuator.air_gap`, `actuator.gap_pos`, `calibration.slope`, `calibration.intercept`, `calibration.window_uL` or `nominal_uL` is absent. A protocol that catches only `LabError` will still show a traceback when someone breaks a config file.

---

### class `Effector` (base)

Identity, config access, the Z table, and the boolean flags that let `Machine` and `Simulator` branch on effector capability without knowing which effector is loaded. Class attribute `kind = "generic"`; subclasses override it and it is what `load()` dispatches on.

### `Effector.__init__(self, cfg: config.ConfigFile) -> None`

Wraps an already-loaded `ConfigFile` and pulls out the identity fields.

- `cfg` - a `lab.config.ConfigFile` for one `config/effectors/<id>.json`. Do not construct this yourself; call `load()`.

Sets:

| attribute | meaning |
| --- | --- |
| `self.cfg` | the `ConfigFile` object |
| `self.spec` | `cfg.data`, the raw parsed JSON dict. Subclasses and `Machine` read fields off this directly for things with no accessor (`z_conflict`, `actuator.decimals`, `z_for_labware`) |
| `self.id` | `spec["id"]`, e.g. `"p300"` |
| `self.display_name` | `spec["display_name"]`, falling back to `id` |
| `self.sha256` | SHA-256 of the config file bytes |

**Raises:** `KeyError` if the JSON has no `id`.

**Why `sha256` is carried on the effector:** `Machine.config_hashes()` writes it into the run manifest as `effectors/<id>.json`, so a generated `.gcode` can be traced back to the exact calibration coefficients that produced it. If someone re-fits the curve, old gcode is still attributable to the old numbers.

### `Effector.z_for(self, labware_id: str, key: str = "work") -> float`

Absolute machine Z, in mm, for this effector working at this labware.

- `labware_id` - a labware type id from `config/labware.json`, e.g. `"reservoir_water"`, `"tube_rack_0p65_5x10"`, `"tiprack_300_96"`. Not a slot number and not a placement name.
- `key` - which Z within that labware's entry. In use today: `"work"` (submerged / dispensing depth), and for the tip rack `"approach"` (47.0) and `"pick"` (59.0).

**Returns:** the Z as a float. Remember +Z is down: p300 at `reservoir_water` `work` is 40.0, which is 22.5 mm *above* the stage floor with a tip fitted (62.5).

**Raises** `EffectorError` in three distinct cases, each with a different fix:

1. No entry for that labware at all - message lists the known labware ids and names the file and key to add.
2. Entry exists but not that key - e.g. `p300.z_for("tiprack_300_96", "work")` fails, because that entry only has `approach` and `pick`.
3. The key is present but `null` - "it has never been measured".

**Why it lives on the effector and not on the labware:** a fitted tip moves the reference point. The p300 bare nosecone contacts the stage at Z 114.75; with a 300 uL tip it contacts at 62.5. The tip is therefore 52.25 mm long, and *the same tube needs a commanded Z 52.25 mm smaller with a tip on*. A per-labware Z table would be silently wrong for every effector but the one it was measured with. The mistake this prevents is exactly the one recorded in `config/effectors/p125.json` under `z_conflict`: p125's water Z (50.0) is 10 mm lower than p300's (40.0) while its tube Z (20.0) is 25 mm higher than p300's (45.0). A constant tip-length offset would move both in the same direction by the same amount, so those two numbers cannot both be right. Keeping the table per-effector is what makes that contradiction visible instead of averaging it away.

**Note:** case 2's "Known:" list filters out keys whose name ends in `note`, but nothing else - so for `p300` / `tiprack_300_96` it also offers `superseded` (value 57.5, the old `Z_TIP_PICK`), which is not a usable Z key.

### `Effector.z_is_uncalibrated(self, labware_id: str) -> bool`

True if this effector's entry for that labware carries `"uncalibrated": true`.

- `labware_id` - labware type id, as for `z_for`.

**Returns:** `bool`. `p300.z_is_uncalibrated("tube_rack_0p65_5x10")` is `True` (its config note: "was marked `<-- TUNE` in the source and was never tuned"); `p300.z_is_uncalibrated("reservoir_water")` is `False`.

**Raises:** nothing.

**Why:** `Machine` and `Simulator` use it to emit a loud warning in the gcode and the manifest rather than refusing to generate. The value is usable, it is just not trustworthy, and the operator needs to know that before the run rather than after.

**Note:** it returns `False` for a labware with no entry at all, which is indistinguishable from a calibrated one. Use `z_for` (which raises) to detect absence; do not use this as a presence test.

### `Effector.needs_tip` (property) -> `bool`

`tips.required` from the config, default `False`. `True` for p300, `False` for p125 and the gripper.

**Why:** `Machine.aspirate_uL` refuses to run if `needs_tip` and no tip is fitted, and `Machine.get_tip` refuses if `not needs_tip` (so a p125 script cannot accidentally drive a tip-press stroke). `protocols/accuracy_test.py` also ANDs it with the `--get-tip` flag so the manifest records what was actually done, not what was requested.

**Note:** for p125 this flag is listed in that file's own `uncalibrated` array. The old p125 script did no tip pickup, and whether that was intentional (bare nosecone) or an omission is unresolved - and it changes every Z in that file.

### `Effector.tip_labware` (property) -> `Optional[str]`

The labware type id this effector takes tips from: `"tiprack_300_96"` for p300, `None` for p125 and the gripper.

**Why:** `Machine._tip_placement()` matches the *labware type* rather than trusting a placement named "tips". Routing by placement name alone would send a second effector with a different tip geometry to the first rack it finds and press a 300 uL tip onto the wrong nosecone.

### `Effector.can_eject_tip` (property) -> `bool`

`tips.eject_supported`, default `False`. `True` only for p300.

**Why:** `Machine.eject_tip` refuses outright when false, and `Simulator` uses it in its end-of-run check for a tip left fitted. The ejector routine (station `tip_eject`: lane X 155, Z 145 down to Z 137, an 8 mm *upward* strip because +Z is down) is flagged `"validated": false` in `deck.json` and has no working gcode reference anywhere in the repo, so gating it behind a per-effector flag keeps it out of the path of any effector that has not opted in.

### `Effector.is_placeholder` (property) -> `bool`

True when `spec["status"] == "placeholder"`. Only the gripper.

**Why:** it is the flag `load()` uses to refuse the file. See `load()`.

### `Effector.uncalibrated(self) -> List[str]`

Every field this effector's config declares unmeasured, each prefixed with the effector id.

**Returns:** e.g. for p300 `['p300.z_for_labware.tube_rack_0p65_5x10.work']`; for p125 four entries including `'p125.tips.required'` and `'p125.z_reference.stage_surface'`; for the gripper the single entry `'gripper.ALL'`.

**Raises:** nothing.

**Why:** `Machine.uncalibrated()` concatenates this with the deck's list and both the printed report and the run manifest carry it. Unmeasured numbers are still used to generate motion, so the run record has to state which numbers were guesses. The id prefix is what keeps deck entries and effector entries distinguishable once they are in one list.

### `Effector.feedrate(self, name: str, default: Optional[int] = None) -> Optional[int]`

Look up a named feedrate (mm/min) in this effector's `feedrates` block.

- `name` - feedrate key, e.g. `"tip_press"`.
- `default` - returned when the key is absent.

**Returns:** `int`, or `None` if the resolved value is `None`. `p300.feedrate("tip_press")` is `600`; `p125.feedrate("tip_press")` is `None` (its `feedrates` block is empty).

**Why:** it exists so an effector can *override* a deck-wide feedrate without every effector having to restate all of them. `Machine.get_tip` reads `eff.feedrate("tip_press") or self.deck.feedrate("tip_press")` - effector first, deck fallback (also 600). A gentler pipette can lower its own press speed in one place.

### `Effector.__repr__(self) -> str`

`<Pipette p300>`. Uses `type(self).__name__`, so the subclass shows through.

---

### class `Pipette`

Air-displacement pipette: plunger geometry, the linear volume calibration, and the guards on both. `kind = "pipette"`.

### `Pipette.__init__(self, cfg: config.ConfigFile) -> None`

Loads and cross-checks the actuator and calibration blocks.

Actuator attributes, all mm on the plunger axis except where noted (p300 / p125 values):

| attribute | p300 | p125 | meaning |
| --- | --- | --- | --- |
| `blowout_pos` | 30.0 | 31.0 | plunger bottomed: full dispense plus blow-out. The high end of legal POS |
| `air_gap` | 5.0 | 6.0 | trailing air gap, mm of plunger travel |
| `gap_pos` | 25.0 | 25.0 | POS with the air gap armed, in air, before descending |
| `post_asp_backoff` | 1.0 | 1.0 | extra retract in air after leaving the liquid, anti-drip (`TRAIL_GAP` in the old p125 script) |
| `stroke_margin` | 1.0 | 1.0 | keep POS at least this far off the hard stop |
| `pos_min` | 0.0 | 0.0 | POS of the hard stop itself |
| `settle_s` | 1.0 | 1.0 | seconds (not mm) to dwell after a plunger move |
| `decimals` | 3 | 3 | rounding for emitted POS and mm values |

Calibration attributes: `slope` (mm per uL), `intercept` (mm), `window` (a `(lo, hi)` tuple in uL from `window_uL`), plus `nominal_uL` from the top level.

- p300: slope 0.0533, intercept 0.122, window (8.0, 320.0), nominal 300.0
- p125: slope 0.1274, intercept 0.049, window (10.0, 140.0), nominal 125.0

**Raises:**

- `EffectorError` if `calibration.model` is not `"linear_mm_per_uL"`. There is exactly one model implemented; a config claiming a polynomial fit must fail loudly rather than be silently treated as linear.
- `EffectorError` if `gap_pos != blowout_pos - air_gap` ("the air gap is not what it claims to be"). This catches the case where someone edits `air_gap` and forgets `gap_pos`, or vice versa - the numbers would still look plausible individually, and every subsequent aspirate POS would be off by the difference. Both current configs satisfy it: 30.0 - 5.0 = 25.0, and 31.0 - 6.0 = 25.0.
- `KeyError` for a missing required field (see the exception-hierarchy note above).

### `Pipette.equation` (property) -> `str`

The calibration curve rendered as text **from the coefficients actually in use**: `"d_mm = 0.0533 * V_uL + 0.122"`.

**Why it is derived rather than read:** the config also carries an `equation` string, but nothing cross-checks it, and a free-text duplicate of a number is precisely the failure mode this library was written to remove - `config/effectors/p300.json` records that the scripts it replaces had a comment curve (`0.05338*V + 0.1218`) that disagreed with the hardcoded displacement table (`[1.722, 8.123, 12.123, 16.123]`) sitting a few lines below it. Reports and gcode headers use this property, so what is printed is always what was computed.

### `Pipette.displacement_mm(self, volume_uL: float) -> float`

Plunger travel in mm for a requested volume, from the stored coefficients only.

- `volume_uL` - requested volume in microlitres.

**Returns:** `round(slope * volume_uL + intercept, decimals)`. p300 at the ISO fractions of nominal (30 / 150 / 225 / 300 uL) gives 1.721 / 8.117 / 12.114 / 16.112 mm.

**Raises:** `VolumeError` via `check_volume` - both the window test and the stroke test - before computing anything.

**Why:** this is the single conversion point in the library. See "Why uL to mm lives here" below.

### `Pipette.volume_uL(self, displacement_mm: float) -> float`

Inverse of the curve, for reporting a measured or commanded stroke back as a volume.

- `displacement_mm` - plunger travel in mm.

**Returns:** `round((displacement_mm - intercept) / slope, 4)`. `p300.volume_uL(17.178)` is `320.0`.

**Raises:** nothing. `ZeroDivisionError` if a config ever set `slope` to 0.

**Note:** deliberately unvalidated and deliberately rounded to 4 decimals rather than `self.decimals`, since it is a reporting path. It will happily invert nonsense - `p300.volume_uL(30.0)` returns `560.5629` uL, well past the 320 uL window. Never feed its output back into `displacement_mm` as if it had been checked.

### `Pipette.aspirate_pos(self, volume_uL: float) -> float`

The plunger POS that holds this volume with the air gap already armed.

- `volume_uL` - requested volume in microlitres.

**Returns:** `round(gap_pos - displacement_mm(volume_uL), decimals)`. p300 at 300 uL: 25.0 - 16.112 = `8.888`.

**Raises:** `VolumeError` (from `displacement_mm`) if the volume is unmeterable; `EffectorError` (from `check_pos`) if the resulting POS is outside `[pos_min + stroke_margin, blowout_pos]`.

**Why it starts from `gap_pos` and not `blowout_pos`:** the air gap is armed *in air*, before the tip descends into the liquid. The stroke that draws liquid therefore starts at POS 25.0, not at POS 30.0, and the gap's 5 mm is not available for metering. Computing from `blowout_pos` would over-aspirate by the whole air gap - about 94 uL on the p300 curve.

### `Pipette.backoff_pos(self, volume_uL: float) -> float`

The POS after the anti-drip retract, i.e. the position the plunger actually rests at while the tip travels to the dispense target.

- `volume_uL` - requested volume in microlitres.

**Returns:** `round(aspirate_pos(volume_uL) - post_asp_backoff, decimals)`. p300 at 300 uL: 8.888 - 1.0 = `7.888`.

**Raises:** the same as `aspirate_pos`, plus its own `check_pos` on the backed-off value.

**Why it exists as a method rather than a subtraction at the call site:** it validates. `Machine.aspirate_uL` and `Pipette.table` both use it, and the comment in `table()` is explicit about the reason - a bare `asp - eff.post_asp_backoff` in a report could print a POS that `check_pos` would have rejected, so the report and the machine would disagree about what is legal.

### `Pipette.max_volume_uL` (property) -> `float`

The largest volume whose *post-backoff* POS still clears the floor.

**Returns:**

```
(gap_pos - pos_min - stroke_margin - intercept - post_asp_backoff) / slope
```

rounded to 4 dp. p300: `(25.0 - 0.0 - 1.0 - 0.122 - 1.0) / 0.0533 = 429.2308` uL. p125: `(25.0 - 0.0 - 1.0 - 0.049 - 1.0) / 0.1274 = 180.1491` uL.

**Raises:** nothing.

**Why it subtracts the backoff:** the machine does not stop at `aspirate_pos`. Once the tip is clear of the liquid it retracts a further `post_asp_backoff` (1.0 mm) in air, so the lowest POS the plunger ever visits during an aspirate is the backoff POS. Constraining `aspirate_pos` alone would validate a position the machine passes straight through, and the run would fail at the position after it - or worse, hit the hard stop with 1.0 mm of commanded travel left and lose steps on an open-loop axis with no error at all.

**Why it subtracts `pos_min`:** the floor is `pos_min + stroke_margin`, exactly the floor `check_pos` enforces, and `pos_min` is where the hard stop *is* rather than an assumed zero. Both current pipettes set `pos_min: 0.0`, so the term vanishes today - but the old p125 script asserted only `POS >= 0` with no margin at all, which is the assumption this parameterisation removes. If the actuator is ever re-zeroed, or a plunger with a mechanical dead length is fitted, the floor moves and every derived limit moves with it, in one place.

**Note:** for both configured pipettes the *calibration window* is the binding constraint, not the stroke: 320 < 429.2 and 140 < 180.1. This property is therefore currently a guard against a future config - a widened window, a smaller `gap_pos`, a larger backoff - rather than a limit anyone hits today. Do not remove it on the grounds that it never fires.

### `Pipette.check_volume(self, volume_uL: float) -> None`

Assert a volume is both fitted and reachable. Returns `None`; the point is the raise.

- `volume_uL` - requested volume in microlitres.

**Raises** `VolumeError` for either of two independent reasons:

1. Outside `window` (tolerance 1e-9): "the curve was never fitted there; the displacement would be an extrapolation". A linear fit valid over 8 to 320 uL says nothing about 2 uL, where surface tension and the tip's dead volume dominate.
2. Above `max_volume_uL`: not enough stroke before POS drops through the `stroke_margin` off the hard stop.

**Why a protocol calls it directly:** `protocols/accuracy_test.py` pre-checks every test volume up front, before emitting a single line of gcode, so an out-of-range point fails at generation time with a message naming the volume rather than half way through a run.

### `Pipette.check_pos(self, pos: float, what: str = "move") -> None`

Assert a plunger POS is inside the legal band.

- `pos` - plunger POS in mm.
- `what` - short description of the operation, interpolated into the message ("aspirating 400.0 uL", "post-aspirate backoff at 300.0 uL", or the op name when called from `Machine.actuator`).

**Raises** `EffectorError` if `pos < pos_min + stroke_margin` (1.0 mm for both pipettes) or `pos > blowout_pos`, with 1e-9 tolerance at both ends. The messages spell out the arithmetic: "below the 1.0 mm floor (pos_min 0.0 + margin 1.0)".

**Why:** it is the one definition of "a legal plunger position", and it is applied at three levels - inside `aspirate_pos`/`backoff_pos` when the value is derived, inside `Machine.actuator` for *every* emitted `ACTUATOR_MOVE` including hand-picked ones like `prewet_asp_pos`, and again in `lab/simulate.py` (which re-derives the same `pos_min + stroke_margin` floor independently while walking the finished gcode). The upper bound matters as much as the lower: commanding past `blowout_pos` drives the plunger into the other stop.

### `Pipette.prewet_enabled` (property) -> `bool`

`prewet.enabled`, default `False`. `True` for p300, `False` for p125 (whose old script did no prewet at all).

**Why:** `Machine.prewet()` raises `ProtocolError` if you call it on an effector with prewet disabled, rather than silently generating a wetting sequence the calibration was not fitted with.

### `Pipette.prewet_cycles` (property) -> `int`

`prewet.cycles`, default 0. 3 for p300.

### `Pipette.prewet_min_cycles` (property) -> `int`

`prewet.min_cycles`, default 0. 3 for p300.

**Why both exist:** `cycles` is the default count, `min_cycles` is a floor a caller cannot go under - `Machine.prewet(cycles=1)` on the p300 raises. The wetting of the tip wall is part of what the gravimetric fit measured; fewer cycles than the fit used makes the stored coefficients wrong in a way no check downstream can see.

### `Pipette.prewet_asp_pos` (property) -> `float`

The plunger POS for the aspirate half of a prewet cycle. Returns `prewet.asp_pos` when present and non-null (10.0 for p300), otherwise derives `round(blowout_pos - prewet.displacement_mm, decimals)`.

**Why it is measured from `blowout_pos` and not `gap_pos`:** prewet is a full wet-and-blow-out cycle with no air gap armed, so its reference is the bottomed plunger. The config note on `p300.prewet.asp_pos` says so explicitly: "measured from BOTTOMED, not from gap_pos". Using `gap_pos` here would shorten the wetting stroke by the 5 mm air gap.

**Note:** on a config where `prewet` has neither `asp_pos` nor `displacement_mm` - which is p125, whose block is just `{"enabled": false}` - this property raises a bare `KeyError('displacement_mm')`, not a `LabError`. `Machine.prewet()` checks `prewet_enabled` first so the normal path never reaches it, but any report or introspection code that touches the property unconditionally will crash ungracefully on p125.

### `Pipette.table(self, volumes: List[float]) -> List[Dict[str, float]]`

Build the per-volume displacement table for a report or a dry-run printout.

- `volumes` - list of volumes in uL.

**Returns:** one dict per volume with keys `volume_uL`, `displacement_mm`, `aspirate_pos`, `backoff_pos`.

**Raises:** `VolumeError` or `EffectorError` on the first bad volume, so the table is all-or-nothing - you never get a partial table with the failing row quietly dropped.

**Why:** it replaces the hardcoded tables the old scripts carried. For p300 at ISO fractions the computed displacements are 1.721 / 8.117 / 12.114 / 16.112 mm against the retired literal `[1.722, 8.123, 12.123, 16.123]`; for p125, 1.642 / 8.012 / 11.993 / 15.974 against `[1.671, 8.020, 11.988, 15.956]`. The p125 10% point moves by 0.029 mm, about 1.8% of a 12.5 uL target, which is the difference between passing and failing that ISO 8655-6 point.

### `Pipette.fractions(self, fracs: List[float]) -> List[float]`

Convert fractions of nominal volume into absolute volumes.

- `fracs` - list of fractions, e.g. `[0.1, 0.5, 0.75, 1.0]`.

**Returns:** `[round(f * nominal_uL, 4) for f in fracs]`. p300: `[30.0, 150.0, 225.0, 300.0]`. p125: `[12.5, 62.5, 93.75, 125.0]`.

**Raises:** nothing - it does *not* validate. `protocols/accuracy_test.py` calls `fractions()` then loops `check_volume()` over the result.

**Why:** the ISO 8655-6 test points are a property of the standard, not of the pipette, so one protocol expresses them once as fractions and both pipettes derive their own absolute volumes. That is what let two per-pipette scripts collapse into one `accuracy_test.py`.

---

### class `Gripper`

Plate gripper. Not built. `kind = "gripper"`. The class exists so `load()` has something to dispatch `"kind": "gripper"` onto, and so the placeholder refusal happens in one place.

### `Gripper.__init__(self, cfg: config.ConfigFile) -> None`

Reads the actuator block without coercing types, since every value in it is currently `null`.

- `self.open_pos`, `self.closed_pos`, `self.grip_pos` - raw values from `actuator`, all `None` today. POS values on the actuator axis, assuming the gripper shares the `ACTUATOR_MOVE` macro (the config leaves that undecided and suggests a `macro_name` field if it does not).
- `self.carries_labware` - `bool`, default `True`.
- `self.held` - `Optional[str]`, initialised to `None`. **Written by nothing in the repo.**

**Raises:** nothing. Unlike `Pipette.__init__`, it validates nothing, because there is nothing valid to validate yet.

**Why `held` is here at all:** a gripper mutates the *deck layout* at run time, which a pipette never does - picking a plate empties a slot and fills the effector. That state has to live with the effector rather than the deck, and the simulator will need it. Nothing tracks it yet; see the `carries_labware_note` in `config/effectors/gripper.json`, and note that `lab/simulate.py` validates plunger POS only for `Pipette`, so a gripper POS would today be emitted entirely unchecked.

### `Gripper.require_ready(self) -> None`

Refuse to proceed while the jaw positions are unmeasured.

**Raises:** `EffectorError` naming the still-null fields and the file to put them in: "gripper: cannot generate motion, these are still null in config/effectors/gripper.json: ['open_pos', 'closed_pos', 'grip_pos']. Measure them first."

**Why:** it is the guard the future `grip()` / `release()` / `move_labware()` primitives on `Machine` should call first. Today it is unreachable from any motion path because those primitives do not exist.

**Note:** it checks only the three jaw positions. `actuator.pos_max` and `actuator.stroke_margin` are also `null` in the config and would be needed for any POS bounds check equivalent to `Pipette.check_pos`; nothing checks them.

---

### Module-level functions

### `_KINDS`

`{"pipette": Pipette, "gripper": Gripper}`. Module-private dispatch table, not a function. The only thing you edit when adding a new effector kind, and `load()` quotes `sorted(_KINDS)` in its error message so an unsupported kind tells the reader exactly what to add and where.

There are no private helper *functions* in this module. Every callable is public.

### `load(effector_id: str, allow_placeholder: bool = False) -> Effector`

Load one end effector by id, dispatching on its `kind` to the right class.

- `effector_id` - the filename stem under `config/effectors/`, i.e. `"p300"`, `"p125"`, `"gripper"`.
- `allow_placeholder` - pass `True` only to *inspect* a placeholder file. Never to generate motion.

**Returns:** a `Pipette` or a `Gripper`, already fully validated by its own `__init__`.

**Raises `EffectorError`** in three cases:

1. Unknown id. The check is against `config.available_effectors()` (a glob of `config/effectors/*.json`), so a typo produces "no effector 'p3000'. Available: ['gripper', 'p125', 'p300']" instead of a `FileNotFoundError` naming a path.
2. Unknown `kind` - "Add a class to lab/effector.py to support it."
3. The file is a placeholder and `allow_placeholder` is `False`: "every numeric field is unmeasured and it must not be used to generate motion."

Also propagates `FileNotFoundError` / `ValueError` from `lab/config.py`, and `KeyError` / `EffectorError` from the class constructor.

**Why the placeholder gate is here:** it is the last chokepoint before an effector object reaches `Machine`. `Machine.__init__` calls `load_effector(effector)` with no `allow_placeholder` argument, so `Machine(effector="gripper", ...)` cannot be constructed at all - the refusal happens before any gcode object exists. That is the only thing standing between the gripper's all-null config and a run that emits unvalidated motion, given that `lab/deck.py` does not consult effectors and `lab/simulate.py` skips POS validation for non-pipettes.

**Note:** `load()` never checks that `cfg.data["id"]` matches `effector_id`. A file `p999.json` containing `"id": "p300"` loads happily as an effector with `id == "p300"`, and `Machine.config_hashes()` would then write the manifest key `effectors/p300.json` against the SHA-256 of `p999.json` - a provenance record naming a file that is not the one that was read.

### `available() -> List[str]`

Sorted list of every effector id with a JSON file on disk: `['gripper', 'p125', 'p300']`.

**Raises:** nothing; returns `[]` if `config/effectors/` does not exist.

**Why:** `protocols/accuracy_test.py` passes it straight to `argparse` as `choices=`, so `--effector` is validated by the filesystem rather than by a hardcoded list that would go stale the moment someone adds a file. It deliberately includes placeholders - `--effector gripper` is offered by the CLI and then refused by `load()` with an explanation, which is more useful than the id simply not existing.

---

### Why uL to mm conversion lives here and nowhere else

The scripts this library replaces carried the calibration in three places at once: a comment stating the curve, a hardcoded list of displacements, and the volumes those displacements were supposed to correspond to. In `examples/300ulaccuracyprecision.py` the comment said `0.05338*V + 0.1218` while the table said `[1.722, 8.123, 12.123, 16.123]` - and the table was what the machine actually ran. The two had drifted, and nothing could detect that, because a millimetre value in a script is just a number.

Concentrating the conversion in `Pipette.displacement_mm` gives four properties that no amount of care at the call site can:

1. **A displacement cannot disagree with the volume it claims to be**, because it is computed from that volume at generation time. `config/effectors/p300.json` states the rule directly: "Store coefficients ONLY. Displacements are computed at runtime. No mm value is ever typed into a protocol script."
2. **Every conversion is validated.** `displacement_mm` calls `check_volume` before it multiplies, so there is no path from a volume to a plunger position that skips the window and stroke checks. If the conversion were inline in a protocol, each protocol would have to remember.
3. **Re-fitting the curve is a one-file edit** whose blast radius is exactly the coefficients, and the new SHA-256 lands in the next run manifest automatically.
4. **The report and the machine cannot diverge**, since `equation`, `table()` and the emitted `ACTUATOR_MOVE` values all come out of the same two floats. Both p300 and p125 record in their configs the older curves they supersede, so the change is auditable rather than silent.

The same argument is why `z_for` lives on the effector: both conversions turn an operator-meaningful quantity (a volume, a labware) into a machine coordinate, and both are only correct relative to which effector is fitted.

---

## `lab/deck.py`

The resolved deck. Everything about *where things are* on the machine lives here: the axis envelope, the nine slot centres, the fixed stations, the keep-out bands, the derived stage surface, and the labware placements read out of `layout.json`. `Deck.load()` joins `config/deck.json` + `config/labware.json` + `config/layout.json` into one object, and validates the joins as it goes, so a protocol can write `deck.placement("tubes").well("A1")` and never contain a coordinate literal. The module answers questions and reports problems; it never emits gcode and never moves anything. Motion planning is `lab/machine.py`, and the crash checks that consume these answers are in `lab/simulate.py`.

Two conventions run through every function below. **+Z is down**: Z=0 is home at the top, a larger commanded Z is physically lower, so "high enough to be safe" is expressed as `z < limit`. And **the X frame has already been corrected**: the X travel limit grew by 70 mm, `deck.json` stores the post-shift numbers (`stations.water.xy` X 0 -> 70, `stations.park.xy` X 350 -> 420, layout `tubes.anchor_xy` X 186 -> 256), and nothing in this module applies a shift at runtime. If you find yourself adding +70 anywhere in code, you are fixing the wrong file.

### `DeckError`

```python
class DeckError(LabError)
```

Raised for a deck the config cannot describe: an unknown placement/station/slot/macro/feedrate name, a duplicate placement name, two placements claiming one slot, or a placement whose `slot_center` copy disagrees with `deck.json`. Subclasses `LabError` (defined in `lab/effector.py`), so a protocol can catch that one base class and print the message.

**Note:** not every failure in `Deck.__init__` is a `DeckError`. The constructor indexes `d["axes"]`, `d["feedrates"]`, `d["z"]` and `labware_cfg["labware"]` directly, so a truncated config raises bare `KeyError`; and `build_placement()` in `lab/labware.py` raises `KeyError`/`ValueError` (unknown grid model, `corners` block missing, rotation given with the `corners` model, stale `corners.solved` values) which propagate out of `Deck.load()` untouched. Catching only `DeckError` around a load will miss those.

### `Axis`

```python
@dataclass(frozen=True)
class Axis:
    name: str
    min: float
    max: float
    home: float
```

One machine axis as configured, no motion semantics attached. `Deck` builds three from `deck.json` `axes`: `x` = (0.0, 420.0, home 420.0), `y` = (0.0, 580.0, home 0.0), `z` = (0.0, 270.0, home 0.0). All in mm.

`home` is stored because X homes at its **maximum** (420) and counts down toward 0, which is the fact behind the whole +70 frame story: the same physical endstop used to be called 350. Nothing here validates `min < max`, and the Z max of 270 is the kinematic limit only - the real Z limit is effector- and location-dependent (see `simulate.py`).

### `Axis.contains`

```python
def contains(self, v: float, tol: float = 1e-6) -> bool
```

True if `v` lies within `[min - tol, max + tol]`. `v` is a coordinate in mm; `tol` is the mm slack allowed at each end. Returns a bool, raises nothing. The tolerance exists so a coordinate arrived at by arithmetic (a pitch sum, a mean of slot centres) is not rejected for landing on X 420.0000000001. Do not raise `tol` to paper over a real overrun; 1e-6 mm is float noise, anything larger is a genuine off-machine coordinate.

### `KeepOut`

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

An X band in which one class of motion is forbidden unless the effector is high enough. There is exactly one in the current config:

```
id "tip_ejector", x_range (148.0, 162.0), forbids "y_motion",
safe_when_z_below 60.0, whitelist ("eject_tip",)
```

The band is the ejector lane X 155 plus or minus 7.0. Y motion through it collides with the ejector mechanism unless the effector is above Z 60, i.e. commanded `z < 60`. `safe_when_z_below` is a **commanded** Z, and because +Z is down a smaller number is physically higher - hence the field name and the `<` in `z_is_safe`. `whitelist` names the operations allowed to violate the band; `simulate._check_keepout` additionally requires the individual step to carry `detail["keepout_exempt"]`, so only the waypoints that actually engage the ejector are exempt and the approach/retract around them are checked like any other traverse.

The 60.0 was stated for the p300 with **no tip**. A fitted tip reaches lower, the safe Z shrinks, and that shrunken value was never measured - so `simulate` treats any *tipped* Y traverse through the band as a hard failure regardless of Z. The 14 mm band width is carried over from an older band centred on X 85 and is listed in `deck.json` `uncalibrated`; `x_range` is the single source of truth for the width, so correct it there if you measure the mechanism.

**Note:** `tipped_is_error` is dead. `Deck.__init__` never passes it (there is no such key in `deck.json`), so it is always the `True` default, and `simulate._check_keepout` hardcodes the tipped failure instead of consulting it. Either wire it up or delete it; as written it invites someone to set it in JSON and believe the setting took effect.

### `KeepOut.contains_x`

```python
def contains_x(self, x: float) -> bool
```

True if the single X coordinate `x` (mm) is inside the band, bounds inclusive. This is the test for "may I move Y here", because a Y move happens at one fixed X. Used by `Deck.keepouts_hit_by_y_move` and by `Machine._nearest_clear_x`.

### `KeepOut.spans_x`

```python
def spans_x(self, x0: float, x1: float) -> bool
```

True if a straight X move from `x0` to `x1` (mm, either order) enters the band at all - implemented as an interval overlap, so it is correct whichever direction the move runs. Returns a bool, raises nothing.

It exists to keep the distinction that makes the whole keep-out scheme workable: a *pure X* move straight through the ejector band is harmless, only *Y* motion inside it collides. A protocol that needs to know "did my traverse pass over the ejector" asks this; a protocol that needs to know "may I turn here" asks `contains_x`.

### `KeepOut.z_is_safe`

```python
def z_is_safe(self, z: float) -> bool
```

True if commanded `z` (mm) is high enough to clear the band, i.e. `z < safe_when_z_below`. Returns `False` when `safe_when_z_below` is `None`, which is the important behaviour: an unmeasured clearance means *never safe*, not *no constraint*. Raises nothing.

### `Station`

```python
@dataclass(frozen=True)
class Station:
    name: str
    xy: Vec2
    z: Optional[float]
    spec: Dict[str, Any]
```

A fixed named position that is not a slot. `xy` is machine mm; `z` is a commanded Z or `None` when the station has no single meaningful depth; `spec` is the raw `deck.json` block, kept whole so routine-specific fields survive without this dataclass having to grow a field per station.

The three current stations show why `spec` matters:

- `water` - xy (70.0, 175.0), z `None`. Submerge depth is per-effector (`effectors/*.json` -> `z_for_labware.reservoir_water.work`), so there is no station Z.
- `park` - xy (420.0, 0.0), z 0.0. X 420 is X home, which is what this station always meant.
- `tip_eject` - xy (155.0, 89.0), z `None`, with the whole ejection routine in `spec`: `lane_x` 155.0, `approach_y` 83.0, `engage_y` 88.0, `retract_y` 80.0, `z_below_hook` 145.0, `z_strip` 137.0, `rehome_after` true, `validated` false. Z145 -> Z137 is an 8 mm **upward** stroke, and that stroke is the strip. `rehome_after` is not optional: the strip force can skip steps on an open-loop machine, so commanded position is untrustworthy afterward and a G28 must follow. `validated: false` means this sequence has never run on hardware and no gcode in the repo contains a reference ejection.

### `Deck.__init__`

```python
def __init__(self, deck_cfg: config.ConfigFile,
             labware_cfg: config.ConfigFile,
             layout_cfg: config.ConfigFile) -> None
```

Resolves three loaded configs into one deck. Prefer `Deck.load()`; construct directly only to load configs from unusual places or to feed synthetic configs in a test.

What it populates: `id`, `raw` (the whole deck dict, kept for the derived Z properties), `x`/`y`/`z` axes, `feedrates` (`travel` 6000, `hold` 3000, `tip_press` 600, `wipe` 1500, mm/min, coerced to `int`), `z_home` 0.0, `z_travel` 5.0, `z_stage_surface` 114.75, `slots` (nine `str` keys -> `(x, y)`), `stations`, `keepouts`, `macros`, `catalogue` (every labware in `labware.json`, placed or not), `placements` (only what `layout.json` loads), and `open_questions` (the layout's list of things still unresolved, currently three, including whether any loaded rack stands higher than the raised location edges).

What it validates, and why each check is here rather than left to a code review:

- A placement naming a labware absent from `labware.json` raises `DeckError` listing the known ids.
- Two placements with the same `name` raise `DeckError`; the dict would otherwise silently keep the last one.
- `_check_recorded_slot_center` per placement, then `_check_slot_occupancy` over the whole layout.

Note `z_home` (from `deck.json` `z.home`) duplicates `axes.z.home`; both are 0.0 and nothing reconciles them.

### `Deck.load`

```python
@classmethod
def load(cls, layout: str = "layout.json") -> "Deck"
```

The normal entry point. `layout` is a filename inside the config directory; `config.load_layout` appends `.json` if you omit it, so `Deck.load("layout")` and `Deck.load("layout.json")` are the same call. Returns a fully validated `Deck`. Raises `FileNotFoundError` if a config is missing, `ValueError` for malformed JSON, and `DeckError`/`KeyError`/`ValueError` for the integrity failures listed above.

Config paths resolve against the package, not the working directory, and `LAB_CONFIG` overrides the directory (see `lab/config.py`). `deck.json` and `labware.json` are always the shipped ones; only the layout is selectable, which is the intended split: **edit `layout.json` for a run, not `deck.json` or `labware.json`**. Each `ConfigFile` carries the sha256 of the bytes it came from, so a generated `.gcode` can be traced to the exact config that produced it.

### `Deck.placement`

```python
def placement(self, name: str) -> Placement
```

The resolved `Placement` (from `lab/labware.py`) for a layout name - currently `"tubes"`, `"tips"`, `"water"`. Raises `DeckError` naming every loaded placement, which is the whole point: a typo fails at load time with the valid options printed, rather than a `KeyError` mid-protocol after the machine has already moved.

### `Deck.placement_for_labware`

```python
def placement_for_labware(self, labware_id: str) -> Placement
```

The one placement using a given catalogue id (e.g. `"tiprack_300_96"`). For code that cares about the *kind* of labware rather than the run-specific name - the tip logic wants "the tip rack", not "the placement someone called tips this week".

Raises `DeckError` if nothing in the layout uses that labware, **and also if more than one placement does**, telling you to ask by name instead. The second case is deliberate: with two tip racks loaded, returning the first match would put tips into an arbitrary one of them and look like it worked.

### `Deck.station`

```python
def station(self, name: str) -> Station
```

Look up a `Station` by name (`"water"`, `"park"`, `"tip_eject"`). Raises `DeckError` listing the known stations.

### `Deck.slot_center`

```python
def slot_center(self, slot: int) -> Vec2
```

Machine `(x, y)` in mm of a slot centre. `slot` is the operator's slot number 1-9 (stringified internally, since JSON object keys are strings). Raises `DeckError` if there is no such slot.

The layout the numbers encode, which is worth having in front of you: slots 1-3 are the **front** row at Y 365, 4-6 the middle at Y 272, 7-9 the **back** at Y 178.5, and within each row X **descends** (336, 200, 65). So slot 1 is (336, 365) and slot 9 is (65, 178.5). Front means largest Y. Per-slot centres are stored rather than derived from a pitch because the frame is irregular by about 1 mm (X pitches 136 and 135, Y pitches 93.0 and 93.5) and deriving them would erase that.

### `Deck.macro`

```python
def macro(self, name: str) -> str
```

The Klipper macro prefix declared under `klipper_macros`: `actuator_move` -> `"ACTUATOR_MOVE POS="`, `home_actuator` -> `"HOME_ACTUATOR"`, `log_temps` -> `"LOG_TEMPS TAG="`. These must exist in `printer.cfg`. Raises `DeckError` listing the known keys.

There is deliberately **no default**. A fallback literal would mean that a mistyped or deleted `klipper_macros` entry silently emits the built-in string: the gcode would look right and address nothing, which on this machine means an aspirate that never happens.

**Note:** `self.macros` is the raw dict, so it still contains the prose key `note`. `deck.macro("note")` returns `"These must exist in printer.cfg."` instead of raising, even though the error message filters `note` out of its "Known:" list.

### `Deck.feedrate`

```python
def feedrate(self, name: str) -> int
```

A named feedrate in mm/min: `travel` 6000, `hold` 3000, `tip_press` 600, `wipe` 1500. Raises `DeckError` listing the known names. Same reasoning as `macro()` - named speeds mean the slow tip-press feedrate cannot be lost to a copy-paste of a travel move.

### `Deck.in_envelope`

```python
def in_envelope(self, x: float, y: float, z: float) -> List[str]
```

Returns a list of human-readable envelope violations for one point (mm), e.g. `"Z 300 outside [0.0, 270.0]"`. **Empty list means fine** - it is a report, not a predicate, so `if deck.in_envelope(...)` reads as "if there are problems". Raises nothing.

This checks the three axis limits and nothing else. It knows nothing about the stage surface, the raised location edges, the loaded labware or the fitted tip - all of which are stricter. `simulate._check_move` calls this first and then layers the physical limits on top. `tools/check_alignment.py` uses it to confirm that shifted historical coordinates still land on the machine.

### `Deck.keepouts_hit_by_y_move`

```python
def keepouts_hit_by_y_move(self, x: float) -> List[KeepOut]
```

The keep-out bands that a Y move performed at this fixed `x` (mm) would enter - the ones whose `forbids == "y_motion"` and whose band contains `x`. Returns `[]` when the X is clear.

This is the routing primitive. `Machine._goto_xy` calls it on both the current and target X to choose move order: X-first by default (that is what the tip box requires, and a pure X move through a band is harmless), Y-first when the *target* X is blocked, and a dogleg through `_nearest_clear_x` when both ends are blocked. With the ejector band at X 148-162, the tube rack columns at X 148 and X 160 sit inside it, so this is not a theoretical case - it fires on real dispense targets. `simulate` calls the same function to check the emitted moves.

### `Deck.keepouts_crossed_by_x_move`

```python
def keepouts_crossed_by_x_move(self, x0: float, x1: float) -> List[KeepOut]
```

Every keep-out whose band a straight X move from `x0` to `x1` (mm) passes through, via `KeepOut.spans_x`. Returns `[]` if none.

**Note:** nothing in the repo calls this, and unlike `keepouts_hit_by_y_move` it does not filter on `forbids` - it returns `y_motion` bands too, which a pure X move is explicitly allowed to cross. If you start using it for a future `forbids: "x_motion"` band, filter on `forbids` first, or it will report bands that do not apply.

### `Deck._check_recorded_slot_center`

```python
def _check_recorded_slot_center(self, entry: Dict[str, Any], p: Placement) -> None
```

Private, called once per placement during construction. If a layout entry carries a convenience copy of its slot centre (`tubes` records `[200.0, 178.5]`, `tips` records `[65.0, 365.0]`), it must match `deck.json` to within 1e-6 mm or the load fails with a `DeckError` telling you to fix whichever is stale or drop the field. Silently returns when the entry has no `slot_center`, the placement has no slot, or the slot is unknown.

A duplicated coordinate that nothing verifies will eventually disagree with its source, and the copy is the one a human reads. Protocol authors never call this.

### `Deck._check_slot_occupancy`

```python
def _check_slot_occupancy(self) -> None
```

Private, called once after all placements are built. Raises `DeckError` if two placements declare the same `slot`, naming both. Physical slots hold one thing; two placements in slot 8 means one of them is not where the file says, and every coordinate derived from it is wrong. Protocol authors never call this.

**Note:** slot values are compared exactly as JSON supplied them, unlike `slot_center()`/`slot_cell()` which stringify. A layout writing `"slot": 8` in one entry and `"slot": "8"` in another would pass this check while colliding physically.

### `Deck.slot_seating_report`

```python
def slot_seating_report(self) -> List[str]
```

Checks whether each slot-mounted labware actually sits in the slot it claims, and returns one explanatory line per problem. Empty list means every seating claim holds. Raises nothing. Included in the run manifest by `lab/logbook.py` and printed by the simulator's preflight.

For each placement with `mount == "slot"` and a slot number, it compares `Placement.grid_center()` against the slot centre and reports the offset if the distance exceeds the tolerance. `fixed` and `station` mounts are skipped by design: they carry `near_slot` as a human reference, not a seating claim - `water` is at the dip point (70, 175), which is 5.0 mm in X and 3.5 mm in Y from slot 9's centre and is not meant to be the trough centre. A placement flagged `slot_disputed` in the layout gets " (flagged disputed in the layout)" appended rather than being exempted.

A slot label is a claim about physical seating, and a claim nothing checks can be wrong for months. This is also the check that caught the +70 frame error: unshifted, the tube rack spans X 78-186 and straddles the slot 8/9 seam at X 132.25, seated in neither; shifted to X 148-256 it fits entirely inside slot 8, where the operator says it is. Current offsets both pass comfortably - `tubes` grid centre (202, 187) is 8.7 mm from slot 8, `tips` grid centre (68.0, 367.35) is 3.8 mm from slot 3.

**Note:** the tolerance is looser than the docstring's "half a slot pitch". The code does `hw, hh = self.slot_pitch()` - which returns the **full** pitch (135.5, 93.25), not half - then accepts any offset within `max(hw, hh) / 2.0` = 67.75 mm. That is a single circular radius taken from the X pitch, so a labware misseated by up to 67.75 mm in **Y** is not reported, even though half the Y pitch is only 46.625 mm and a 50 mm Y error puts the grid centre inside the adjacent row's cell. If you tighten this, compare `abs(dx)` against `px / 2` and `abs(dy)` against `py / 2` separately.

### The stage surface trio: `slot_pitch`, `stage_region`, `point_on_stage`, `slot_cell`

These four functions exist to encode one physical fact that is easy to state and easy to forget: **the stage surface only exists over the slot area.** Within that area there is a hard floor at commanded Z 114.75 (bare p300 nosecone) or Z 62.5 (with a 300 uL tip). Outside it, there is nothing under the effector at all, which is precisely what makes the ejector's Z 145 - 30 mm past the stage floor - a legal command. The ejector works at X 155, Y 80-89, and the derived stage region starts at Y 131.875, so the ejector hangs past the front edge of the stage. `simulate` asserts this rather than taking it on trust: `test_ejector_is_off_the_stage` checks `point_on_stage` is `False` at all three ejector Y waypoints, and `_check_move` downgrades the depth check to a warning only when `point_on_stage` is `False`.

The region is *derived* from the slot table, not stored, so it cannot drift out of step when a slot centre is corrected. They are documented here in dependency order rather than file order.

### `Deck.slot_pitch`

```python
def slot_pitch(self) -> Tuple[float, float]
```

Mean X and Y spacing between adjacent slot centres, in mm. Returns `(135.5, 93.25)` for the current nine slots: the distinct X values are {65, 200, 336} so `(336 - 65) / 2 = 135.5`, and the distinct Y values {178.5, 272, 365} give `(365 - 178.5) / 2 = 93.25`. Returns 0.0 for an axis with fewer than two distinct values, so a single-slot deck does not divide by zero.

This is a **mean**, and the real frame is irregular (X pitches 136 and 135, Y pitches 93.0 and 93.5). That is fine for the purpose - sizing location cells and clearance tolerances - but do not use it to compute a well position; use the stored per-slot centre.

### `Deck.stage_region`

```python
def stage_region(self) -> Optional[Tuple[float, float, float, float]]
```

Returns `(x_min, x_max, y_min, y_max)` in mm over which the stage surface exists, or `None` if the deck declares no slots. Computed as the extreme slot centres expanded by half the slot pitch, which for the current deck gives:

```
(-2.75, 403.75, 131.875, 411.625)
```

Raises nothing. The ejector at Y 80-89 is 43 mm clear of `y_min` 131.875, which is the whole reason Z 145 is reachable there. `simulate._check_raised_edges` also uses the region as a fast reject: a move whose swept bounding box misses the region entirely cannot clip a stage edge, so it returns immediately.

**Note:** `x_min` comes out at -2.75, i.e. 2.75 mm outside the X envelope. Harmless as an on/off-stage test, but do not treat the region as a reachable area.

### `Deck.point_on_stage`

```python
def point_on_stage(self, x: float, y: float) -> bool
```

True if `(x, y)` (mm) lies within `stage_region()`, bounds inclusive. `False` when there are no slots. Raises nothing.

This is the switch between two different Z rules. On-stage, `simulate` applies the effective tip-aware Z limit and fails a move that goes below it. Off-stage, it emits an `off_stage_depth` warning instead, saying the depth is only safe if nothing is under the effector there - true for the ejector, which hangs past the front edge. Before adding any new off-stage routine that goes deep, satisfy yourself that the same "nothing underneath" argument really holds at that XY.

### `Deck.slot_cell`

```python
def slot_cell(self, x: float, y: float) -> Optional[str]
```

Which stage location contains `(x, y)` (mm), as the slot key string (`"8"`), or `None` if the point is off-stage or in no cell. A cell is the slot centre plus or minus half the slot pitch, so slot 8 owns X [132.25, 267.75] and Y [131.875, 225.125]. Where cells overlap because the real pitch is irregular, the nearest centre wins. Raises nothing.

This is what separates a traverse that crosses a location boundary - and so passes over a raised edge - from motion that stays inside one location and does not. The edges of each stage location stand 9.5 mm above the stage floor (62.5 - 53.0), so with a 300 uL tip fitted, boundary-crossing motion must be at commanded Z 53.0 or **less** (physically higher). Motion staying inside one cell is not over an edge, which is what keeps the wall wipe legal: it works at Z 45 and Z 41, far below the 53.0 crossing limit, but every wipe waypoint returns cell `"8"` (asserted by `test_wall_wipe_inside_one_location_is_still_allowed`), so no edge is involved.

**Note:** because the cells are sized by the *mean* pitch while the real frame is irregular, there are hairline seams where `point_on_stage` is `True` but `slot_cell` returns `None` - e.g. X 268.0 at Y 272 falls 0.25 mm outside both slot 4's cell and slot 5's. `simulate` treats a `None` cell as a boundary crossing and applies the stricter edge limit, so the failure mode is conservative, not dangerous. Also note `layout.json` quotes slot 8's Y cell as [131.75, 225.25], computed from the 93.5 pitch rather than the 93.25 mean this code uses; the 0.125 mm disagreement changes nothing for the tube rack at Y 151-223, but the code is the authority.

### `Deck.z_slot_edge_bare`

```python
@property
def z_slot_edge_bare(self) -> Optional[float]
```

The **bare-nosecone** commanded Z at which the effector would strike a raised location edge: `105.25` for the current config. Returns `None` if any of the three inputs is absent. Raises nothing.

The measurement was taken with a tip on (`z.slot_edge_with_p300_tip` = 53.0), so this derives the bare equivalent by adding the tip length, and it computes that tip length from the deck's own two stage measurements - `stage_surface` 114.75 minus `stage_surface_with_p300_tip` 62.5 = 52.25 - rather than reading `labware.json`. One measurement therefore covers both the tipped and bare cases and the two files cannot disagree. `simulate._check_raised_edges` starts from this bare value and subtracts the actual fitted tip length, so a shorter or longer tip is handled correctly; if the tip length is unknown it warns and skips rather than guessing.

The 53.0 tipped limit is currently the **binding traverse constraint** on this machine, tighter than the stage floor and tighter than any labware rim. Travel height Z 5.0 clears it by 48.0 mm, which is why `deck.json` now calls travel height verified rather than assumed.

### `Deck.near_slots`

```python
def near_slots(self) -> Dict[str, int]
```

Maps placement name -> slot number for placements carrying a `near_slot` reference instead of a real seating. Currently `{"water": 9}`. Raises nothing; goes into the run manifest.

`near_slot` is documentation for a human ("the water trough is over by slot 9"), deliberately kept distinct from `slot` so that `slot_seating_report` does not try to validate it and so that `_check_slot_occupancy` does not treat it as a claim on the slot. Keeping the two in separate keys is what lets the seating check be strict about the ones that *are* claims.

### `Deck.overlap_report`

```python
def overlap_report(self) -> List[str]
```

Pairwise footprint overlap between all placements, as report lines. Empty means no problems found. Raises nothing.

An intersecting pair produces `OVERLAP: <a> footprint intersects <b> footprint`. A pair where either footprint is unmeasured produces an explicit `cannot check <a> vs <b>: no footprint measured for ...` line, so an unknown can never be mistaken for a pass. That distinction is the reason this returns strings rather than a bool.

Right now `sbs_96_wellplate` is the only labware with a `footprint`, and it is not loaded, so all three pairs report "cannot check". Footprint bounds come from `Placement.footprint_bounds()`, which centres the measured outline on the *grid* centre - fine for a plate whose grid is centred on its footprint, approximate for a user-fabricated rack, and another reason it returns `None` rather than a guess.

### `Deck.uncalibrated`

```python
def uncalibrated(self) -> List[str]
```

Every field the config itself admits was never tuned, as dotted names. Raises nothing. `Machine.uncalibrated()` concatenates this with the effector's list, and `protocols/accuracy_test.py` prints it before a run. Current output:

```
tubes(tube_rack_0p65_5x10).wipe.dx
tubes(tube_rack_0p65_5x10).wipe.dz
tubes(tube_rack_0p65_5x10).z.work
tips(tiprack_300_96).grid.a1_corner
tips(tiprack_300_96).consume_order.exit_edge
water(reservoir_water).z.work
deck.keepouts.tip_ejector.x_range (width unverified; lane X 155 itself is confirmed and post-shift)
deck.stations.tip_eject (routine never validated on hardware)
```

Four sources: each placed labware's own `uncalibrated` list; `deck.z.stage_surface` if it was never measured (it now is, 114.75, so this line no longer appears); the deck's structured `uncalibrated` array; and any station whose `validated` key is present and false. The deck-level list is a **structured array, not a prose note that gets grepped** - rewording a comment must not be able to make an uncalibrated field look calibrated.

**Note:** two silent gaps. Only *placed* labware contributes, so unmeasured fields on catalogue entries that this layout does not load (`sbs_96_wellplate`, `tube_rack_1p5`) are not reported - defensible, since they cannot affect the run. And a station with no `validated` key at all is never flagged: absence reads as validated. `water` and `park` have no such key.

### `Deck.summary`

```python
def summary(self) -> str
```

Multi-line human-readable dump: deck id, envelope with the "(+Z is DOWN)" reminder, travel and home Z, one line per placement (labware id, slot/station/fixed, grid shape, and the X/Y span of its **well centres**, not its outline), and one line per keep-out. For eyeballing a layout before a run; `tests/smoke_geometry.py` prints it. Raises nothing, though it will propagate an `IndexError` from `Placement.xy` if a placement's grid is degenerate.

Current output:

```
deck sdl_klipper_deck
  envelope  X[0.0,420.0] Y[0.0,580.0] Z[0.0,270.0] (+Z is DOWN)
  z_travel  5.0   z_home 0.0
  tips     tiprack_300_96           slot 3     8x12  X[18,118] Y[335.5,399.2]
  tubes    tube_rack_0p65_5x10      slot 8     5x10  X[148,256] Y[151,223]
  water    reservoir_water          station water 1x1  X[70,70] Y[175,175]
  keepout  tip_ejector: X[148.0,162.0] forbids y_motion unless Z < 60.0
```

---

## `lab/machine.py`

`Machine` is the single object a protocol script drives. It owns the resolved `Deck`, the loaded `Effector`, a buffered `Gcode` writer and a `Simulator`, and it exposes operations at the level a procedure is written in (`get_tip`, `prewet`, `aspirate_uL`, `dispense`, `eject_tip`, `park`) rather than at the level of coordinates. Three invariants hold everywhere in this module and are the reason most of it exists:

- **+Z is DOWN.** Z=0 is home at the top; a larger commanded Z is physically lower. `z_travel` is 5.0, and the ejector strip from Z 145 to Z 137 is 8 mm *upward*.
- **Volumes in, millimetres never.** Every liquid call takes microlitres. The conversion to plunger millimetres happens in `Pipette` from stored coefficients (p300: `d = 0.0533 * V + 0.122`; p125: `d = 0.1274 * V + 0.049`). No mm value is typed into a protocol.
- **Nothing reaches disk until it has been checked.** The `Gcode` is constructed buffered (`filename=None`). Every state-changing action is recorded as a `Step` and handed to the simulator as it is generated; `finalize()` is the only thing that writes, and it refuses if the report has errors.

The module also contains one exception type: **`ProtocolError(LabError)`**, raised when the protocol asks for something the machine cannot legally do (unhomed motion, a tip when one is already fitted, dispensing with nothing aspirated). It is a subclass of `LabError`, so a protocol can catch that one class and print the message instead of a traceback.

---

**Construction**

### `Machine.__init__(self, effector: str, name: str, layout: str = "layout.json", strict: bool = True, temp_log: bool = True) -> None`

Loads the deck, labware catalogue, layout and one end effector, and stands up an empty buffered gcode program plus a simulator wired to both.

- `effector` - effector id, i.e. the basename of `config/effectors/<id>.json` ("p300", "p125"; "gripper" exists but is a placeholder).
- `name` - the run name. This is the output basename: `<name>.gcode`, `<name>.manifest.json`, `<name>.balance.csv`, `<name>.toolpath.png`.
- `layout` - layout file in the config dir; `.json` is appended if missing. This is the per-run file that says what is physically on the deck.
- `strict` - passed to `Simulator`. True (default) means the first hard check failure raises `SimulationError` at the step that caused it, so generation stops at the offending line rather than at the end.
- `temp_log` - if False, every `log_temps()` call becomes a no-op. Recorded in `self.toggles` either way so the manifest says which it was.

Returns nothing. Raises `FileNotFoundError` if a config file is missing, `ValueError` if one is not valid JSON, `DeckError` for a layout the deck cannot describe (unknown labware id, duplicate placement name, two placements claiming one slot, a `slot_center` copy that disagrees with `deck.json`), and `EffectorError` for an unknown effector id, an unknown `kind`, or a placeholder (`Machine(effector="gripper", ...)` raises: "every numeric field is unmeasured and it must not be used to generate motion").

The `Gcode` is given `home_position=(deck.x.home, deck.y.home, deck.z.home)` = `(420.0, 0.0, 0.0)`. X homes at max, which is why the home X is 420 and not 0.

Two things are deliberately shouted at construction time, before any motion exists:

- every string in `layout.open_questions` becomes a simulator warning (currently three, including "Which X edge of the tiprack is the post-pickup exit");
- an effector `z_conflict` block becomes a warning naming the conflict and the action. p125 carries one: its water Z 50 is 10 mm lower than p300's 40 while its tube Z 20 is 25 mm higher than p300's 45, which a constant tip-length offset cannot explain. An unresolved conflict recorded in a config is an open crash risk, so it is repeated on every run rather than left in the file for someone to notice.

Public attributes a protocol legitimately reads or sets:

| attribute | meaning |
| --- | --- |
| `deck`, `effector`, `sim`, `g` | the resolved deck, the effector, the simulator, the gcode buffer |
| `homed`, `tip_fitted`, `fluid_uL`, `pos` | machine state. `pos` is the plunger POS in mm, `None` until first commanded |
| `current_tip` | `(row, col)` of the fitted tip, or `None` |
| `steps` | every `Step` generated, in order |
| `dispenses` | one dict per dispense; becomes the balance CSV rows |
| `toggles` | free-form dict recorded in the manifest. `protocols/accuracy_test.py` writes its own knobs here |
| `notes` | text from `note()` |

---

**The recording layer**

Every emitted action goes through `_record`, which is what makes the simulator incremental rather than a post-pass over text. A protocol author never calls anything in this group.

### `xyz(self) -> Tuple[float, float, float]`

The gcode writer's current commanded position, as `(x, y, z)`. Public and safe to call. Before homing the axes read `None`, so this can return `(None, None, None)`; the first two steps of a run legitimately carry `None` coordinates.

### `_record(self, **kw: Any) -> Step`

Builds a `Step`, stamps it with the current machine state, appends it to `self.steps` and hands it to `self.sim.check(step)`.

Stamps `i` (a monotonic counter), `tip=self.tip_fitted`, `fluid_uL=self.fluid_uL` and `gline=self.g.line_count()`, and merges `keepout_exempt: True` into `detail` if the ejector-engagement flag is currently set. Returns the `Step`. Raises `SimulationError` (from `sim.check`, in strict mode) if the step violates a hard check.

Why it matters: `tip` and `fluid_uL` are captured *at record time*, so the order in which an operation flips `self.tip_fitted` relative to its moves decides which Z envelope those moves are checked against. `get_tip` sets `tip_fitted = True` before the lift-out; `eject_tip` clears it immediately after the strip.

### `_engaging_ejector(self)` (context manager)

Sets `self._keepout_exempt` for the duration of the block, so `_record` tags those steps `keepout_exempt: True`.

The keep-out exemption is granted per step, not per operation. The simulator only honours it when both the step carries the flag *and* `step.op` is in the band's whitelist (`["eject_tip"]`). That is what stops the ejector's approach and retract traverses, which are ordinary moves at travel height, from inheriting an exemption from the operation they belong to.

### `_raw(self, line: str, op: str, **detail: Any) -> Step`

Emits one verbatim line and records it as `kind="macro"` with the line text in `detail`. Used for the Klipper macros (`HOME_ACTUATOR`, `LOG_TEMPS TAG=...`). Going through this rather than `g.raw` keeps the macro visible in `steps` and in the manifest.

### `_require_homed(self, what: str) -> None`

Raises `ProtocolError(f"{what} before the machine was homed. Call startup().")` if `self.homed` is False. Guards `goto`, `get_tip` and `eject_tip`. Without it the first move would emit a literal `XNone` into the gcode (gcodepy's `travel_absolute` also guards this, but with a less useful message).

### `_pipette(self) -> Pipette`

Returns `self.effector` narrowed to `Pipette`, or raises `ProtocolError` naming the effector's `kind`. Guards `prewet`, `aspirate_uL` and `dispense` so a gripper cannot be asked to meter liquid.

---

**Motion**

Two rules are enforced structurally rather than by review: motion is never diagonal (one axis per gcode line, one `Step` per axis), and XY only happens at travel height. The simulator hard-fails any XY step whose start or end Z is not `z_travel` unless the op is `wall_wipe` or `eject_tip`.

### `_move_z(self, z: float, feedrate: int, op: str, labware: Optional[str] = None) -> None`

Absolute Z move, recorded as one `Step` with `axes="Z"`.

- `z` - absolute commanded Z in mm. Larger is lower.
- `feedrate` - mm/min.
- `op` - the operation label the step is attributed to.
- `labware` - labware id, so the simulator can apply that labware's well-bottom check to this step.

Returns nothing; emits nothing and records nothing if the delta rounds to zero at 6 dp. The guard duplicates gcodepy's `move_z_absolute` tolerance on purpose: without it a `Step` would exist for a move that produced no gcode line, and the step indices in a simulator error would no longer line up with the file.

### `_move_xy(self, x: float, y: float, feedrate: int, op: str, labware: Optional[str] = None, order: str = "XY") -> None`

Moves the axes named in `order`, one at a time, each as its own gcode line and its own checked `Step`.

- `order` - a string whose characters are iterated. `"XY"` is X then Y, `"YX"` is Y then X, `"X"` moves X only and ignores `y` entirely (`_route_xy` and `_exit_tip_box` both rely on that).

Each axis is skipped if its delta is within 1e-6. One axis per step is also what lets the simulator treat the swept region as an axis-aligned box between `frm` and `to`, which is how `_check_traverse_clearance` catches a path that sweeps over a rack without ending on it.

**Note:** `order` is neither validated nor upper-cased here. `order="xy"` would take the `else` branch for both characters and silently emit a single Y move and no X move at all. The public entry points are safe (`goto` upper-cases and validates `prefer`), but a new caller inside this module could hit it.

### `goto(self, x: float, y: float, op: str = "goto", feedrate: Optional[int] = None, prefer: Optional[str] = None) -> None`

Lift to travel height, then traverse. The only public way to move XY.

- `x`, `y` - absolute machine coordinates in mm.
- `op` - label for the resulting steps.
- `feedrate` - mm/min; defaults to `deck.feedrate("travel")` = 6000.
- `prefer` - `"XY"` or `"YX"`, an axis-order *preference*, normally taken from the destination labware's `approach_order`. The keep-out router overrides it when geometry demands and warns when it does.

Returns nothing. Raises `ProtocolError` if not homed, or if `prefer` is neither "XY" nor "YX", or if the destination cannot be reached without a Y move inside a keep-out band.

The lift comes first and unconditionally. A protocol that has just finished a dispense at Z 45 inside a tube would otherwise traverse the deck at tube depth; the simulator would catch it, but this makes it unrepresentable.

### `goto_placement(self, placement: str, row: int, col: int, op: str = "goto") -> Tuple[float, float]`

Traverse to a well by placement name and 0-indexed `(row, col)`, honouring that labware's `approach_order`. Returns the resolved `(x, y)`.

Raises `DeckError` if no placement has that name, `IndexError` if the well is outside the grid.

This is the one to use for a well: it is `goto_well` plus `prefer=p.labware.approach_order`. For `tube_rack_0p65_5x10` that is `"YX"`, which the labware config explains: the tube caps make Y motion across the holder difficult, so the effector must reach its Y while still clear of the holder in X and then come in along X. X motion passes between cap rows.

### `_route_xy(self, x, y, feedrate, op, labware=None, prefer=None) -> None`

Traverse to XY without ever moving Y inside a keep-out band. This is the routing brain and the one function in the module worth reading twice.

The physical constraint: `deck.json` declares one keep-out, `tip_ejector`, spanning **X [148.0, 162.0]** (the confirmed ejector lane X 155 plus or minus 7.0). It forbids **Y motion only** - a pure X move straight through the band is harmless, because the mechanism is a hazard to the effector crossing it in Y, not to the effector passing along it in X. The band is nominally safe when the effector is above Z 60 (commanded `z < 60`, and travel height 5.0 qualifies), but that figure was stated for the bare p300, so the simulator treats **any tipped Y move in the band as a hard error**, exemption aside. Since every liquid-handling move happens with a tip on, in practice the band is absolute.

Why X-first is the default: the tip box requires it. After a pickup the effector must leave the rack laterally before any Y motion, because passing over an occupied tip position shears the fresh tip off the nosecone.

Why it deviates: X-first puts the Y leg at the **target** X; Y-first puts it at the **current** X. With the ejector band at X [148, 162], the tube holder in slot 8 has two columns inside it - column 8 at X 160.0 and column 9 at X 148.0 - so an X-first approach to either would perform its Y leg exactly where Y motion is forbidden. Whichever end is clear of every band decides the order; the preference only breaks the tie.

The decision sequence, after `prefer` is upper-cased and validated:

| condition | action |
| --- | --- |
| `y` unchanged (within 1e-6) | single X move, `order="X"`. `prefer` is irrelevant |
| `prefer="YX"` and current X clear | `order="YX"`, no warning |
| `prefer="XY"` and target X clear | `order="XY"`, no warning |
| target X clear (preference was YX) | `order="XY"` plus an `order_override:<op>` warning naming both X values |
| current X clear, `prefer="YX"` | `order="YX"` |
| current X clear, `prefer="XY"` | `order="YX"` plus an `order_override:<op>` warning |
| both blocked | dogleg |

The dogleg: `via = _nearest_clear_x(x)`; if none exists, `ProtocolError` naming both X values. Otherwise a `dogleg:<via>` warning, then three moves - X to `via` at the current Y, Y to the target at `via`, X to the target at the target Y. Verified: from (155, 300) to (150, 200) it emits X 155 -> 146, Y 300 -> 200 at X 146, X 146 -> 150. Both warning keys dedupe (`order_override` per op name, `dogleg` per via X), so a loop of 50 dispenses into a blocked column warns once.

**Note:** the docstring's example cites "the tube rack columns at X150 and X162". The real blocked columns are column 8 at X 160.0 and column 9 at X 148.0; X 162 is the band's upper edge, not a column. The code is right, the comment's numbers are not.

### `_nearest_clear_x(self, target: float, pad: float = 2.0) -> Optional[float]`

The X closest to `target` at which no band forbids Y motion, or `None`. Considers only `x_range[0] - pad` and `x_range[1] + pad` for each band containing `target`, discards candidates outside the X envelope [0, 420] or inside another band, and returns the nearest. For the single current band that is 146.0 or 164.0.

**Note:** `pad` is 2.0 mm, measured off band edges that `deck.json` itself flags as unverified: "The 14 mm width is carried over from the previously documented band centred on X 85 and is still UNVERIFIED at this location." A dogleg therefore clears the mechanism by 2 mm against a boundary nobody has measured. Widening the band in config is the fix; the pad is not the place to buy margin.

### `goto_station(self, name: str, op: Optional[str] = None) -> None`

Traverse to a named fixed station from `deck.json` (`water` at (70.0, 175.0), `park` at (420.0, 0.0), `tip_eject` at (155.0, 89.0)). `op` defaults to `f"goto:{name}"`. Raises `DeckError` for an unknown station. Does not pass `prefer`, so stations are always approached X-first-preferred.

### `goto_well(self, placement: str, row: int, col: int, op: str = "goto") -> Tuple[float, float]`

Resolve a well to machine coordinates and traverse there. Returns `(x, y)`. Raises `DeckError` for an unknown placement name, `IndexError` for a well outside the grid.

**Note:** identical to `goto_placement` except that it does **not** pass the labware's `approach_order`, so a `goto_well("tubes", ...)` approaches the tube holder X-first, across the caps, which is the motion `approach_order: "YX"` was added to avoid. Neither method is used by any protocol or test in the repo today. Prefer `goto_placement`, or give `goto_well` the same `prefer` and delete one of them.

---

**Actuator and annotation**

### `actuator(self, pos: float, settle: float = 0.0, op: str = "actuator") -> float`

Command the plunger to an absolute POS, validated, tracked and recorded.

- `pos` - absolute plunger position in mm. For p300 the legal range is the floor `pos_min + stroke_margin` = 1.0 up to `blowout_pos` = 30.0. Larger POS is further bottomed (more dispensed).
- `settle` - seconds to dwell afterwards; 0 emits nothing.
- `op` - label.

Returns the rounded POS actually commanded. Raises `EffectorError` via `Pipette.check_pos` if the POS is below the floor ("would put plunger POS at 0.5, below the 1.0 mm floor") or past blow-out. A non-pipette effector is not POS-validated here.

Emits `ACTUATOR_MOVE POS=` (from `deck.klipper_macros`) with the value formatted at the effector's `actuator.decimals`, which is 3 for both pipettes. The comment records why: quantizing once at fixed width avoids compounding two roundings, 2 dp would inject 0.63% error at the 10% ISO point, and 4 dp is below the actuator's step.

### `dwell(self, seconds: float, op: str = "dwell") -> None`

Emit `G4` for `seconds` and record it. A zero, negative or `None`-ish value is a silent no-op. gcodepy's `dwell` raises `ValueError` on a non-positive duration, so this guard is what lets callers pass a settle time straight from config without checking it first.

### `wait(self) -> None`

Emit `M400` (wait for the move queue to drain). Used before and after anything where the next action must not start early: after arriving at a well, around the tip press, around each ejector waypoint.

**Note:** this is the only emitter in the module that records no `Step`. The simulator never sees it, and `M400` lines are counted in `gcode_lines` but not in the step count.

### `log_temps(self, tag: str) -> None`

Emit `LOG_TEMPS TAG=<tag>`, or nothing at all if the machine was constructed with `temp_log=False`. Raises `DeckError` if `klipper_macros.log_temps` is missing. `tag` is a free-form label; `protocols/accuracy_test.py` uses `"start"`, `"prewet_done"`, `f"v{volume:g}_r{rep}"`, `"end"` so a temperature trace can be joined back to individual dispenses.

### `note(self, text: str) -> None`

Emit `; <text>` as a gcode comment, append it to `self.notes` (which the manifest carries), and record a `kind="note"` step. The point is that a note lands in both artifacts, so a comment explaining a run cannot exist in the gcode and be absent from the manifest.

---

**Operations**

### `home(self, op: str = "home") -> None`

Home Z, then XY, then the actuator. Emits `G28 Z`, `G28 X Y`, `HOME_ACTUATOR`, records a step for each, and sets `self.homed = True`. Z first is deliberate: it lifts the effector before any XY reference move. After this the writer's position is the home position `(420.0, 0.0, 0.0)`.

### `startup(self) -> None`

Home everything, reference the plunger at blow-out, lift to travel height, then `M400`.

The plunger goes to `blowout_pos` (30.0 for p300) **before** anything else moves, and the comment gives the reason: bottomed first means no vacuum while pressing onto a tip. A plunger left mid-stroke would aspirate air through the tip during the press and the first real aspiration would be wrong by an unknown amount.

Raises whatever `home`, `actuator` or `_move_z` raise. Non-pipette effectors skip the plunger reference.

### `park(self) -> None`

Traverse to the `park` station and retract Z. Uses the station's own `z` if it has one (0.0, which is Z home, the top) and `deck.z_home` otherwise.

`park` at (420.0, 0.0) is X home and Y home, which is what that station has always meant; `deck.json` records that it was X 350 in the pre-shift frame and gained the +70 X correction, and that "park at X350 in a 0-350 machine WAS 'go to X home'" is part of the evidence for the frame shift itself.

**Note:** because `goto` lifts to travel height first, parking straight after the post-ejection `G28` emits `G0 F6000 Z5` followed immediately by `G0 F6000 Z0` - down 5 mm and back up. Harmless, but it looks like a bug in the output and is worth recognising as not one.

### `get_tip(self, row: int = 0, col: Optional[int] = None, placement: str = "tips") -> Tuple[int, int]`

Press the nosecone into a tip, lift it out, and leave the rack in X before any Y motion. Returns the `(row, col)` actually picked.

- `row` - 0-indexed rack row. Default 0.
- `col` - 0-indexed column, or `None` to let `_next_free_tip_col` choose the first unused column working inward from the exit edge.
- `placement` - layout placement name, cross-checked against the effector's own `tips.labware`.

Raises `ProtocolError` if not homed; if the effector's `tips.required` is false ("p125 does not use tips ... so get_tip() is meaningless"); if a tip is already fitted; if the named placement holds the wrong labware; if the requested position was already consumed this run; or if the row is exhausted. Raises `IndexError` for a position outside the grid, and `EffectorError` if the effector has no `pick` Z for that rack.

The sequence, for p300 on the rack in slot 3 (grid solved from three measured well centres, X 18.0 to 118.0, Y 335.5 to 399.2, real pitch 9.0909 by 9.1):

1. `goto(x, y, prefer=p.labware.approach_order)` - the tiprack declares no `approach_order`, so X-first. `M400`.
2. Descend to the `approach` Z if the effector declares one (p300: 47.0), otherwise stay at travel height. The fallback is why `_has_z` exists: an effector without an approach entry gets a no-op, not a crash.
3. Descend to the `pick` Z (p300: 59.0) at `tip_press` feedrate 600, then `M400`, then dwell 1 s. `pick=59.0` came from a direct rack measurement and supersedes the `Z_TIP_PICK=57.5` in the old script.
4. Set `tip_fitted = True`, `current_tip = (row, col)`, and register the position as consumed so it can never be picked twice.
5. Lift to travel height at 600. This is the first step recorded with `tip=True`, so from here the simulator uses the tipped Z envelope: the deepest legal commanded Z on-stage drops from 114.75 to 62.5.
6. `_exit_tip_box(...)` - leave the rack in X.

The consumed-position check is the sharpest guard here, and its message says why: "Pressing into an empty rack hole drives the bare nosecone down to the pick depth with nothing to stop it." Z 59.0 was calibrated with a tip in the hole to stop the descent.

**Note:** `exit_edge, exit_col = self._tip_exit(p)` unpacks `exit_col` and never uses it. Harmless, but it reads as though the default column comes from there when it actually comes from `_next_free_tip_col`.

### `_tip_placement(self, placement: str) -> Placement`

Resolves which rack this effector takes tips from. If the named placement exists, its labware id must equal the effector's `tips.labware` or `ProtocolError` names both and points at the config field to change. If the name does not exist and the effector declares a tip labware, it falls back to `deck.placement_for_labware(want)`.

Prevents a specific misdiagnosis: routing by placement name alone would send a second effector that declares a different rack to the wrong rack, and the resulting `EffectorError` would then tell the operator to add the *wrong* rack to that effector's Z table. Not for protocol authors.

### `_next_free_tip_col(self, p: Placement, row: int, exit_edge: str) -> int`

First unused column in `row`, ordered from the exit edge inward (ascending X for `low_x`, descending for `high_x`). Raises `ProtocolError` when the row is exhausted, suggesting a reload or a different row.

This encodes the physical constraint, not a convenience: the effector exits across every column between the one it picked and the edge, so the edge column must be consumed first and the box must empty toward the middle. Verified consumption order on the current rack: columns 0, 1, 2, 3. Not for protocol authors, but reading it explains why you cannot pick an arbitrary column on a full rack.

### `_has_z(self, labware_id: str, key: str) -> bool`

True if the effector's `z_for_labware[labware_id][key]` exists and is not null. Distinguishes "no approach height configured" from "configured as null, meaning never measured" - `Effector.z_for` raises on the latter, so `get_tip` has to ask before it asks for the value.

### `_tip_exit(self, p: Placement) -> Tuple[str, int]`

Returns `("low_x"|"high_x", column_index)` from the labware's `consume_order.exit_edge`, accepting `low_x`/`min_x` and `high_x`/`max_x`.

For anything else - and `tiprack_300_96` currently says `"unconfirmed"` - it assumes the low-X edge and emits a warning naming the assumed column and X ("assuming the low-X edge (column 0, X 18.0) ... if it is the high-X edge, every tip pickup in this run exits the wrong way"). Guessing is acceptable only because `check_tip_exit` independently fails if the chosen exit path crosses an occupied position, whichever edge turns out to be right. Not for protocol authors.

### `_exit_tip_box(self, p: Placement, row: int, col: int, edge: str) -> None`

Moves X to one column pitch beyond the outermost well centre on the exit side, clamped into the X envelope, at travel feedrate. On the current rack that is `18.0 - 9.0909 = 8.909`.

Before moving, it builds a `Step` by hand (bypassing `_record`) and calls `sim.check_tip_exit(step, row, col, crossed)` with the list of positions the X path crosses. That check hard-fails if any of them still holds a tip.

One pitch beyond the outermost centre clears every tip *position* without requiring the rack's physical outline, which has never been measured (the three corners are well centres, not the box). And the move is not redundant with the next `goto`: it guarantees the effector is out of the box before `_route_xy` makes any decision that could put Y first. Not for protocol authors.

### `eject_tip(self) -> None`

Hook the tip collar on the ejector and pull UP to strip it, then re-home.

Takes no arguments. Raises `ProtocolError` if not homed, if the effector's `tips.eject_supported` is false, or if no tip is fitted. All geometry comes from `deck.stations.tip_eject`: `lane_x` 155.0, `approach_y` 83.0, `engage_y` 88.0, `retract_y` 80.0, `z_below_hook` 145.0, `z_strip` 137.0, `rehome_after` true, `validated` **false**.

Because `validated` is false, every call warns: the routine has never been validated on hardware, no gcode file in the repo contains an ejection sequence, so it has no working reference. Dry-run it, then step it through manually at reduced feedrate.

The choreography, with Z direction spelled out at every step (feedrate 6000 unless noted; `slow` is 600):

1. **Lift to travel height**, Z 5.0. No-op if already there.
2. **Approach through the router**, `_route_xy(155.0, 83.0)`, at Z 5. Hand-ordering was rejected here: the lane X 155 is itself inside the keep-out band [148, 162], so the Y leg cannot happen at the target X. The router works that out from geometry and does Y first at the current X, then X in. Verified from the tip-box exit: `Y 399.2 -> 83.0` at X 8.909, then `X 8.909 -> 155.0` at Y 83.0. Then `M400`.
3. **Descend below the hook**, Z 5.0 -> **145.0** at feedrate 600. That is 140 mm **DOWN**. Legal only because Y 83 is off the front of the stage region (which starts at Y 131.875), so the stage surface at 114.75 is not there to hit; the simulator warns exactly that, and `Deck.point_on_stage` is the test rather than an assumption. `M400`.
4. **Engage** (exempt), `Y 83.0 -> 88.0` at X 155, Z 145. 5 mm of Y, laterally bringing the collar under the hook. This is precisely the motion the keep-out band forbids, at precisely the X the band exists for, which is why the exemption is scoped to these steps and nothing else. `M400`.
5. **Strip** (exempt), `Z 145.0 -> 137.0` at 600. **+Z is down, so this is 8 mm UPWARD** - the tip collar is held by the hook and the machine pulls the nosecone up out of the tip. `tip_fitted` and `current_tip` are cleared immediately after. `M400`.
6. **Back down clear of the hook** (exempt), `Z 137.0 -> 145.0`, 8 mm **DOWN**. Recorded with `tip=False`, since the tip is gone by now.
7. **Retract out of the lane** (exempt), `Y 88.0 -> 80.0` at X 155, Z 145. 8 mm of Y, still inside the band, still exempt.
8. **Lift**, `Z 145.0 -> 5.0`, 140 mm **UP**, at travel feedrate, outside the exemption block (a Z-only move, so no keep-out check applies anyway). `M400`. Then a `kind="note"` step with `detail={"final": True}`, which is what arms the simulator's must-re-home rule. Placing that marker *after* the lift is deliberate: if it came first, the lift itself would be the move that trips "an ejection was not followed by G28".
9. **Re-home**, `G28` (all axes), if `rehome_after`. Mandatory, not tidy-up: the strip force can skip steps on an open-loop machine, so the commanded position afterwards is untrustworthy. The simulator enforces it in both directions - the first `move` or `actuator` step after the ejection marker without an intervening home is a hard failure, and a run that *ends* on an ejection with no `G28` is a whole-run error that makes `finalize` refuse.

**Note:** the approach at step 2 does not pass `prefer="YX"`, so `prefer` defaults to "XY" and the router logs `eject_tip: prefers XY but the target X 155.0 is inside a keep-out band, so the Y leg was done first`. The routing is correct; the warning is misleading, because Y-first is the required order here rather than an override of a preference. Passing `prefer="YX"` would silence it and document the intent.

### `goto_reservoir(self, station: str = "water") -> None`

Traverse to a reservoir station (default `water` at (70.0, 175.0)) with `op="reservoir"`. A thin alias over `goto_station`.

### `prewet(self, cycles: Optional[int] = None, station: str = "water") -> None`

Wet the tip wall so the first real aspiration is not wetting dry plastic, which otherwise costs a few uL on the first draw only and shows up as a single outlier.

- `cycles` - number of aspirate/blow-out cycles, or `None` for the effector's configured `prewet.cycles` (3 for p300).
- `station` - station name whose placement supplies the working Z.

Raises `ProtocolError` if the effector is not a pipette, if `prewet.enabled` is false ("p125 has prewet disabled in its config; enable it there rather than calling prewet() anyway"), or if `cycles` is below the configured `min_cycles` (3). Raises `EffectorError` if the reservoir's `work` Z is null.

Sequence: traverse to the station, `M400`, plunger to `blowout_pos` 30.0 with a 0.5 s settle, descend to the reservoir `work` Z (40.0 for p300, tip submerged), `M400`, then `n` times `[asp_pos 10.0, settle 1 s, blowout 30.0, settle 1 s]`, then lift to travel height. Records the count in `self.toggles["prewet_cycles"]`, so the manifest states how much prewetting the run actually did. Note `prewet.asp_pos` is measured from bottomed, not from `gap_pos`.

### `_station_labware(self, station: str) -> str`

The labware id of the placement mounted at a station name. Raises `ProtocolError` if no placement sits there: "the library cannot know what is there". Prevents the alternative, which is defaulting to some assumed reservoir and descending to a Z calibrated for a different vessel.

**Note:** `prewet` resolves the same id the long way round, `deck.placement_for_labware(self._station_labware(station)).labware.id`, which is the identity function on the result. Harmless, except that `placement_for_labware` raises `DeckError` if two placements ever share that labware, so the redundant hop adds a failure mode `aspirate_uL` (which calls `_station_labware` directly) does not have.

### `aspirate_uL(self, volume_uL: float, station: str = "water") -> float`

One independent aspiration. Returns the plunger POS used for the draw.

- `volume_uL` - **microlitres**, always. Converted to mm inside the pipette from the stored calibration.
- `station` - source station name, default `water`.

Raises `ProtocolError` if the effector is not a pipette, if it requires a tip and none is fitted, or if the machine is already holding fluid. Raises `VolumeError` if the volume is outside the calibration window (p300: [8.0, 320.0] uL - "The curve was never fitted there; the displacement would be an extrapolation") or beyond the stroke available. Raises `EffectorError` if a POS in the stroke would breach the plunger limits, or if the reservoir has no `work` Z.

The "already holding" check encodes the experimental design, not a mechanical limit: every tube gets its own independent aspiration, so carrying fluid across two dispenses would silently turn a set of independent measurements into a serial dispense.

`displacement_mm`, `aspirate_pos` **and** `backoff_pos` are all computed before a single line is emitted. The reason is stated in the code: discovering at the backoff that the plunger has run out of travel would leave a half-finished aspiration in the buffer with the state claiming it completed. The backoff is a real position the machine visits, so validating only the aspirate POS would validate a position it never reaches.

Sequence, for 150.0 uL on p300 (`d = 8.117 mm`, `asp = 25 - 8.117 = 16.883`, `backoff = 15.883`): clear `_last_aspirate`, traverse to the station, `M400`, plunger to `gap_pos` 25.0 with a 1 s settle **in air** (the 5 mm air gap must be armed before the tip is in liquid or it draws water instead), descend to Z 40.0, `M400`, plunger to 16.883 and settle 1 s, lift to travel height, then if `post_asp_backoff` is nonzero retract a further 1.0 mm to POS 15.883 as anti-drip - in air, after leaving the water. Finally record `{volume_uL, displacement_mm, aspirate_pos, backoff_pos}` in `_last_aspirate` for the dispense to consume.

### `dispense(self, placement: str, row: int, col: int, wall_wipe: Optional[bool] = None) -> Dict[str, Any]`

Full dispense to blow-out, optionally wipe the droplet off the tip, and return the manifest row.

- `placement` - layout placement name, e.g. `"tubes"`.
- `row`, `col` - 0-indexed. On `tube_rack_0p65_5x10` (slot 8) increasing col moves toward **lower** X at 12.0 mm pitch (X 256.0 down to 148.0) and increasing row toward lower Y at 18.0 mm (Y 223.0 down to 151.0). That is the opposite column sense from the tiprack; do not carry one convention across both.
- `wall_wipe` - `True`/`False` to force, `None` to wipe if the labware has a `wipe` block (the tube holder does: `dx` 3.2, `dz` 4.0, `feed` 1500, all three flagged uncalibrated).

Returns a dict with `order_index`, `placement`, `labware`, `slot`, `well` (an "A10"-style name), `row`, `col`, `x`, `y`, `z`, `target_volume_uL`, the four fields carried over from the aspiration, and `mass_dry_mg` / `mass_wet_mg` / `temp_C` set to `None` for the balance to fill in. Also appended to `self.dispenses`, which becomes `<name>.balance.csv`.

Raises `ProtocolError` if the effector is not a pipette, if nothing is aspirated, or if `wall_wipe=True` on labware with no wipe block. Raises `DeckError` for an unknown placement, `IndexError` for a well outside the grid (row 9 of a 5 x 10 rack raises here, not a `ProtocolError`), and `EffectorError` if the labware's `work` Z is null.

The wipe decision is settled before any motion. The comment gives the reason and it is a good one: raising after the blow-out would leave a complete, irreversible dispense in the buffer with no manifest row to account for it, so the gcode and the balance sheet would disagree about how many dispenses happened.

Sequence: `goto` with `prefer=p.labware.approach_order` (`"YX"` for the tube holder), descend to the `work` Z (45.0 for p300 on the tube rack, **flagged uncalibrated** - "was marked `<-- TUNE` in the source and was never tuned"), `M400`, plunger to `blowout_pos` 30.0 with a 1 s settle, zero `fluid_uL`, optionally wipe, lift to travel height, `M400`. `_last_aspirate` is then cleared: one aspiration feeds exactly one dispense.

### `_wall_wipe(self, p: Placement, x: float, y: float) -> None`

Step sideways to the inner wall, drag the droplet UP, then pull back off the wall. Uses `dx`, `dz` and `feed` from the labware's `wipe` block.

Emits three moves at well depth: X to `x + dx` (148.0 -> 151.2 for column 9), Z to `z_work - dz` (45.0 -> 41.0, and **subtracting is UP**), then X back to `x`. It does not restore Z; the caller's lift to travel height does.

Before moving it asserts its own starting pose and raises `ProtocolError` if the effector is not exactly at `(x, y, z_work)`. That guard is load-bearing: `wall_wipe` is one of only two ops the simulator exempts from the travel-height rule, so calling it from anywhere else would traverse the deck at tube depth with the check that would normally catch it switched off. `dx` must stay under the tube's inner radius, and the whole wipe stays inside one stage location, which is what keeps it clear of the raised-edge rule (that rule applies to motion crossing a location boundary, not motion inside one). Never call this from a protocol.

---

**Finalize**

### `simulate(self) -> Report`

Run the whole-run checks and return the `Report`. Idempotent: `Simulator.finish` returns the same object on every subsequent call, so inspecting the report and then calling `finalize` does not duplicate every whole-run line into the manifest.

The whole-run layer adds what per-step checks cannot see: a run ending on an ejection with no `G28` (error), footprint overlap between placements (error), fluid still held at the end (warning), a tip still fitted and never ejected (warning), and the slot-seating report.

### `finalize(self, outdir: str = ".", plot: bool = True) -> Dict[str, Any]`

Run the whole-run checks, then write the gcode, manifest and balance CSV - or refuse.

- `outdir` - output directory, created if needed.
- `plot` - also render `<name>.toolpath.png` (XY path coloured by Z plus an inverted Z-vs-step trace). Returns `None` for `plot` if matplotlib is not installed.

Returns `{"gcode", "manifest", "plot", "report", "lines", "dispenses"}` - the first three are paths (`plot` may be `None`), `report` is the `Report`, `lines` is the gcode line count, `dispenses` is the count.

Raises `ProtocolError` with the full report text if `report.ok` is false. In strict mode most failures have already raised `SimulationError` during generation, so the errors that surface *here* are the whole-run ones, which are appended without raising. Verified: with `rehome_after` disabled, `finalize` raises "simulation failed; refusing to write gcode" and no file is written.

This is the only function in the module that touches the filesystem, which is the whole point of constructing the `Gcode` buffered.

**Note:** `outdir` is created before the `report.ok` check, so a refused run still leaves an empty directory behind.

---

**Provenance**

These exist so a `.gcode` file found on disk in six months can be traced to the exact inputs that produced it. `logbook.build_manifest` calls all four.

### `config_hashes(self) -> Dict[str, str]`

SHA-256 of the bytes of every config consumed, keyed by a readable name: `deck.json`, `labware.json`, the layout file's own basename, and `effectors/<id>.json`. Hashing the bytes rather than recording a version field means an edit cannot be silently unrecorded.

### `git_commit(self) -> Optional[str]`

`git rev-parse HEAD` run in `config.repo_root()`, stripped. Returns `None` on empty output or any failure (including the 10 s timeout) rather than raising - provenance that is unavailable must not be able to stop a run from being written.

### `git_dirty(self) -> Optional[bool]`

True if `git status --porcelain` in the repo root produces any output, i.e. there are uncommitted or untracked changes anywhere in the repo. `None` if git could not be run. A `git_commit` recorded with `git_dirty: true` in the manifest means the commit hash does not fully describe the code that generated the file.

### `uncalibrated(self) -> List[str]`

Every field the configs admit was never tuned, deck entries followed by effector entries, as dotted strings. On the current configs this returns nine entries, among them `tubes(tube_rack_0p65_5x10).wipe.dx`, `tips(tiprack_300_96).consume_order.exit_edge`, `deck.keepouts.tip_ejector.x_range`, `deck.stations.tip_eject (routine never validated on hardware)` and `p300.z_for_labware.tube_rack_0p65_5x10.work`.

The list is assembled from structured `uncalibrated` arrays and explicit `validated: false` flags, never grepped out of prose notes, so rewording a comment cannot make an uncalibrated field look calibrated. `protocols/accuracy_test.py` prints it at the end of every run.

---

## `lab/simulate.py`

Dry-run checking. The simulator is fed every action as `Machine` generates it, and it either stays quiet, records a warning, or raises `SimulationError` on the first hard violation. Nothing reaches disk until the run has passed: `Machine.finalize()` calls `Simulator.finish()` and refuses to write the `.gcode` if `Report.ok` is false. The module holds no hardware or Moonraker code at all - it is pure geometry against `config/deck.json`, `config/labware.json`, `config/layout.json` and the effector spec.

Two conventions govern every number below. **+Z is DOWN**: Z=0 is home at the top, a larger commanded Z is physically lower, and "above Z 60" means `z < 60`. The ejector's Z 145 -> Z 137 is an 8 mm *upward* stroke. And **a check that cannot be evaluated is not a pass**: where a required measurement is absent - a tip length, the stage surface, a labware rim, a footprint - the simulator emits an explicit `cannot check ...` warning naming the missing config field instead of silently skipping the comparison. This matters because the failure mode of a missing measurement is not "no check ran", it is "a human read a clean report". The p300 run in `out/p300_accuracy.manifest.json` currently carries 14 warnings, 9 of which are `cannot check` lines, and that is the honest state of the deck.

### `class SimulationError(LabError)`

```python
SimulationError(step: Step, message: str)
```

Raised the moment a hard check fails; a run that raises this must never become a `.gcode` file.

- `step` - the `Step` being checked. Stored as `.step`.
- `message` - what is physically wrong, in operator-facing terms. Stored as `.message`.

The formatted `str()` is three lines: `step {i} [{op}/{kind}]: {message}`, then the motion (`at {frm} -> {to}`), then the plunger and payload state (`POS {pos_before} -> {pos_after}  tip={tip}  fluid={fluid_uL} uL`). It exists so that the operator gets the machine state that made the move illegal, not just a coordinate. Subclasses `LabError`, so a protocol's single `except LabError` catches it alongside `DeckError`, `EffectorError`, `VolumeError` and `ProtocolError` (`tests/test_review_fixes.py:test_8_all_errors_share_a_base`).

Note that `step.gline` (the gcode line number at the time of recording) is on the step but is *not* printed in the message; read it off `err.step.gline` when you need to find the emitted line.

### `@dataclass class Step`

```python
Step(i: int, op: str, kind: str, frm: Optional[XYZ] = None, to: Optional[XYZ] = None,
     axes: str = "", pos_before: Optional[float] = None, pos_after: Optional[float] = None,
     tip: bool = False, fluid_uL: float = 0.0, feedrate: Optional[int] = None,
     labware: Optional[str] = None, detail: Dict[str, Any] = {}, gline: int = 0)
```

One generated action plus the machine state it produced. Constructed by `Machine._record()`, never by a protocol.

- `i` - 0-based step index, unique within a run. Appears in every error message.
- `op` - the operation that produced it (`"get_tip"`, `"aspirate"`, `"dispense"`, `"eject_tip"`, `"wall_wipe"`, `"startup"`, `"park"`, ...). Checks are keyed on this for whitelists, so a new operation name that needs an exemption must be added to the relevant set explicitly.
- `kind` - one of `move`, `actuator`, `home`, `dwell`, `macro`, `note`. Selects which checks run: only `move` gets geometry, only `actuator` gets plunger limits, `home` clears the post-ejection flag, and `dwell`/`macro`/`note` are recorded but geometrically inert.
- `frm`, `to` - `(x, y, z)` in machine mm before and after. `None` for steps that are not motion.
- `axes` - which axes this single step commanded, as a string such as `"X"`, `"Y"` or `"Z"`. `Machine._move_xy` emits one axis per step, which is what lets the swept path be treated as an axis-aligned box.
- `pos_before`, `pos_after` - plunger POS in mm, actuator steps only.
- `tip` - whether a tip was fitted when this step was emitted. Every Z limit in this module depends on it.
- `fluid_uL` - held volume in uL at emission time.
- `feedrate` - mm/min, as commanded.
- `labware` - the *labware id* (for example `"tube_rack_0p65_5x10"`), not the placement name, when the step is working at a specific vessel. Drives the well-bottom check.
- `detail` - per-step flags. Two are load-bearing: `detail["keepout_exempt"]` (set by `Machine._engaging_ejector()`) and `detail["final"]` on the marker that says an ejection has completed. `detail["crossed"]` carries the tip positions an exit path sweeps over.
- `gline` - `Gcode.line_count()` at the time of recording, for mapping a step back to an emitted line.

It exists because the checks are physical, and physics depends on state that a G-code line does not carry. `G1 Z62.5` is legal bare and a crash with a tip on; only the step knows which.

#### `Step.moves_y` / `Step.moves_xy`

`bool` properties: `"Y" in axes`, and `"X" in axes or "Y" in axes`. Used to decide whether the travel-height rule, the keep-out rule and the raised-edge rule apply. Note they read `axes`, so `home` steps (which have `axes == ""` and record their axes under `detail["axes"]`) are never treated as XY motion - homing is not geometrically checked at all.

### `_LOW_XY_OK` (module constant)

```python
_LOW_XY_OK = {"wall_wipe", "eject_tip"}
```

The only two operations allowed to move X or Y at something other than the travel height (Z 5.0). The wall wipe works inside a tube at `z_work` and steps 3.2 mm sideways to the inner wall; the ejector engagement moves Y at Z 145. Everything else that moves XY off travel height is a hard error.

This set grants an exemption from the travel-height rule *only*. Keep-out exemption is deliberately **not** granted here - it is granted per step through `detail["keepout_exempt"]`, so the approach and retract moves that surround an ejector engagement cannot inherit the exemption from the operation name they share with it.

### `@dataclass class Report`

```python
Report(errors: List[str] = [], warnings: List[str] = [], steps: int = 0,
       path: List[Tuple[float, float, float, str]] = [])
```

The accumulated result of a run. Lives on `Simulator.report`, is returned by `finish()`, is embedded whole into the manifest under `"simulation"`, and is what `Machine.finalize()` consults before writing.

- `errors` - hard violations, as `"step {i} [{op}]: {message}"` strings. Non-empty means no gcode.
- `warnings` - everything else, including every `cannot check` line. De-duplicated by key (see `warn`).
- `steps` - highest step index seen, plus one.
- `path` - `(x, y, z, op)` for every step whose `to` is fully known. Feeds `plot_path`.

#### `Report.ok`

`bool` property: `not self.errors`. Warnings never block a run - by design, since most of them are missing measurements rather than known violations, and blocking on them would stop the machine being used at all.

#### `Report.text()`

```python
text() -> str
```

Renders the report as a header line (`simulation: N steps, E error(s), W warning(s)`) followed by one indented line per error (`  ERROR   ...`) then per warning (`  warning ...`). Returns the string; raises nothing. Used verbatim in the `ProtocolError` that `Machine.finalize()` raises when it refuses to write.

### `class Simulator`

```python
Simulator(deck: Deck, effector: Effector, strict: bool = True)
```

Incremental checker. `Machine` constructs exactly one and calls `check()` on every step as it is generated.

- `deck` - resolved `Deck`. Source of the envelope, `z_travel`, `z_stage_surface`, the slot table, the keep-out bands and the loaded placements.
- `effector` - the loaded `Effector`. Source of `tip_labware`, `can_eject_tip` and, for a `Pipette`, the plunger limits.
- `strict` - when `True` (the default), `fail()` raises `SimulationError` immediately, so generation stops at the first violation and the traceback points at the protocol line that caused it. When `False`, failures are only appended to `report.errors` and generation continues, which is how you collect *all* violations in one pass; `Machine.finalize()` still refuses to write. Non-strict mode is what `tools/` and several tests use to compare error counts before and after a move.

Public state a protocol or tool may read:

- `report` - the live `Report`.
- `consumed_tips: Set[Tuple[int, int]]` - `(row, col)` of every tip picked this run. `Machine.get_tip` reads it to refuse a second press into an already-emptied hole (pressing into an empty rack hole drives the bare nosecone to the pick depth, Z 59.0, with nothing to stop it) and `Machine._next_free_tip_col` reads it to pick the next column working inward from the exit edge.

#### `Simulator.warn(key, message)`

```python
warn(key: str, message: str) -> None
```

Records a warning the first time a given `key` is seen and ignores it thereafter.

- `key` - the de-duplication identity. Choose it to distinguish physically different cases, not textually different messages.
- `message` - the operator-facing text appended to `report.warnings`.

Returns `None`, raises nothing. It exists because a protocol loops: the accuracy test emits 418 steps over 20 tubes, and an unkeyed "z.top is unmeasured" would appear hundreds of times and bury the one warning that mattered. The keying is subtle in one place worth copying: the Z-envelope gap is keyed `z_limit:tipped` / `z_limit:bare` precisely because the tipped case can be unresolvable while the bare case is fine, and collapsing them would swallow the more dangerous of the two as a duplicate.

#### `Simulator.fail(step, message)`

```python
fail(step: Step, message: str) -> None
```

Records a hard violation and, in strict mode, raises.

- `step` - the offending step (or, for the post-ejection rule, the step being blamed).
- `message` - what is physically wrong.

Raises `SimulationError` when `self.strict`; otherwise returns after appending to `report.errors`.

#### `Simulator.tip_length()`

```python
tip_length() -> Optional[float]
```

Length in mm from the nosecone reference to the tip orifice, looked up as `deck.catalogue[effector.tip_labware].spec["tip"]["length"]`.

Returns `52.25` for the p300 today, or `None` if the effector declares no tip labware (the p125 has `tips.labware: null`) or the labware is not in the catalogue or carries no `tip.length`. Raises nothing - a missing value is returned as `None` so callers can turn it into a `cannot check` warning rather than a crash or a false pass.

The 52.25 is itself derived, not guessed: `deck.z.stage_surface` 114.75 (bare p300 nosecone touching the stage) minus `deck.z.stage_surface_with_p300_tip` 62.5.

#### `Simulator.effective_z_limit(tip_fitted, clearance=0.0)`

```python
effective_z_limit(tip_fitted: bool, clearance: float = 0.0) -> Tuple[Optional[float], List[str]]
```

The deepest legal commanded Z over the stage, together with a list of whatever is missing if it cannot be computed.

- `tip_fitted` - whether a tip is on. This is the whole point of the function.
- `clearance` - mm of extra safety margin to subtract from the limit. Nothing in the repo passes it today; it is the hook for "never come within N mm of the floor".

Returns `(limit_mm, [])` when computable, otherwise `(None, missing)` where `missing` names config fields such as `"deck z.stage_surface"` or `"tiprack_300_96 tip.length"`. Raises nothing. May emit the `stage_ref:<effector id>` warning: `deck.z.stage_surface_reference` records that 114.75 was measured with the **p300** bare nosecone, so running a p125 borrows a number that was never measured for that pipette body, and the warning says so.

With the current config: bare limit **114.75**, tipped limit **114.75 - 52.25 = 62.5** (`tests/test_review_fixes.py:test_3c`). Because +Z is down, fitting a tip extends the reference point downward and the maximum commanded Z *shrinks* by the tip length - the number goes down as the danger goes up, which is the sign error this function exists to contain.

Returning `missing` alongside the limit is the second reason it exists. Measuring `z.stage_surface` alone would switch the check on for bare moves and leave it off for the deeper, more dangerous tipped ones, with nothing in the report saying which case was actually verified.

#### `Simulator.check(step)`

```python
check(step: Step) -> None
```

The single entry point: dispatches one step through every applicable check and updates the report.

- `step` - the step just generated.

Returns `None`. Raises `SimulationError` in strict mode via any check it dispatches to. In order, it:

1. Advances `report.steps` to `max(report.steps, step.i + 1)`.
2. **Post-ejection re-home check.** A `home` step clears the pending flag. Any `move` or `actuator` step while the flag is still set is a hard failure: *"a tip ejection was not followed by G28"*. Physical failure prevented: the strip force can skip steps on an open-loop machine, so after an ejection the commanded position no longer describes where the gantry is, and the next move is computed from a lie. `deck.json` states this as `stations.tip_eject.rehome_after: true` with `rehome_note` calling G28 mandatory. Note the error blames `self._eject_step`, not the step that tripped it, so the message points at the ejection that owed a G28 rather than at the innocent move after it.
3. Runs `_check_move` for `kind == "move"`, or `_check_actuator` for `kind == "actuator"`.
4. Arms the flag when it sees the ejection-complete marker (`op == "eject_tip"` and `detail["final"]`). `Machine.eject_tip` records that marker *after* the final lift to travel height, so the lift itself is not what trips the rule.
5. Appends `(*step.to, step.op)` to `report.path`, but only when every component of `to` is non-`None` - otherwise a `home("Z")` step would inject `None` coordinates into the plot data (`tests/test_review_fixes.py:test_12`).

#### `Simulator._check_move(step)` (private)

Runs the five geometric checks that apply to a motion step. Protocol authors never call it; `check()` does. In order:

- **Envelope.** `deck.in_envelope(x, y, z)` against X [0, 420], Y [0, 580], Z [0, 270] from `deck.json`. Any violation is a hard failure. Prevents commanding past the kinematic limits, where Klipper aborts the run mid-protocol - which, halfway through 20 tubes with a wet tip, is itself a lost experiment.
- **Off-stage depth warning.** If the target XY is outside `deck.stage_region()` and Z is deeper than `z_stage_surface` (114.75), a one-time `off_stage_depth` warning fires. This is not an error, because the stage surface only exists over the slot area: the derived region today is X [-2.75, 403.75], Y [131.875, 411.625], and the ejector lane at (155, 89) is 43 mm clear of the front edge in Y, which is exactly what makes its Z 145 reachable. The warning exists so that "we are below the stage floor" is never silent, even when it is legal.
- **Absolute crash depth.** On-stage only, via `effective_z_limit(step.tip)`. `z > limit` is a hard failure. Prevents driving the nosecone or the tip into the stage: 62.5 with a tip, 114.75 bare (`test_tipped_stage_crash_is_blocked`, `test_bare_stage_crash_is_blocked`). If the limit cannot be computed the `z_limit:tipped` / `z_limit:bare` warning states plainly that only the kinematic limit and the per-labware Z were checked.
- **Travel-height rule.** Any step with X or Y in `axes` whose `op` is not in `_LOW_XY_OK` must have both `frm[2]` and `to[2]` equal to `deck.z_travel` (5.0) within 1e-6, or it is a hard failure worded *"this is the crash mode that costs a tip"*. Prevents the ordinary case of dragging a fitted tip sideways at working depth across a tube rim or a rack. Travel height is verified rather than assumed: at Z 5.0 a fitted 300 uL tip clears the raised location edges by 48.0 mm and the stage floor by 57.5 mm.
- **Keep-out bands.** For a Y move, every band returned by `deck.keepouts_hit_by_y_move(step.frm[0])` is passed to `_check_keepout`. Using `frm[0]` is sound only because `Machine._move_xy` emits one axis per step, so X is constant across a Y step.
- **Traverse clearance** over loaded labware, via `_check_traverse_clearance`, subject to the same `_LOW_XY_OK` exemption.
- **Well bottom.** When the step names a `labware`, `z > labware.z("bottom")` is a hard failure (*"Z {z} is past the {labware} well bottom"*). Prevents a dispense driving the tip into the bottom of a 0.65 mL tube, which blocks the orifice and makes the dispensed mass meaningless. Every `z.bottom` in `labware.json` is `null` today, so what this actually produces is three `bottom:<labware id>` warnings - the check is off, and says so.

#### `Simulator._check_raised_edges(step)` (private)

The edges of each stage location are raised, and an XY move that crosses a location boundary passes over one. `deck.z_slot_edge_bare` derives the bare-nosecone reference as `slot_edge_with_p300_tip` 53.0 plus the tip length 52.25 = **105.25**, so the limit in force is 105.25 bare and 53.0 with a tip. The compared value is `max(frm[2], to[2])`, the deeper end of the move. Deeper than the limit is a hard failure. Physical failure prevented: the tip strikes an edge piece on the way over. The edges stand `62.5 - 53.0 = 9.5` mm above the stage floor.

Three early exits, each of which is load-bearing:

- No measurement (`z_slot_edge_bare is None`): a `slot_edge` warning naming `slot_edge_with_p300_tip` / `stage_surface_with_p300_tip`, then return.
- The swept box does not touch `deck.stage_region()`: return. This is what makes the ejector legal - it works at Y 80 to 89, clear of the front of the stage, at a depth no stage feature reaches.
- `slot_cell(frm)` equals `slot_cell(to)` and is not `None`: return, because motion staying inside one location never goes over an edge. This is precisely what keeps the wall wipe legal - it works at tube depth (Z 45.0 for the tube rack) but never leaves the cell the tube sits in.

For a tipped move with no known tip length, a `slot_edge_tip` warning fires and the check is skipped rather than run against the bare limit.

**Note:** `Deck.slot_cell` builds cells from the *mean* slot pitch (135.5 x 93.25) while the stored centres are irregular (X pitch 136/135, Y pitch 93.0/93.5, as `deck.json:slot_pitch_note` records). The half-pitch cells therefore do not tile exactly: they overlap by 0.5 mm in X, and in Y they leave a 0.25 mm seam between 225.125 and 225.375. A point in that seam is on-stage but has no cell, so `slot_cell` returns `None` and any move through it is treated as crossing a boundary. That direction is conservative, so it is safe - but it means an error message can say `off-stage -> 8` for a point that is physically on the stage.

#### `Simulator._check_keepout(step, k, z)` (private)

Applies one `KeepOut` band to one Y move. The only band configured today is `tip_ejector`, X [148.0, 162.0], `forbids: y_motion`, `safe_when_z_below: 60.0`, `whitelist: ["eject_tip"]`.

- Exempt and returns early only if **both** `step.detail["keepout_exempt"]` is set **and** `step.op` is in the band's whitelist. Two conditions, not one, because the flag alone would let any operation buy an exemption and the op name alone would hand it to every approach and retract move in `eject_tip`.
- If a tip is fitted, that is an unconditional hard failure regardless of Z. `safe_when_z_below: 60.0` was stated for the bare 300 uL effector; a fitted tip reaches 52.25 mm lower and the real limit was never measured, so the honest answer is refusal rather than an unmeasured comparison. This is the "cannot be evaluated is not a pass" rule applied to a *hard* check rather than a warning.
- Otherwise `k.z_is_safe(z)` requires `z < 60.0`, i.e. physically above the mechanism. `z_is_safe` returns `False` when `safe_when_z_below` is `None`, so a band with no measured safe height forbids Y motion outright.

Physical failure prevented: Y-axis motion through X 148-162 collides with the ejector mechanism. `deck.json` flags the 14 mm band width as still unverified at this location (it was carried over from a previously documented band centred on X 85), and that admission is in `deck.uncalibrated`, so it reaches the manifest on every run.

#### `Simulator._check_traverse_clearance(step)` (private)

Does the whole swept segment clear the rim of every loaded placement? Because `Machine._move_xy` emits one axis per step, the swept region is the axis-aligned box between `frm` and `to`, and a placement is implicated when that box overlaps its bounds. Overlap plus `z > top - 1e-6` (at or below the rim) is a hard failure.

Per placement it prefers `footprint_bounds()` - the physical outline - and falls back to `well_bounds()` (outermost well centres) with a `footprint:<name>` warning saying the check will not catch a path that clips the rim. A missing `z.top` produces a `ztop:<name>` warning and the placement is skipped.

It checks the swept box rather than the destination for two reasons, both of which were real bugs (`tests/test_review_fixes.py:test_4`): a traverse that starts over the rack and ends at the reservoir crosses the whole rack in between, and labware that is never a move endpoint would otherwise never be checked at all. With `z.top` unmeasured on all four labware definitions, this check is currently reporting three `cannot check` warnings and enforcing nothing - which is exactly the third layout open question ("Do any of the loaded racks stand HIGHER than the raised location edges?").

#### `Simulator._check_actuator(step)` (private)

For a `Pipette`, checks `step.pos_after` against the floor `pos_min + stroke_margin` (0.0 + 1.0 = **1.0** mm for the p300) and the ceiling `blowout_pos` (**30.0** mm). Either side is a hard failure. Returns immediately if `pos_after` is `None`.

Physical failure prevented: driving the plunger into its hard stop, or past blow-out, both of which mean the volume actually delivered is not the volume requested - and on the accuracy bench a silently clipped stroke looks like a calibration result. This duplicates `Pipette.check_pos`, which `Machine.actuator` already calls before emitting; the point of the copy is that the simulator sees the recorded state of every step, including any produced by a path that bypassed `Machine.actuator`.

**Note:** the check is inside `isinstance(eff, Pipette)`, so a `Gripper`'s actuator positions are not range-checked anywhere. That is consistent with `Gripper.require_ready()` refusing to generate motion at all while `open_pos` / `closed_pos` / `grip_pos` are null, but it is a gap to close when the gripper is built.

#### `Simulator.record_tip_pickup(step, row, col)`

```python
record_tip_pickup(step: Step, row: int, col: int) -> None
```

Marks a tip position as consumed.

- `step` - the pickup step. Currently unused by the body; kept for symmetry with `check_tip_exit` and for future error messages.
- `row`, `col` - 0-based grid indices in the tip rack.

Returns `None`, raises nothing. The resulting `consumed_tips` set drives three separate safety behaviours: the exit-path check below, `Machine.get_tip`'s refusal to press into an already-used hole, and `Machine._next_free_tip_col`'s inward-from-the-exit-edge consumption order.

#### `Simulator.check_tip_exit(step, row, col, crossed)`

```python
check_tip_exit(step: Step, row: int, col: int, crossed: Sequence[Tuple[int, int]]) -> None
```

After a pickup, the X exit path may cross only positions whose tips have already been consumed.

- `step` - the exit step (recorded by `Machine._exit_tip_box` with `kind="note"`).
- `row`, `col` - the position just picked, for the message.
- `crossed` - every `(row, col)` the X exit sweeps over, computed by the caller from the column X values and the chosen exit edge.

Raises `SimulationError` in strict mode if any crossed position is not in `consumed_tips`, with the remedy in the message: *"Consume columns starting at the exit edge and work inward."* Physical failure prevented: passing over an occupied tip position shears the freshly fitted tip straight off the nosecone. `tests/test_lab.py:test_tip_exit_path_over_occupied_positions` asks for column 5 on a full rack and expects the refusal.

This check is the reason the unresolved `consume_order.exit_edge: "unconfirmed"` in `labware.json` is survivable. `Machine._tip_exit` assumes the low-X edge and warns loudly; if that assumption is wrong, this check fails the run rather than letting 20 pickups exit the wrong way.

#### `Simulator.finish(held_fluid, tip_fitted)`

```python
finish(held_fluid: float, tip_fitted: bool) -> Report
```

Runs the checks that can only be evaluated once the whole run exists, then returns the completed report. Called by `Machine.simulate()`, which passes its own end state.

- `held_fluid` - uL still in the tip at the end of the run.
- `tip_fitted` - whether a tip is still on at the end.

Returns the `Report`. Raises nothing - even the pending-ejection failure is appended straight to `report.errors` rather than going through `fail()`, so `finish()` never raises in strict mode and `Machine.finalize()` is what refuses to write.

The four whole-run checks:

- Run ends on an ejection with no G28 after it: **error**. Same reasoning as the per-step version; a run that hands back an untrusted position is not finished.
- Run ends still holding fluid: **warning**, quoting the volume. Usually a protocol bug - a missing final dispense means one tube never got its aliquot and the balance sheet has a row that will never be filled.
- Run ends with a tip fitted and never ejects it, but only when `effector.can_eject_tip`: **warning**. The p300 accuracy run trips this deliberately, because the ISO protocol wants a fresh tip fitted by hand between the three repeats.
- `deck.slot_seating_report()` lines: **warnings**, prefixed `"slot seating: "`. Each says a slot-mounted placement's grid centre is more than half a slot pitch from the centre of the slot it claims, i.e. either the slot number or the slot frame is wrong. Empty on the current layout.
- `deck.overlap_report()` lines: lines starting `OVERLAP` become **errors** (two placements physically intersect, so one of them is not where the layout says), everything else becomes a **warning**. Today all three pairs come back as `cannot check ...: no footprint measured`, because no footprint is measured for the tube rack, the tip rack or the reservoir.

`finish()` is idempotent, guarded by `self._finished`. Without that, a caller that inspects the report and then calls `finalize()` would get every whole-run line two or three times, and the manifest would record the pile-up (`tests/test_review_fixes.py:test_7_simulate_is_idempotent`).

### `plot_path(report, out_path, title="toolpath")`

```python
plot_path(report: Report, out_path: str, title: str = "toolpath") -> Optional[str]
```

Renders the run as a PNG: an equal-aspect XY scatter coloured by Z over a grey path line, above a Z-versus-step trace.

- `report` - a `Report` with a populated `path`.
- `out_path` - where to write the PNG. Written at dpi 130 with the Agg backend.
- `title` - figure title; `Machine.finalize` passes the protocol name.

Returns `out_path` on success, or `None` if matplotlib is not installed or `report.path` is empty. Raises nothing for the missing-dependency case - matplotlib is deliberately optional, and `finalize()` simply reports `"plot": None`.

Two details exist to keep the sign convention readable: the XY subplot title says *"colour = Z (larger Z is LOWER)"*, and the Z-versus-step axis is inverted (`ax2.invert_yaxis()`) so that lower on the plot means lower in space. Reading a toolpath plot with the axis the natural way round is how a maintainer talks themselves into a crash.

Be aware that `report.path` contains a point for *every* step with known coordinates, including `home` steps and zero-length `note` markers (the ejection-complete marker records `frm == to`), so the trace includes stationary points and jumps to the home position.

## `lab/logbook.py`

The run manifest: a JSON sidecar plus a balance-entry CSV, written next to every generated `.gcode`. The purpose is gravimetric traceability. `mass_dry_mg`, `mass_wet_mg` and `temp_C` are written out **empty** so the operator fills them in at the balance and the readings join back to the generated positions on tube identity, instead of being transcribed by hand into a separate spreadsheet. Everything needed to reproduce the run goes in the JSON: the git commit of the generating code, the SHA-256 of every config file consumed, the calibration coefficients actually used, and every field the config admits was never calibrated.

### `build_manifest(machine, report)`

```python
build_manifest(machine: "Machine", report: Report) -> Dict[str, Any]
```

Assembles the manifest as a plain JSON-serialisable dict. Pure: it reads `machine` and `report` and mutates nothing.

- `machine` - the `Machine` after the protocol has run. Typed as a string to avoid a circular import; `Machine` imports `write_manifest` lazily inside `finalize()` for the same reason.
- `report` - the `Report` from `Simulator.finish()`.

Returns the dict. Raises whatever the underlying accessors raise; in practice nothing, since `Pipette.__init__` has already required `spec["calibration"]` and validated `model == "linear_mm_per_uL"` before this point.

Top-level keys, and why each is there:

- `schema_version` (1) and `generated_utc` - ISO-8601 UTC to the second.
- `protocol` - `machine.name`, which is also the filename stem.
- `effector` - `id`, `kind`, `display_name`, `nominal_uL`.
- `calibration` - for a `Pipette` only, otherwise `{}`. Carries `equation` (from `Pipette.equation`, **derived** from `slope` and `intercept` rather than copied from the config's free-text `equation` string, so the reported curve cannot drift from the coefficients that produced the millimetres), `slope_mm_per_uL`, `intercept_mm`, `window_uL`, the gravimetric context (`method`, `temperature_C`, `water_density_g_per_mL`, `z_factor_uL_per_mg`, `pressure_kPa`) that any mass-to-volume conversion at the balance needs, and the plunger constants (`blowout_pos`, `air_gap`, `gap_pos`, `post_asp_backoff`, `stroke_margin`, `max_volume_uL`). For the p300 that is `d_mm = 0.0533 * V_uL + 0.122` over an 8.0 to 320.0 uL window.
- `provenance` - `config_dir`, `git_commit`, `git_dirty`, and `config_sha256` for `deck.json`, `labware.json`, the layout file by its own name, and `effectors/<id>.json`. This is the block that answers "which numbers produced this mass?" six months later. `git_dirty: true` in the committed example manifest is the block doing its job.
- `deck` - `id`, `z_travel` (5.0), `z_home` (0.0), `z_stage_surface` (114.75), `feedrates`.
- `layout` - the layout `id` plus, per placement, the fully resolved geometry: `origin`, `col_vec`, `row_vec`, `rows`, `cols`, `mount`, `slot`, `station`, `rotation`. Recording the solved vectors rather than the input corners means the manifest states where the wells actually were, not what the config was asked to solve.
- `open_questions` - straight from `layout.json`. These are also warned about on every run by `Machine.__init__`.
- `near_slots` - placements carrying a `near_slot` human reference rather than a seating claim (`{"water": 9}` today).
- `slot_seating` - `deck.slot_seating_report()`.
- `toggles`, `notes` - whatever the protocol recorded, so the experimental design (replicates, fractions, volumes, whether prewet and wipe ran) is on the record with the numbers.
- `uncalibrated` - `machine.uncalibrated()`, which is `deck.uncalibrated() + effector.uncalibrated()`. The example run lists nine entries, including `tubes(tube_rack_0p65_5x10).z.work` and `deck.stations.tip_eject (routine never validated on hardware)`. This is the manifest half of "a check that cannot be evaluated is not a pass": every unmeasured number travels with the data it influenced, so a suspicious result can be checked against what was never actually known.
- `simulation` - `steps`, `errors`, `warnings` verbatim. The `cannot check` warnings end up here, attached permanently to the run they applied to.
- `gcode_lines`, `dispenses` - line count and the full dispense records, the same rows the CSV gets.

**Note:** `Machine.finalize()` calls `write_manifest` only after `report.ok`, so `simulation.errors` in a manifest on disk is always empty. A non-empty `errors` list can only appear if something calls `build_manifest`/`write_manifest` directly, bypassing that gate.

### `_CSV_COLUMNS` (module constant)

The 18 balance-sheet columns, in order: `order_index`, `placement`, `labware`, `slot`, `well`, `row`, `col`, `x`, `y`, `z`, `target_volume_uL`, `displacement_mm`, `aspirate_pos`, `backoff_pos`, `mass_dry_mg`, `mass_wet_mg`, `temp_C`.

The first fourteen are generated; the last three are blank for hand entry. `displacement_mm`, `aspirate_pos` and `backoff_pos` come from the aspiration that fed that specific dispense, so a suspect mass can be traced to the exact plunger stroke that produced it rather than to a nominal volume.

**Note:** the writer uses `extrasaction="ignore"`, so a field added to `Machine.dispense`'s record but not to this list is silently dropped from the CSV. It still appears under `dispenses` in the JSON, which makes the omission easy to miss. Add to both.

### `write_manifest(machine, report, outdir)`

```python
write_manifest(machine: "Machine", report: Report, outdir: Path) -> Path
```

Writes `<name>.manifest.json` and, when there is at least one dispense, `<name>.balance.csv` beside it.

- `machine`, `report` - as for `build_manifest`.
- `outdir` - directory to write into. Coerced with `Path()` and created with `mkdir(parents=True, exist_ok=True)`, so a caller may pass a string.

Returns the `Path` to the JSON file. Raises whatever the filesystem raises. Both files are written UTF-8; the CSV is opened with `newline=""` as `csv` requires, and the JSON is indented 2.

The two files are named from `machine.name` so the whole run is one stem: `p300_accuracy.gcode`, `p300_accuracy.manifest.json`, `p300_accuracy.balance.csv`, `p300_accuracy.toolpath.png`. A run with no dispenses (a tip-handling or ejector dry run, say) gets a JSON and no CSV.

**Note:** the CSV path is not returned and is not in `Machine.finalize()`'s result dict either, so a caller that wants it must reconstruct `outdir / f"{machine.name}.balance.csv"`.

---


---

## Added after the bulk of this reference was written

These landed after the sections above were drafted, so they are collected here.

### `Labware.height_mm`

Physical height above the stage, contents included when known
(`height_with_contents_mm` wins over `height_mm`). Stored as a height rather
than a commanded Z because a Z is wrong the moment a tip goes on: the 35 mm
0.65 mL holder tops out at commanded Z 79.75 bare and 27.5 with a 300 uL tip.
Returns `None` when unmeasured.

### `Labware.height_is_empty_only`

True when only the empty height is known, so tubes and caps are unaccounted
for. Both tube holders return True today, and a run that traverses over one
warns about it. The number that matters is **57.5 mm** - stage 114.75 minus tip
52.25 minus travel 5.0 - the tallest anything can be and still clear travel
height with a tip. That leaves 22.5 mm of headroom on the 0.65 mL holder and
12.5 mm on the 1.5 mL one.

### `Labware.approach_order`

`"YX"`, `"XY"` or `None`. A labware asking for `"YX"` wants the effector to
reach its Y position while still clear in X, then come in along X. Both tube
holders ask for it: the caps make Y motion across a holder difficult, while X
motion passes between cap rows. The keep-out router still overrides it when
geometry demands and warns when it does, because a band is a collision and the
order is an ease-of-motion choice.

### `Machine.goto_placement(placement, row, col, op="goto")`

Traverse to a well, honouring that labware's `approach_order`. Returns the
`(x, y)` it went to. `dispense` and `get_tip` already do this internally; use
this when writing a protocol that moves somewhere without dispensing.

### `Machine.goto(..., prefer=None)`

`prefer` is `"XY"` or `"YX"`; anything else raises `ProtocolError`. Omit it and
the default is X-first, which is what the tip box requires.

### `Simulator.labware_top_z(placement, tip_fitted)`

Returns `(commanded_z, reason_if_unknown)` for the top of a placement's
labware, converted from its physical height for the current tip state. The
reason string is what the `cannot check` warning quotes.

### `Simulator.max_labware_height()`

`z_stage_surface - tip_length - z_travel`, i.e. 57.5 mm today. Anything taller
is struck at travel height with a tip fitted.

### `a1_from_slot_edge` (placement model, `lab/labware.py`)

For slot-mounted labware dimensioned from the *location edges* rather than from
a measured machine coordinate. Names which edge each inset is taken from:

```json
"a1_from_slot_edge": {
  "x_edge": "high", "x_inset": 14.0,
  "y_edge": "high", "y_inset": 13.0
}
```

The location cell is the slot centre expanded by half the slot pitch, so
135.5 x 93.25 mm here. `Deck.__init__` passes the pitch in; a placement using
this model outside a slot raises `ValueError`. The 1.5 mL tube holder uses it:
13 mm down from the top of the location, 14 mm in from the left, which resolves
A1 in slot 5 to (253.75, 305.625).

### `Deck.stage_region()`, `Deck.point_on_stage(x, y)`, `Deck.slot_cell(x, y)`

The stage surface only exists over the slot area. `stage_region()` derives it
from the slot centres expanded by half the slot pitch - X[-2.75, 403.75],
Y[131.88, 411.62] - so it cannot drift out of step with the slot table.
`slot_cell` returns which location a point is in, or `None` off-stage, and that
is what distinguishes a traverse crossing a location boundary (over a raised
edge) from motion staying inside one location (the wall wipe, inside a tube).

### `Deck.z_slot_edge_bare`

Bare-nosecone Z at which the effector would strike a raised location edge:
105.25, derived as the measured with-tip limit 53.0 plus the tip length 52.25.
One measurement, both cases.

### `Deck.slot_seating_report()`, `Deck.near_slots()`

`slot_seating_report()` returns a line per slot-mounted placement whose grid
centre sits more than half a slot pitch from its slot centre - meaning either
the slot number or the slot frame is wrong. `near_slots()` returns placements
carrying a `near_slot` human reference rather than a seating claim.

### `Deck.macro(name)`

A Klipper macro prefix, and it raises `DeckError` rather than defaulting.
Falling back to a hardcoded literal would mean a mistyped `klipper_macros` entry
silently emits the built-in string, and the gcode would look right while
addressing nothing.
