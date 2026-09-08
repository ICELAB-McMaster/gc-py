"""Quick geometry check: resolve every placement and print the coordinates.

Run from anywhere:  python tests/smoke_geometry.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lab.deck import Deck
from lab import effector


def main() -> None:
    deck = Deck.load()
    print(deck.summary())
    print()

    # Walk whatever the layout actually holds. Naming placements here made this
    # crash the moment a rack came off the deck, which is exactly when you most
    # want to run it.
    for name, p in sorted(deck.placements.items()):
        if p.labware.rows * p.labware.cols == 1:
            continue
        print(f"{name}  ({p.labware.display_name})")
        print(f"  col_vec    {tuple(round(v, 4) for v in p.col_vec)}")
        print(f"  row_vec    {tuple(round(v, 4) for v in p.row_vec)}")
        print(f"  A1         {p.xy(0, 0)}")
        print(f"  col X      {[round(v, 4) for v in p.col_x_values()]}")
        print(f"  row Y      {[round(v, 4) for v in p.row_y_values()]}")
        print(f"  centre     {p.grid_center()}")
        print(f"  height     {p.labware.height_mm} mm"
              f"   approach {p.labware.approach_order or 'XY (default)'}")
        print()

    print("keep-out interaction with every loaded grid")
    for k in deck.keepouts:
        print(f"  {k.id} X{k.x_range}, forbids {k.forbids} unless Z < "
              f"{k.safe_when_z_below}")
        for name, p in sorted(deck.placements.items()):
            hits = [
                (c, p.xy(0, c)[0])
                for c in range(p.labware.cols)
                if k.contains_x(p.xy(0, c)[0])
            ]
            if hits:
                print(f"    {name}: columns inside the band -> {hits}")
    print()

    print("effectors")
    for eid in effector.available():
        e = effector.load(eid, allow_placeholder=True)
        tag = " [PLACEHOLDER]" if e.is_placeholder else ""
        print(f"  {eid:<8} kind={e.kind:<8} {e.display_name}{tag}")
        if isinstance(e, effector.Pipette):
            print(f"           d = {e.slope}*V + {e.intercept}   "
                  f"window {e.window} uL   max {e.max_volume_uL} uL")
            for row in e.table(e.fractions([0.10, 0.50, 0.75, 1.00])):
                print(f"           {row['volume_uL']:>8.2f} uL  "
                      f"d={row['displacement_mm']:>7.3f}  "
                      f"asp={row['aspirate_pos']:>7.3f}  "
                      f"backoff={row['backoff_pos']:>7.3f}")
    print()

    print("uncalibrated")
    for u in deck.uncalibrated():
        print(f"  {u}")
    print()
    print("overlap report")
    for line in deck.overlap_report():
        print(f"  {line}")


if __name__ == "__main__":
    main()
