---
name: wallpaper-maker
description: >-
  Turn any image into an Xteink X3 sleep-screen wallpaper, and push it to the
  reader over WiFi. Use when the user wants a custom sleep screen or lock
  screen on the device, when changing how images are converted (size,
  dithering, tone) or delivered, or when a wallpaper is on the device and does
  not show up. Triggers include "sleep screen", "wallpaper", "lock screen",
  "screensaver", "make this photo work on the X3", "the wallpaper isn't
  showing".
---

# wallpaper-maker — the X3's sleep screen

Drop images in a folder, run one command, get files the reader can draw.
Run the second command and they are on the reader.

Not a cover maker. An EPUB cover is content inside a book and goes through
[`epub-builder/scripts/prepare_cover.py`](../epub-builder/scripts/prepare_cover.py);
this is the device's *sleep screen*, a wholly separate firmware feature with a
different format, a different folder and a different delivery path.

Deterministic, like the builder and the server: no model anywhere in it. The
same image converts to a byte-identical file every time. What stands in for
this suite's "gate after every model step" is
[`scripts/selftest.py`](scripts/selftest.py), which grades every output through
a port of the **device's own BMP reader** — see *The gate* below.

## Usage

```bash
.venv/bin/pip install -r wallpaper-maker/requirements.txt   # Pillow, once

# 1. drop images in workspace/wallpapers/, then:
.venv/bin/python wallpaper-maker/scripts/make_wallpaper.py
#    -> workspace/wallpapers/build/*.bmp

# 2. on the device: Home -> File Transfer -> Join a Network, then:
python3 wallpaper-maker/scripts/push_wallpaper.py
```

There is nothing to configure and nothing to answer. Every parameter below has
a right answer on this device, so it is already chosen.

Flags exist for the two things that are genuinely taste — `--fit contain` to
letterbox instead of crop, `--preview` to also write a PNG of the dithered
result you can look at on a computer — plus `--dither atkinson|none`, which
you should not need. For the push: `--host` when mDNS is unhelpful, `--list`
to see what is on the device, `--replace` to clear it first.

## What comes out, and why

