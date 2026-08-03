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
#    (workspace/wallpapers/finally-some-peace.png ships with the repo, so this
#     produces something on a fresh clone with nothing dropped in yet)

# 2. on the device: Home -> File Transfer -> Join a Network, then:
python3 wallpaper-maker/scripts/push_wallpaper.py
```

There is nothing to configure and nothing to answer. Every parameter below has
a right answer on this device, so it is already chosen.

Flags exist for the things that are genuinely taste — `--fit contain` to keep
the whole frame instead of cropping, `--mat edges|blur|none` for a different
surround on an image too small to fill the panel, `--preview` to also write a
PNG of what went into the BMP — plus `--dither floyd`, which substitutes our
own error diffusion for the reader's (*Who quantises*, below). For the
push: `--ip` when the reader cannot be found by name, `--list` to see what is
on the device, `--replace` to clear it first.

## What comes out, and why

| Choice | Why it is that and not something else |
|---|---|
| **528x792 exactly** | The panel, portrait. The firmware centres an image that fits and only ever scales *down* — so a smaller image is a stamp in a black field, and a larger one is resampled by an ESP32. |
| **BMP** | The sleep screen reads nothing else. Not PNG, not JPEG, and **not `.pxc`** — see the note below. |
| **4 bpp, indexed, palette on the four states** | Quantised here with the reader's *own* algorithm, then mapped straight through by it — a sixth of the size of a full-tone file, and it bypasses the firmware's X4-tuned constants, which are too bright on an X3. See *Who quantises* below. |
| **40-byte DIB header** | The firmware reads the palette from a fixed offset after the first 40 header bytes, not from `biSize` or `bfOffBits`. A BITMAPV4/V5 header feeds it colour-space fields as colours. |
| **0 / 85 / 170 / 255** | The palette values that make the reader map the file through untouched (`lum >> 6`), *and* — device-confirmed — a fair model of what the four states actually look like on an X3, which is why the quantiser aims at them too. |
| **autocontrast → gamma 0.85 → sharpen** | A phone photo uses half the range; on four levels that half becomes two. Stretch, then lift midtones (e-ink reflects less than the screen you chose the image on), then restore the local contrast the downscale cost. All before dithering. |
| **cover-crop, centred** | A wallpaper should reach all four edges. `--fit contain` if the whole frame matters. |
| **enlargement stops at 1.5x** | Past about half again, a photo is a smear even after dithering. What is left over gets a mat — a picture in a frame beats a blurred one that fills the screen. |
| **EXIF rotation honoured first** | A phone portrait is stored landscape with a rotate flag; cropping before rotating crops the wrong axis. |

## The mat: what surrounds an image too small to fill the panel

Four sectors, mitred along the lines from each panel corner to the
corresponding image corner, each continuing the edge it touches. **`--mat
waves` is the default** and is described below; `--mat edges` is the quiet
version, a single flat level per sector. Both share the same two rules:

- **The mitre is proportional, not decorative.** Along a corner-to-corner line
  the two sides' distances are equal *relative to their own margins* — the cut
  a picture framer makes. Uneven margins give an uneven mitre, which is right.
- **Nothing is drawn between the picture and its border.** The mat continues
  the edge, so a rule around the image would cut across the one join the whole
  thing exists to make. At four levels a hairline is not a hairline either — it
  is a hard black or white line against whatever it sits on. Sky runs straight
  into the sector above it; where a sector matches the edge exactly the
  boundary simply disappears, which is the point.

### `--mat edges` — a flat level per side

Each sector takes its level from a band of the image along its edge, so a photo
with sky above and ground below gets a light mat above and a dark one below:
the fill matches the pixels it actually meets, not the picture's overall mood.

**Each sector snaps to a value the quantiser reconstructs exactly**, and that
is what makes it work here. A mat filled with the raw mean dithers into a large
field of grain; one landing on a reconstruction point comes out perfectly flat,
and flat is the only large area this panel draws without noise. Which values
those are depends on the tuning in force, so `_band_level` takes them as an
argument rather than assuming. Both routes currently land on 0/85/170/255.

If all four bands round to the same level the joins vanish and it degenerates
into a plain single-colour mat — which is the correct behaviour, not a bug, and
is why there is no separate "fill with black or white by overall lightness"
mode: that is this one, on a uniform image.

### `--mat waves` — the edges through rippled glass (the default)

Each edge travels outward into its own sector along a wandering path: step out
one pixel, go diagonally across or straight ahead, never more than one across
per step. Every strand of the edge follows the same walk, so the picture
appears to keep going in all four directions while wobbling as it travels —
distorted-mirror, not blur.

The one-across-per-step rule is the whole guarantee. Strands that never
separate by more than a pixel stay neighbours the whole way out, so nothing
tears open behind them and no pixel is left unpainted. It also means a thin
margin self-limits: the wave can only be as tall as the distance it has had to
climb, and lookups past the ends of the edge clamp, which carries the fill into
the panel corners.

What decides the direction of each step is a sum of three sines, so the
wandering comes out as waves rather than as noise — where the curve is steep
you get a run of diagonals, where it flattens, a run of straights. Their
heights are *not* chosen: a crest a strand cannot climb in the space available
comes out as a 45° zigzag instead of a wave, so the knob is steepness
(`WAVE_SLOPE`) and amplitude follows as `period × slope / 2π`. Three components
at 0.35 keep the combined slope under one, which is what separates a wave from
a sawtooth. The shape is seeded from the image itself, so every wallpaper
ripples its own way and does so identically on every run.

The sectors still meet on the mitres — that is what stops four independent
ripples from becoming mush; it reads as four panes of distorted glass in a
frame. There is no snapped level here: the picture and its border are the same
pixels, so there is nothing to round to.

One thing to expect rather than debug: **the wave only shows where the edge
varies along its length.** A picture whose top row is flat sky propagates to a
flat field however hard it wobbles. Busy edges ripple; plain edges do not.

`--mat blur` is the softer alternative: the image itself, enlarged past all
reason and washed out, for when the picture should look like it continues
rather than like it is hung. `--mat none` is a plain white surround.

The mat also replaces the old white letterbox in `--fit contain`, so a
panorama gets framed rather than barred.

## Who quantises: we do, with CrossPoint's algorithm — and its *other* constants

The panel has four states and a photograph has 256 tones, so something has to
choose. `crosspoint_bmp.firmware_quantise` is a port of the firmware's
`AtkinsonDitherer::processPixel` — 1/8 of the error to each of six neighbours,
no serpentine — run here on a real CPU. The result ships as a 4-bpp file whose
palette sits on the panel's four states, so the reader maps it straight through
and quantises nothing. **Same algorithm, a sixth of the bytes of a full-tone
file, and none of the device's work.**

### The two sets of numbers, and why we use the disabled ones

CrossPoint has **two** constant sets in that one function, and only one is live:

```cpp
if (false) {  // original thresholds
    43 / 128 / 213   ->   0,  85, 170, 255
} else {      // fine-tuned to X4 eink display
    30 /  50 / 140   ->  15,  30,  80, 210
}
```

They do different jobs and are easy to confuse, so, plainly:

- The **thresholds** decide which of the four states a tone becomes.
- The **reconstruction values** are the ditherer's belief about what each state
  *looks like*, used only to compute `error = adjusted − quantizedValue`, which
  is then handed to the neighbouring pixels.

A third number set, **0/85/170/255**, also appears in `Bitmap.cpp` as
`lum >> 6` — but that is the *input* side, deciding which state a palette entry
in a file means. It is not a claim about appearance. That is why two sets can
coexist without either being wrong.

The live branch is labelled for the **X4**. On an **X3** it renders visibly too
bright, and the reason is mechanical: its reconstruction values sit below what
the panel really shows, so `adjusted − quantizedValue` comes out too positive
and every pixel pushes its neighbours lighter. At an input of 128 it charges
**+48** where the true error is about **−42**. On a real photograph that lifts
the panel mean about **30 levels** above the tone we asked for.

So we run the firmware's algorithm with the firmware's *own* disabled constants,
which are right for this panel. Measured against the toned image, the X4 branch
overshoots by +30.9 and the even branch lands within −4.8.

**This means our output is deliberately not what the device would make of a
full-tone file** — it is what the device would make of one if it were tuned for
the X3. Shipping 4-bpp on a native palette is exactly what buys us that: the
file is mapped straight through, so the X4 tuning never gets to run. Hand the
reader an undithered image instead and you get the X4 tuning and the bright
picture, which is what the earlier default did.

`--dither floyd` (or `atkinson`, `none`) substitutes our own error diffusion
against the same even ramp. Kept for comparison and because the gate can grade
it, not because you should reach for it.

## The grid on flat areas (the `--dither floyd` route)

This applies to the route where *we* dither. Error diffusion has no randomness
in it: hand it a large area of near-constant
tone and it does not produce grain — it produces a **regular lattice** of
minority pixels, evenly spaced, which the eye reads as a grid laid over the
picture. It is the classic weakness of Floyd–Steinberg and it is invisible
until an image with a big flat area turns up. A near-black night sky did it
here: the sky sat at 24 of the 85 between black and the next level, so roughly
one pixel in 3.5 had to be lifted, and they came out in ranks.

The fix is to perturb the quantiser's threshold per pixel, with two properties
that both matter:

- **Blue noise, not white.** White noise carries energy at every scale
  including the ones the eye resolves, so it trades the lattice for visible
  clumping — measurably worse on mid-greys. High-passing the noise leaves only
  structure finer than the eye picks out: enough to stop the lock-in, not
  enough to be seen. The field is generated at panel size rather than tiled, so
  it adds no period of its own, and it is seeded from the image, so output
  stays byte-identical run to run.
- **Shaped by how sparse the dither is at that tone.** The lattice is only
  visible when the dots are far apart, so the nudge is scaled by
  `|sin(2π·tone/85)|`:

  | source tone | what the dither does there | nudge |
  |---|---|---|
  | exactly on a level | nothing is dithered at all | **none** — otherwise it would speckle a solid area |
  | halfway between levels | dots land every other pixel: a checkerboard, finer than the eye resolves and the smoothest thing this panel can draw | **none** — perturbing it is pure loss |
  | the sparse ground between | dots far enough apart to line up — where the grid forms | **full** |

  It keys off the *source* tone, not the running error-diffused value, which
  swings far too wildly to say anything about local dither density. Getting
  that wrong is why the first attempt made mid-greys worse.

Cost, measured: local tone accuracy drops from 1.01 to 1.13 RMS — negligible —
while the lattice score on a flat tone falls from 0.90 to 0.05. `DITHER_NOISE`
is the amplitude; below about 0.3 the lattice starts showing again.

Worth knowing that this fix is *not* why the default changed. It made our
dither much better and still lost to letting the reader do it.

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
leave it.

### Finding the reader

`push_wallpaper.py` tries three things, cheapest first, with a short timeout so
a stale address costs a moment rather than most of a minute:

1. **The address that answered last time**, kept in `last-device.json` beside
   this file — gitignored, since it is a fact about your LAN and not about this
   repo. `X3_LAST_DEVICE` moves it, which is how the self-test keeps its hands
   off yours.
2. **`crosspoint.local`**, which is how the first device-confirmed run found
   the reader — mDNS survived a LAN with its own DNS filtering and reverse
   proxy, so it is worth trying. It is second rather than first only because
   when it does fail it fails slowly and confusingly: a local resolver will
   happily answer for a `.local` name and never mention the reader.
3. **The firmware's UDP discovery ping** — `hello` to port 8134, which needs no
   name service and answers `crosspoint (on <host>);<ws port>`.

`--ip 192.168.1.42` (or `--host`, same flag) skips all of it. An address given
that way is used exactly as given — if it does not answer that is an error, not
a reason to go hunting and push to whatever turns up. On success it is written
down, so `--ip` is normally a one-off: the next run finds the reader on its
own, until the DHCP lease moves and the fallbacks take over again.

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
| A picture floating in a black frame | Not 528x792 — the firmware never scales *up*, and that black is the device's, not our mat. Re-run `make_wallpaper.py`. |
| Upload fails with "File already exists" | The firmware refuses collisions. `--replace`, or delete on the device. |
| `push_wallpaper: cannot reach ...` | The device is not on the File Transfer screen — the web server only runs while it is. |
| `no reader found (tried ...)` | mDNS is not working here and nothing answered the discovery ping. Read the address off the File Transfer screen and pass `--ip`; it is remembered after that. |

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

It checks the mat the same way, by the property that matters on this panel: a
block of border must decode to a *single* level, and the sectors above and
below a light-over-dark picture must not come out the same. For `--mat waves`
it asserts the invariant the construction rests on — the walk starts on the
edge and never moves more than one across per step — and then proves the
consequence directly: a source containing no black must paint a mat containing
no black, since the canvas starts black and any hole would still be showing.

It also asserts the two failure modes the encoder is *shaped around* still
fail — a 108-byte DIB header is misread, a 24-bpp file is not native — so that
if the firmware ever changes, the reasoning in the code fails loudly instead of
quietly going stale.

The push protocol is graded the same way, against a port of the device's
file-transfer API that keeps its awkward parts: dot-prefixed entries hidden
from `/api/files`, and uploads rejected rather than overwritten.

Fixtures cover the shapes that break a naive converter: a wide landscape (the
crop must take the middle), an image far smaller than the panel (must be framed
rather than blown up), a small light-over-dark one (each sector must follow its
own edge), an alpha image (must flatten onto white, not multiply to black), a
phone-style EXIF rotation (must rotate before cropping), and a flat gradient
(where dithering either works or bands visibly).

**Status: device-confirmed (2026-08), end to end.** An X3 drew a wallpaper built
here as its sleep screen, quantised with the tuning below, converted and pushed
from `tgbot/` over WiFi without a terminal touching it. The reader was found by
name, `/.sleep` created and filled, the mode switched to Custom.

The route there is worth keeping, because two of the three steps were wrong at
some point and only the panel said so:

- the first version dithered here with plain Floyd-Steinberg and laid a visible
  **lattice** over any large flat area — a night sky found it;
- the second let the reader quantise and came back **too bright**, which turned
  out to be the firmware running its X4-tuned constants on an X3;
- the third — quantising here with the firmware's algorithm and its own
  disabled even constants — is the one in the photograph, and it is right.

**Still inferred: the mat.** Every source photographed so far filled the panel,
so no `--mat waves`, `edges`, `blur` or `none` border has been on the glass.
The gate covers it and a matted wallpaper is the same file shape as any other,
so there is no reason to expect trouble — it simply has not been seen. Anything
under about 350px wide would settle it.

## Files

```
SKILL.md                     this file
requirements.txt             Pillow (the only dependency)
last-device.json             gitignored; the reader's address, once one answers
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
