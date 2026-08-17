#!/usr/bin/env python3
"""Port of the X3's *own* sleep-screen path: the file scan and the BMP reader.

This is the gate's oracle, the counterpart of `tools/opds-server/scripts/
crosspoint_client.py`. A file can be a perfectly valid BMP and still never
reach the panel — or reach it re-dithered by the firmware, which throws away
the dithering we did with a real CPU and full precision. So the self-test does
not ask "is this a valid BMP", it asks "what does *this device* do with it".

Behaviour ported (CrossPoint firmware, read at tag 1.5.0 and master @ 2026-08;
the two are byte-identical for every file below):

  src/activities/boot_sleep/SleepActivity.cpp   which files are even looked at,
                                                and where the image lands
  lib/GfxRenderer/Bitmap.cpp                    header parsing, the palette
                                                read, the native-palette test,
                                                the per-bpp row unpack
  lib/GfxRenderer/BitmapHelpers.cpp             adjustPixel (identity: the
                                                firmware ships USE_BRIGHTNESS
                                                = false)
  lib/FsHelpers/FsHelpers.cpp                   hasBmpExtension

Quirks kept on purpose, because they are what we design against:

  * The palette is read from wherever the file cursor sits after the first 40
    DIB bytes — *not* from `bfOffBits` and not from `biSize`. A BITMAPV4/V5
    header (biSize > 40) therefore feeds the reader 108 bytes of colour-space
    fields as if they were palette entries. Emit a 40-byte header or lose.
  * `colorsUsed == 0` means 2**bpp for paletted files, so a 4-bpp BMP with no
    explicit count has its palette read as 16 entries.
  * `hasGreyscale()` is `bpp > 1` — a 1-bpp file is drawn with the plain
    black-and-white waveform, never the four-level grey one.
  * The sleep-screen folder scan skips any name starting with '.', whatever it
    contains.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

# The X3 panel, portrait: the sleep screen is always drawn portrait
# (SleepActivity resets the renderer orientation before painting).
PANEL_W, PANEL_H = 528, 792

# The four physical states of the panel. `Bitmap::readNextRow` emits a 2-bit
# index; these are the luminances that map onto them one-to-one.
NATIVE_LEVELS = (0, 85, 170, 255)

# Bitmap.cpp's safety ceiling.
MAX_IMAGE_WIDTH, MAX_IMAGE_HEIGHT = 2048, 3072


class BmpReaderError(Exception):
    """Named exactly as the firmware's BmpReaderError enum, so a failure here
    reads like the device's own log line."""


@dataclass
class ParsedBmp:
    width: int
    height: int
    top_down: bool
    bpp: int
    colors_used: int
    row_bytes: int
    off_bits: int
    palette_lum: list           # index -> luminance, as the firmware computes it
    native_palette: bool        # True => direct map, the firmware dithers nothing

    @property
    def has_greyscale(self) -> bool:
        """`Bitmap::hasGreyscale()` — decides whether the sleep screen runs the
        four-level grey pipeline or the plain BW one."""
        return self.bpp > 1


def _luminance(b: int, g: int, r: int) -> int:
    """The firmware's integer luma: (77*R + 150*G + 29*B) >> 8."""
    return (77 * r + 150 * g + 29 * b) >> 8


