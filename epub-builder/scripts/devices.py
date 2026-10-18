"""Reader profiles: the panel each target device draws on.

One table, imported by everything that sizes pixels for a device (the cover in
`prepare_cover.py`, figures in `ai-tools/pdf2epub/scripts/figures.py`, the
wallpapers in `tools/wallpaper-maker/`, the slim copies in
`tools/epub-slimmer/`), so none of them can disagree about a screen. Sizes are
the portrait canvas in pixels; `extras/readers.md` is where each number comes
from.

`status` is what the reader itself answers in `GET /api/status` as `device`
when it runs CrossPoint: the X3 build says "X3", the X4 Pro build reports its
board profile's name. That is how a tool that has found a reader on the network
knows which row of this table it is talking to.

The suite is named for the X3, which stays the default everywhere.
"""

DEVICES = {
    "x3": {"name": "Xteink X3", "panel": (528, 792), "status": "X3"},
    "x4pro": {"name": "Xteink X4 Pro", "panel": (480, 800), "status": "xteink_x4_pro"},
}

DEFAULT_DEVICE = "x3"


def panel(device: str) -> tuple:
    """(width, height) of the device's portrait panel. Unknown name -> KeyError
    listing the valid ones, never a silent fallback."""
    try:
        return DEVICES[device]["panel"]
    except KeyError:
        raise KeyError(f"unknown device {device!r}; known: {', '.join(DEVICES)}") from None


def from_status(reported: str | None) -> str | None:
    """The table key for what `/api/status` calls `device`, or None for a
    reader this table does not describe (an X4, a Sticky, a future board).

    None rather than a guess: sizing a wallpaper for the wrong panel is the
    failure this table exists to prevent."""
    for key, row in DEVICES.items():
        if reported == row["status"]:
            return key
    return None
