# SD-card fonts for the X3 (CrossPoint firmware)

Copy the family folders onto the SD card under `/fonts/` — the layout below is
exactly what CrossPoint expects (`/fonts/<Family>/<Family>_<size>.cpfont`).
Fonts are scanned **once at boot**, so power-cycle the reader after copying,
then pick the family under **Settings → Reader → Font Family**. ("Manage
Fonts" is the WiFi download store; not needed for these.)

| Family | Use for | What it is |
|---|---|---|
| `WenZilla` | **Chinese books (recommended)** | Hybrid: **NV Zilla Slab** for Latin + **LXGW WenKai** for CJK. Chinese in kaiti (like HSK textbooks); Latin and pinyin in a warm slab serif instead of WenKai's plainer Latin — so mixed hanzi+pinyin (`gloss-pinyin`) and any Latin in the text read nicely. Full CJK, 22.4k glyphs, streamed from SD. Regular only. |
| `CrimKai` | Chinese books, if you prefer a book serif | Same idea as WenZilla with the Latin half swapped: **NV Scarlet** (Cochineal — Michael Sharpe's extension of Sebastian Kosch's **Crimson**) for Latin + **LXGW WenKai** for CJK. An oldstyle garalde instead of a slab: lighter colour on the page, a slightly larger x-height, and pen-written roots that sit naturally next to brush-written kaiti. Full CJK, 22.4k glyphs. Regular only. |
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

**Get these files with GitHub's Download button or a clone — never with "save
link as", which hands you the HTML page under a `.cpfont` name.** Then verify
the card rather than trusting the copy: the reader lists fonts by filename and
never opens them, so a truncated file appears in the size menu and only fails
when selected, reverting to built-in Noto.

```bash
curl http://crosspoint.local/api/fonts     # every family, file and byte count
```

Every `size` there must match `CHECKSUMS.tsv` in this directory. The failure
modes this catches, and the others that look identical, are in `../readers.md`
under "Stupid errors".

**About the pinyin tones (WenZilla):** NV Zilla Slab ships most pinyin vowels
but lacks eight — `ǎ ǐ ǒ Ǎ` and the ü-tones `ǖ ǘ ǚ ǜ`. `synth_pinyin.py`
(here) draws them into Zilla's own style as composites (base vowel + the font's
existing spacing caron / diaeresis / macron), so **all** pinyin renders in
Zilla, not in a fallback face. Run it before the font build.

**CrimKai needs none of that.** Cochineal carries all 40-odd pinyin vowels as
drawn glyphs, tone marks included, so the family is built from the source font
untouched — every tone mark is designed rather than composited. If you are
choosing between the two on pinyin alone, that is the difference; on anything
else it is slab versus oldstyle, and taste decides.

**Hard-won rule: build CJK fonts with broad preset intervals
(`latin-ext,cjk`), never with sparse custom ranges.** Sparse-interval
`.cpfont` files pass every format check but silently fail to load on the
device (the font setting reverts to built-in Noto). Found on CrossPoint
1.4.1 / X3, 2026-07, and not re-tested since — the converter and the loader
are unchanged in 1.5.0, so assume it still bites. Upstream issue candidate.