def parse_headers(data: bytes) -> ParsedBmp:
    """Port of `Bitmap::parseHeaders`. Raises BmpReaderError with the firmware's
    own error name; returns what the firmware would hold in the object."""
    if len(data) < 54:
        raise BmpReaderError("FileInvalid")
    if data[0:2] != b"BM":
        raise BmpReaderError("NotBMP (missing 'BM')")

    (off_bits,) = struct.unpack_from("<I", data, 10)
    (bi_size,) = struct.unpack_from("<I", data, 14)
    if bi_size < 40:
        raise BmpReaderError("DIBTooSmall (<40 bytes)")

    width, raw_height = struct.unpack_from("<ii", data, 18)
    top_down = raw_height < 0
    height = -raw_height if top_down else raw_height
    planes, bpp = struct.unpack_from("<HH", data, 26)
    (comp,) = struct.unpack_from("<I", data, 30)

    if planes != 1:
        raise BmpReaderError("BadPlanes (!= 1)")
    if bpp not in (1, 2, 4, 8, 24, 32):
        raise BmpReaderError("UnsupportedBpp (expected 1, 2, 4, 8, 24, or 32)")
    if not (comp == 0 or (bpp == 32 and comp == 3)):
        raise BmpReaderError("UnsupportedCompression (expected BI_RGB or BI_BITFIELDS for 32bpp)")

    (colors_used,) = struct.unpack_from("<I", data, 46)
    if colors_used == 0 and bpp <= 8:
        colors_used = 1 << bpp
    if colors_used > 256:
        raise BmpReaderError("PaletteTooLarge")

    if width <= 0 or height <= 0:
        raise BmpReaderError("BadDimensions")
    if width > MAX_IMAGE_WIDTH or height > MAX_IMAGE_HEIGHT:
        raise BmpReaderError("ImageTooLarge (max 2048x3072)")

    row_bytes = (width * bpp + 31) // 32 * 4

    # The palette is read from offset 54 — right after the first 40 DIB bytes —
    # regardless of biSize or bfOffBits. This is the quirk that makes a
    # BITMAPV4HEADER file decode as noise.
    palette_lum = list(range(256))
    pos = 54
    for i in range(colors_used):
        if pos + 4 > len(data):
            raise BmpReaderError("FileInvalid")
        b, g, r, _ = data[pos:pos + 4]
        palette_lum[i] = _luminance(b, g, r)
        pos += 4

    if off_bits > len(data):
        raise BmpReaderError("SeekPixelDataFailed")

    # Does every palette entry sit on one of the panel's four states? If so the
    # firmware maps straight through and dithers nothing — which is the whole
    # point of shipping a pre-dithered indexed file.
    native_palette = bpp <= 2
    if not native_palette and colors_used > 0:
        native_palette = True
        for i in range(colors_used):
            lum = palette_lum[i]
            reconstructed = (lum >> 6) * 85
            if lum > reconstructed + 21 or lum + 21 < reconstructed:
                native_palette = False
                break

    return ParsedBmp(width=width, height=height, top_down=top_down, bpp=bpp,
                     colors_used=colors_used, row_bytes=row_bytes, off_bits=off_bits,
                     palette_lum=palette_lum, native_palette=native_palette)


def decode_levels(data: bytes, hdr: ParsedBmp) -> list:
    """Port of `Bitmap::readNextRow` for the native-palette path: the 2-bit
    indices the firmware hands the renderer, in screen order (top row first).

    Only the direct-map branch is ported. Anything else means the firmware is
    dithering our file for us, which the gate rejects rather than emulates.
    """
    if not hdr.native_palette:
        raise BmpReaderError("not a native palette: the firmware would re-dither this file")

    out = [0] * (hdr.width * hdr.height)
    for row in range(hdr.height):
        start = hdr.off_bits + row * hdr.row_bytes
        raw = data[start:start + hdr.row_bytes]
        if len(raw) != hdr.row_bytes:
            raise BmpReaderError("ShortReadRow")

        # Rows arrive bottom-up unless the height was negative.
        y = row if hdr.top_down else hdr.height - 1 - row
        base = y * hdr.width

        for x in range(hdr.width):
            if hdr.bpp == 4:
                nibble = raw[x >> 1] & 0x0F if (x & 1) else raw[x >> 1] >> 4
                lum = hdr.palette_lum[nibble]
            elif hdr.bpp == 8:
                lum = hdr.palette_lum[raw[x]]
            elif hdr.bpp == 2:
                lum = hdr.palette_lum[(raw[x >> 2] >> (6 - (x & 3) * 2)) & 0x03]
            elif hdr.bpp == 1:
                lum = hdr.palette_lum[1 if raw[x >> 3] & (0x80 >> (x & 7)) else 0]
            elif hdr.bpp in (24, 32):
                step = hdr.bpp // 8
                p = x * step
                lum = _luminance(raw[p], raw[p + 1], raw[p + 2])
            else:
                raise BmpReaderError("UnsupportedBpp")
            # adjustPixel() is identity in shipped firmware (USE_BRIGHTNESS false).
            out[base + x] = lum >> 6
    return out


