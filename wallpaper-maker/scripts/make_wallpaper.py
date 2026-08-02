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
                         filled: four sectors mitred from the panel corners,
                         each continuing the image edge it touches. By default
                         the edge *travels* into its sector along a wandering
                         path (--mat waves), so the picture appears to carry on
                         in all four directions through rippled glass; --mat
                         edges is the quiet version, a flat level per sector.
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
  make_wallpaper.py small.png --waves     # ripple a small one out to the edges

Then get them onto the reader with `push_wallpaper.py`, which is the part OPDS
cannot do for you (see SKILL.md).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
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
WAVE_PERIOD = (90.0, 220.0)  # --mat waves: px between crests, per component
WAVE_COMPONENTS = 3         # ... summed, so the ripple is organic rather than a sine
WAVE_SOURCE_DEPTH = 3       # ... edge pixels averaged into the travelling line
# How steep each component is allowed to get, in pixels across per pixel out.
# This is the knob, and amplitude follows from it — a strand can only move one
# across per step, so asking a short wave for a tall crest just pins it at 45
# degrees and the "wave" comes out a zigzag. Three components at 0.35 keep the
# combined slope under 1, so the walk follows the curve instead of saturating.
WAVE_SLOPE = 0.35


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


def _mitres(ox: int, oy: int, iw: int, ih: int) -> dict:
    """The four mat sectors, split from each panel corner to the image corner
    it faces. Shared by every mat style, so they all frame the same way."""
    x1, y1 = ox + iw, oy + ih
    return {
        "top": [(0, 0), (PANEL_W, 0), (x1, oy), (ox, oy)],
        "bottom": [(0, PANEL_H), (PANEL_W, PANEL_H), (x1, y1), (ox, y1)],
        "left": [(0, 0), (ox, oy), (ox, y1), (0, PANEL_H)],
        "right": [(PANEL_W, 0), (x1, oy), (x1, y1), (PANEL_W, PANEL_H)],
    }


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

    canvas = Image.new("L", (PANEL_W, PANEL_H), levels["top"])
    draw = ImageDraw.Draw(canvas)
    for side, poly in _mitres(ox, oy, iw, ih).items():
        draw.polygon(poly, fill=levels[side])

    return canvas


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


def _wave_walk(span: int, rng: random.Random) -> list:
    """The path one strand of edge takes as it travels outward.

    Literally the walk you would draw by hand: step out one pixel at a time and
    go diagonally up, diagonally down, or straight ahead — never more than one
    across per step. What decides which is a sum of sines, so the wandering
    comes out as *waves* rather than as noise: where the curve is steep you get
    a run of diagonals, where it flattens you get a run of straights.

    The one-per-step rule is not a detail, it is the whole guarantee. Strands
    that never separate by more than a pixel stay neighbours the whole way out,
    so nothing tears open behind them and no pixel is left unpainted. It also
    means a thin margin self-limits: the wave can only be as tall as the
    distance it has had to climb.

    Each component's height therefore *follows* from its length rather than
    being chosen: a crest a strand cannot climb in the space available is a
    crest that comes out as a 45-degree zigzag instead of a wave. So the knob
    is steepness, and amplitude is period * slope / 2pi.
    """
    parts = []
    for _ in range(WAVE_COMPONENTS):
        period = rng.uniform(*WAVE_PERIOD)
        parts.append((period * WAVE_SLOPE / (2 * math.pi), period))

    walk, cur = [], 0
    for d in range(span + 1):
        target = sum(a * math.sin(2 * math.pi * d / p) for a, p in parts)
        step = round(target)
        cur += 1 if step > cur else (-1 if step < cur else 0)
        walk.append(cur)
    return walk


def _edge_line(img: Image.Image, box, size) -> bytes:
    """One edge of the image as a single line of pixels.

    Averaged over a few pixels' depth (BOX resampling is a plain mean) so that
    one noisy pixel does not become a full-length streak.
    """
    return img.crop(box).resize(size, Image.BOX).tobytes()


def _flow(line: bytes, extent: int, count: int, origin: int, walk: list,
          distance) -> bytes:
    """Send `line` outward along `walk`: `count` copies of it, each `extent`
    long and displaced by where the walk has got to.

    `distance(i)` is how far the i-th copy sits outside the image edge. Lookups
    past either end of the line clamp to its end pixels, which is what carries
    the fill into the panel corners with nothing left unpainted.
    """
    amp = max((abs(s) for s in walk), default=0)
    span = len(line)
    extended = bytes(line[min(max(j - amp - origin, 0), span - 1)]
                     for j in range(extent + 2 * amp))

    out = bytearray()
    for i in range(count):
        d = distance(i)
        shift = amp + walk[d if 0 <= d < len(walk) else 0]
        out += extended[shift:shift + extent]
    return bytes(out)


