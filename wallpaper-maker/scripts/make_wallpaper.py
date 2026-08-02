#!/usr/bin/env python3
"""Turn any image into an X3 sleep-screen wallpaper. No questions asked.

Drop pictures in `workspace/wallpapers/`, run this, collect
`workspace/wallpapers/build/*.bmp`. That is the whole interface: every choice
below is made for you, because every one of them has a right answer on this
device and none of them is a matter of taste you should have to hold in your
head at 11pm.

What comes out, and why each part of it:

  528x792, exactly       The X3 panel, portrait. The firmware centres an image
                         that fits and only ever scales *down*, so anything
                         smaller lands as a postage stamp in a black field and
                         anything larger is resampled by an ESP32. Hitting the
                         panel exactly is the only size that is neither.
  a mat, when the        Filling the panel from a small source means enlarging
  source is small        it arbitrarily, and past about half again that is a
                         smear. So enlargement stops, and what is left over is
                         framed: four sectors mitred from the panel corners,
                         each taking its level from the image edge it touches.
                         Snapped to a native level, because a flat area on one
                         of the four is the only large area this panel draws
                         without dither grain.
  4-bpp indexed BMP      BMP because the sleep screen reads nothing else — not
                         PNG, not JPEG, and not .pxc, which is the EPUB reader's
                         internal pixel cache and has never been a wallpaper
                         format. Indexed because a 4-entry grey palette on the
                         panel's own levels trips the firmware's native-palette
                         test, and then it maps our pixels straight through.
                         Hand a 24-bpp file over and the ESP32 re-dithers it —
                         our careful full-precision work, thrown away and redone
                         with an integer approximation on a 240 MHz core.
  4 grey levels          0 / 85 / 170 / 255. Not a design choice: they are the
                         four charge states the panel has.
  Floyd-Steinberg,       Error diffusion is what makes four levels look like a
  serpentine             photograph. Serpentine (alternating scan direction)
                         because straight raster order walks the residual error
                         in one direction and leaves diagonal worms in skies.
  autocontrast, then     A phone photo uses maybe half the range; on four levels
  gamma 0.85, then       that half becomes two. Stretch first, lift the midtones
  unsharp mask           (e-ink reads darker than the screen you chose the image
                         on), then restore the local contrast that downscaling
                         cost. All three before dithering, so the dither is
                         deciding about the image you actually want.

Usage:
  make_wallpaper.py                       # workspace/wallpapers/ -> .../build/
  make_wallpaper.py photo.jpg             # one file, same output folder
  make_wallpaper.py shots/ --out /tmp/w   # anywhere in, anywhere out
  make_wallpaper.py photo.jpg --preview   # also write a PNG you can look at

Then get them onto the reader with `push_wallpaper.py`, which is the part OPDS
cannot do for you (see SKILL.md).
"""

from __future__ import annotations

import argparse
import re
import struct
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps, ImageStat

REPO_ROOT = Path(__file__).resolve().parents[2]

# The X3 panel and its four physical states. Both are device facts, not knobs;
# `reference/readers.md` is where they are written down and evidenced.
PANEL_W, PANEL_H = 528, 792
LEVELS = (0, 85, 170, 255)

DEFAULT_IN = REPO_ROOT / "workspace" / "wallpapers"
DEFAULT_OUT = DEFAULT_IN / "build"

# Everything Pillow will open that anyone plausibly drops in a folder.
SOURCE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif",
                   ".tiff", ".ppm", ".pgm", ".heic", ".avif"}

# The tone curve, applied in this order. Tuned once, for e-ink, against the
# four levels below — not per image, and not per user.
AUTOCONTRAST_CUTOFF = 0.5   # % clipped at each end before the range is stretched
GAMMA = 0.85                # <1 lifts midtones; e-ink reflects less than a screen
UNSHARP_AMOUNT = 1.35       # local contrast back after the downscale