# The firmware's quantiser constants, from `AtkinsonDitherer::processPixel`
# (lib/GfxRenderer/BitmapHelpers.h). There are TWO sets in that function and
# only one is live:
#
#     if (false) {  // original thresholds      43 / 128 / 213 -> 0,85,170,255
#     } else {      // fine-tuned to X4 eink display
#                                               30 /  50 / 140 -> 15,30,80,210
#
# The live one is labelled for the **X4**. On an X3 it renders visibly too
# bright, device-confirmed 2026-08: its reconstruction values sit below what the
# panel actually shows, so `error = adjusted - quantizedValue` comes out too
# positive and each pixel pushes its neighbours lighter. At an input of 128 it
# charges +48 where the true error is about -42.
#
# So we run the firmware's *algorithm* with the firmware's *other* constants —
# the ones it disabled — because those match this panel. Note what this means:
# our output is deliberately NOT what the device would make of a full-tone file.
# It is what the device would make of one if it were tuned for the X3. Handing
# it 4-bpp on a native palette is what lets us have that: the file is mapped
# straight through, so the X4 tuning never runs.
EVEN_THRESHOLDS = (43, 128, 213)
EVEN_RECONSTRUCT = (0, 85, 170, 255)      # the disabled branch; right for the X3

X4_THRESHOLDS = (30, 50, 140)
X4_RECONSTRUCT = (15, 30, 80, 210)        # the live branch; too bright on an X3

# What `firmware_quantise` uses unless told otherwise.
FIRMWARE_THRESHOLDS = EVEN_THRESHOLDS
FIRMWARE_RECONSTRUCT = EVEN_RECONSTRUCT


def firmware_quantise(img_bytes: bytes, w: int, h: int, bottom_up: bool = True,
                      tuning: str = "even") -> bytearray:
    """Exactly what the reader would do to an undithered greyscale image.

    A byte-for-byte port of `AtkinsonDitherer::processPixel` plus the loop that
    drives it: 1/8 of the error to each of six neighbours, no serpentine.

    `tuning` picks which of the two constant sets in that function to use.
    "even" is the firmware's own disabled branch and the default, because it is
    the one that matches an X3; "x4" is the branch the firmware actually runs,
    kept so the gate can demonstrate what it does.

    Running it here means we can hand the panel the *result* as a 4-bpp file it
    maps straight through, instead of the full-tone image it would have had to
    chew on — same pixels, a sixth of the bytes, and none of the ESP32's work.

    `bottom_up` matters and is not cosmetic. Error diffusion is stateful down
    the rows, and the firmware diffuses in the order it reads them: a positive
    height means the file starts at the *bottom* of the picture, so that is
    where the error starts travelling. Getting this backwards still looks fine
    but is not the same image.
    """
    thresholds = X4_THRESHOLDS if tuning == "x4" else EVEN_THRESHOLDS
    reconstruct = X4_RECONSTRUCT if tuning == "x4" else EVEN_RECONSTRUCT

    e0 = [0] * (w + 4)
    e1 = [0] * (w + 4)
    e2 = [0] * (w + 4)
    out = bytearray(w * h)

    rows = range(h - 1, -1, -1) if bottom_up else range(h)
    for y in rows:
        base = y * w
        for x in range(w):
            # adjustPixel() is identity in shipped firmware, and for a grey
            # pixel the integer luma returns the value unchanged.
            adjusted = img_bytes[base + x] + e0[x + 2]
            if adjusted < 0:
                adjusted = 0
            elif adjusted > 255:
                adjusted = 255

            if adjusted < thresholds[0]:
                level = 0
            elif adjusted < thresholds[1]:
                level = 1
            elif adjusted < thresholds[2]:
                level = 2
            else:
                level = 3
            out[base + x] = level

            err = (adjusted - reconstruct[level]) >> 3            # error / 8
            e0[x + 3] += err
            e0[x + 4] += err
            e1[x + 1] += err
            e1[x + 2] += err
            e1[x + 3] += err
            e2[x + 2] += err

        e0, e1, e2 = e1, e2, [0] * (w + 4)

    return out