def _waves_mat(img: Image.Image, ox: int, oy: int) -> Image.Image:
    """Each edge, distorted outward — rippled glass around the picture.

    Every side sends its own edge into its own sector, so the picture appears
    to keep going in all four directions while wobbling as it travels. The
    sectors still meet on the mitres, which is what stops four independent
    ripples from turning into mush: it reads as four panels of distorted glass
    in a frame, rather than as one confused wash.

    Unlike the flat mat, this one has no snapped level: the picture and its
    border are the same pixels, so there is nothing to round to.

    The wave shape is seeded from the image itself, so each wallpaper ripples
    its own way and does so identically on every run.
    """
    iw, ih = img.size
    rng = random.Random(hashlib.sha1(img.tobytes()).digest())
    depth = max(1, min(WAVE_SOURCE_DEPTH, iw // 2, ih // 2))

    x1, y1 = ox + iw - 1, oy + ih - 1
    sides = {
        # side: (edge line, line length, copies, where the line starts, how far out)
        "top": (_edge_line(img, (0, 0, iw, depth), (iw, 1)),
                PANEL_W, PANEL_H, ox, lambda y: oy - y),
        "bottom": (_edge_line(img, (0, ih - depth, iw, ih), (iw, 1)),
                   PANEL_W, PANEL_H, ox, lambda y: y - y1),
        "left": (_edge_line(img, (0, 0, depth, ih), (1, ih)),
                 PANEL_H, PANEL_W, oy, lambda x: ox - x),
        "right": (_edge_line(img, (iw - depth, 0, iw, ih), (1, ih)),
                  PANEL_H, PANEL_W, oy, lambda x: x - x1),
    }

    canvas = Image.new("L", (PANEL_W, PANEL_H))
    mitres = _mitres(ox, oy, iw, ih)
    for side, (line, extent, count, origin, distance) in sides.items():
        walk = _wave_walk(max(PANEL_W, PANEL_H), rng)
        raw = _flow(line, extent, count, origin, walk, distance)

        if side in ("top", "bottom"):
            field = Image.frombytes("L", (PANEL_W, PANEL_H), raw)
        else:
            # Built column-major — one row per panel column — then stood upright.
            field = Image.frombytes("L", (PANEL_H, PANEL_W), raw) \
                         .transpose(Image.TRANSPOSE)

        mask = Image.new("L", (PANEL_W, PANEL_H), 0)
        ImageDraw.Draw(mask).polygon(mitres[side], fill=255)
        canvas.paste(field, (0, 0), mask)

    return canvas


def mat(img: Image.Image, style: str = "waves") -> Image.Image:
    """Centre a smaller image on the panel and fill what is left around it.

    Nothing is drawn between the picture and its border. Every style here works
    by continuing the edge outward, so a rule around the image would cut across
    the one join the mat exists to make — and at four levels a hairline is not
    a hairline, it is a hard black or white line against whatever it sits on.
    """
    if img.size == (PANEL_W, PANEL_H):
        return img

    ox = (PANEL_W - img.width) // 2
    oy = (PANEL_H - img.height) // 2

    if style == "none":
        canvas = Image.new("L", (PANEL_W, PANEL_H), 255)
    elif style == "blur":
        canvas = _blur_mat(img)
    elif style == "waves":
        canvas = _waves_mat(img, ox, oy)
    else:
        canvas = _edge_mat(img, ox, oy)

    canvas.paste(img, (ox, oy))
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


def render(src: Path, *, fit: str = "cover", mat_style: str = "waves",
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


def probe(src: Path, *, fit: str = "cover") -> dict:
    """Will this image fill the panel, or does it need a mat?

    Asked by callers that want to offer the mat choice only when there is one
    to make — `tgbot/` puts it to you as a button. The answer is not "is the
    image smaller than 528x792": a 500x750 photo is smaller in both dimensions
    and still fills, because it only has to grow 1.06x and MAX_UPSCALE allows
    1.5x. Nor is a small overflow matted, since MIN_MAT_AREA rules a sliver out.
    Both thresholds are judgement calls that live here, so the question is
    answered by running the real thing rather than by re-deriving it elsewhere.
    """
    img = load_grayscale(src)
    scaled = scale_to_panel(img, fit)
    return {"file": str(src), "width": img.width, "height": img.height,
            "fills": scaled.size == (PANEL_W, PANEL_H),
            "panel": [PANEL_W, PANEL_H]}


def convert(src: Path, out_dir: Path, *, fit: str = "cover",
            mat_style: str = "waves", algorithm: str = "floyd",
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
    ap.add_argument("--mat", choices=("waves", "edges", "blur", "none"),
                    default="waves", dest="mat_style",
                    help="what surrounds an image too small to fill the panel. "
                         "waves: each edge sent outward along a wandering path, "
                         "like rippled glass (default). edges: four flat "
                         "sectors, each on the level of the edge it touches. "
                         "blur: the image itself, enlarged and washed out. "
                         "none: plain white")
    ap.add_argument("--waves", action="store_const", const="waves",
                    dest="mat_style", help="the default; spelled out")
    ap.add_argument("--dither", choices=("floyd", "atkinson", "none"), default="floyd",
                    help="you should not need this; floyd is the default for good reason")
    ap.add_argument("--preview", action="store_true",
                    help="also write a PNG of the dithered result, to look at")
    ap.add_argument("--probe", action="store_true",
                    help="write nothing; report as JSON whether each image "
                         "fills the panel or needs a mat, so a caller can ask "
                         "about the mat only when there is a choice")
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

    if args.probe:
        json.dump([probe(s, fit=args.fit) for s in sources],
                  sys.stdout, ensure_ascii=False)
        return 0

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
