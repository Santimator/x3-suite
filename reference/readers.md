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
   hanzi+pinyin reads nicely. **`CrimKai/`** is the same thing with a book
   serif in place of the slab (Crimson via Cochineal), if you prefer that;
   **`WenKaiFull/`** is the pure-kaiti baseline (device-confirmed). Glyphs
   stream from SD. All three ship sizes **8, 10, 12, 14, 16, 18** — the small
   three exist for the UI fallback, not for reading.
   For Arabic the font is not a preference but the whole feature:
   **`NaskhFull/`** ships 12–18 and is what makes a book body render at all
   (the menus already work without it) — **device-confirmed 2026-08**, see
   "Arabic in the book body". Install details: `reference/fonts/README.md`.
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
| Arabic body text (bidi + contextual joining) | **Works** — right-to-left order and joined letterforms are the firmware's own, and correct. Needs an SD font carrying the *presentation forms*; no built-in reading font does. `NaskhFull` device-confirmed 2026-08 |

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

## Arabic in the book body (why the menus work and the pages do not)

*Mechanism **source-confirmed** against the crosspoint-reader source at tag
**v1.5.0**; outcome **device-confirmed 2026-08** — an X3 with `NaskhFull/`
installed and selected renders an Arabic book body: joined letterforms, in
right-to-left order, from a family built with the intervals below. Before it,
the same device rendered Arabic menus and an empty page.*

**The firmware does bidi and joining itself, and it is done well.** A UI or
book string goes through `BidiUtils::applyBidiVisual`
(`lib/MiniBidi/BidiUtils.cpp:94`), reached from the renderer's
`resolveVisualText` (`lib/GfxRenderer/GfxRenderer.cpp:646`, called at `:542`,
`:567`, `:1877`). Inside it, order matters and is deliberate: `do_bidi()`
first for visual order, `do_shape()` second, because contextual forms are
resolved from *visual* adjacency (`BidiUtils.cpp:139-140`). Both are a port of
mintty's `minibidi.c`, extended for Perso-Arabic letters and in-stream
diacritics (`lib/MiniBidi/minibidi.c`).

**The shaper emits presentation forms, not letters.** That is the fact the
font has to be built around. The shape table's own comment is literal —
`uchar form_b; /* isolated form = 0xFE00 + form_b (Presentation Forms-B) */`
(`minibidi.c:169`) — and the lookup returns `0xFE00 + form_b + form`
(`minibidi.c:336`), landing every ordinary Arabic letter in **U+FE70–U+FEFC**.
Perso-Arabic extensions map into **Forms-A**, U+FB50–U+FBFF
(`minibidi.c:254`, from UnicodeData.txt). Lam-alef collapses to one of
**U+FEF5–U+FEFC** (`minibidi.c:398`), and the absorbed alef is overwritten
with `LIGATURE_PLACEHOLDER` = `0xFFFF` (`minibidi.c:414`,
`lib/MiniBidi/minibidi.h:93`), which is filtered out before emission along
with ZWJ/ZWNJ (`BidiUtils.cpp:146`) — so no font needs a glyph for it.

**A font covering only U+0600–U+06FF therefore renders nothing.** It covers
what the *book* contains and none of what the *renderer* asks for.

**The built-in fonts split the difference, which is why this looks like a book
bug.** The interface fonts were built with Arabic: `ubuntu_10_regular.h:6` and
`ubuntu_12_regular.h:6` (list rows / headers) and `notosans_8_regular.h:6`
(subtitles) all record a `fontconvert.py` command taking
`NotoSansArabic-Regular.ttf` with `--additional-intervals 0xFE80,0xFEFC` plus
selected FB ranges and the Arabic-Indic digits `0x0660,0x0669`. The **reading**
fonts do not: `notosans_12_regular.h:6` and `notoserif_12_regular.h:6` (and
their 14/16/18 siblings) are `NotoSans-Regular.ttf` / `NotoSerif-Regular.ttf`
with no `--additional-intervals` at all. Menus, titles and the file browser
render Arabic on stock 1.5.0; the page cannot.