def sleep_scan_accepts(filename: str) -> bool:
    """Port of the filter in `SleepActivity::renderCustomSleepScreen`: which
    names in /.sleep or /sleep are even opened. A dotfile is skipped whatever
    it holds, and the extension test is case-insensitive."""
    if not filename or filename[0] == ".":
        return False
    return filename.lower().endswith(".bmp")


def placement(hdr: ParsedBmp, screen_w: int = PANEL_W, screen_h: int = PANEL_H):
    """Port of the geometry in `SleepActivity::renderBitmapSleepScreen`.

    Returns (x, y, scaled). The device only ever scales *down*: an image larger
    than the panel is fitted (and centred on the short axis), anything else is
    centred as-is with no resampling — so an under-size wallpaper is a small
    picture floating in black, never a stretched one.
    """
    if hdr.width > screen_w or hdr.height > screen_h:
        ratio = hdr.width / hdr.height
        screen_ratio = screen_w / screen_h
        if ratio > screen_ratio:
            return 0, round((screen_h - screen_w / ratio) / 2), True
        return round((screen_w - screen_h * ratio) / 2), 0, True
    return (screen_w - hdr.width) // 2, (screen_h - hdr.height) // 2, False


# --------------------------------------------------------------------------
# Looking at one — as the panel would


def render_png(src, dest) -> dict:
    """Write what the *device* would draw for this BMP, and say how it read it.

    The point of rendering through this module rather than through Pillow is
    that a wallpaper already on the SD card is interesting precisely when it
    does not look like you expect. Pillow shows you the file; this shows you
    the panel — the native-palette direct map, the 2-bit indices, the placement
    of an under-size image in its black field.

    When the palette is *not* native the firmware would re-dither the file on
    the ESP32, and this module deliberately does not emulate that (the gate
    rejects such files rather than predicting them). Rather than refuse to show
    anything, we fall back to Pillow's own decode and say the preview is
    approximate — identifying the picture is the job, and "the device will
    redo this one" is worth knowing besides.

    Pillow is imported here and nowhere else in this file: parsing stays
    stdlib, and only looking costs a dependency.
    """
    from pathlib import Path
    from PIL import Image

    src, dest = Path(src), Path(dest)
    data = src.read_bytes()
    hdr = parse_headers(data)
    x, y, scaled = placement(hdr)
    report = {"file": str(src), "width": hdr.width, "height": hdr.height,
              "bpp": hdr.bpp, "native_palette": hdr.native_palette,
              "greyscale": hdr.has_greyscale, "exact": False,
              "panel": [PANEL_W, PANEL_H], "x": x, "y": y, "scaled_down": scaled,
              "drawn_by_sleep_scan": sleep_scan_accepts(src.name)}

    try:
        levels = decode_levels(data, hdr)
        img = Image.new("L", (hdr.width, hdr.height))
        img.putdata([NATIVE_LEVELS[v] for v in levels])
        report["exact"] = True
    except BmpReaderError as exc:
        report["note"] = str(exc)
        img = Image.open(src).convert("L")

    # Show it where it lands: the panel's own field, so an under-size wallpaper
    # previews as the small picture in black that the device actually paints.
    if not scaled and (hdr.width < PANEL_W or hdr.height < PANEL_H):
        canvas = Image.new("L", (PANEL_W, PANEL_H), 0)
        canvas.paste(img, (x, y))
        img = canvas
    img.save(dest)
    report["png"] = str(dest)
    return report


def main(argv=None) -> int:
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser(
        description="What would the X3 draw for this BMP?")
    ap.add_argument("bmp")
    ap.add_argument("--png", metavar="OUT",
                    help="write a PNG of what the panel would show")
    args = ap.parse_args(argv)

    if args.png:
        json.dump(render_png(args.bmp, args.png), sys.stdout, ensure_ascii=False)
        return 0

    from pathlib import Path
    hdr = parse_headers(Path(args.bmp).read_bytes())
    json.dump({"width": hdr.width, "height": hdr.height, "bpp": hdr.bpp,
               "native_palette": hdr.native_palette},
              sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
