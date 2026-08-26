"""Run manifest: a JSON sidecar written next to every generated .gcode.

The point is gravimetric traceability. Balance readings should join to this
file on tube ID instead of being transcribed by hand, so `mass_dry_mg`,
`mass_wet_mg` and `temp_C` are written out empty and filled in at the balance.

Everything needed to reproduce the run is recorded: the git commit of the
generating code, the SHA-256 of every config file consumed, the calibration
coefficients actually used, and every field the config admits is uncalibrated.
"""

from __future__ import annotations

import csv
import datetime as _dt
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict

from .effector import Pipette
from .simulate import Report

if TYPE_CHECKING:  # pragma: no cover
    from .machine import Machine


def build_manifest(machine: "Machine", report: Report) -> Dict[str, Any]:
    eff = machine.effector
    deck = machine.deck

    cal: Dict[str, Any] = {}
    if isinstance(eff, Pipette):
        cal = {
            "equation": eff.equation,
            "slope_mm_per_uL": eff.slope,
            "intercept_mm": eff.intercept,
            "window_uL": list(eff.window),
            "method": eff.spec["calibration"].get("method"),
            "temperature_C": eff.spec["calibration"].get("temperature_C"),
            "water_density_g_per_mL": eff.spec["calibration"].get(
                "water_density_g_per_mL"
            ),
            "z_factor_uL_per_mg": eff.spec["calibration"].get("z_factor_uL_per_mg"),
            "pressure_kPa": eff.spec["calibration"].get("pressure_kPa"),
            "blowout_pos": eff.blowout_pos,
            "air_gap": eff.air_gap,
            "gap_pos": eff.gap_pos,
            "post_asp_backoff": eff.post_asp_backoff,
            "stroke_margin": eff.stroke_margin,
            "max_volume_uL": eff.max_volume_uL,
        }

    return {
        "schema_version": 1,
        "generated_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(
            timespec="seconds"
        ),
        "protocol": machine.name,
        "effector": {
            "id": eff.id,
            "kind": eff.kind,
            "display_name": eff.display_name,
            "nominal_uL": eff.spec.get("nominal_uL"),
        },
        "calibration": cal,
        "provenance": {
            "config_dir": str(machine.deck.deck_cfg.path.parent),
            "git_commit": machine.git_commit(),
            "git_dirty": machine.git_dirty(),
            "config_sha256": machine.config_hashes(),
        },
        "deck": {
            "id": deck.id,
            "z_travel": deck.z_travel,
            "z_home": deck.z_home,
            "z_stage_surface": deck.z_stage_surface,
            "feedrates": deck.feedrates,
        },
        "layout": {
            "id": deck.layout_cfg.get("id"),
            "placements": [
                {
                    "name": p.name,
                    "labware": p.labware.id,
                    "mount": p.mount,
                    "slot": p.slot,
                    "station": p.station,
                    "rotation": p.rotation,
                    "origin": list(p.origin),
                    "col_vec": list(p.col_vec),
                    "row_vec": list(p.row_vec),
                    "rows": p.labware.rows,
                    "cols": p.labware.cols,
                }
                for p in deck.placements.values()
            ],
        },
        "open_questions": deck.open_questions,
        "near_slots": deck.near_slots(),
        "slot_seating": deck.slot_seating_report(),
        "toggles": machine.toggles,
        "notes": machine.notes,
        "uncalibrated": machine.uncalibrated(),
        "simulation": {
            "steps": report.steps,
            "errors": report.errors,
            "warnings": report.warnings,
        },
        "gcode_lines": machine.g.line_count(),
        "dispenses": machine.dispenses,
    }


_CSV_COLUMNS = [
    "order_index", "placement", "labware", "slot", "well", "row", "col",
    "x", "y", "z", "target_volume_uL", "displacement_mm", "aspirate_pos",
    "backoff_pos", "mass_dry_mg", "mass_wet_mg", "temp_C",
]


def write_manifest(machine: "Machine", report: Report, outdir: Path) -> Path:
    """Write <name>.manifest.json and the balance-entry CSV beside it."""
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    manifest = build_manifest(machine, report)
    jpath = outdir / f"{machine.name}.manifest.json"
    jpath.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    if machine.dispenses:
        cpath = outdir / f"{machine.name}.balance.csv"
        with cpath.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=_CSV_COLUMNS, extrasaction="ignore")
            w.writeheader()
            for row in machine.dispenses:
                w.writerow(row)

    return jpath
