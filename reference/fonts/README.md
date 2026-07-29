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

**Why six sizes (8, 10, 12, 14, 16, 18).** 12–18 are reading sizes. 8, 10 and
12 are what CrossPoint **1.5.0+** loads to draw CJK in the *interface* — the
chapter list, the library, the file browser — because the built-in UI fonts are
Latin-only. It matches by exact point size, so a missing UI size means those
rows stay unreadable (blank on 1.5.0's predecessor 1.4.1, where the fallback
didn't exist at all). Full story in `../readers.md`, "CJK in the interface".
Two consequences: Settings → Reader → Font Size will also offer 8 and 10 as
reading sizes, and on firmware older than 1.5.0 the extra files are simply
inert.

**After copying, verify the card — do not trust the copy.** Font discovery
parses *filenames only*; it never opens a `.cpfont`. So a truncated or
half-copied file still appears in Settings → Reader → Font Size, and only fails
when you select it — at which point the family silently reverts to the built-in
Noto. Check what the device actually holds, over WiFi, no card removal:

```bash
curl http://crosspoint.local/api/fonts     # every family, file and byte count
```

Every `size` there must match `CHECKSUMS.tsv` in this directory. Device-confirmed
2026-07: an SD copy left `WenZilla_8/10` truncated at ~236 KB (of 1.7 / 2.5 MB)
and never wrote WenKaiFull's two at all, which presented exactly as "the 8 and
10 pt sizes don't work" — same visible symptom as the sparse-interval trap
below, entirely different cause. Eject the card properly (let the write buffer
flush) and re-check before concluding anything about the fonts themselves.

**About the pinyin tones (WenZilla):** NV Zilla Slab ships most pinyin vowels
but lacks eight — `ǎ ǐ ǒ Ǎ` and the ü-tones `ǖ ǘ ǚ ǜ`. `synth_pinyin.py`
(here) draws them into Zilla's own style as composites (base vowel + the font's
existing spacing caron / diaeresis / macron), so **all** pinyin renders in
Zilla, not in a fallback face. Run it before the font build.

**Hard-won rule: build CJK fonts with broad preset intervals
(`latin-ext,cjk`), never with sparse custom ranges.** Sparse-interval
`.cpfont` files pass every format check but silently fail to load on the
device (the font setting reverts to built-in Noto). Found on CrossPoint
1.4.1 / X3, 2026-07, and not re-tested since — the converter and the loader
are unchanged in 1.5.0, so assume it still bites. Upstream issue candidate.
