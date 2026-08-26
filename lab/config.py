"""Config discovery, loading and hashing.

Config paths resolve against this file, not the working directory, so a
protocol runs correctly from anywhere. Override with the LAB_CONFIG env var.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_CONFIG_DIR = _REPO_ROOT / "config"


def config_dir() -> Path:
    """Directory holding deck.json / labware.json / layout.json / effectors/."""
    env = os.environ.get("LAB_CONFIG")
    if env:
        p = Path(env).expanduser().resolve()
        if not p.is_dir():
            raise FileNotFoundError(
                f"LAB_CONFIG points at {p!s}, which is not a directory"
            )
        return p
    return _DEFAULT_CONFIG_DIR


def repo_root() -> Path:
    return _REPO_ROOT


def config_path(*parts: str) -> Path:
    return config_dir().joinpath(*parts)


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ConfigFile:
    """A loaded JSON config plus the hash of the bytes it came from.

    The hash goes into the run manifest so a generated .gcode can always be
    traced back to the exact config that produced it.
    """

    __slots__ = ("path", "data", "sha256")

    def __init__(self, path: Path) -> None:
        if not path.is_file():
            raise FileNotFoundError(
                f"config file not found: {path!s}\n"
                f"  config dir is {config_dir()!s}\n"
                f"  set LAB_CONFIG to point somewhere else"
            )
        self.path = path
        raw = path.read_bytes()
        self.sha256 = hashlib.sha256(raw).hexdigest()
        try:
            self.data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path!s} is not valid JSON: {exc}") from exc

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def require(self, key: str) -> Any:
        if key not in self.data:
            raise KeyError(f"{self.path.name} is missing required key {key!r}")
        return self.data[key]

    def __repr__(self) -> str:
        return f"<ConfigFile {self.path.name} sha={self.sha256[:8]}>"


def load(*parts: str) -> ConfigFile:
    return ConfigFile(config_path(*parts))


def load_deck() -> ConfigFile:
    return load("deck.json")


def load_labware() -> ConfigFile:
    return load("labware.json")


def load_layout(name: str = "layout.json") -> ConfigFile:
    if not name.endswith(".json"):
        name += ".json"
    return load(name)


def load_effector_spec(effector_id: str) -> ConfigFile:
    return load("effectors", f"{effector_id}.json")


def available_effectors() -> Dict[str, Path]:
    """Every effector json on disk, keyed by id. One file per end effector."""
    d = config_dir() / "effectors"
    if not d.is_dir():
        return {}
    return {p.stem: p for p in sorted(d.glob("*.json"))}