# How far a small image is enlarged before we stop and frame it instead. Past
# about half again, a photo is visibly soft even after dithering, and a picture
# in a mat beats a smeared one that fills the screen.
MAX_UPSCALE = 1.5
MIN_MAT_AREA = 0.06         # thinner than this reads as a mistake — fill instead
EDGE_BAND = 0.08            # fraction of the image each mat sector samples
BLUR_DIVISOR = 12           # --mat blur: panel width / this = blur radius
BLUR_CONTRAST = 0.55        # ... then flattened, so the sharp image stays foreground


def sanitize_stem(stem: str) -> str:
    """A name the SD card and the firmware's folder scan both accept.

    FAT is the easy half. The hard half is that the sleep-screen scan skips any
    name beginning with '.', so a leading dot is a silently invisible file.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-.")
    return cleaned or "wallpaper"


def load_grayscale(path: Path) -> Image.Image:
    """Open, honour EXIF rotation, flatten onto white, and go to 8-bit grey.

    EXIF first: a phone portrait shot is stored landscape with a rotate flag,
    and cover-cropping it before rotating would crop the wrong axis.
    """
    img = Image.open(path)
    img.load()
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "P", "PA"):
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, img)
    return img.convert("L")


def scale_to_panel(img: Image.Image, mode: str = "cover") -> Image.Image:
    """Scale as far as we are willing to, and no further.

    'cover' fills the panel and centre-crops the overflow — a wallpaper should
    reach all four edges, and the device will not do this for us. But filling
    can demand an arbitrary enlargement, and a 200px icon blown up 4x is a
    smear. So enlargement stops at MAX_UPSCALE and whatever is left over gets
    a mat (see `mat`). 'contain' keeps the whole frame and is matted by
    definition.

    Returns a panel-sized image when it fills, a smaller one when it does not.
    """
    w, h = img.size
    s_cover = max(PANEL_W / w, PANEL_H / h)
    s_contain = min(PANEL_W / w, PANEL_H / h)

    def filled() -> Image.Image:
        return ImageOps.fit(img, (PANEL_W, PANEL_H), method=Image.LANCZOS,
                            centering=(0.5, 0.5))

    if mode == "cover" and s_cover <= MAX_UPSCALE:
        return filled()

    scale = min(s_contain, MAX_UPSCALE)
    size = (max(1, round(w * scale)), max(1, round(h * scale)))

    # A sliver of mat reads as a mistake rather than as framing. If the border
    # would be that thin, take the extra enlargement instead.
    if mode == "cover":
        mat_area = 1 - (size[0] * size[1]) / (PANEL_W * PANEL_H)
        if mat_area < MIN_MAT_AREA:
            return filled()

    return img.resize(size, Image.LANCZOS)


def _band_level(img: Image.Image, box) -> int:
    """The native level a strip of the image sits closest to.

    Snapping matters more than it looks: a mat filled with the raw mean dithers
    into a large field of visible grain, while one sitting exactly on 0/85/170/
    255 comes out perfectly flat. Flat is the only large area this panel draws
    without noise.
    """
    return LEVELS[_nearest(ImageStat.Stat(img.crop(box)).mean[0])]


def _edge_mat(img: Image.Image, ox: int, oy: int) -> Image.Image:
    """A passe-partout: four sectors, each continuing the edge it touches.

    Every side takes its level from a band of the image along that edge, so a
    photo with sky above and ground below gets a light mat above and a dark one
    below — the fill matches the pixels it actually meets, rather than the
    picture's overall mood.

    The sectors are split along the lines from each panel corner to the
    corresponding image corner. That is not just a tidy diagonal: along it the
    two sides' distances are equal *in proportion to their own margins*, which
    is the mitre a picture framer cuts. Uneven margins therefore give an uneven
    mitre, which is correct — and if all four bands round to the same level the
    joins vanish and it degenerates into a plain mat, which is also correct.
    """
    iw, ih = img.size
    band_w = max(1, min(round(iw * EDGE_BAND), iw))
    band_h = max(1, min(round(ih * EDGE_BAND), ih))

    levels = {
        "top": _band_level(img, (0, 0, iw, band_h)),
        "bottom": _band_level(img, (0, ih - band_h, iw, ih)),
        "left": _band_level(img, (0, 0, band_w, ih)),
        "right": _band_level(img, (iw - band_w, 0, iw, ih)),
    }

    x1, y1 = ox + iw, oy + ih
    canvas = Image.new("L", (PANEL_W, PANEL_H), levels["top"])
    draw = ImageDraw.Draw(canvas)
    for side, poly in (
        ("top", [(0, 0), (PANEL_W, 0), (x1, oy), (ox, oy)]),
        ("bottom", [(0, PANEL_H), (PANEL_W, PANEL_H), (x1, y1), (ox, y1)]),
        ("left", [(0, 0), (ox, oy), (ox, y1), (0, PANEL_H)]),
        ("right", [(PANEL_W, 0), (x1, oy), (x1, y1), (PANEL_W, PANEL_H)]),
    ):
        draw.polygon(poly, fill=levels[side])

    return canvas, levels


def _blur_mat(img: Image.Image) -> Image.Image:
    """The image itself, enlarged past all reason and blurred into a wash.

    The alternative to a flat mat, for when the picture should look like it
    continues rather than like it is hung. Blur is what makes this work here:
    it is the one operation that *guarantees* a low-frequency background, and
    low frequency is exactly what error diffusion renders well. Smearing the
    edge row outward instead — the obvious version of this idea — leaves long
    streaks that the dither turns into scan lines.
    """
    cover = ImageOps.fit(img, (PANEL_W, PANEL_H), method=Image.LANCZOS,
                         centering=(0.5, 0.5))
    washed = cover.filter(ImageFilter.GaussianBlur(PANEL_W / BLUR_DIVISOR))
    return ImageEnhance.Contrast(washed).enhance(BLUR_CONTRAST)


def mat(img: Image.Image, style: str = "edges") -> Image.Image:
    """Centre a smaller image on the panel and fill what is left around it."""
    if img.size == (PANEL_W, PANEL_H):
        return img

    ox = (PANEL_W - img.width) // 2
    oy = (PANEL_H - img.height) // 2

    if style == "none":
        canvas, levels = Image.new("L", (PANEL_W, PANEL_H), 255), None
    elif style == "blur":
        canvas, levels = _blur_mat(img), None
    else:
        canvas, levels = _edge_mat(img, ox, oy)

    canvas.paste(img, (ox, oy))

    if levels:
        # A hairline against each sector, so the picture reads as framed rather
        # than as one that failed to fill the screen. Free at four levels, and
        # the only place a hard edge is wanted.
        draw = ImageDraw.Draw(canvas)
        x1, y1 = ox + img.width - 1, oy + img.height - 1
        for side, line in (("top", [(ox - 1, oy - 1), (x1 + 1, oy - 1)]),
                           ("bottom", [(ox - 1, y1 + 1), (x1 + 1, y1 + 1)]),
                           ("left", [(ox - 1, oy - 1), (ox - 1, y1 + 1)]),
                           ("right", [(x1 + 1, oy - 1), (x1 + 1, y1 + 1)])):
            draw.line(line, fill=0 if levels[side] >= 170 else 255)

    return canvas


def tone(img: Image.Image) -> Image.Image:
    """Stretch, lift, sharpen — the three things that decide whether four grey
    levels read as a photograph or as mud."""
    img = ImageOps.autocontrast(img, cutoff=AUTOCONTRAST_CUTOFF)
    lut = [min(255, round(255.0 * (i / 255.0) ** GAMMA)) for i in range(256)]
    img = img.point(lut)
    return ImageEnhance.Sharpness(img).enhance(UNSHARP_AMOUNT)


def _nearest(value: float) -> int:
    """Nearest of the four levels, as an index. The panel has no fifth state, so
    values outside 0..255 (error diffusion overshoots freely) just clamp."""
    if value <= 42.5:
        return 0
    if value <= 127.5:
        return 1
    if value <= 212.5:
        return 2
    return 3


def dither(img: Image.Image, algorithm: str = "floyd") -> bytearray:
    """8-bit grey -> one 2-bit level index per pixel.

    Floyd-Steinberg with serpentine scanning is the default and the reason the
    output looks like an image rather than a poster. Atkinson (the firmware's
    own choice, for covers) diffuses only 3/4 of the error: crisper, more
    contrast, and it will happily clip a gradient sky to flat white. 'none' is
    for line art that is already four-tone.
    """
    w, h = img.size
    buf = [float(v) for v in img.tobytes()]
    out = bytearray(w * h)

    if algorithm == "none":
        for i, v in enumerate(buf):
            out[i] = _nearest(v)
        return out

    for y in range(h):
        row = y * w
        rightward = (y % 2 == 0)
        xs = range(w) if rightward else range(w - 1, -1, -1)
        step = 1 if rightward else -1
        for x in xs:
            i = row + x
            old = buf[i]
            level = _nearest(old)
            out[i] = level
            err = old - LEVELS[level]
            if not err:
                continue

            if algorithm == "atkinson":
                share = err / 8.0
                for dx, dy in ((1, 0), (2, 0), (-1, 1), (0, 1), (1, 1), (0, 2)):
                    nx, ny = x + dx * step, y + dy
                    if 0 <= nx < w and ny < h:
                        buf[ny * w + nx] += share
            else:  # Floyd-Steinberg
                nx = x + step
                if 0 <= nx < w:
                    buf[i + step] += err * 7 / 16
                if y + 1 < h:
                    below = i + w
                    bx = x - step
                    if 0 <= bx < w:
                        buf[below - step] += err * 3 / 16
                    buf[below] += err * 5 / 16
                    bx = x + step
                    if 0 <= bx < w:
                        buf[below + step] += err * 1 / 16
    return out


def encode_bmp4(levels: bytearray, w: int, h: int) -> bytes:
    """Pack level indices into a 4-bpp indexed BMP the firmware maps straight
    through.

    Written by hand rather than by Pillow (which cannot emit 4-bpp at all), and
    written to a *40-byte* BITMAPINFOHEADER on purpose: the firmware reads the
    palette from the fixed offset right after those 40 bytes, so a V4/V5 header
    would feed it colour-space fields as if they were colours. Rows go bottom-up,
    the ordinary BMP convention, so a desktop viewer shows the same image the
    reader will.
    """
    row_bytes = (w * 4 + 31) // 32 * 4

    # 16 entries, not 4: some viewers assume a full 2**bpp table. The unused
    # twelve are black, which is also a native level, so the firmware's
    # native-palette test still passes.
    palette = bytearray()
    for i in range(16):
        v = LEVELS[i] if i < 4 else 0
        palette += bytes((v, v, v, 0))

    off_bits = 14 + 40 + len(palette)
    pixel_bytes = row_bytes * h

    header = struct.pack("<2sIHHI", b"BM", off_bits + pixel_bytes, 0, 0, off_bits)
    dib = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 4, 0, pixel_bytes,
                      2835, 2835, 16, 16)

    rows = []
    for y in range(h - 1, -1, -1):          # bottom-up
        base = y * w
        row = bytearray(row_bytes)
        for x in range(0, w, 2):
            hi = levels[base + x]
            lo = levels[base + x + 1] if x + 1 < w else 0
            row[x >> 1] = (hi << 4) | lo
        rows.append(bytes(row))

    return bytes(header) + bytes(dib) + bytes(palette) + b"".join(rows)


def levels_to_image(levels: bytearray, w: int, h: int) -> Image.Image:
    """The dithered result as an ordinary grey PNG — what the panel will show,
    for looking at on a computer."""
    img = Image.new("L", (w, h))
    img.putdata([LEVELS[v] for v in levels])
    return img


def render(src: Path, *, fit: str = "cover", mat_style: str = "edges",
           algorithm: str = "floyd") -> bytearray:
    """Source file -> one 2-bit level per panel pixel. The whole pipeline, in
    one place, so the self-test grades the same path the converter writes.

    Tone comes *before* the mat on purpose: autocontrast measures the picture,
    and a large flat border in the histogram would skew the stretch it chooses.
    The mat is then computed from the toned image, so its levels match the
    pixels it is about to sit against.
    """
    img = mat(tone(scale_to_panel(load_grayscale(src), fit)), mat_style)
    return dither(img, algorithm)


def convert(src: Path, out_dir: Path, *, fit: str = "cover",
            mat_style: str = "edges", algorithm: str = "floyd",
            preview: bool = False) -> Path:
    levels = render(src, fit=fit, mat_style=mat_style, algorithm=algorithm)

    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / (sanitize_stem(src.stem) + ".bmp")
    dest.write_bytes(encode_bmp4(levels, PANEL_W, PANEL_H))
    if preview:
        levels_to_image(levels, PANEL_W, PANEL_H).save(dest.with_suffix(".png"))
    return dest


def collect(inputs: list) -> list:
    """Files to convert. A directory contributes the images directly inside it —
    not `build/`, and not recursively, so re-running never eats its own output."""
    found = []
    for item in inputs:
        if item.is_dir():
            found += sorted(p for p in item.iterdir()
                            if p.is_file() and p.suffix.lower() in SOURCE_SUFFIXES)
        elif item.is_file():
            found.append(item)
        else:
            print(f"make_wallpaper: no such file or directory: {item}", file=sys.stderr)
    return found


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="*", type=Path,
                    help=f"images or folders (default: {DEFAULT_IN.relative_to(REPO_ROOT)}/)")
    ap.add_argument("--out", type=Path, help="output folder (default: <input>/build)")
    ap.add_argument("--fit", choices=("cover", "contain"), default="cover",
                    help="cover: fill the panel, crop the overflow (default). "
                         "contain: keep the whole frame, and mat the rest")
    ap.add_argument("--mat", choices=("edges", "blur", "none"), default="edges",
                    dest="mat_style",
                    help="what surrounds an image too small to fill the panel. "
                         "edges: four sectors, each continuing the edge it "
                         "touches (default). blur: the image itself, enlarged "
                         "and washed out. none: plain white")
    ap.add_argument("--dither", choices=("floyd", "atkinson", "none"), default="floyd",
                    help="you should not need this; floyd is the default for good reason")
    ap.add_argument("--preview", action="store_true",
                    help="also write a PNG of the dithered result, to look at")
    args = ap.parse_args()

    inputs = args.inputs or [DEFAULT_IN]
    if not args.inputs and not DEFAULT_IN.exists():
        DEFAULT_IN.mkdir(parents=True, exist_ok=True)
        print(f"make_wallpaper: created {DEFAULT_IN.relative_to(REPO_ROOT)}/ — "
              "drop images in it and run this again.")
        return 0

    sources = collect(inputs)
    if not sources:
        where = ", ".join(str(i) for i in inputs)
        print(f"make_wallpaper: no images found in {where}", file=sys.stderr)
        return 1

    if args.out:
        out_dir = args.out
    elif args.inputs and args.inputs[0].is_dir():
        out_dir = args.inputs[0] / "build"
    elif args.inputs:
        out_dir = args.inputs[0].resolve().parent / "build"
    else:
        out_dir = DEFAULT_OUT

    for src in sources:
        try:
            dest = convert(src, out_dir, fit=args.fit, mat_style=args.mat_style,
                           algorithm=args.dither, preview=args.preview)
        except Exception as exc:                      # unreadable / truncated / odd
            print(f"  {src.name}: FAILED — {exc}", file=sys.stderr)
            continue
        print(f"  {src.name} -> {dest}  ({PANEL_W}x{PANEL_H}, 4 levels, "
              f"{dest.stat().st_size // 1024} KB)")

    print(f"\n{len(sources)} image(s) -> {out_dir}")
    print("Next: python3 wallpaper-maker/scripts/push_wallpaper.py "
          "(device: Home -> File Transfer -> Join a Network)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