**And nothing rescues it.** The renderer's only font redirect,
`GfxRenderer::resolveTextFontId`, is gated on `utf8IsCjkCodepoint(cp)`
(`GfxRenderer.cpp:205`) — an Arabic codepoint never qualifies. It also runs
built-in-UI-font → SD-font, the opposite direction from what a book body would
need, and that map is only ever populated for the three UI font ids
(`src/SdCardFontSystem.cpp:166`, over `kUiFontSizes`). The setup that
populates it bails out first unless the SD family answers to one of
`{0x4E00, 0x3042, 0x30A2, 0xAC00}` — Han, Hiragana, Katakana, Hangul
(`SdCardFontSystem.cpp:150`). An Arabic family is skipped by design, which is
also why `NaskhFull` shipping 8/10/12 pt files would be dead weight.

**Nor is there one to download.** `lib/EpdFont/scripts/sd-fonts.yaml` — the
catalog behind Manage Fonts — lists 21 families and no Arabic one, and
`fontconvert_sdcard.py:47` has a `hebrew` preset (`0x0590-0x05FF`,
`0xFB1D-0xFB4F`: base block plus presentation forms, exactly the right shape)
but no `arabic`. Hence spelling the intervals out below. Upstream issue
candidate, and an easy one: `"arabic": [(0x0600,0x06FF), (0xFB50,0xFDFF),
(0xFE70,0xFEFF)]` next to `hebrew` is the whole patch.

**The build** (what produced `reference/fonts/NaskhFull/`; same pinned
converter as the CJK families below):

```bash
# Noto Naskh Arabic + Noto Sans fallback, from notofonts.github.io:
#   fonts/NotoNaskhArabic/hinted/ttf/NotoNaskhArabic-Regular.ttf   (v2.021)
#   fonts/NotoSans/hinted/ttf/NotoSans-Regular.ttf                 (v2.015)
# hinted/, not full/: the converter renders with FreeType's native hinting
# (FT_LOAD_RENDER, no FORCE_AUTOHINT unless asked), and the two builds encode
# the same Arabic coverage anyway.
python3 fontconvert_sdcard.py \
    --intervals 'latin-ext,(0x0600-0x06FF),(0xFB50-0xFDFC),(0xFDFE-0xFDFF),(0xFE70-0xFEFF)' \
    --sizes 12,14,16,18 \
    --regular NotoNaskhArabic-Regular.ttf --fallback-regular NotoSans-Regular.ttf \
    --name NaskhFull --output-dir NaskhFull/
```

