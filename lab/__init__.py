"""Self-driving lab: deck, effectors, motion primitives, simulation, logging.

Importing this package makes ``gcodepy`` importable from the in-repo ``src/``
tree if it is not already on the path. Without it the library only works from
the one interpreter that happens to have the editable install (here: 3.12);
with it, any interpreter that can run the code can run a protocol.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"

try:  # already installed (editable or otherwise)?
    import gcodepy  # noqa: F401
except ImportError:  # pragma: no cover - depends on the environment
    if _SRC.is_dir() and str(_SRC) not in sys.path:
        sys.path.insert(0, str(_SRC))

__all__ = [
    "config",
    "deck",
    "effector",
    "labware",
    "logbook",
    "machine",
    "simulate",
]
