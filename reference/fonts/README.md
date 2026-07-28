# SD-card fonts for the X3 (CrossPoint firmware)

Copy the family folders onto the SD card under `/fonts/` — the layout below is
exactly what CrossPoint expects (`/fonts/<Family>/<Family>_<size>.cpfont`).
Fonts are scanned **once at boot**, so power-cycle the reader after copying,
then pick the family under **Settings → Reader → Font Family**. ("Manage
Fonts" is the WiFi download store; not needed for these.)

| Family | Use for | What it is |
|---|---|---|
| `WenZilla` | **Chinese books (recommended)** | Hybrid: **NV Zilla Slab** for Latin + **LXGW WenKai** for CJK. Chinese in kaiti (like HSK textbooks); Latin and pinyin in a warm slab serif instead of WenKai's plainer Latin — so mixed hanzi+pinyin (`gloss-pinyin`) and any Latin in the text read nicely. Full CJK, 22.4k glyphs, streamed from SD. Regular only. |
| `WenKaiFull` | Chinese books (pure kaiti) | LXGW WenKai 霞鹜文楷 (kaiti / brush style) + Noto fallback, Latin included from WenKai itself. Full CJK — 22.5k glyphs. **Device-confirmed working**; the baseline WenZilla is built on. |

All SIL OFL licensed. Built with CrossPoint's own `fontconvert_sdcard.py`; the
reproducible recipe (and the full device debugging record, including why
charset-subsetted fonts must NOT be used) is in `../readers.md`.

**About the pinyin tones (WenZilla):** NV Zilla Slab ships most pinyin vowels
but lacks eight — `ǎ ǐ ǒ Ǎ` and the ü-tones `ǖ ǘ ǚ ǜ`. `synth_pinyin.py`
(here) draws them into Zilla's own style as composites (base vowel + the font's
existing spacing caron / diaeresis / macron), so **all** pinyin renders in
Zilla, not in a fallback face. Run it before the font build.

**Hard-won rule: build CJK fonts with broad preset intervals
(`latin-ext,cjk`), never with sparse custom ranges.** Sparse-interval
`.cpfont` files pass every format check but silently fail to load on the
device (the font setting reverts to built-in Noto). Confirmed on CrossPoint
1.4.1 / X3, 2026-07 — upstream issue candidate.