**The one deliberate hole in that range: U+FDFD.** The obvious interval is
`(0xFB50-0xFDFF)` in one piece, and it **crashes the converter** —
`struct.error: ubyte format requires 0 <= number <= 255`, thrown at
`fontconvert_sdcard.py:790` packing `GLYPH_STRUCT_FORMAT = "<BBHhhH2xI"`
(`:775`), whose first two fields are the glyph's width and height as `uint8`.
The offender is exactly one codepoint at every size: U+FDFD ARABIC LIGATURE
BISMILLAH AR-RAHMAN AR-RAHEEM, which rasterizes **304×43 px at 12 pt** and
**455×64 at 18 pt** — wider than the 528 px panel, and unrepresentable in the
format. Splitting the range around it costs nothing the shaper can ever ask
for (`do_shape` never emits the FC00–FDFF word ligatures; they appear only if
the book's own text contains one literally). Upstream issue candidate: an
oversize glyph should be skipped with a warning, not abort the build.

**Coverage, verified before building** — the trap with modern Arabic fonts is
that many implement the joined forms in GSUB and encode *none* of them in the
cmap, which subsets to a font that installs cleanly and draws tofu. Noto Naskh
Arabic 2.021 encodes them, and this was checked with fontTools first: of the
codepoints Unicode actually assigns, **U+FE80–U+FEFC 125/125**,
**U+FB50–U+FDFF 802/802**, **U+0600–U+06FF 256/256**, Arabic-Indic digits
10/10. Reading the built files back gives the same answer: 125 of 125 forms-B
and all 8 lam-alef ligatures carry a non-empty bitmap. If you rebuild from a
different Arabic face, run that count first — Scheherazade New and Amiri also
encode the forms; many others do not.

**And the interval question is now answered.** `NaskhFull` is the only family
here built from custom ranges rather than a preset, which made it the obvious
suspect if it had failed to load — it did not. Four broad ranges over 18
validated intervals load and render on 1.5.0. So the sparse-interval trap is
about **sparsity**, not about custom ranges: hundreds of tiny intervals break,
a handful of wide ones are fine, preset or not. That is a narrowing of the rule
below, not an exception to it.

**Failure modes, and which layer each accuses** (kept because each still names
a different layer, and none of them is the font by default):

- **Lists in the picker, reverts to built-in Noto on opening a book** → a
  build-time problem, not a rendering one. Suspect a **truncated file** first —
  check `GET /api/fonts` against `reference/fonts/CHECKSUMS.tsv` — and the
  **intervals** second, if you rebuilt with narrower ranges than the command
  above.
- **Boxes or blanks where the letters should be** → the font is loading and
  the glyphs are missing: presentation forms absent from the build. Not the
  intervals as a whole, the *coverage*.
- **Correct letters, wrong order** (words reversed, punctuation at the wrong
  end) → a **bidi** problem, and nothing a font can fix. Bidi runs before
  shaping and is independent of it.
- **Menus fine, page broken** → the expected stock-1.5.0 state, and the reason
  this family exists. It means no SD font is selected, or the selection did not
  survive the boot scan.

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
this path. **Device-confirmed 2026-08:** an X3 drew a wallpaper built by
`wallpaper-maker/` as its sleep screen, from `/.sleep`, mode Custom, quantised
off-device against the even ramp — the render is correct on the glass, which is
also what settled the tuning question below. Panel-sized files only so far: an
image small enough to be matted has not been put on the panel, so the placement
rules for an under-size image remain read-from-source.* `wallpaper-maker/` builds these and
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
| **The dither ships two tunings, and runs the wrong one here** | `AtkinsonDitherer::processPixel` and `FloydSteinbergDitherer::processPixel` each contain an even ramp (thresholds **43/128/213** → **0/85/170/255**) behind `if (false)`, and a live override labelled *"fine-tuned to X4 eink display"* (thresholds **30/50/140** → **15/30/80/210**). There is no X3 branch. **Device-confirmed 2026-08: the live X4 tuning is visibly too bright on an X3** — its reconstruction values sit below what the panel shows, so `error = adjusted - quantizedValue` runs too positive and each pixel pushes its neighbours lighter (at input 128 it charges +48 where the true error is about -42; ~30 levels of lift on a photograph). Quantise off-device against the **even** ramp, and ship 4-bpp so the live branch never runs. Upstream issue candidate |
| Native palette | If every palette entry's luma is within ±21 of 0/85/170/255, the firmware maps pixels straight through (`lum >> 6`) and **dithers nothing**. Otherwise it re-dithers on-device (Atkinson). `adjustPixel` is identity — shipped with `USE_BRIGHTNESS = false` |
| Grey vs BW | `hasGreyscale()` is `bpp > 1`: a 1-bpp file gets the plain BW waveform, never the four-level grey pipeline |
| Size | Panel-exact **528×792**. Larger is scaled down and centred; **smaller is never scaled up** — it is centred in black |
| Refresh | A single HALF refresh (stock parity), plus two extra passes for the grey planes |

Two shapes work, and they are a real choice:

- **528×792, 24-bpp greyscale, undithered** — the firmware quantises it with
  Atkinson. Simple and gives a good picture, but six times the bytes and the
  ESP32 redoes the arithmetic on every sleep.
- **528×792, 4-bpp indexed, 40-byte DIB header, palette on 0/85/170/255,
  pre-quantised** — trips the native-palette test, so the panel shows exactly
  the pixels computed off-device, at a sixth of the bytes, and the firmware's
  X4-tuned dither never runs. This is what `wallpaper-maker/` does, quantising
  with the firmware's algorithm against the **even** ramp (see the row above).

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
a 1.5.0 RC. `/move` too, in the `/Books` test recorded under Subfolders below —
`mkdir` + `move` put a book in a folder and the reader opened it there.
`/api/fonts`, `/api/fonts/upload` and the `fontFamily` setting were driven end
to end from `tgbot/` (2026-08), as was **WebDAV `MOVE` on a directory**, which
renamed a font family folder with its contents intact. What is still
read-from-source: `/download` and the WebSocket upload on 81. The font
registry rows below are read from `lib/EpdFont/SdCardFontRegistry.cpp` and
`src/network/CrossPointWebServer.cpp` at the 1.5.0 tag.*

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
| Renaming loses your place | **A rename or move through the web API forgets where you were reading.** Reading state is keyed by *path*: `RecentBook` is `{path, title, author, coverBmpPath}` and `operator==` compares only the path, so the entry in `/.crosspoint/recent.json` still names a file that is no longer there, and the file at the new path matches no entry and opens at the start. The fix is written and unused — `RecentBooksStore::updatePath(oldPath, newPath, oldCachePath, newCachePath)` repoints an entry, moves its cover cache with it, persists, and keeps its list position. **Nothing in the firmware calls it: zero call sites anywhere, not just in the web server** (the device's own file browser cannot rename or move at all — long-press deletes, short-press opens, and that is the whole of it). The handlers also `clearBookCache()`, deleting the parsed cache under `/.crosspoint/`, so the book re-parses on next open. **Device-confirmed 2026-08** — a book moved into `/Books` opened fine and had lost its position. Upstream issue candidate: the web move/rename handlers are the only place that can call `updatePath`, and they don't |
| Subfolders | **Books in folders work.** `mkdir` + `move` put a book in `/Books` and the reader still found and opened it (device-confirmed 2026-08). The firmware expects this: `SETTINGS.opdsDownloadFolder` (`char[64]`, empty = SD root) sends OPDS downloads into a folder, creating it if missing |
| Names on the card | Whatever is there, byte for byte, and `Storage.exists()` compares bytes. Books from other catalogs arrive **NFD-decomposed** (`é` as `e` + U+0301); normalizing a name before sending it back yields `Item not found` against a file plainly present. Round-trip listings verbatim |
| Settings | `GET`/`POST /api/settings`, partial JSON by key (`{"sleepScreen": 2}`); keys are in `src/SettingsList.h` |
| Delete | `POST /delete?path=...`, or `paths=` with a **JSON array** to batch. A directory is removed only when already **empty**, so clearing one means the files first and the folder after |
| Fonts | `GET /api/fonts` → `{families:[{name, sizes:[…], files:[{name,size}]}], maxFamilies}` (128). `POST /api/fonts/upload` with `family` + `file` — the firmware creates the family directory itself, and checks the `CPFONT\0\0` magic on the first chunk, so a "save link as" HTML page is refused. It cannot catch a **truncated** upload: the magic is fine and the file is short. Verify sizes against `reference/fonts/CHECKSUMS.tsv` |
| Font delete | `POST /api/fonts/delete`, JSON body `{"family":"WenZilla"}` — removes the whole family through the firmware's own `FontInstaller` and marks the registry dirty. Prefer it to deleting the files by hand |
| Font rename | **There is no rename endpoint, and none is needed: the family name *is* the folder name.** `SdCardFontRegistry::scanRoot` takes `family.name` straight from the directory entry, and `parseFilename` only reads `<anything>_<size>.cpfont` for the size — the file prefix is never compared to the folder. So renaming a family is renaming one directory, and the `.cpfont` files inside can keep any prefix. `/rename` refuses directories, so it goes through **WebDAV `MOVE`**, which means the visible `/fonts` only — a family under `/.fonts` cannot be renamed by any route |
| Fonts roots, and which one you get | Two are scanned, `/.fonts` **first** then `/fonts` (`discover()`), de-duplicated by name — first root wins. A fresh install picks the root from `defaultRoot()`: `/.fonts` if it exists, else `/fonts` if it exists, else **`/.fonts`**. So a device that has never held a font gets its first upload put somewhere `/api/files` hides and WebDAV refuses. `GET /api/fonts` reports names, sizes and files but **never a path**, so the only way to tell which root a family is in is to look for it in `/fonts` |
| A family the reader ignores | The scan skips any directory *or* file whose name starts with `.` or `_`. Rename a family to `_Old` and it vanishes from the picker with no error — the folder is still there |
| When the font list re-scans | At boot, and otherwise **only when something marks the registry dirty — the font upload and delete endpoints do, nothing else does**. `GET /api/fonts` calls `refreshIfDirty()`, not `discover()`. So after a WebDAV rename the reader still reports the *old* family name, and `fontFamily`'s `options` still carries it, until the next boot. Renaming the family that is currently selected therefore drops the selection (it is stored by name, `SETTINGS.sdFontFamilyName`, and the next boot's scan clears a name it cannot find) |
| **Forcing a re-scan without a power-cycle** | `POST /api/fonts/delete` with a family name that exists in **neither** root. `FontInstaller::deleteFamily` walks both roots, sees nothing (`sawAny == false`), removes nothing and returns **OK — "already gone"** — and the handler marks the registry dirty on OK. The next `GET /api/fonts` then runs `discover()` for real. So a delete aimed at a name that cannot exist is a remote re-scan, and the only safe way to have one: pick a random name that passes `isValidFamilyName` and matches no family. This is what makes rename usable at all, and it also makes a family copied to the card by hand appear without rebooting |
| **The device's own file browser hides fonts** | `FileBrowserActivity` lists directories plus files ending `.epub`, `.xtc`, `.txt`, `.md` or `.bmp` — and nothing else (`src/activities/home/FileBrowserActivity.cpp:55-57`; the firmware picker mode narrows it further, to `.bin`). A `.cpfont` is in none of those lists, so **every font family folder looks empty when browsed on the reader**, including the ones that work. Check a family in Settings → Reader → Font Family, which reads the registry, never in the file browser, which reads the extensions. *Device-observed 2026-08, and it cost an evening of believing a working rename had emptied a folder.* `/api/files` over WiFi has no such filter and lists everything, so the bot and the device disagree here by design |
| A stale registry is visible in the bytes | `handleFontList` stats each file at the path the registry recorded and writes `size: 0` when it will not open. A family listing **every file at 0 B** is therefore not an empty family — it is a registry pointing at paths that no longer exist, i.e. something moved or deleted the folder since the last scan |
| Family names the firmware will not touch | `FontInstaller::isValidFamilyName` accepts **only** `[A-Za-z0-9_-]` — no spaces, dots or accents — and it guards the **delete** as well as the upload. Nothing enforces it on the way in: a folder created by hand on the card, or renamed through WebDAV (which applies no such rule), can carry any name the filesystem allows. Such a family is scanned and listed and readable, and `POST /api/fonts/delete` answers **500 for the name itself** — it never gets as far as looking for the folder. Rename it back to a plain name and the delete works. Upstream issue candidate: the two rules should be the same rule |
| Leaving File Transfer mode | **No endpoint, no WebSocket command, nothing.** The full route table is the 18 routes above plus `onNotFound` and the WebDAV handler; the WebSocket on 81 speaks only `START`/binary/`DONE`. The server stops when `CrossPointWebServerActivity` sees the **Back button** (`mappedInput.wasPressed(Button::Back)` → `onGoHome()`), polled inside its own client loop. Nothing remote can end the session, so "push and the reader goes back to sleep by itself" is not available. Upstream issue candidate: a `POST /api/exit` would cost the firmware very little |
| Font size | A CJK family is ~24 MB in six files, the largest 6.8 MB. Minutes over WiFi; the twenty-second timeout used everywhere else is far too short. `NaskhFull` is 1.1 MB in four files and goes up in seconds — the timeout only bites on the big ones |
| Settings shape | `GET /api/settings` returns a **list** of `{key, name, category, type, value}`, and an enum also carries an `options` array of its labels. Not a flat key→value map |
| Selecting a font | `fontFamily` is an enum whose index depends on the scanned registry (built-ins, then SD families) — but the setter stores the **name** (`SETTINGS.sdFontFamilyName`) and the getter resolves it back each time. So a family can be selected *before* the reboot that makes it usable: the choice persists and the next boot's scan makes it real. Find the index by looking the family up in `options`, never by adding to a built-in count. `POST /api/settings` rebuilds the list from the live registry, which `/api/fonts/upload` marks dirty, so a just-uploaded family is already there |
| WebDAV | Port 80, PUT overwrites atomically. **Refuses any path segment beginning with `.`** — `isProtectedPath` walks every segment, not just the last — so `/.sleep` and everything in it is unreachable this way, and `/sleep` is not |
| Folders | `/rename` and `/move` both end in "Only files can be…"; a **directory can only be renamed through WebDAV `MOVE`**, which has no directory check and calls the filesystem's own rename. So a visible folder like `/sleep` can be renamed and `/.sleep` cannot, by either route. **Device-confirmed 2026-08:** `/fonts/NaskhFull` → `/fonts/RenameTest` moved the folder with all four `.cpfont` files intact, and the reader listed the family under its new name with the right sizes and bytes. Contents come across; the operation is not the shallow rename it might look like |
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

**Building one, without losing an evening to it.** There is no font tool in
this suite and there should not be: the upstream builder is maintained, and
duplicating it would mean maintaining a copy. What this repo has instead is
the list of ways it goes wrong. If you are building a family — by hand or with
the web builder — this is the whole of it:

1. **Intervals: a broad preset** (`latin-ext,cjk`). Never the book's exact
   charset. A subset font passes every check the firmware makes, lists in the
   picker, and then silently reverts to built-in Noto when a book is opened.
   Glyphs stream from SD, so the big font costs no RAM — there is nothing to
   gain by subsetting and an evening to lose.
2. **Sizes `8,10,12,14,16,18`.** The last three are for reading; the first
   three are what 1.5.0's interface fallback looks for by **exact** size. Omit
   them and books render perfectly while the chapter list, library and file
   browser draw *blank* for CJK — no error, no tofu, nothing.
3. **Regular only.** Bold and italic duplicate 22k CJK bitmaps per style for no
   CJK gain, since WenKai has no bold or italic.
4. **Layout `/fonts/<Family>/<Family>_<size>.cpfont`.** Loose files are ignored,
   and the scan happens at boot only — a file copied to a running device does
   not exist as far as the reader is concerned.
5. **Verify the bytes afterwards.** Discovery parses filenames and never opens
   a file, so a truncated copy is indistinguishable from a good one until you
   select it. `GET /api/fonts` reports byte counts; compare them against
   `reference/fonts/CHECKSUMS.tsv` (the Telegram bot does this automatically
   after a push).

Reading the symptom backwards: *lists but reverts to Noto* → sparse intervals,
or a truncated file, in that order. *Chapter list blank* → no 8/10/12. *Tofu at
8 pt but nothing at 10/12* → that is the U+FFFD tell described above, and it
means the glyph is genuinely missing rather than the font being broken.

1. **Use broad preset intervals — never sparse custom ranges.** This is the
   hard-won one: fonts subset to a book's exact charset (hundreds of tiny
   Unicode intervals) pass every structural check in the firmware's parser
   yet **silently fail to load on-device** — the font lists in the picker,
   but opening a book reverts the setting to built-in Noto. The identical
   font content built with broad presets (`latin-ext,cjk` → ~100 wide
   intervals, 22.5k glyphs) loads and renders. Glyphs stream from SD, so the
   big font costs no RAM. *Upstream issue candidate; found and verified on
   1.4.1, not re-tested since — the converter and loader are unchanged in
   1.5.0, so assume it still holds.* **What it is not** is a rule against
   custom ranges as such: `NaskhFull` is built from four hand-written ranges
   (18 validated intervals) and loads and renders on 1.5.0, device-confirmed
   2026-08. Sparsity is the trap; a handful of wide ranges is not sparse.
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
# The tag is `v1.5.0` — upstream's earlier tags have no `v`, and this line
# said `1.5.0` until 2026-08, which raw.githubusercontent answers with a
# 404 page that curl -O happily saves as a 14-byte "script".
BASE=https://raw.githubusercontent.com/crosspoint-reader/crosspoint-reader/v1.5.0/lib/EpdFont/scripts
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

