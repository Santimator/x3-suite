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
   - **Use the prebuilt fonts in `workspace/CHARSET/fonts/`** (recommended):
     `NotoSansSC-GradedReader_{12,14,16,18}.cpfont` — Noto Sans SC
     regular+bold subset to the full HSK 1-4 pipeline universe (1223 glyphs,
     0.4–0.8 MB per size). Built with CrossPoint's own
     `fontconvert_sdcard.py`; see "Building the font offline" below.
   - Grab a prebuilt full-CJK `.cpfont` from
     https://github.com/crosspoint-reader/crosspoint-fonts — works, but full
     CJK is 20k+ glyphs and the CJK fork warns big fonts can OOM the ESP32.
   - Fonts go to `/fonts/` (or `/.fonts/`) on the SD card, or upload via the
     web interface in File Transfer mode; select under Settings → Fonts.

   **Note on the web font builder** (https://crosspointreader.com/fonts): it
   is a wrapper around the converter script and **requires you to upload a
   base TTF/OTF yourself** — it ships no fonts. If you have nothing to feed
   it, use the offline build below (or the prebuilt files above) instead.
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

Copy the resulting `.cpfont` files to `/fonts/` on the SD card (or upload via
the web interface in File Transfer mode), then pick the font in Settings.
Rebuild only when `charset.py` reports characters outside the current set.

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
