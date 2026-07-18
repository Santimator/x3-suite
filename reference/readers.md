# Device notes — Xteink X3 (CrossPoint firmware)

The suite's EPUBs target an **Xteink X3** (ESP32-C3, ~400 KB RAM, 528×792
e-ink) running **CrossPoint** (open-source firmware; 1.4.1 as of mid-2026),
fed through a Calibre-Web-Automated ingest folder. Everything below is
**device-confirmed** (photos, 2026-07) unless marked otherwise.

## The working recipe (confirmed end-to-end)

1. **Firmware:** CrossPoint (web-flash from https://crosspointreader.com/;
   stock is restorable the same way). Stock firmware ships no CJK glyphs at
   all — hanzi render as tofu boxes — so it is not an option for Chinese.
2. **Font:** `reference/fonts/WenKaiFull/` → copy the folder to the SD card
   under `/fonts/`, power-cycle (fonts scan once at boot), select
   *WenKaiFull* under Settings → Reader → Font Family. Full-CJK LXGW WenKai
   (kaiti style), glyphs streamed from SD. `EBGaramond/` is the matching
   Latin upgrade. Install details: `reference/fonts/README.md`.
3. **Books:** build with `pinyin_mode: gloss-pinyin` (or `gloss-underline` /
   `plain`) — see the mode verdicts below.

## Rendering verdicts (what the engine actually does)

| Feature | Verdict |
|---|---|
| `<ruby>` pinyin | **Broken** — `<rt>` leaks inline: 石shí头tou |
| Interlinear (CSS inline-block stacking) | **Broken** — collapses inline: shí石tou头 |
| Plain hanzi body | **Perfect** |
| `gloss-*` marked-plain modes | Work (plain text + `<u>` / trailing Latin) |
| Embedded EPUB fonts (`@font-face`) | Ignored — the renderer only rasterizes pre-converted `.cpfont` bitmaps; never fatten the books with fonts |
| Glossary/internal links | Harmless; not tappable (no touchscreen) — kept for phone reading |

Keep chapter CSS trivial: the engine honors basic text properties only.
`ruby`/`interlinear` modes remain in the builder for capable readers
(Apple Books renders ruby beautifully) — never for the X3.

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
   big font costs no RAM. *Upstream issue candidate; verified on 1.4.1.*
2. **Layout:** one folder per family — `/fonts/<Family>/<Family>_<size>.cpfont`;
   loose files are ignored. Scan happens at boot only.
3. **Reproducible build** (what produced `reference/fonts/`):

```bash
pip install freetype-py fonttools
curl -sSLO https://raw.githubusercontent.com/crosspoint-reader/crosspoint-reader/master/lib/EpdFont/scripts/fontconvert_sdcard.py
curl -sSLO https://raw.githubusercontent.com/crosspoint-reader/crosspoint-reader/master/lib/EpdFont/scripts/cpfont_version.py

# WenKaiFull: LXGW WenKai + Noto Sans SC fallback, full CJK presets.
# TTF sources: github.com/lxgw/LxgwWenKai releases, or Ubuntu's
# fonts-lxgw-wenkai package; Noto from github.com/notofonts/noto-cjk.
python3 fontconvert_sdcard.py --intervals latin-ext,cjk \
    --sizes 12,14,16,18 \
    --regular LXGWWenKai-Regular.ttf --fallback-regular NotoSansSC-Regular.otf \
    --name WenKaiFull --output-dir WenKaiFull/

# EBGaramond: Latin only, three real styles (Ubuntu: fonts-ebgaramond).
python3 fontconvert_sdcard.py --intervals latin-ext \
    --sizes 12,14,16,18 \
    --regular EBGaramond12-Regular.otf --bold EBGaramond12-Bold.otf \
    --italic EBGaramond12-Italic.otf \
    --name EBGaramond --output-dir EBGaramond/
```

## Font style guide

- **LXGW WenKai (楷体/kaiti)** — models brush-written stroke shapes, the
  typographic tradition HSK textbooks use. Best for learners: what you read
  is what you should write. SIL OFL.
- **EB Garamond** — classical book serif for Latin text; warmer and less
  tiring than the built-in Noto. SIL OFL.
- Alternatives if taste differs: Noto Sans/Serif SC (print-style, sturdier
  at tiny sizes), TW-Kai (traditional-oriented), Ma Shan Zheng (true brush
  calligraphy — pretty, tiring as body text).

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
