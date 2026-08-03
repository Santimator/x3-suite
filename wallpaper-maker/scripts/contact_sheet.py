#!/usr/bin/env python3
"""Many wallpapers as one small picture, numbered so you can point at them.

    contact_sheet.py OUT.png a.bmp b.bmp c.bmp
    contact_sheet.py OUT.png workspace/wallpapers/build --start 25

Wallpapers are all exactly 528x792, so a sheet of them is a clean 2:3 grid with
no letterboxing to arrange around. Each cell carries its **index**, drawn on the
image, because a contact sheet you cannot point at is only decoration: the
caller puts numbered buttons underneath and the two line up.

Why this exists at all: showing a folder of wallpapers one message at a time
costs an upload each, and a burst of them earns a rate-limit from Telegram —
which, once, silently swallowed the reply that came after it. One sheet is one
upload however many wallpapers there are.

Thumbnails are decoded with Pillow rather than through `crosspoint_bmp`, the
port of the firmware's reader. That is deliberate and the opposite of the rule
elsewhere: at 110 px across, what the panel would do with the dithering is
invisible, and Pillow is about eight times faster per file. The *single*
full-size preview still goes through the port, because that is where "what the
device actually draws" is the whole question.

Emits JSON on stdout: the path written, the cell size, and the names in the
order they were numbered.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# A cell wide enough to recognise a photograph you took, small enough that two
# dozen fit in an image Telegram will not recompress into mush.
CELL_W, CELL_H = 110, 165
GAP = 6
MARGIN = 8
BACKGROUND = 245          # near-white; the panel's own white is 255 and the
                          # thumbnails should sit *on* something, not bleed out
LABEL_H = 22


def _font():
    # No font file, no dependency on reference/fonts: Pillow's built-in face
    # can be asked for a size since 10.1, which is enough for two digits.
    try:
        return ImageFont.load_default(size=15)
    except TypeError:
        return ImageFont.load_default()


def build(files: list, dest: Path, *, start: int = 1, cols: int = 8) -> dict:
    files = [Path(f) for f in files]
    rows = (len(files) + cols - 1) // cols
    cols = min(cols, max(1, len(files)))
    width = MARGIN * 2 + cols * CELL_W + (cols - 1) * GAP
    height = MARGIN * 2 + rows * (CELL_H + LABEL_H) + (rows - 1) * GAP

    sheet = Image.new("L", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(sheet)
    font = _font()
    named = []

    for i, src in enumerate(files):
        col, row = i % cols, i // cols
        x = MARGIN + col * (CELL_W + GAP)
        y = MARGIN + row * (CELL_H + LABEL_H + GAP)
        try:
            thumb = Image.open(src).convert("L").resize((CELL_W, CELL_H),
                                                        Image.LANCZOS)
        except Exception:
            # An unreadable file still gets a cell, so the numbering never
            # shifts under the buttons that refer to it.
            thumb = Image.new("L", (CELL_W, CELL_H), 200)
            ImageDraw.Draw(thumb).line((0, 0, CELL_W, CELL_H), fill=60, width=2)
        sheet.paste(thumb, (x, y))
        draw.rectangle((x, y, x + CELL_W - 1, y + CELL_H - 1), outline=120)

        n = str(start + i)
        # The number goes *under* the thumbnail rather than over it: a wallpaper
        # is a photograph and there is no corner guaranteed to be quiet.
        draw.rectangle((x, y + CELL_H, x + CELL_W - 1, y + CELL_H + LABEL_H - 1),
                       fill=30)
        draw.text((x + CELL_W // 2, y + CELL_H + LABEL_H // 2), n,
                  fill=255, font=font, anchor="mm")
        named.append({"n": start + i, "name": src.name, "path": str(src)})

    dest.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(dest, optimize=True)
    return {"png": str(dest), "count": len(files), "cols": cols,
            "size": [width, height], "items": named}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out", type=Path)
    ap.add_argument("inputs", nargs="+", type=Path,
                    help="BMPs, or a folder of them")
    ap.add_argument("--start", type=int, default=1,
                    help="number the first cell from here (for later pages)")
    ap.add_argument("--cols", type=int, default=8)
    args = ap.parse_args(argv)

    files = []
    for item in args.inputs:
        if item.is_dir():
            files += sorted(p for p in item.iterdir()
                            if p.suffix.lower() == ".bmp")
        elif item.is_file():
            files.append(item)
    if not files:
        print("contact_sheet: nothing to draw", file=sys.stderr)
        return 1

    json.dump(build(files, args.out, start=args.start, cols=args.cols),
              sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
