#!/usr/bin/env python3
"""The gate: would the X3 draw exactly the pixels we computed?

Run after changing anything in this directory:

    .venv/bin/python wallpaper-maker/scripts/selftest.py

A wallpaper has three ways to fail, and only the first is visible from a
desktop: the file is broken, the device never opens it, or the device opens it
and *redoes the work* — re-dithering our four-level image with the ESP32's
integer approximation, which is exactly what we spent the CPU here to avoid.
Nothing in a normal image-validity check catches the second or third, so this
grades every output through `crosspoint_bmp`, a port of the firmware's own
folder scan and BMP reader.

Checks, in order:
  1. sources of every awkward shape convert at all
  2. the firmware's folder scan would open the file we wrote
  3. its BMP parser accepts the headers, and reads the palette we meant
  4. the palette is *native*, so the device maps pixels through and dithers none
  5. the file decodes, through the device's own row unpacking, to the exact
     levels we computed — bit for bit
  6. it lands at 0,0 unscaled: the panel's size, so nothing is resampled
  7. a source too small to fill is framed, and the frame draws flat
  8. the same source converts to a byte-identical file, twice
  9. the two failure modes we designed around really are failure modes
 10. the push protocol drives the firmware's file-transfer API correctly
"""

from __future__ import annotations

import io
import os
import json
import random
import struct
import subprocess
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import crosspoint_bmp as cp
import make_wallpaper as mw
from PIL import Image, ImageDraw

SCRIPTS = Path(__file__).resolve().parent

failures = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)
    return ok


# ---------------------------------------------------------------- fixtures ---
# Deliberately awkward: the shapes that break a naive converter. A wide
# landscape (cover-crop must take the middle, not the left edge), an image
# smaller than the panel (must be scaled *up* — the device will not do it), an
# alpha image (must flatten, not multiply into black), a phone-style EXIF
# rotation (must rotate before cropping, or the crop takes the wrong axis), and
# a flat gradient (where error diffusion either works or bands visibly).

def make_sources(directory: Path) -> list:
    made = []

    grad = Image.new("RGB", (1200, 1600))
    px = grad.load()
    for y in range(1600):
        for x in range(0, 1200, 4):
            v = int(255 * (x / 1200 * 0.5 + y / 1600 * 0.5))
            for dx in range(4):
                px[x + dx, y] = (v, v, v)
    grad.save(directory / "gradient.png")
    made.append(directory / "gradient.png")

    wide = Image.new("RGB", (2400, 1000), (30, 90, 160))
    for i in range(0, 2400, 60):
        for y in range(1000):
            for x in range(i, min(i + 30, 2400)):
                wide.putpixel((x, y), (220, 200, 40))
    wide.save(directory / "wide.jpg", quality=92)
    made.append(directory / "wide.jpg")

    tiny = Image.new("RGB", (100, 140), (200, 200, 200))
    tiny.putpixel((50, 70), (0, 0, 0))
    tiny.save(directory / "tiny.png")
    made.append(directory / "tiny.png")

    alpha = Image.new("RGBA", (600, 900), (0, 0, 0, 0))
    for y in range(300, 600):
        for x in range(200, 400):
            alpha.putpixel((x, y), (10, 10, 10, 255))
    alpha.save(directory / "alpha.png")
    made.append(directory / "alpha.png")

    # Small, and split light-over-dark: the case the mat exists for. Each
    # sector must take its level from the edge it touches, so top and bottom
    # cannot come out the same.
    split = Image.new("RGB", (200, 260), (245, 245, 245))
    for y in range(130, 260):
        for x in range(200):
            split.putpixel((x, y), (18, 18, 18))
    split.save(directory / "split.png")
    made.append(directory / "split.png")

    rotated = Image.new("RGB", (1600, 1200), (140, 140, 140))
    for x in range(1600):                       # a bright band along the long edge
        for y in range(0, 120):
            rotated.putpixel((x, y), (250, 250, 250))
    exif = Image.Exif()
    exif[0x0112] = 6                            # "rotate 90 CW", the phone default
    rotated.save(directory / "rotated.jpg", exif=exif, quality=92)
    made.append(directory / "rotated.jpg")

    return made


# ------------------------------------------------------- a pretend X3 ---------
# Ported from src/network/CrossPointWebServer.cpp: not a mock of what we wish
# the endpoints did, but of what they do — including the two behaviours that
# shape push_wallpaper.py.

