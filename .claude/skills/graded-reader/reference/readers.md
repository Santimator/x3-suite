# Target reader notes — Xteink X3 / CrossPoint

The deliverable EPUB is meant to drop into a Calibre-Web-Automated ingest folder
and ultimately render on an **Xteink X3** (ESP32-C3, ~400 KB RAM) running either
the stock firmware or **CrossPoint** (open-source replacement firmware;
CrossPoint 1.4 is current as of mid-2026). A CJK-focused fork
(`crosspoint-reader-cjk`) also exists but supports the **X4 only**.

## CONFIRMED on-device (stock firmware, 2026-07): no CJK glyphs

Device test with `letter-writer.epub` on a stock-firmware X3:

- **Every Han character renders as a tofu box (□).** Latin text — including
  pinyin *with tone marks* (yuǎn fāng) and the English glossary — renders
  perfectly. Diagnosis: the stock font has Latin + Latin-Extended coverage and
  **zero CJK glyphs**. This is a font problem, not a markup/encoding problem.
- **Ruby `<rt>` pinyin did not appear** above (or inline with) the body text —
  the stock engine appears to drop ruby annotation text entirely. So even with
  a CJK font, stock likely shows hanzi-only body text.

CrossPoint's user guide confirms the same for its defaults: built-in fonts
cover Latin/Cyrillic/Vietnamese, and **CJK is explicitly unsupported until you
install a custom SD-card font**.

## The fix is reader-side (books are fine)

Embedding a font in the EPUB (`@font-face`) will NOT help on this class of
device: the renderer uses pre-converted bitmap fonts (`.cpfont`) and cannot
rasterize an embedded TTF. Do not fatten the books; fix the device:

1. **Flash CrossPoint** (supports X3 + X4): https://crosspointreader.com/ —
   web-flash from the browser; stock firmware can be restored later.
