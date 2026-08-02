#!/usr/bin/env python3
"""Port of the X3's *own* sleep-screen path: the file scan and the BMP reader.

This is the gate's oracle, the counterpart of `opds-server/scripts/
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