| Choice | Why it is that and not something else |
|---|---|
| **528x792 exactly** | The panel, portrait. The firmware centres an image that fits and only ever scales *down* — so a smaller image is a stamp in a black field, and a larger one is resampled by an ESP32. |
| **BMP** | The sleep screen reads nothing else. Not PNG, not JPEG, and **not `.pxc`** — see the note below. |
| **4 bpp, indexed, 4-grey palette** | A palette on the panel's own levels passes the firmware's *native palette* test, and then it maps our pixels straight through. Hand it 24 bpp and the ESP32 re-dithers the image itself, discarding full-precision work and redoing it with an integer approximation. |
| **40-byte DIB header** | The firmware reads the palette from a fixed offset after the first 40 header bytes, not from `biSize` or `bfOffBits`. A BITMAPV4/V5 header feeds it colour-space fields as colours. |
| **0 / 85 / 170 / 255** | Not a choice: the four charge states the panel has. |
| **Floyd–Steinberg, serpentine** | Error diffusion is what makes four levels read as a photograph. Serpentine because straight raster order walks the residual error one way and leaves diagonal worms in skies. Atkinson (the firmware's own pick, for covers) is crisper and will clip a gradient sky flat. |
| **autocontrast → gamma 0.85 → sharpen** | A phone photo uses half the range; on four levels that half becomes two. Stretch, then lift midtones (e-ink reflects less than the screen you chose the image on), then restore the local contrast the downscale cost. All before dithering. |
| **cover-crop, centred** | A wallpaper should reach all four edges. `--fit contain` if the whole frame matters. |
| **EXIF rotation honoured first** | A phone portrait is stored landscape with a rotate flag; cropping before rotating crops the wrong axis. |

### `.pxc` is not a wallpaper format

It is worth saying plainly, because converters on the web offer it and this
repo used to imply it: `.pxc` is CrossPoint's **EPUB image pixel cache**
(`lib/Epub/Epub/converters/PixelCache.h`) — what the reader writes beside a
decoded JPEG so it does not decode it thirteen times per page. The sleep-screen
scan filters on `hasBmpExtension` and opens nothing else. A `.pxc` in `/.sleep`
is skipped in silence.

## Getting them onto the device

**Not over OPDS, and not because we did not try.** The X3's OPDS client follows
an acquisition link only when its type is exactly `application/epub+zip`, and
saves what it downloads to the SD *root* as `<author> - <title>.epub`. There is
no content type it accepts and no destination it can be aimed at. A catalog is
a pull, and this device only ever pulls books.

So delivery runs the other way — a push into the file-transfer web server the
firmware already ships (port 80, no auth, CORS open), which is the same server
the browser file manager talks to.

```
Home -> File Transfer -> Join a Network      # the device prints its address
python3 wallpaper-maker/scripts/push_wallpaper.py
```

The device stays on that screen while transferring; the server stops when you
leave it. `push_wallpaper.py` finds the reader at `crosspoint.local`, or by the
firmware's own UDP discovery ping (`hello` to port 8134) if mDNS is unhelpful,
or wherever `--host` says.

Where the files land is decided, not asked:

- **`/.sleep/`** — the firmware's preferred pool. Checked first; one file is
  picked at random each time the device sleeps. Hidden, so it stays out of the
  file browser. This is the default target.
- **`/sleep/`** — the visible fallback, read *only* when `/.sleep` does not
  exist. If wallpapers are already there, we push there instead: creating
  `/.sleep` would silently shadow every one of them.

Then the sleep screen has to be set to **Custom** to use the pool at all, so
the push does that too (`POST /api/settings {"sleepScreen": 2}`);
`--no-set-mode` if you would rather set it by hand under Settings → Display.

Two firmware behaviours the pusher works around, both worth knowing before you
reach for `curl`: an upload onto an existing filename is **rejected**, not
overwritten, so replacing means deleting first; and **WebDAV — the other way
in — refuses every path segment beginning with a dot**, which rules out
`/.sleep` entirely. Hence the plain HTTP API.

### The OPDS-shaped alternative, for completeness

There is one way a picture reaches the sleep screen through the catalog: set
the sleep screen to **Cover**, and it draws the cover of the book you last had
open. So a wallpaper wrapped in an EPUB as its cover, downloaded over OPDS and
opened once, becomes the sleep screen. It is one image, tied to whatever you
are reading, and it is not what this tool does — but it is the honest answer to
"can OPDS put a picture on the sleep screen", and it is why
`prepare_cover.py` keeps the cover ≤ 528x792.

## When it does not show up

The device reports nothing at all — a wallpaper that fails is simply the
default sleep screen, with no message anywhere. In order of likelihood:

| Symptom | Cause |
|---|---|
| Default CrossPoint sleep screen | Sleep screen is not set to **Custom** (Settings → Display), or `sleep`/`.sleep` holds no file the scan accepts. |
| Some images appear, one never does | Its name starts with `.` (skipped whatever it contains), or the extension is not `.bmp`, or the header did not parse. |
| Images stopped appearing after a push | `/.sleep` exists and takes priority; anything in `/sleep` is now invisible. Move them, or delete `/.sleep`. |
| Grainier or flatter than the preview | The file is not 4-bpp-indexed with a native palette, so the firmware re-dithered it on-device. Re-run `make_wallpaper.py`; do not hand-edit the BMP in an image editor, which will re-save it 24-bpp. |
| A picture floating in a black frame | Not 528x792. The firmware never scales *up*. |
| Upload fails with "File already exists" | The firmware refuses collisions. `--replace`, or delete on the device. |
| `push_wallpaper: cannot reach ...` | The device is not on the File Transfer screen — the web server only runs while it is. |

## The gate

An image-validity check would pass files this device never draws, and files it
draws by redoing our work. So [`scripts/selftest.py`](scripts/selftest.py)
grades every output through
[`scripts/crosspoint_bmp.py`](scripts/crosspoint_bmp.py) — a port of the
firmware's folder scan, header parsing, palette read and per-bpp row unpacking,
quirks deliberately included. It checks that the scan would open the file, that
the parser accepts it, that the palette is native so **nothing is re-dithered**,
that the file decodes back to the exact levels we computed, that it lands at
0,0 unscaled, and that a second run is byte-identical.

It also asserts the two failure modes the encoder is *shaped around* still
fail — a 108-byte DIB header is misread, a 24-bpp file is not native — so that
if the firmware ever changes, the reasoning in the code fails loudly instead of
quietly going stale.

The push protocol is graded the same way, against a port of the device's
file-transfer API that keeps its awkward parts: dot-prefixed entries hidden
from `/api/files`, and uploads rejected rather than overwritten.

Fixtures cover the shapes that break a naive converter: a wide landscape (the
crop must take the middle), an image smaller than the panel (must be scaled
up), an alpha image (must flatten onto white, not multiply to black), a
phone-style EXIF rotation (must rotate before cropping), and a flat gradient
(where dithering either works or bands visibly).

**Status: source-confirmed, not yet device-confirmed.** Every rule above was
read from the CrossPoint firmware (tag 1.5.0 and master @ 2026-08, which are
byte-identical for all of it) and is enforced by the gate; no photo of an X3
showing one of these wallpapers exists yet. `reference/readers.md` keeps that
distinction — update both when a device confirms it.

## Files

```
SKILL.md                     this file
requirements.txt             Pillow (the only dependency)
scripts/
  make_wallpaper.py          image -> X3 sleep-screen BMP
  push_wallpaper.py          BMPs -> the device, over its file-transfer API
  crosspoint_bmp.py          port of the device's BMP reader — the gate's oracle
  selftest.py                the gate
```

## Sources

Firmware paths behind every claim here, for when this needs re-checking:
`src/activities/boot_sleep/SleepActivity.cpp` (the scan, the folders, the
placement), `lib/GfxRenderer/Bitmap.cpp` and `lib/GfxRenderer/BitmapHelpers.cpp`
(the reader, the native-palette test, `adjustPixel`),
`lib/FsHelpers/FsHelpers.cpp` (`hasBmpExtension`),
`src/network/CrossPointWebServer.cpp` and `src/network/WebDAVHandler.cpp`
(delivery), `src/CrossPointSettings.h` and `src/SettingsList.h` (the sleep-screen
enum and its web key), `lib/Epub/Epub/converters/PixelCache.h` (what `.pxc`
actually is), `docs/webserver-endpoints.md` and `USER_GUIDE.md` §3.5.