class FakeDevice(BaseHTTPRequestHandler):
    root: Path = None
    show_hidden = False
    settings: dict = {}

    def log_message(self, *a):
        pass

    def _sd(self, path: str) -> Path:
        return self.root / path.lstrip("/")

    def _args(self) -> dict:
        query = urllib.parse.urlparse(self.path).query
        return {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}

    def _send(self, code: int, body: bytes, ctype="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        route = urllib.parse.urlparse(self.path).path
        if route == "/api/status":
            return self._send(200, json.dumps(
                {"version": "1.5.0", "model": "Xteink X3", "ip": "127.0.0.1"}
            ).encode(), "application/json")
        if route == "/api/files":
            target = self._sd(self._args().get("path", "/"))
            entries = []
            if target.is_dir():
                for item in sorted(target.iterdir()):
                    # scanFiles() hides dot-prefixed entries unless the device's
                    # showHiddenFiles setting is on.
                    if item.name.startswith(".") and not self.show_hidden:
                        continue
                    entries.append({"name": item.name,
                                    "size": 0 if item.is_dir() else item.stat().st_size,
                                    "isDirectory": item.is_dir(),
                                    "isEpub": item.suffix.lower() == ".epub"})
            return self._send(200, json.dumps(entries).encode(), "application/json")
        return self._send(404, b"Not found")

    def do_POST(self):
        route = urllib.parse.urlparse(self.path).path
        args = self._args()
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""

        if route == "/mkdir":
            target = self._sd(args.get("path", "/")) / args["name"]
            if target.exists():
                return self._send(400, b"Folder already exists")
            target.mkdir(parents=True)
            return self._send(200, b"Folder created")

        if route == "/delete":
            target = self._sd(args["path"])
            if not target.exists():
                return self._send(400, b"Not found")
            target.unlink() if target.is_file() else target.rmdir()
            return self._send(200, b"Deleted")

        if route == "/upload":
            directory = self._sd(args.get("path", "/"))
            name, payload = self._multipart(body)
            if not directory.is_dir():
                return self._send(400, b"Failed to create file on SD card")
            dest = directory / name
            # The firmware refuses a collision instead of overwriting.
            if dest.exists():
                return self._send(400, f"File already exists: {name}".encode())
            dest.write_bytes(payload)
            return self._send(200, f"File uploaded successfully: {name}".encode())

        if route == "/api/settings":
            self.settings.update(json.loads(body))
            return self._send(200, b"Settings applied")

        return self._send(404, b"Not found")

    def _multipart(self, body: bytes):
        ctype = self.headers.get("Content-Type", "")
        boundary = ctype.split("boundary=")[-1].encode()
        part = body.split(b"--" + boundary)[1]
        head, payload = part.split(b"\r\n\r\n", 1)
        name = head.split(b'filename="')[1].split(b'"')[0].decode()
        return name, payload.rsplit(b"\r\n", 1)[0]


def serve(root: Path):
    FakeDevice.root = root
    FakeDevice.settings = {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeDevice)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"127.0.0.1:{server.server_address[1]}"


# ------------------------------------------------------------------ checks ---

def grade_output(src: Path, bmp: Path, algorithm: str = "device") -> None:
    """Grade a written wallpaper through the firmware's own reader.

    Both routes ship a 4-bpp file with a native palette, so both get the strong
    contract: the file must decode back to exactly the levels chosen here. Only
    *who chose them* differs — the reader's own quantiser by default, our error
    diffusion otherwise.
    """
    data = bmp.read_bytes()
    label = bmp.name

    if not check(f"{label}: the folder scan would open it", cp.sleep_scan_accepts(bmp.name)):
        return

    try:
        hdr = cp.parse_headers(data)
    except cp.BmpReaderError as exc:
        check(f"{label}: the device's BMP parser accepts it", False, str(exc))
        return
    check(f"{label}: the device's BMP parser accepts it", True)

    check(f"{label}: exactly the panel, so nothing is resampled",
          (hdr.width, hdr.height) == (cp.PANEL_W, cp.PANEL_H),
          f"{hdr.width}x{hdr.height}")

    x, y, scaled = cp.placement(hdr)
    check(f"{label}: lands at 0,0 unscaled", (x, y, scaled) == (0, 0, False),
          f"x={x} y={y} scaled={scaled}")

    check(f"{label}: 4 bpp, greyscale pipeline", hdr.bpp == 4 and hdr.has_greyscale,
          f"bpp={hdr.bpp}")
    check(f"{label}: palette is native — the device re-dithers nothing",
          hdr.native_palette)

    # The one that matters on this route: recompute the pipeline, then read the
    # file back the way the firmware reads it. Equal means the panel gets our
    # pixels and nothing else.
    intended = mw.render(src, algorithm=algorithm)
    try:
        got = cp.decode_levels(data, hdr)
    except cp.BmpReaderError as exc:
        check(f"{label}: decodes to the levels we computed", False, str(exc))
        return
    same = len(got) == len(intended) and all(a == b for a, b in zip(got, intended))
    check(f"{label}: decodes to the levels we computed", same)


def _block(levels: list, x: int, y: int, n: int = 16) -> set:
    return {levels[(y + dy) * cp.PANEL_W + (x + dx)]
            for dy in range(n) for dx in range(n)}


def check_mat(sources: list) -> None:
    """The mat, on images too small to fill the panel.

    These are the `--mat edges` checks, asked explicitly rather than by
    default: the point of snapping each sector to a native level is that the
    mat draws *flat*, and a fill anywhere else dithers into a field of grain.
    So the check is literally that — a block of mat decodes to a single level.
    """
    tiny = next(s for s in sources if s.stem == "tiny")
    split = next(s for s in sources if s.stem == "split")

    edges = mw.render(tiny, mat_style="edges")
    check("mat: a small source is framed, not enlarged to fill",
          len(_block(edges, 8, 8)) == 1 and len(set(edges)) > 1)
    check("mat: --mat edges draws flat — no dither grain",
          all(len(_block(edges, x, y)) == 1
              for x, y in ((8, 8), (cp.PANEL_W - 24, 8),
                           (8, cp.PANEL_H - 24), (cp.PANEL_W - 24, cp.PANEL_H - 24))))

    # 200x260 stops at MAX_UPSCALE, so the picture sits centred with mat above
    # and below; sample the middle of each, clear of the mitres.
    two_tone = mw.render(split, mat_style="edges")
    top = _block(two_tone, cp.PANEL_W // 2 - 8, 16)
    bottom = _block(two_tone, cp.PANEL_W // 2 - 8, cp.PANEL_H - 32)
    check("mat: each sector follows its own edge, light over dark",
          len(top) == 1 and len(bottom) == 1 and top != bottom,
          f"top={top} bottom={bottom}")

    plain = mw.render(tiny, mat_style="none")
    check("mat: --mat none is plain white", _block(plain, 8, 8) == {3})

    # Graded on `split`, not `tiny`: a blur of a uniform source is uniform, so
    # it would land on the same flat level as the edge mat and prove nothing.
    blurred = mw.render(split, mat_style="blur")
    varied = any(len(_block(blurred, x, y)) > 1
                 for x, y in ((8, 8), (cp.PANEL_W - 24, cp.PANEL_H - 24)))
    check("mat: --mat blur is a wash, not a flat fill",
          varied and blurred != mw.render(split, mat_style="edges"))


def check_waves(sources: list) -> None:
    """--mat waves, and the property the whole construction rests on.

    A strand of edge may move at most one across per step out. That is what
    keeps neighbouring strands neighbours the whole way, so nothing tears open
    behind them — the fill is gap-free by construction rather than by patching
    holes afterwards.
    """
    walk = mw._wave_walk(600, random.Random(1))
    check("waves: the walk leaves the edge where it found it", walk[0] == 0)
    check("waves: never more than one across per step out — no tears",
          all(abs(b - a) <= 1 for a, b in zip(walk, walk[1:])))
    check("waves: it wanders far enough to see", max(abs(s) for s in walk) >= 8)
    check("waves: it wanders both ways",
          min(walk) < 0 < max(walk))

    # Nothing left unpainted, tested directly: the canvas starts black and every
    # painted pixel comes from a source with no black in it, so a single level-0
    # pixel anywhere is a hole.
    source = Image.new("L", (300, 400), 200)
    ImageDraw.Draw(source).rectangle([0, 0, 299, 30], fill=120)
    ImageDraw.Draw(source).rectangle([0, 370, 299, 399], fill=90)
    painted = mw.mat(source, "waves")     # mat() also pastes the picture back on
    check("waves: every pixel of the mat is painted — no holes",
          min(painted.tobytes()) > 0,
          f"{painted.tobytes().count(0)} unpainted pixels")

    ox, oy = (cp.PANEL_W - 300) // 2, (cp.PANEL_H - 400) // 2

    # The four sectors must tile the mat exactly, or the gap shows as black.
    covered = Image.new("L", (cp.PANEL_W, cp.PANEL_H), 0)
    for poly in mw._mitres(ox, oy, 300, 400).values():
        ImageDraw.Draw(covered).polygon(poly, fill=255)
    ImageDraw.Draw(covered).rectangle([ox, oy, ox + 299, oy + 399], fill=255)
    check("waves: the four sectors tile the panel with no seam left over",
          covered.tobytes().count(0) == 0,
          f"{covered.tobytes().count(0)} px uncovered")

    # Through the whole pipeline: still a wave, still the same one every time,
    # and still what you get without asking, since waves is the default.
    # `split`, not `tiny`: waves only show where the edge varies along its
    # length, so a uniform source ripples to a uniform field by design and would
    # make this check pass or fail for the wrong reason.
    split = next(s for s in sources if s.stem == "split")
    rippled = mw.render(split, mat_style="waves")
    check("waves: rippled, not flat like the edge mat",
          rippled != mw.render(split, mat_style="edges"))
    check("waves: the same source ripples the same way twice",
          rippled == mw.render(split, mat_style="waves"))
    check("waves: it is what a plain run produces", rippled == mw.render(split))


def repeat_score(levels, w: int, h: int, reach: int = 6) -> float:
    """Strongest excess-over-chance self-match when the pattern is slid.

    A lattice matches itself when slid by one cell; grain matches itself at no
    shift but its own. Returned 0..1, where 0 is "no repeating structure".
    Deliberately stdlib — the gate must not need numpy when the tool does not.
    """
    n = len(levels)
    p = sum(1 for v in levels if v) / n
    chance = p * p + (1 - p) * (1 - p)
    if chance >= 0.999:                       # nothing dithered; nothing to repeat
        return 0.0
    best = 0.0
    for dy in range(0, reach + 1):
        for dx in range(-reach, reach + 1):
            if dy == 0 and dx <= 0:
                continue
            if abs(dx) <= 1 and dy <= 1:      # touching neighbours are not a repeat
                continue
            match = total = 0
            for y in range(0, h - dy):
                r1, r2 = y * w, (y + dy) * w
                for x in range(max(0, -dx), min(w, w - dx)):
                    total += 1
                    if levels[r1 + x] == levels[r2 + x + dx]:
                        match += 1
            best = max(best, (match / total - chance) / (1 - chance))
    return best


def check_flat_tones() -> None:
    """No flat area may come out as a lattice.

    This is the regression that a real wallpaper found: error diffusion is
    deterministic, so a large near-constant area does not become grain, it locks
    into a regular grid of minority pixels. A near-black night sky did it, and
    nothing in "is this a valid BMP" or "does the device draw it" could see it.

    Asserted only on the tones the code actually claims to fix — the ones its
    own shaping curve perturbs. Halfway between two levels the dither *should*
    repeat: that is a one-pixel checkerboard, finer than the eye resolves and
    the smoothest thing the panel can draw. A gate that demanded no repeat
    there would be demanding a worse picture.
    """
    graded = worst = 0
    worst_tone = None
    for tone in range(4, 256, 6):
        if mw._SPARSITY[tone] <= 0.5:          # checkerboard territory, left alone
            continue
        graded += 1
        levels = mw.dither(Image.new("L", (56, 56), tone))
        score = repeat_score(levels, 56, 56)
        if score > worst:
            worst, worst_tone = score, tone
    check(f"flat tones dither to grain, not a lattice ({graded} tones graded)",
          worst < 0.25, f"worst {worst:.2f} at tone {worst_tone}")

    # The other half of the contract: a tone sitting exactly on a level is not
    # dithered at all, so the nudge must not speckle it.
    for level in (0, 85, 170, 255):
        levels = mw.dither(Image.new("L", (48, 48), level))
        if not check(f"a solid area at level {level} stays solid",
                     len(set(levels)) == 1, f"{len(set(levels))} levels present"):
            break


def check_tuning() -> None:
    """The firmware ships two constant sets and runs the wrong one for an X3.

    `AtkinsonDitherer::processPixel` carries an even ramp behind `if (false)`
    and an override labelled "fine-tuned to X4 eink display". We run the even
    one, and this is the check that says why: on a real photograph the X4
    branch pushes the panel mean far above the tone we asked for, because its
    reconstruction values sit below what an X3 actually shows, so every pixel
    charges its neighbours too positive an error.

    Device-confirmed 2026-08: the X4 branch is visibly too bright on an X3.
    """
    panel = Image.new("L", (cp.PANEL_W, cp.PANEL_H))
    px = []
    for y in range(cp.PANEL_H):                 # a full-range vertical ramp
        px += [min(255, y * 255 // cp.PANEL_H)] * cp.PANEL_W
    panel.putdata(px)
    asked = sum(px) / len(px)

    def panel_mean(tuning):
        lv = cp.firmware_quantise(panel.tobytes(), cp.PANEL_W, cp.PANEL_H, tuning=tuning)
        return sum(cp.NATIVE_LEVELS[v] for v in lv) / len(lv)

    even, x4 = panel_mean("even"), panel_mean("x4")
    check("the tuning we use tracks the tone asked for",
          abs(even - asked) < 12, f"asked {asked:.0f}, got {even:.0f}")
    check("the firmware's live 'X4' branch would run much brighter",
          x4 - even > 20, f"x4 {x4:.0f} vs even {even:.0f}")


def check_designed_failures() -> None:
    """The two traps the encoder is shaped around. If these ever stop failing,
    the reasoning in make_wallpaper.py has gone stale and should be re-read."""
    # Something with all four levels in it, so a misread cannot coincide with
    # the truth the way an all-black image would.
    levels = bytearray((x + y) % 4 for y in range(cp.PANEL_H) for x in range(cp.PANEL_W))
    good = mw.encode_bmp4(levels, cp.PANEL_W, cp.PANEL_H)

    # 1. A real BITMAPV4HEADER: 68 more DIB bytes (masks, colour space, gamma)
    #    before the palette. The firmware still reads the palette from the fixed
    #    offset after the first 40, so those fields become the "colours".
    v4 = bytearray(good)
    v4[54:54] = bytes(68)
    struct.pack_into("<I", v4, 14, 108)                       # biSize
    (off_bits,) = struct.unpack_from("<I", v4, 10)
    struct.pack_into("<I", v4, 10, off_bits + 68)             # bfOffBits
    struct.pack_into("<I", v4, 2, len(v4))                    # bfSize
    try:
        hdr = cp.parse_headers(bytes(v4))
        misread = cp.decode_levels(bytes(v4), hdr) != list(levels)
    except cp.BmpReaderError:
        misread = True
    check("a 108-byte DIB header would be misread (why we emit 40)", misread)

    # 2. A 24-bpp file. No palette, so the native test cannot pass and the
    #    firmware dithers it itself — on an ESP32, over our finished work.
    grey = Image.new("RGB", (cp.PANEL_W, cp.PANEL_H), (128, 128, 128))
    buf = io.BytesIO()
    grey.save(buf, "BMP")
    hdr24 = cp.parse_headers(buf.getvalue())
    check("a 24-bpp file would be re-dithered on-device (why we emit 4-bpp)",
          hdr24.bpp == 24 and not hdr24.native_palette)


def check_push(build_dir: Path) -> None:
    with tempfile.TemporaryDirectory() as sd_dir:
        sd = Path(sd_dir)
        server, host = serve(sd)
        remembered = sd / "last-device.json"
        env = {**os.environ, "X3_LAST_DEVICE": str(remembered)}
        try:
            run = lambda *extra, addr=("--ip", host): subprocess.run(
                [sys.executable, str(SCRIPTS / "push_wallpaper.py"),
                 str(build_dir), *addr, *extra],
                capture_output=True, text=True, timeout=60, env=env)

            # Looking must not change anything: creating /.sleep just to read it
            # would shadow whatever the device keeps in /sleep.
            listed = run("--list")
            check("push: --list leaves the device alone",
                  listed.returncode == 0 and not (sd / ".sleep").exists(),
                  listed.stderr.strip())

            first = run()
            pushed = sorted(p.name for p in (sd / ".sleep").glob("*.bmp")) \
                if (sd / ".sleep").is_dir() else []
            expected = sorted(p.name for p in build_dir.glob("*.bmp"))
            check("push: creates /.sleep and uploads every wallpaper",
                  first.returncode == 0 and pushed == expected,
                  first.stderr.strip() or f"{pushed} != {expected}")
            check("push: switches the sleep screen to Custom",
                  FakeDevice.settings.get("sleepScreen") == 2,
                  str(FakeDevice.settings))

            # The firmware rejects an upload onto an existing name, so a second
            # push must delete first rather than silently doing nothing.
            marker = sorted((sd / ".sleep").glob("*.bmp"))[0]
            marker.write_bytes(b"stale")
            again = run()
            check("push: replaces a file already on the device",
                  again.returncode == 0 and marker.read_bytes() != b"stale",
                  again.stderr.strip())

            cleared = run("--replace")
            check("push: --replace clears the folder first",
                  cleared.returncode == 0
                  and sorted(p.name for p in (sd / ".sleep").glob("*.bmp")) == expected,
                  cleared.stderr.strip())

            # --ip is meant to be a one-off: the address that answered is
            # written down, and the next run finds the reader without being
            # told where it is.
            check("push: the address that worked is remembered",
                  remembered.is_file()
                  and json.loads(remembered.read_text()).get("host") == host,
                  remembered.read_text() if remembered.exists() else "not written")

            blind = run("--list", addr=())
            check("push: a later run finds the reader with no address given",
                  blind.returncode == 0 and host in blind.stdout,
                  blind.stderr.strip() or blind.stdout.strip())
        finally:
            server.shutdown()

    # A device that already keeps wallpapers in the visible /sleep must not have
    # them shadowed by a /.sleep we created.
    with tempfile.TemporaryDirectory() as sd_dir:
        sd = Path(sd_dir)
        (sd / "sleep").mkdir()
        (sd / "sleep" / "existing.bmp").write_bytes(b"x")
        server, host = serve(sd)
        try:
            result = subprocess.run(
                [sys.executable, str(SCRIPTS / "push_wallpaper.py"), str(build_dir),
                 "--host", host], capture_output=True, text=True, timeout=60)
            check("push: uses /sleep when the device already reads it",
                  result.returncode == 0 and not (sd / ".sleep").exists()
                  and len(list((sd / "sleep").glob("*.bmp"))) > 1,
                  result.stderr.strip())
        finally:
            server.shutdown()


def main() -> int:
    print(__doc__.split("Checks, in order:")[0].strip().splitlines()[0])
    print()

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        sources = make_sources(work)
        build = work / "build"

        print("converting:")
        outputs = []
        for src in sources:
            try:
                outputs.append((src, mw.convert(src, build)))
                print(f"  ok    {src.name}")
            except Exception as exc:
                check(f"{src.name}: converts", False, str(exc))

        print("\nthrough the device's own reader — the default route:")
        for src, bmp in outputs:
            grade_output(src, bmp)

        # The 4-bpp route is still supported and still has to hold its much
        # stronger contract: the panel gets exactly the pixels we chose.
        print("\n... and the route that quantises with our own dither (--dither floyd):")
        for src in sources[:3]:
            bmp = mw.convert(src, build / "floyd", algorithm="floyd")
            grade_output(src, bmp, algorithm="floyd")

        print("\nthe mat, on sources too small to fill the panel:")
        check_mat(sources)

        print("\n--mat waves:")
        check_waves(sources)

        print("\nflat areas, where error diffusion locks into a pattern:")
        check_flat_tones()

        print("\nwhich of the firmware's two tunings we run:")
        check_tuning()

        print("\nthe failure modes we design around:")
        check_designed_failures()

        print("\ndeterminism:")
        for src, bmp in outputs:
            before = bmp.read_bytes()
            again = mw.convert(src, build / "again")
            check(f"{bmp.name}: byte-identical on a second run",
                  before == again.read_bytes())

        print("\nthe push protocol, against a port of the device's API:")
        check_push(build)

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s)")
        for name in failures:
            print(f"  - {name}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