# CrimKai: WenZilla with the Latin half swapped for a book serif — NV Scarlet
# (Cochineal, Michael Sharpe's extension of Sebastian Kosch's Crimson), CJK
# still from WenKai. Same repo as Zilla: github.com/nicoverbruggen/ebook-fonts
# (fonts/extra, NV_Scarlet-Regular.ttf). No synth step: Cochineal draws every
# pinyin tone vowel itself, so the source font is used as it ships.
python3 fontconvert_sdcard.py --intervals latin-ext,cjk \
    --sizes 8,10,12,14,16,18 \
    --regular NV_Scarlet-Regular.ttf --fallback-regular LXGWWenKai-Regular.ttf \
    --name CrimKai --output-dir CrimKai/

# NaskhFull (Arabic) is built with the same converter and four sizes instead
# of six; its command, its sources and why its intervals are spelled out by
# hand are in "Arabic in the book body" above.
```

The recipe is deterministic: re-running it reproduces the committed 12 pt files
**byte for byte** (both families when the 8 and 10 pt sizes were added, 2026-07;
`WenZilla_12` again when CrimKai was built, 2026-08). That is the cheap way to
confirm your sources and script version match this repo's before you trust a
new size you built — and it is worth doing *first*, because it fails loudly
where a mismatched WenKai or converter would otherwise just quietly hand you a
family that differs from the committed one. The WenKai in the 2026-08 check was
Ubuntu noble's `fonts-lxgw-wenkai` 1.315+repack-1.

**Which WenKai is not a detail.** Re-run before building `NaskhFull` (2026-08):
that same Ubuntu package (an 18,769,824-byte TTF) reproduces
`WenZilla_12.cpfont` byte for byte, md5 `fd33f3fc245ca18e86649c236986bb28`.
Upstream's own `v1.315` release TTF (18,950,002 bytes) does **not** — identical
22,415 glyphs over 61 intervals, but a file 41 bytes shorter and a different
md5. The repack is this repo's source of record. If a reproduction comes out
tens of bytes off, check the TTF before suspecting the converter.

## Font style guide

- **LXGW WenKai (楷体/kaiti)** — models brush-written stroke shapes, the
  typographic tradition HSK textbooks use. Best for learners: what you read
  is what you should write. SIL OFL.
- **NV Zilla Slab** — a slab serif (Mozilla's Zilla Slab, e-reader-tuned by
  nicoverbruggen) used for the Latin/pinyin half of WenZilla: even weight,
  sturdy at small e-ink sizes, and a friendlier companion to kaiti hanzi than
  WenKai's own Latin. SIL OFL.
- **NV Scarlet** — an oldstyle garalde (Cochineal, Michael Sharpe's extension
  of Sebastian Kosch's Crimson, e-reader-tuned by nicoverbruggen) used for the
  Latin/pinyin half of CrimKai. The book-typography answer to Zilla's slab:
  measured at 150 dpi it puts the same 2 px stem on the screen at 12 pt and
  3 px at 16 pt, with a marginally larger x-height and about a tenth less ink
  overall — so it reads lighter without going spindly, and its pen-written
  roots echo kaiti's brush-written ones. Carries every pinyin tone vowel
  itself. SIL OFL.
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
- **Reading a font folder in the device's own file browser.** It lists books
  and wallpapers by extension and nothing else, so a font family folder is
  *always* empty there — a working one and a broken one look identical. Judge a
  family in Settings → Reader → Font Family. Believing the browser instead
  turned a rename that had worked perfectly into an evening of chasing
  filesystem corruption that was never there.
- **Believing the font list after you have changed the fonts folder.** The
  registry is a boot-time snapshot; only an upload or a delete refreshes it.
  Rename a family folder over WebDAV and the reader will go on listing the old
  name, reporting every file at 0 B (the recorded paths no longer open), while
  a delete of that name answers "OK, already gone" and removes nothing. None of
  that is damage: nothing is lost, and one forced re-scan — or a power-cycle —
  makes the whole picture correct. The trap is reading the stale list as the
  state of the card. *Hit on 2026-08 through `tgbot/`, and the reason it now
  re-scans after every rename.*

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
- https://github.com/notofonts/notofonts.github.io (Noto Naskh Arabic, Noto
  Sans) · https://github.com/mintty/mintty (the bidi/shaping port's upstream)
- https://github.com/bigbag/papyrix-reader ·
  https://github.com/aBER0724/crosspoint-reader-cjk
- On-device photo evidence: stock tofu (2026-07); CrossPoint + WenKaiFull
  five-mode diagnostic (2026-07)
