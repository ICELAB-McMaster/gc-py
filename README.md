# gc-py
`gc-py` is a Python g-code generator, useful for firmware like [Marlin](https://github.com/MarlinFirmware/Marlin).
This project was heavily inspired by [gcodepy](https://github.com/rmeno12/gcodepy).
More active development was needed to add features and thus it was forked.

## Self-driving lab

The `lab/` package drives a Klipper pipetting robot on top of `gcodepy`. Deck
geometry, labware, and one JSON per end effector live in `config/`; a protocol
script is then just the procedure.

```bash
python protocols/accuracy_test.py --effector p300 --outdir out
```

See [docs/LAB.md](docs/LAB.md) for the layout, the API, and the rules the
library enforces.

**+Z is DOWN** on this machine: `Z=0` is home at the top, and a larger commanded
Z is physically lower.

## Planned features
- Work envelope layout
- Station System for frequently used cordinates
- 