2. **Install a CJK font on the SD card.** Options, best first:
   - **Use the prebuilt fonts in `workspace/CHARSET/fonts/`** (recommended) —
     the folder mirrors the SD layout (one subfolder per family), so copy the
     whole `fonts/` folder to the SD card root as-is; see its README.
     Two families, both regular+bold, subset to the full HSK 1-4 pipeline
     universe (1223 glyphs), built with CrossPoint's own
     `fontconvert_sdcard.py` (see "Building the font offline" below):
     - `LXGWWenKai-GradedReader_{12,14,16,18}.cpfont` — **霞鹜文楷 (LXGW
       WenKai)**, the open-source kaiti: brush-written regular-script style
       like HSK textbook typography, canonical handwritten stroke shapes,
       the e-reader community's favorite for Chinese. SIL OFL 1.1.
       0.35–0.73 MB per size. **Pick this one for reading.**
     - `NotoSansSC-GradedReader_{12,14,16,18}.cpfont` — Noto Sans SC, a
       clean print-style sans; crisper at very small sizes. OFL. Backup.
   - Grab a prebuilt full-CJK `.cpfont` from
     https://github.com/crosspoint-reader/crosspoint-fonts — works, but full
     CJK is 20k+ glyphs and the CJK fork warns big fonts can OOM the ESP32.
   - Layout matters: the firmware scans `/fonts/` (or `/.fonts/`) for
     **family folders** — loose `.cpfont` files are ignored. One folder per
     family, size files inside:
     `/fonts/LXGWWenKai-GradedReader/LXGWWenKai-GradedReader_12.cpfont` etc.
     The scan runs **once at boot**, so power-cycle after copying. Fonts
     then appear under **Settings → Reader → Font Family**. ("Manage Fonts"
     is the WiFi download store — not needed for SD fonts.)

   **Note on the web font builder** (https://crosspointreader.com/fonts): it
   is a wrapper around the converter script and **requires you to upload a
   base TTF/OTF yourself** — it ships no fonts. If you have nothing to feed
   it, use the offline build below (or the prebuilt files above) instead.

   **If selecting the font "doesn't stick"** (books show no hanzi and the
   setting reverts to a built-in Noto): the firmware clears
   `sdFontFamilyName` whenever `loadFamily()` fails at book-open time. Our
   full fonts pass every *structural* check in the 1.4.x parser (verified by
   re-implementing `SdCardFont::load()` against the files), so the remaining
   suspect is heap exhaustion on the ESP32-C3 while allocating the resident
   tables (intervals/advance/prewarm × styles). Use the lean variant in
   `workspace/CHARSET/fonts/` first:
   `WenKaiHSK_{12,14,16,18}.cpfont` and `NotoHSK_...` — single style,
   books-only charset (533 glyphs, 71-155 KB/file), ~5× lighter resident
   footprint. Bold headings render in regular weight; acceptable trade.
   Note the official crosspoint-fonts catalog ships **no CJK family at
   all** — CJK on this device is community-pioneer territory.
3. **Re-run the diagnostic EPUB** (below) to pick the pinyin mode — ruby
   support under CrossPoint is still unconfirmed; `interlinear` is the likely
   winner, `plain` the safe floor.

## Subsetting: the graded-reader advantage

A graded reader's character universe is small and *known*. `scripts/charset.py`
scans the built book(s) and emits exactly what a font must cover:

```
python scripts/charset.py workspace/BOOK [workspace/BOOK2 ...] --out-dir DIR
```

- `CHARSET.txt` — every distinct character (all four current books together:
  **536 chars, 403 hanzi**) for `pyftsubset --text-file=...`
- `INTERVALS.txt` — merged codepoint ranges for CrossPoint's
  `fontconvert_sdcard.py --intervals ...` or the custom-range field of the web
  font builder (https://crosspointreader.com/fonts)

With `--include-lists` the charset covers every character of every vocab-list
word plus all tone-marked pinyin (currently 1224 chars) — the font then covers
anything the pipeline can write at this level, not just the current books.
Font source suggestion: Noto Sans SC / Noto Serif SC (OFL-licensed).

### Building the font offline (what produced `workspace/CHARSET/fonts/`)

The web builder needs a base font uploaded; this is the same thing headless:

```bash
pip install freetype-py fonttools
curl -sSLO https://raw.githubusercontent.com/crosspoint-reader/crosspoint-reader/master/lib/EpdFont/scripts/fontconvert_sdcard.py
curl -sSLO https://raw.githubusercontent.com/crosspoint-reader/crosspoint-reader/master/lib/EpdFont/scripts/cpfont_version.py
curl -sSLo noto.otf      https://raw.githubusercontent.com/notofonts/noto-cjk/main/Sans/SubsetOTF/SC/NotoSansSC-Regular.otf
curl -sSLo noto-bold.otf https://raw.githubusercontent.com/notofonts/noto-cjk/main/Sans/SubsetOTF/SC/NotoSansSC-Bold.otf

# INTERVALS.txt -> the converter's (0xA-0xB),(0x20-0x7E),... syntax
python3 -c "
segs = open('INTERVALS.txt').read().strip().split(',')
print(','.join(f'({a}-{b or a})' for a, _, b in (s.partition('-') for s in segs)))" > intervals_arg.txt

python3 fontconvert_sdcard.py --intervals "$(cat intervals_arg.txt)" \
    --sizes 12,14,16,18 --regular noto.otf --bold noto-bold.otf \
    --name NotoSansSC-GradedReader --output-dir fonts/
```

Copy the resulting `.cpfont` files into a family folder on the SD card —
`/fonts/<FontName>/<FontName>_SIZE.cpfont` — power-cycle the reader (fonts
scan at boot), then pick the family under Settings → Reader → Font Family.
Rebuild only when `charset.py` reports characters outside the current set.

The WenKai build is identical except for the sources: `LXGWWenKai-Regular.ttf`
+ `LXGWWenKai-Bold.ttf` as `--regular`/`--bold` with the Noto files as
`--fallback-regular`/`--fallback-bold` (WenKai lacks exactly one of our
glyphs, the ↩ back-link arrow — the fallback supplies it). When github.com
is unreachable, the Ubuntu archive carries the TTFs as `fonts-lxgw-wenkai`
(`apt-get download fonts-lxgw-wenkai && dpkg-deb -x ...`); upstream is
https://github.com/lxgw/LxgwWenKai (releases).

### Font style guide (which family for what)

- **LXGW WenKai (楷体 style)** — the "pretty and readable" pick: models real
  brush-written stroke order and shapes, the same typographic tradition HSK
  textbooks use for body text. Best for learners: what you read is what you
  should write.
- **Noto Sans SC (black/sans style)** — sturdier at tiny sizes and for UI.
- Other open kaiti/brush options if taste differs: TW-Kai (Taiwan MOE,
  traditional-oriented), AR PL UKai (older, Arphic license), Ma Shan Zheng
  (Google Fonts, true brush calligraphy — pretty but tiring for body text).
  Making a font from scratch is a different hobby: ~1200 hand-drawn glyphs
  even for our subset. Unnecessary — WenKai already is the thing.

## Ruby — still not confirmed under CrossPoint

Nothing in CrossPoint's release notes, user guide, or the CJK fork mentions
`<ruby>`/`<rt>`. On stock firmware the annotation text is dropped. Do not bet
the structure on ruby: `build_epub.py` keeps pinyin display a parameter
(`ruby` / `interlinear` / `plain`).

### How to settle it (one device test)

```
python scripts/build_epub.py BOOK --out render-test.epub --diagnostic
```

One EPUB, chapter 1 rendered three ways on labeled pages. Flip through, pick
the cleanest, set `pinyin_mode` in the book's `book.json`, rebuild. Re-test
only after firmware changes.

## Sources

- https://github.com/crosspoint-reader/crosspoint-reader (X3+X4; USER_GUIDE:
  default fonts have no CJK; SD fonts enable it; docs/sd-card-fonts.md)
- https://github.com/crosspoint-reader/crosspoint-fonts (prebuilt .cpfont, CI)
- https://crosspointreader.com/ + /fonts (web flasher; browser font builder
  with Chinese-Simplified preset and custom ranges)
- https://github.com/aBER0724/crosspoint-reader-cjk (X4 only; warns large CJK
  fonts OOM the ESP32-C3 — motivates subsetting)
- On-device photo evidence, stock X3, 2026-07 (tofu boxes; rt dropped)
