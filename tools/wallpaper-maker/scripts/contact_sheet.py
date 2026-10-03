#!/usr/bin/env python3
"""Many wallpapers as one small picture, numbered so you can point at them.

    contact_sheet.py OUT.png a.png b.jpg c.bmp
    contact_sheet.py OUT.png workspace/wallpapers --start 25

Every cell is 2:3, the panel's shape. A built wallpaper fills it exactly; an
original photo of any other shape is fitted inside it on the sheet's ground,
so what you see is the picture, not a crop of it. Each cell carries its **index**, drawn on the
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
order they were numbered, each with whether it is transparent anywhere — shown
on the sheet as stripes where it is clear and a small checkerboard by its
number.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

# A cell wide enough to recognise a photograph you took, small enough that two
# dozen fit in an image Telegram will not recompress into mush.
CELL_W, CELL_H = 110, 165
GAP = 6
MARGIN = 8
BACKGROUND = 245          # near-white; the panel's own white is 255 and the
                          # thumbnails should sit *on* something, not bleed out
LABEL_H = 22


def _font():
    # No font file, no dependency on extras/fonts: Pillow's built-in face
    # can be asked for a size since 10.1, which is enough for two digits.
    try:
        return ImageFont.load_default(size=15)
    except TypeError:
        return ImageFont.load_default()


def _hatch(size) -> Image.Image:
    """Light diagonal stripes: what a clear area looks like on the sheet."""
    w, h = size
    img = Image.new("L", (w, h), 255)
    draw = ImageDraw.Draw(img)
    for k in range(-h, w, 7):
        draw.line((k, h, k + h, 0), fill=175, width=1)
    return img


def _thumb(src: Path) -> tuple:
    """One cell, and whether the picture is transparent anywhere.

    Rotated as the camera meant and fitted. Where the picture is clear it is
    shown *striped* rather than white, so the sheet says where the page will
    show through, not just that it will — a white sky and a cut-out window
    look different here, as they do on the reader.
    """
    img = Image.open(src)
    img.draft("RGB", (CELL_W * 2, CELL_H * 2))     # JPEG: decode small, fast
    img = ImageOps.exif_transpose(img)
    rgba = img.convert("RGBA")
    rgba.thumbnail((CELL_W, CELL_H), Image.LANCZOS)
    alpha = rgba.getchannel("A")
    clear = alpha.getextrema()[0] < 128
    ground = (_hatch(rgba.size) if clear else Image.new("L", rgba.size, 255))
    ground = ground.convert("RGBA")
    # Binary, as the wallpaper itself is: half-clear is clear.
    rgba.putalpha(alpha.point(lambda v: 255 if v >= 128 else 0))
    pic = Image.alpha_composite(ground, rgba).convert("L")
    cell = Image.new("L", (CELL_W, CELL_H), BACKGROUND)
    cell.paste(pic, ((CELL_W - pic.width) // 2, (CELL_H - pic.height) // 2))
    return cell, clear


def _badge(draw, x: int, y: int) -> None:
    """A tiny checkerboard — the usual sign for "transparent" — drawn rather
    than typed, so it does not depend on what glyphs the font carries."""
    s = 5
    for r in range(2):
        for c in range(2):
            fill = 255 if (r + c) % 2 == 0 else 110
            draw.rectangle((x + c * s, y + r * s, x + c * s + s - 1, y + r * s + s - 1),
                           fill=fill)
    draw.rectangle((x - 1, y - 1, x + 2 * s, y + 2 * s), outline=255)


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
        clear = False
        try:
            thumb, clear = _thumb(src)
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
        if clear:
            _badge(draw, x + CELL_W - 16, y + CELL_H + (LABEL_H - 10) // 2)
        named.append({"n": start + i, "name": src.name, "path": str(src),
                      "transparent": clear})

    dest.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(dest, optimize=True)
    return {"png": str(dest), "count": len(files), "cols": cols,
            "size": [width, height], "items": named}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out", type=Path)
    ap.add_argument("inputs", nargs="+", type=Path,
                    help="images, or a folder of them")
    ap.add_argument("--start", type=int, default=1,
                    help="number the first cell from here (for later pages)")
    ap.add_argument("--cols", type=int, default=8)
    args = ap.parse_args(argv)

    files = []
    for item in args.inputs:
        if item.is_dir():
            files += sorted(p for p in item.iterdir() if p.is_file()
                            and p.suffix.lower() in (".png", ".bmp", ".jpg",
                                                     ".jpeg", ".webp"))
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
