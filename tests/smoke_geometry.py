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

    tubes = deck.placement("tubes")
    print("0.65 mL tube holder")
    print(f"  pitch      X {tubes.col_vec}  Y {tubes.row_vec}")
    print(f"  anchor A1  {tubes.xy(0, 0)}")
    print(f"  col X      {tubes.col_x_values()}")
    print(f"  row Y      {tubes.row_y_values()}")
    print(f"  centre     {tubes.grid_center()}")
    print()

    tips = deck.placement("tips")
    print("300 uL tiprack (three-corner solve)")
    print(f"  col_vec    {tips.col_vec}")
    print(f"  row_vec    {tips.row_vec}")
    print(f"  col X      {[round(v, 4) for v in tips.col_x_values()]}")
    print(f"  row Y      {[round(v, 4) for v in tips.row_y_values()]}")
    print(f"  centre     {tips.grid_center()}")
    print()

    # The old accuracy protocol used rows 0-3, cols 0-4 of the tube rack.
    print("accuracy-test tube block (rows 0-3, cols 0-4)")
    for r in range(4):
        print("   " + "  ".join(f"{tubes.xy(r, c)}" for c in range(5)))
    print()

    print("keep-out interaction with the tube rack")
    for k in deck.keepouts:
        hits = [
            (c, tubes.xy(0, c)[0])
            for c in range(tubes.labware.cols)
            if k.contains_x(tubes.xy(0, c)[0])
        ]
        print(f"  {k.id} X{k.x_range}: tube columns inside band -> {hits}")
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
