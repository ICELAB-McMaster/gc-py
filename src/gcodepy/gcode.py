from typing import Callable, Optional, Tuple, Union
import io
import math
import os
from . import util


def fmt(v: float, decimals: int = 4) -> str:
    """Format a coordinate for emission.

    Plain str() of a float leaks up to 17 significant digits, which is how
    ``G0 F1500 X125.99999999999999`` ended up in a generated file. Fixed
    decimals with the trailing zeros stripped keeps lines short and readable
    without inventing precision the machine does not have.
    """
    if v is None:
        raise ValueError("cannot format a None coordinate")
    s = f"{float(v):.{decimals}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


# TODO:
# minimize feedrate output
# draw absolute
# travel func
# catch more invalid inputs
class Gcode:
    """Gcode writer.

    Pass a filename to write straight to disk, exactly as before. Pass
    ``filename=None`` to buffer everything in memory instead: nothing touches
    the filesystem until :meth:`dump` is called. That is what lets a caller
    validate a whole program and refuse to write it if a check fails.
    """

    def __init__(
        self,
        filename: Optional[str] = None,
        layer_height: float = 0.2,
        line_width: float = 0.4,
        filament_width: float = 1.75,
        home_position: Tuple[float, float, float] = (0, 0, 0),
        extrusion_length_calculator: Callable[
            [float, float, float, float], float
        ] = util.calculate_extrusion_length,
    ) -> None:
        self.filename = filename
        self.buffered = filename is None
        self.file = io.StringIO() if self.buffered else open(filename, "w")
        self.layer_height = layer_height
        self.line_width = line_width
        self.filament_width = filament_width
        self.home_pos = home_position
        self.extrusion_length_calculator = extrusion_length_calculator
        self.pos = [None] * 3
        self.e = None

    # ---- output ----------------------------------------------------------

    def raw(self, line: str):
        """Emit one verbatim line. Use this instead of ``.file.write``.

        Going through a method rather than the file handle keeps every emitted
        line countable and interceptable, which raw handle writes are not.
        """
        if not line.endswith("\n"):
            line += "\n"
        self.file.write(line)

    def getvalue(self) -> str:
        """The gcode written so far. Buffered instances only."""
        if not self.buffered:
            raise RuntimeError(
                "getvalue() needs a buffered Gcode - construct it with "
                "filename=None"
            )
        return self.file.getvalue()

    def line_count(self) -> int:
        return self.getvalue().count("\n")

    def dump(self, path: str) -> str:
        """Write the buffer to disk. Buffered instances only."""
        text = self.getvalue()
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        # Default text mode, matching what the unbuffered path has always done:
        # on Windows that means CRLF, which is what every existing .gcode in
        # this repo uses. Klipper accepts either, but consistency keeps diffs
        # against the previously generated files readable.
        with open(path, "w") as fh:
            fh.write(text)
        self.filename = path
        return path

    def close(self):
        if self.buffered:
            return  # keep the buffer readable; dump() is what persists it
        self.file.close()

    # ---- position guards -------------------------------------------------

    def _require_position(self, what: str):
        if any(p is None for p in self.pos):
            raise RuntimeError(
                f"{what} needs a known position, but the machine has not been "
                f"homed yet (pos={self.pos}). Call home() first."
            )

    def home(self, Axis: str ="ALL"):
        axis = Axis.upper()
    
        if axis == "ALL":
            selected = ("X", "Y", "Z")
            self.file.write("G28\n")
        else:
            selected = tuple(axis)
            if not selected or any(a not in "XYZ" for a in selected):
                raise ValueError(f"Invalid axis: {axis!r}")
            self.file.write(f"G28 {' '.join(selected)}\n")
        
        axis_index = {"X": 0, "Y": 1, "Z": 2}
        for a in selected:
            i = axis_index[a]
            self.pos[i] = self.home_pos[i]

    def get_x(self) -> float:
        return self.pos[0]

    def get_y(self) -> float:
        return self.pos[1]

    def get_z(self) -> float:
        return self.pos[2]

    def get_e(self) -> float:
        return self.e

    def set_layer_height(self, layer_height: float):
        self.layer_height = layer_height

    def set_line_width(self, line_width: float):
        self.line_width = line_width

    def set_filament_width(self, filament_width: float):
        self.filament_width = filament_width

    def zero_extruder(self):
        self.file.write("G92 E0\n")
        self.e = 0

    def set_tool_temp(self, temperature: int, tool_index: int = 0):
        out = f"M104 S{temperature}"
        if tool_index != 0:
            out += f" T{tool_index}"
        self.file.write(out + "\n")

    def wait_finish(self):
        self.file.write("M400 \n")

    def dwell(self, seconds: float = 0, milliseconds: float = 0):
        total_ms = round(seconds * 1000 + milliseconds)
        if total_ms <= 0:
            raise ValueError("Dwell duration must be positive")
        self.file.write(f"G4 P{total_ms}\n")
    
    def wait_tool_temp(
        self, temperature: int, tool_index: int = 0, on_cool: bool = False
    ):
        out = "M109"
        if on_cool:
            out += f" R{temperature}"
        else:
            out += f" S{temperature}"
        if tool_index != 0:
            out += f" T{tool_index}"
        self.file.write(out + "\n")

    def set_bed_temp(self, temperature: int):
        out = f"M140 S{temperature}"
        self.file.write(out + "\n")

    def wait_bed_temp(self, temperature: int, on_cool: bool = False):
        out = "M190"
        if on_cool:
            out += f" R{temperature}"
        else:
            out += f" S{temperature}"
        self.file.write(out + "\n")

    def travel(self, delta: Tuple[float, float, float], feedrate: int = 2400):
        self._require_position("travel()")
        # Round after accumulating: travel() is relative but emits the absolute
        # result, so without this the drift is physically followed, not cosmetic.
        self.pos[0] = round(self.pos[0] + delta[0], 6)
        self.pos[1] = round(self.pos[1] + delta[1], 6)
        self.pos[2] = round(self.pos[2] + delta[2], 6)
        out = f"G0 F{feedrate}"
        moved = False
        if delta[0] != 0.0:
            out += f" X{fmt(self.pos[0])}"
            moved = True
        if delta[1] != 0.0:
            out += f" Y{fmt(self.pos[1])}"
            moved = True
        if delta[2] != 0.0:
            out += f" Z{fmt(self.pos[2])}"
            moved = True
        if not moved:
            return  # an all-zero delta would emit a bare G0 with no axis word
        self.file.write(out + "\n")

    def travel_arc(
        self,
        i: float,
        j: float,
        delta: Tuple[float, float, float] = (0.0, 0.0, 0.0),
        clockwise: bool = True,
        feedrate: int = 2400,
    ):
        # draw_arc's signature is (i, j, delta, e, clockwise, feedrate). Passing
        # e first shifted every argument by one and made this raise on any input.
        self.draw_arc(i, j, delta, 0.0, clockwise, feedrate)

    def travel_arc_r(
        self,
        r: float,
        delta: Tuple[float, float, float],
        clockwise: bool = True,
        feedrate: int = 2400,
    ):
        # Same argument-order slip as travel_arc: draw_arc_r is
        # (r, delta, e, clockwise, feedrate), so e must come third, not first.
        self.draw_arc_r(r, delta, 0.0, clockwise, feedrate)

    def travel_absolute(
        self, location: Tuple[float, float, float], feedrate: int = 2400
    ):
        if any(v is None for v in location):
            raise ValueError(
                f"travel_absolute got a None coordinate: {location}. This usually "
                f"means an axis was never homed, and it would otherwise emit a "
                f"literal 'XNone' into the gcode."
            )
        self.pos[0] = location[0]
        self.pos[1] = location[1]
        self.pos[2] = location[2]
        self.file.write(
            f"G0 F{feedrate} X{fmt(self.pos[0])} Y{fmt(self.pos[1])} "
            f"Z{fmt(self.pos[2])}\n"
        )

    # ---- absolute motion helpers ----------------------------------------
    #
    # travel() is relative and omits any axis whose delta is zero;
    # travel_absolute() always writes all three. These two wrap that
    # asymmetry so a caller can think purely in absolute coordinates and
    # still get a single-axis move out.

    def move_z_absolute(self, z: float, feedrate: int = 2400, tol: float = 1e-6):
        """Go to an absolute Z, emitting nothing if already there."""
        self._require_position("move_z_absolute()")
        dz = round(z - self.pos[2], 6)
        if abs(dz) > tol:
            self.travel((0, 0, dz), feedrate=feedrate)

    def move_xy_absolute(
        self,
        x: float,
        y: float,
        feedrate: int = 2400,
        order: str = "XY",
        tol: float = 1e-6,
    ):
        """Go to an absolute XY one axis at a time, never diagonally.

        ``order`` is "XY" (X first) or "YX". X-first is what the tip rack
        requires, so callers that care should not vary it.
        """
        self._require_position("move_xy_absolute()")
        order = order.upper()
        if order not in ("XY", "YX"):
            raise ValueError(f"order must be 'XY' or 'YX', got {order!r}")

        def do_x():
            if abs(x - self.pos[0]) > tol:
                self.travel_absolute((x, self.pos[1], self.pos[2]), feedrate=feedrate)

        def do_y():
            if abs(y - self.pos[1]) > tol:
                self.travel_absolute((self.pos[0], y, self.pos[2]), feedrate=feedrate)

        if order == "XY":
            do_x()
            do_y()
        else:
            do_y()
            do_x()

    def draw(
        self, delta: Tuple[float, float, float], e: float = None, feedrate: int = 2400
    ):
        if e is None:
            e = self.extrusion_length_calculator(
                util.dist(delta),
                self.line_width,
                self.layer_height,
                self.filament_width,
            )
        self.pos[0] += delta[0]
        self.pos[1] += delta[1]
        self.pos[2] += delta[2]
        self.e += e
        out = f"G1 F{feedrate}"
        if delta[0] != 0.0:
            out += f" X{self.pos[0]}"
        if delta[1] != 0.0:
            out += f" Y{self.pos[1]}"
        if delta[2] != 0.0:
            out += f" Z{self.pos[2]}"
        out += f" E{e}"
        self.file.write(out + "\n")

    def draw_arc(
        self,
        i: float,
        j: float,
        delta: Tuple[float, float, float] = (0.0, 0.0, 0.0),
        e: float = None,
        clockwise: bool = True,
        feedrate: int = 2400,
    ):
        if e is None:
            e = self.extrusion_length_calculator(
                util.arclen((i, j), delta[:2], clockwise),
                self.line_width,
                self.layer_height,
                self.filament_width,
            )
        out = "G2" if clockwise else "G3"
        out += f" F{feedrate}"
        self.pos[0] += delta[0]
        self.pos[1] += delta[1]
        self.pos[2] += delta[2]
        self.e += e
        out += f" I{i} J{j} E{self.e}"
        if delta[0] != 0.0:
            out += f" X{self.pos[0]}"
        if delta[1] != 0.0:
            out += f" Y{self.pos[1]}"
        if delta[2] != 0.0:
            out += f" Z{self.pos[2]}"
        self.file.write(out + "\n")

    def draw_arc_r(
        self,
        r: float,
        delta: Tuple[float, float, float],
        e: float = None,
        clockwise: bool = True,
        feedrate: int = 2400,
    ):
        if r == 0 or r is None:
            raise ValueError("Radius r cannot be 0 or None.")
        if e is None:
            d2 = (delta[0] / 2, delta[1] / 2)
            m = -1 if clockwise ^ (r < 0) else 1
            l = util.dist(d2)
            h2 = (r - l) * (r + l)
            h = 0 if h2 < 0 else math.sqrt(h2)
            s = (-d2[1], d2[0])
            s = [s[i] / l * m * h for i in range(2)]
            center = tuple(d2[i] + s[i] for i in range(2))
            e = self.extrusion_length_calculator(
                util.arclen(center, delta[:2], clockwise),
                self.line_width,
                self.layer_height,
                self.filament_width,
            )
        if delta[0] == 0.0 and delta[1] == 0.0:
            raise ValueError("Both x and y cannot be 0. Make sure to set at least one!")
        out = "G2" if clockwise else "G3"
        out += f" F{feedrate}"
        self.pos[0] += delta[0]
        self.pos[1] += delta[1]
        self.pos[2] += delta[2]
        self.e += e
        out += f" R{r} E{self.e}"
        if delta[0] != 0.0:
            out += f" X{self.pos[0]}"
        if delta[1] != 0.0:
            out += f" Y{self.pos[1]}"
        if delta[2] != 0.0:
            out += f" Z{self.pos[2]}"
        self.file.write(out + "\n")

    def draw_func(
        self,
        func: Callable[
            [float],
            Tuple[Tuple[float, float, float], Union[float, None], Union[int, None]],
        ],
        start: float = 0.0,
        end: float = 1.0,
        nsamples: int = 1000,
    ):
        scale = (end - start) / nsamples
        start_pos = (self.get_x(), self.get_y(), self.get_z())
        last_pos = start_pos
        for i in range(0, nsamples):
            t = start + i * scale
            start_delta, e, feed = func(t)
            delta = [start_delta[i] + start_pos[i] - last_pos[i] for i in range(3)]
            if feed is not None:
                self.draw(delta, e=e, feedrate=feed)
            else:
                self.draw(delta, e=e)
            last_pos = (self.get_x(), self.get_y(), self.get_z())
