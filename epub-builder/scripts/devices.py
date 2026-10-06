"""Reader profiles: the panel each target device draws on.

One table, imported by everything that sizes pixels for a device (the cover in
`prepare_cover.py`, figures in `ai-tools/pdf2epub/scripts/figures.py`), so the
two can never disagree about a screen. Sizes are the portrait canvas in pixels;
`extras/readers.md` is where each number comes from.

The suite is named for the X3, which stays the default everywhere. The X4 Pro
is here because a converted book can be aimed at either.
"""

DEVICES = {
    "x3": {"name": "Xteink X3", "panel": (528, 792)},
    "x4pro": {"name": "Xteink X4 Pro", "panel": (480, 800)},
}

DEFAULT_DEVICE = "x3"


def panel(device: str) -> tuple:
    """(width, height) of the device's portrait panel. Unknown name -> KeyError
    listing the valid ones, never a silent fallback."""
    try:
        return DEVICES[device]["panel"]
    except KeyError:
        raise KeyError(f"unknown device {device!r}; known: {', '.join(DEVICES)}") from None
