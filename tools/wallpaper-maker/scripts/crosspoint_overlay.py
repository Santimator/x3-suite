#!/usr/bin/env python3
"""A port of how CrossPoint draws a *Transparent custom* sleep screen from PNG.

The gate's oracle for what `make_wallpaper.py` writes, the way `crosspoint_bmp`
is the oracle for BMP. Stdlib only, and deliberately not Pillow: the question
is never "is this a valid PNG" but "what does *this firmware* do with it", and
the firmware has its own decoder with its own limits.

Ported from CrossPoint 1.6.5:

  src/activities/boot_sleep/SleepActivity.cpp
      renderTransparentCustomSleepScreen  /sleep-overlay.bmp|.png at the root,
                                          then /.sleep-overlay/, then
                                          /sleep-overlay/, one file at random
      findNextValidSleepImage             skips dot names; .bmp or .png only
  lib/Epub/Epub/converters/PngToFramebufferConverter.cpp
      isSupportedBitDepth                 8 bits, or 1/2/4 for grey and indexed
      convertLineToGray                   palette grey = (77r + 150g + 29b) >> 8,
                                          alpha from tRNS (palette[768 + idx])
      the draw loop                       a pixel lands when alpha >= 8 and
                                          alpha > the 4x4 Bayer threshold; its
                                          level is grey >> 6 (no dithering on
                                          this path)
  lib/Epub/Epub/converters/DirectPixelWriter.h
      writePixel(.., writeWhiteInBw)      with an alpha line, white is *written*
                                          — an opaque white pixel erases the page

That last line is the whole reason PNG is the format: in the overlay folder a
plain BMP has no alpha, so its white is skipped and the page shows through, and
on the X3 the greys ghost as well (device-observed 2026-10). An opaque PNG
paints every pixel, white included.

    crosspoint_overlay.py wall.png --png preview.png [--page page.png | --sample-page]

writes what the panel would show (over a white page, the given one, or a page
of sample text) and prints a JSON report.
"""

from __future__ import annotations

import json
import struct
import sys
import zlib
from pathlib import Path

PANEL_W, PANEL_H = 528, 792
OVERLAY_DIRS = ("/.sleep-overlay", "/sleep-overlay")
ROOT_FILES = ("/sleep-overlay.bmp", "/sleep-overlay.png")

BAYER_4X4 = (0, 128, 32, 160, 192, 64, 224, 96, 48, 176, 16, 144, 240, 112, 208, 80)
MIN_VISIBLE_ALPHA = 8

# PNG_MAX_BUFFERED_PIXELS in the firmware's platformio.ini; the grey line
# buffer is half of it, one byte per source pixel.
PNG_MAX_BUFFERED_PIXELS = 16416
MAX_GRAY_LINE = PNG_MAX_BUFFERED_PIXELS // 2

GREY, TRUECOLOR, INDEXED, GREY_ALPHA, TRUECOLOR_ALPHA = 0, 2, 3, 4, 6
CHANNELS = {GREY: 1, TRUECOLOR: 3, INDEXED: 1, GREY_ALPHA: 2, TRUECOLOR_ALPHA: 4}


class PngError(Exception):
    pass


def scan_accepts(name: str) -> bool:
    """`findNextValidSleepImage` for the overlay kind: no dot names, and the
    extension is .bmp or .png (case-insensitive). Everything else is skipped
    without a word."""
    if not name or name.startswith("."):
        return False
    return name.lower().endswith((".bmp", ".png"))


def bayer(x: int, y: int) -> int:
    return BAYER_4X4[((y & 3) << 2) | (x & 3)]


def drawn(alpha: int, x: int, y: int) -> bool:
    """Does the firmware put this pixel on the panel at all?"""
    return alpha >= MIN_VISIBLE_ALPHA and alpha > bayer(x, y)


def _chunks(data: bytes):
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise PngError("not a PNG")
    pos = 8
    while pos + 8 <= len(data):
        (length,) = struct.unpack_from(">I", data, pos)
        kind = data[pos + 4:pos + 8]
        yield kind, data[pos + 8:pos + 8 + length]
        pos += 12 + length


