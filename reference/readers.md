# Device notes — Xteink X3 (CrossPoint firmware)

The suite's EPUBs target an **Xteink X3** (ESP32-C3, ~400 KB RAM, 528×792
e-ink) running **CrossPoint** (open-source firmware; 1.5.0 as of mid-2026),
fed over WiFi from the suite's own OPDS server (`opds-server/`) or by SD card.
Everything below is **device-confirmed** (photos, 2026-07) unless marked
otherwise.

## The working recipe (confirmed end-to-end)

1. **Firmware:** CrossPoint **1.5.0 or newer** (web-flash from
   https://crosspointreader.com/; stock is restorable the same way). Stock
   firmware ships no CJK glyphs at all — hanzi render as tofu boxes — so it is
   not an option for Chinese. 1.5.0 is the floor because it is the first
   release that renders CJK in the *interface* (chapter list, library, file
   browser) — see "CJK in the interface" below.
2. **Font:** copy a family folder from `reference/fonts/` to the SD card under
   `/fonts/`, power-cycle (fonts scan once at boot), select it under Settings →
   Reader → Font Family. **`WenZilla/`** is the recommended Chinese font — LXGW
   WenKai kaiti for hanzi + NV Zilla Slab for Latin/pinyin, so mixed
   hanzi+pinyin reads nicely. **`WenKaiFull/`** is the pure-kaiti baseline
   (device-confirmed). Glyphs stream from SD. Both ship sizes **8, 10, 12, 14,
   16, 18** — the small three exist for the UI fallback, not for reading.
   Install details: `reference/fonts/README.md`.
3. **Books:** build with `reading_style: after` in book.json (pinyin after each
   glossed word) — see the rendering verdicts below.

## Rendering verdicts (what the engine actually does)

| Feature | Verdict |
|---|---|
| `<ruby>` pinyin | **Broken** — `<rt>` leaks inline: 石shí头tou |
| Interlinear (CSS inline-block stacking) | **Broken** — collapses inline: shí石tou头 |
| Plain hanzi body | **Perfect** |
| `reading_style: after` (trailing pinyin) | Works — the X3 default |
| Embedded EPUB fonts (`@font-face`) | Ignored — the renderer only rasterizes pre-converted `.cpfont` bitmaps; never fatten the books with fonts |
| Glossary/internal links | Harmless; not tappable (no touchscreen) — kept for phone reading |
| CJK in *interface* text (TOC, library, browser) | Needs 1.5.0 **and** 8/10/12 pt `.cpfont` sizes — see below. On 1.4.1 those rows render **blank** |

Keep chapter CSS trivial: the engine honors basic text properties only.
`reading_style: ruby` remains for capable readers (Apple Books renders ruby
beautifully) — never for the X3.

## CJK in the interface (why the chapter list was blank)

*Source-confirmed against the firmware (tags 1.4.1 and 1.5.0), symptom
device-confirmed 2026-07.*

The book *body* and the *interface* are drawn with different fonts. Body text
uses the SD-card family you selected; every list row, header and title in the
UI uses a **built-in Ubuntu face compiled into flash — Latin only**
(`UI_10_FONT_ID` for list rows, `UI_12_FONT_ID` for headers/titles,
`SMALL_FONT_ID` — Noto Sans — for subtitle lines). Selecting a CJK SD font
never changed that.

On **1.4.1** the result is a chapter list of **blank but navigable rows**: the
EPUB nav is parsed correctly (the rows are there, and selecting one jumps to
the right chapter), the labels simply draw nothing. Blank rather than tofu
because `EpdFont::getGlyph()` substitutes U+FFFD for a missing glyph, and
Ubuntu has no U+FFFD — the converter validates it away, so the substitution
also misses and the renderer draws nothing at all. The 8 pt Noto face *does*
carry U+FFFD, which is the tell: at 8 pt you get boxes, at 10/12 pt you get
nothing. A title like `诚实的重要 · 分级读物 (HSK 3)` shows up as `· (HSK 3)`.

**1.5.0** adds a size-matched CJK fallback (`SdCardFontSystem::setupUiFallbacks`):
a UI string containing CJK the built-in font can't draw is rendered — whole
string, not per glyph — with your selected SD family. Two conditions, both
required:

- the SD family must actually cover CJK (probed with 一/あ/ア/가), and
- it must ship `.cpfont` files at **exactly 8, 10 and 12 pt**. The lookup is
  `findFile(pointSize)`, an exact match — there is no nearest-size fallback, so
  a 12–18 family (what this repo shipped until 2026-07) leaves the 10 pt list
  rows blank even on 1.5.0.

Hence the sizes in `reference/fonts/`. Side effects worth knowing: those three
sizes also appear in Settings → Reader → Font Size (reading at 8 pt is your
call), and a mixed title renders *entirely* in the SD font, Latin included.
With no SD font selected there is no fallback and CJK breaks again.

**Device-confirmed end to end on a 1.5.0 RC (2026-07):** with 8/10/12 pt
`.cpfont` files installed, the chapter list and the home-screen book titles
render their hanzi. Free heap on that device sat at ~35 KB and the extra UI
sizes did not disturb it — only the interval table is resident per font (~370
bytes at 61 intervals), glyphs stream from SD.

Two upstream issue candidates: the exact-size lookup could use the
`findNearestSize` that already exists next to it, and a UI font without U+FFFD
fails silently (blank) instead of showing the boxes the firmware's own
`docs/sd-card-fonts.md` promises.

## Screen text capacity (measured on-device, 2026)

The panel is 528×792 px. At the default reading size the text column holds
about:

| | chars/line | lines/screen |
|---|---|---|
| Prose (justified) | ~34–37 | ~19–21 |
| Verse (hanging indent) | wraps ~36 | ~10–12 |

**~36 characters is the effective line width.** These are *facts to design
against, not targets to hit*:

- **Don't cap verse line length.** Metrical lines stay whole; if one is too
  wide the reader handles it by bumping the font down or rotating to landscape.
  Our job is the opposite — **waste no vertical space**: `line-height` is
  minimized by `line_spacing: "tight"` in book.json, so the reader, not our
  CSS, decides how airy the page is.
- **Prose is width-agnostic** — the engine reflows it; just reconstruct real
  paragraphs (don't preserve column-broken short lines).
- **Small screen ⇒ structure matters.** ~12–20 lines per screen means clear
  chapters/acts/scenes and headings beat one long undifferentiated chapter.

## Cover images (EPUB, not wallpaper)

The **embedded EPUB cover** is a normal image the reader renders — distinct
from the device *sleep-screen wallpaper*, which is a separate feature with its
own format and folder (see "The sleep screen" below; `wallpaper-maker/` builds
those). For the cover:

- **PNG or baseline JPEG only.** Progressive JPEG and GIF fall back to an
  `[Image]` placeholder on-device.
- **Grayscale.** The panel is e-ink; colour is dropped and wastes bytes.
- **Keep it ≤ 528×792 (panel size).** CrossPoint re-converts the cover for the
  home-screen thumbnail and sleep screen; a ~2000px-tall cover takes ~10 s each
  time. Panel-sized is instant.

`epub-builder/scripts/prepare_cover.py` enforces all three (and can draw
the title onto a template cover). Content figures follow the same grayscale
rule at 480px width (`prepare.py`).

## The sleep screen (wallpaper)

*Source-confirmed against the firmware (`src/activities/boot_sleep/SleepActivity.cpp`,
`lib/GfxRenderer/Bitmap.cpp`, `lib/GfxRenderer/BitmapHelpers.cpp`,
`lib/FsHelpers/FsHelpers.cpp`), tags 1.5.0 and master @ 2026-08 — byte-identical
for all of it apart from the quick-resume refresh mode, which does not touch
this path. **Device-confirmed 2026-08:** an X3 drew wallpapers built by
`wallpaper-maker/` as its sleep screen, from `/.sleep`, with the mode set to
Custom. Panel-sized 4-bpp files only so far — an image small enough to be
matted has not been put on the panel yet, so the placement rules for an
under-size image remain read-from-source.* `wallpaper-maker/` builds these and
pushes them.

| What | Requirement |
|---|---|
| Format | **BMP only.** The folder scan filters on `hasBmpExtension` (case-insensitive) and opens nothing else |
| `.pxc` | **Not a wallpaper format** — it is the EPUB reader's *pixel cache* (`lib/Epub/Epub/converters/PixelCache.h`), written beside a decoded JPEG. Web converters that offer `.pxc` for a sleep screen are wrong for this firmware; the file is skipped in silence |
| Where | `/.sleep/` (preferred, checked first, one file picked at random per sleep), else `/sleep/`, else a single `/sleep.bmp` at the root. **`/.sleep` existing makes `/sleep` invisible** |
| Names | Anything starting with `.` is skipped inside those folders, whatever it holds |
| Enabled by | Settings → Display → Sleep Screen = **Custom** (enum index 2; `COVER` draws the open book's cover instead, `COVER_CUSTOM` does both by context) |
| Bit depths accepted | 1, 2, 4, 8, 24, 32; `BI_RGB` only (`BI_BITFIELDS` for 32) |
| Palette offset | Read from a **fixed offset after the first 40 DIB bytes** — not from `biSize`, not from `bfOffBits`. A BITMAPV4/V5 header feeds colour-space fields to the palette reader |
| Native palette | If every palette entry's luma is within ±21 of 0/85/170/255, the firmware maps pixels straight through (`lum >> 6`) and **dithers nothing**. Otherwise it re-dithers on-device (Atkinson). `adjustPixel` is identity — shipped with `USE_BRIGHTNESS = false` |
| Grey vs BW | `hasGreyscale()` is `bpp > 1`: a 1-bpp file gets the plain BW waveform, never the four-level grey pipeline |
| Size | Panel-exact **528×792**. Larger is scaled down and centred; **smaller is never scaled up** — it is centred in black |
| Refresh | A single HALF refresh (stock parity), plus two extra passes for the grey planes |

Two shapes work, and they are a real choice:

- **528×792, 24-bpp greyscale, undithered** — the firmware quantises it with
  Atkinson. Better on photographs, because Atkinson carries only 3/4 of the
  error and lets a near-black sky fall to solid black. This is what
  [wallpaperconverter.jakegreen.dev](https://wallpaperconverter.jakegreen.dev/)
  emits and what `wallpaper-maker/` now defaults to.
- **528×792, 4-bpp indexed, 40-byte DIB header, palette on 0/85/170/255,
  pre-dithered** — trips the native-palette test, so the panel shows exactly the
  pixels computed off-device, at a sixth of the bytes. Exact, and on a dark
  photograph worse to look at.

Note the third-party guidance that 1-bit and 4-bit are "not recommended" is
right for a *generic* 4-bit encoder: an even 16-grey ramp fails the ±21 native
test, so the device re-dithers it anyway and 24-bit is the better of those two.
The 4-bit route above is a third case, and 1-bit really is bad — `hasGreyscale()`
is `bpp > 1`, so it never gets the four-level pipeline at all.

## Getting files onto the device over WiFi

*Source-confirmed (`src/network/CrossPointWebServer.cpp`,
`src/network/WebDAVHandler.cpp`, `docs/webserver-endpoints.md`). **Device-confirmed
2026-08** for the HTTP path — `/api/status`, `/api/files`, `/mkdir`, `/upload`
and `/api/settings` all drove a real X3 from
`wallpaper-maker/scripts/push_wallpaper.py`, and `/rename` was driven by hand on
a 1.5.0 RC. `/download` and `/move` are wired in `crosspoint_device.py` but
read-from-source; WebDAV and the WebSocket upload likewise.*

*Careful with the version claim here: this file is **1.5.0**, and master is no
longer the same. As of 2026-08 `CrossPointWebServer.cpp` is 1974 lines at the
1.5.0 tag and 1913 on master, and 1.5.0 carries `server->enableCORS(true)` plus
task-watchdog registration that master does not. The route table below is
identical across 1.4.1, 1.5.0 and master — every endpoint here has existed since
at least 1.4.1 — but "master = 1.5.0 byte-for-byte" is no longer true for this
file and should not be repeated.*

The OPDS client (above) is a **book-only pull**: acquisition type exactly
`application/epub+zip`, saved to the SD root as `<author> - <title>.epub`.
Nothing else can be delivered through it, to anywhere. Everything that is not a
book goes the other way — a push into the firmware's file-transfer web server,
started on the device at **Home → File Transfer → Join a Network** and running
only while that screen is up.

| What | Detail |
|---|---|
| Transport | Plain HTTP, **port 80**, no auth, CORS open. `crosspoint.local` via mDNS — device-confirmed working, including on a LAN running its own DNS filtering and reverse proxy |
| Discovery | UDP **8134**: send `hello`, get `crosspoint (on <hostname>);<ws port>` |
| Upload | `POST /upload?path=DIR`, multipart. Path is a *query* param — the handler needs it before the body finishes arriving |
| Overwrite | **Not supported.** An upload onto an existing name returns 400 `File already exists`; `POST /delete?path=...` first |
| Folders | `POST /mkdir?path=PARENT&name=NAME`, no name validation — dot-prefixed folders are fine |
| Listing | `GET /api/files?path=...`. Entries carry `name`, `size`, `isDirectory`, `isEpub`. **Hides dot-prefixed entries** unless the device's `showHiddenFiles` is on, so a missing folder and a hidden one look alike |
| Download | `GET /download?path=...`. EPUBs come back as `application/epub+zip`, everything else `application/octet-stream`. Refused (403) when the *final* segment starts with a dot |
| Rename | `POST /rename?path=FILE&name=NEWNAME` — **a real rename, one call**. `name` is a bare name: no `/` or `\` (400), may not start with a dot (403). Files only. Renaming to the current name returns 200 `Name unchanged`. **Device-confirmed 2026-08**, with spaces in both names |
| Move | `POST /move?path=FILE&dest=DIR`. Files only, not directories |
| Parameters | Read from **either** the query string or a form body — `hasArg`/`arg` see both. `push_wallpaper.py` and `crosspoint_device.py` use the query form throughout |
| Protected names | `rename`, `move`, `delete` and `download` all guard on the **final path segment** only. So a file *inside* `/.sleep` is fair game — which is why wallpapers can be replaced — while `/.sleep` itself cannot be renamed or deleted |
| Names on the card | Whatever is there, byte for byte, and `Storage.exists()` compares bytes. Books from other catalogs arrive **NFD-decomposed** (`é` as `e` + U+0301); normalizing a name before sending it back yields `Item not found` against a file plainly present. Round-trip listings verbatim |
| Settings | `GET`/`POST /api/settings`, partial JSON by key (`{"sleepScreen": 2}`); keys are in `src/SettingsList.h` |
| Fonts | `POST /api/fonts/upload` with `family` + `file` — the network route for `reference/fonts/` |
| WebDAV | Port 80, PUT overwrites atomically. **Refuses any path segment beginning with `.`** — `isProtectedPath` walks every segment, not just the last — so `/.sleep` and everything in it is unreachable this way, and `/sleep` is not |
| Folders | `/rename` and `/move` both end in "Only files can be…"; a **directory can only be renamed through WebDAV `MOVE`**, which has no directory check and calls the filesystem's own rename. So a visible folder like `/sleep` can be renamed and `/.sleep` cannot, by either route. *Source-confirmed 1.5.0, not yet device-confirmed.* |
| Also | WebSocket fast upload on **81** (`START:<name>:<size>:<path>` → `READY` → binary → `DONE`) |

## OPDS client — what the firmware actually requires

*Read from the firmware source (`lib/OpdsParser/`,
`src/network/HttpDownloader.cpp`, `src/util/UrlUtils.cpp`,
`src/activities/browser/OpdsBookBrowserActivity.cpp`, master @ 2026-07), not from
the user guide. **Source-confirmed, and device-confirmed 2026-07** — an X3
browsed a catalog served by `opds-server/` and downloaded books from it; that
server implements all of it, and its self-test grades against a port of this
client. Those four files are byte-identical between 1.5.0 and the master that
was read, so this table describes 1.5.0 exactly. 1.5.0 did rework them
substantially from 1.4.1 — notably it now percent-encodes unsafe characters in
a URL before fetching it, which our server is unaffected by (its slugs are
ASCII, and `{searchTerms}` is substituted before that encoding runs).*

| What | Requirement |
|---|---|
| Feed format | **Atom XML only** (expat) — no OPDS 2.0 / JSON path |
| Entry survival | Needs a non-empty `<title>` **and** a resolved href, or it is **dropped silently** — no error anywhere |
| Book link | `rel` *containing* `opds-spec.org/acquisition` **and** `type` **exactly** `application/epub+zip`; a `;profile=` suffix breaks it |
| Preferred href | Contains `.epub` or `/epub/` when an entry offers several acquisition links |
| Navigation link | `type` containing `application/atom+xml` |
| Search | Feed-level `rel="search"` whose href **literally contains `{searchTerms}`**. An OpenSearch *descriptor* link is never fetched — the conventional setup silently yields no search |
| Pagination | Feed-level `rel="next"` / `rel="previous"` — spelled `previous`, not `prev`; ignored inside an entry |
| URL joining | **Not RFC 3986.** A relative href is *appended* to the current feed URL, so serve absolute URLs |
| Element matching | Substring on the qualified name, so `dc:identifier` registers as `:id` and overwrites the entry id |
| Covers | Not parsed at all — a cover link is bytes read and discarded |
| Transport | **Plain HTTP.** esp-tls is built with insecure mode off against a bundled CA store: a self-signed certificate cannot handshake |
| Auth | HTTP Basic, sent preemptively, and **only when username and password are both non-empty** |
| Redirects | Followed by hand, max 5 hops; any final status but 200 is a failure |
| Downloads | Saved to the SD root as `<author> - <title>.epub`, sanitized to 100 bytes — so two books with the same author and title overwrite each other |
| Servers stored | Up to 8 (Settings → System → OPDS Servers) |

## Building `.cpfont` fonts — the rules that matter

CrossPoint's converter is `lib/EpdFont/scripts/fontconvert_sdcard.py`
(same script the https://crosspointreader.com/fonts web builder wraps — note
the builder requires you to *upload* a base TTF/OTF; it ships no fonts, and
the official catalog contains zero CJK families).

1. **Use broad preset intervals — never sparse custom ranges.** This is the
   hard-won one: fonts subset to a book's exact charset (hundreds of tiny
   Unicode intervals) pass every structural check in the firmware's parser
   yet **silently fail to load on-device** — the font lists in the picker,
   but opening a book reverts the setting to built-in Noto. The identical
   font content built with broad presets (`latin-ext,cjk` → ~100 wide
   intervals, 22.5k glyphs) loads and renders. Glyphs stream from SD, so the
   big font costs no RAM. *Upstream issue candidate; found and verified on
   1.4.1, not re-tested since — the converter and loader are unchanged in
   1.5.0, so assume it still holds.*
2. **Ship 8, 10 and 12 pt as well as the reading sizes.** Those three are what
   the 1.5.0 UI fallback looks for, by exact size, to draw CJK in the chapter
   list, library and browser (see "CJK in the interface"). Cost: ~4 MB of SD
   per family, and a couple of KB of RAM — only the interval table is resident,
   glyphs stream.
3. **Layout:** one folder per family — `/fonts/<Family>/<Family>_<size>.cpfont`;
   loose files are ignored. Scan happens at boot only.
4. **Reproducible build** (what produced `reference/fonts/`):

```bash
pip install freetype-py fonttools
# Pinned to the 1.5.0 tag, not master: master's converter has since grown
# ligature extraction, which changes the bytes it emits for Latin faces.
BASE=https://raw.githubusercontent.com/crosspoint-reader/crosspoint-reader/1.5.0/lib/EpdFont/scripts
curl -sSLO $BASE/fontconvert_sdcard.py
curl -sSLO $BASE/cpfont_version.py

# WenKaiFull: LXGW WenKai + Noto Sans SC fallback, full CJK presets.
# TTF sources: github.com/lxgw/LxgwWenKai releases, or Ubuntu's
# fonts-lxgw-wenkai package; Noto from github.com/notofonts/noto-cjk
# (Sans/SubsetOTF/SC/NotoSansSC-Regular.otf).
# 8,10,12 are the UI-fallback sizes; 12-18 are the reading sizes.
python3 fontconvert_sdcard.py --intervals latin-ext,cjk \
    --sizes 8,10,12,14,16,18 \
    --regular LXGWWenKai-Regular.ttf --fallback-regular NotoSansSC-Regular.otf \
    --name WenKaiFull --output-dir WenKaiFull/

# WenZilla (recommended): Latin from NV Zilla Slab, CJK from LXGW WenKai.
# The primary supplies Latin+pinyin; the fallback fills every CJK codepoint
# (per-codepoint: primary first, fallback second — so Latin stays Zilla).
# Zilla source: github.com/nicoverbruggen/ebook-fonts (fonts/extra,
# NV_Zilla_Slab-Regular.ttf). First patch in the 8 pinyin glyphs Zilla lacks:
python3 ../fonts/synth_pinyin.py            # NV_Zilla_Slab-Regular.ttf -> ...-Pinyin.ttf
python3 fontconvert_sdcard.py --intervals latin-ext,cjk \
    --sizes 8,10,12,14,16,18 \
    --regular NV_Zilla_Slab-Pinyin.ttf --fallback-regular LXGWWenKai-Regular.ttf \
    --name WenZilla --output-dir WenZilla/
# Regular only: bold/italic would duplicate the 22k CJK bitmaps per style (~4x
# size) for no CJK gain, since WenKai has no bold/italic.

```

The recipe is deterministic: re-running it reproduces the committed 12 pt files
of both families **byte for byte** (checked when the 8 and 10 pt sizes were
added, 2026-07). That is the cheap way to confirm your sources and script
version match this repo's before you trust a new size you built.

## Font style guide

- **LXGW WenKai (楷体/kaiti)** — models brush-written stroke shapes, the
  typographic tradition HSK textbooks use. Best for learners: what you read
  is what you should write. SIL OFL.
- **NV Zilla Slab** — a slab serif (Mozilla's Zilla Slab, e-reader-tuned by
  nicoverbruggen) used for the Latin/pinyin half of WenZilla: even weight,
  sturdy at small e-ink sizes, and a friendlier companion to kaiti hanzi than
  WenKai's own Latin. SIL OFL.
- Alternatives if taste differs: Noto Sans/Serif SC (print-style, sturdier
  at tiny sizes), TW-Kai (traditional-oriented), Ma Shan Zheng (true brush
  calligraphy — pretty, tiring as body text).

## Stupid errors (each one cost an evening)

Written down because every one of them *looks* like a broken font or a broken
book, and none of them is.

- **Downloading a font from GitHub with "save link as".** On a file's GitHub
  page that saves the HTML page, not the binary — you get a few hundred KB of
  markup with a `.cpfont` name. Use the raw **Download** button (or
  `git clone` / `curl -L` on the raw URL). *This is what produced two 236 KB
  "fonts" that looked exactly like a bad build.*
- **Trusting the copy to the SD card.** Discovery parses *filenames only* — it
  never opens the file — so a truncated or half-written `.cpfont` still appears
  in Settings → Reader → Font Size and only fails when selected, at which point
  the family silently reverts to built-in Noto. Verify what the device actually
  holds, over WiFi, and match it against `reference/fonts/CHECKSUMS.tsv`:

  ```bash
  curl http://crosspoint.local/api/fonts     # families, files, byte counts
  curl http://crosspoint.local/api/status    # firmware version, free heap
  ```

- **Forgetting the power-cycle.** Fonts are scanned once at boot. A file copied
  to a running device does not exist as far as the reader is concerned.
- **Expecting an RC build to match the public source.** `/api/status` on the
  1.5.0 RC returns `"version":"1.5.0-rc+"` — nothing after the `+`, so the
  binary names no commit. And the web flasher serves admin-uploaded binaries
  (crosspoint-tools keeps them at `builds/beta/{id}/firmware.bin`), not builds
  of the public `release/1.5.0` branch, which lacks code the RC clearly has.
  Read the device first, the source second.
- **Reading the sparse-interval rule as optional.** It is the one build-time
  trap that also ends in "reverts to built-in Noto" — see the font-building
  rules above.

## Alternative firmware (evaluated, not needed)

- **Papyrix** (bigbag/papyrix-reader): supports X3+X4, has a purpose-built
  CJK path (streaming `.bin` fonts, full-BMP direct indexing) and strong
  typography (Knuth-Plass justification). The fallback plan if CrossPoint's
  CJK ever regresses; unnecessary now that WenKaiFull works.
- **crosspoint-reader-cjk** fork: real CJK system but built against the
  **X4 hardware SDK** — do not flash on an X3.

## Sources

- https://github.com/crosspoint-reader/crosspoint-reader (firmware, converter,
  docs/sd-card-fonts.md; font catalog in lib/EpdFont/scripts/sd-fonts.yaml)
- https://crosspointreader.com/ (web flasher; /fonts builder)
- https://github.com/lxgw/LxgwWenKai · https://github.com/notofonts/noto-cjk
- https://github.com/bigbag/papyrix-reader ·
  https://github.com/aBER0724/crosspoint-reader-cjk
- On-device photo evidence: stock tofu (2026-07); CrossPoint + WenKaiFull
  five-mode diagnostic (2026-07)