def _unfilter(raw: bytes, height: int, stride: int, bpp: int) -> list:
    rows, prev, pos = [], bytearray(stride), 0
    for _ in range(height):
        kind, line = raw[pos], bytearray(raw[pos + 1:pos + 1 + stride])
        pos += 1 + stride
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            if kind == 1:
                line[i] = (line[i] + a) & 255
            elif kind == 2:
                line[i] = (line[i] + b) & 255
            elif kind == 3:
                line[i] = (line[i] + (a + b) // 2) & 255
            elif kind == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[i] = (line[i] + pred) & 255
            elif kind != 0:
                raise PngError(f"bad filter {kind}")
        rows.append(bytes(line))
        prev = line
    return rows


def _sample(row: bytes, x: int, bits: int) -> int:
    """`readPackedSample`: most-significant sample first."""
    if bits == 8:
        return row[x]
    off = x * bits
    return (row[off >> 3] >> (8 - bits - (off & 7))) & ((1 << bits) - 1)


def decode(data: bytes) -> dict:
    """Header facts plus one (grey, alpha) per pixel, as the firmware sees them."""
    ihdr = plte = trns = None
    idat = bytearray()
    for kind, body in _chunks(data):
        if kind == b"IHDR":
            ihdr = body
        elif kind == b"PLTE":
            plte = body
        elif kind == b"tRNS":
            trns = body
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
    if ihdr is None:
        raise PngError("no IHDR")
    w, h, bits, ctype, _, _, interlace = struct.unpack(">IIBBBBB", ihdr)
    if interlace:
        raise PngError("interlaced")
    if ctype not in CHANNELS:
        raise PngError(f"colour type {ctype}")
    # isSupportedBitDepth
    if not (bits == 8 or (bits in (1, 2, 4) and ctype in (GREY, INDEXED))):
        raise PngError(f"{bits}-bit colour type {ctype} is not supported")
    if w > MAX_GRAY_LINE:
        raise PngError(f"{w}px wide exceeds the grey line buffer")

    chans = CHANNELS[ctype]
    stride = (w * chans * bits + 7) // 8
    rows = _unfilter(zlib.decompress(bytes(idat)), h, stride, max(1, chans * bits // 8))

    palette = plte or b""
    alphas = trns or b""
    grey, alpha = bytearray(w * h), bytearray(w * h)
    for y, row in enumerate(rows):
        base = y * w
        for x in range(w):
            if ctype == INDEXED:
                idx = _sample(row, x, bits)
                r, g, b = palette[idx * 3:idx * 3 + 3] or b"\0\0\0"
                grey[base + x] = (r * 77 + g * 150 + b * 29) >> 8
                alpha[base + x] = alphas[idx] if idx < len(alphas) else 255
            elif ctype == GREY:
                s = _sample(row, x, bits)
                grey[base + x] = s * 255 // ((1 << bits) - 1)
                key = struct.unpack(">H", trns)[0] if trns else None
                alpha[base + x] = 0 if key is not None and s == key else 255
            elif ctype == GREY_ALPHA:
                grey[base + x], alpha[base + x] = row[x * 2], row[x * 2 + 1]
            else:
                r, g, b = row[x * chans:x * chans + 3]
                grey[base + x] = (r * 77 + g * 150 + b * 29) >> 8
                alpha[base + x] = row[x * 4 + 3] if ctype == TRUECOLOR_ALPHA else 255
    return {"width": w, "height": h, "bits": bits, "colour_type": ctype,
            "grey": grey, "alpha": alpha}


def placement(w: int, h: int) -> tuple:
    """Where the panel puts it: centred, never scaled up (same rule as BMP)."""
    scaled = w > PANEL_W or h > PANEL_H
    return (max(0, (PANEL_W - w) // 2), max(0, (PANEL_H - h) // 2), scaled)


def panel_levels(img: dict) -> list:
    """Per pixel of a panel-sized file: the level drawn (0..3), or None where
    the page underneath stays."""
    out = []
    g, a, w = img["grey"], img["alpha"], img["width"]
    for i in range(len(g)):
        x, y = i % w, i // w
        out.append(g[i] >> 6 if drawn(a[i], x, y) else None)
    return out


def composite(levels: list, page: bytes | None = None) -> bytes:
    """What the glass shows: our levels where drawn, the page elsewhere.
    `page` is panel-sized 8-bit grey; white when absent."""
    tone = (0, 85, 170, 255)
    return bytes(tone[v] if v is not None else (page[i] if page else 255)
                 for i, v in enumerate(levels))


def report(path: Path) -> dict:
    data = path.read_bytes()
    img = decode(data)
    x, y, scaled = placement(img["width"], img["height"])
    a = img["alpha"]
    return {"file": str(path), "width": img["width"], "height": img["height"],
            "bits": img["bits"], "colour_type": img["colour_type"],
            "drawn_by_sleep_scan": scan_accepts(path.name),
            "transparent": any(v < 255 for v in a),
            "x": x, "y": y, "scaled_down": scaled,
            "exact": all(v in (0, 85, 170, 255) for v in img["grey"])}


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("png", type=Path)
    ap.add_argument("--png", dest="out", type=Path,
                    help="write what the panel would show")
    ap.add_argument("--page", type=Path,
                    help="a panel-sized image to show through the transparent parts")
    ap.add_argument("--sample-page", action="store_true",
                    help="show a page of sample text through them instead")
    args = ap.parse_args(argv)
    try:
        rep = report(args.png)
    except (PngError, OSError, zlib.error) as exc:
        print(f"crosspoint_overlay: {exc}", file=sys.stderr)
        return 1
    if args.out:
        from PIL import Image                      # only for writing the picture
        img = decode(args.png.read_bytes())
        if (img["width"], img["height"]) != (PANEL_W, PANEL_H):
            canvas = Image.new("L", (PANEL_W, PANEL_H), 0)
            pic = Image.frombytes("L", (img["width"], img["height"]),
                                  bytes(img["grey"]))
            canvas.paste(pic.resize((min(pic.width, PANEL_W), min(pic.height, PANEL_H))),
                         (rep["x"], rep["y"]))
            canvas.save(args.out)
        else:
            page = None
            if args.sample_page:
                from make_wallpaper import sample_page     # Pillow, same folder
                page = sample_page()
            elif args.page:
                page = Image.open(args.page).convert("L").resize(
                    (PANEL_W, PANEL_H)).tobytes()
            shown = composite(panel_levels(img), page)
            Image.frombytes("L", (PANEL_W, PANEL_H), shown).save(args.out)
        rep["png"] = str(args.out)
    json.dump(rep, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
