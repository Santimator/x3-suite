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
| `NaskhFull` | **Arabic books** | Noto Naskh Arabic + Noto Sans fallback, Latin included from Naskh itself. Carries the Arabic **presentation forms** the firmware's shaper emits — which no built-in reading font does, so without it an Arabic book body renders nothing. 2k glyphs, 1.1 MB, four sizes. Regular only. **Not device-confirmed** — see below. |

All SIL OFL licensed. Built with CrossPoint's own `fontconvert_sdcard.py`; the
reproducible recipe (and the full device debugging record, including why
charset-subsetted fonts must NOT be used) is in `../readers.md`.

**Why six sizes (8, 10, 12, 14, 16, 18) — for the three CJK families.**
`NaskhFull` ships four (12–18) and the Arabic section below says why. 12–18 are reading sizes. 8, 10 and
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

**Arabic: why a font is the whole fix (`NaskhFull`).** CrossPoint does the
bidi reordering and the contextual joining itself, in `lib/MiniBidi/`, and it
does them in that order — reorder to visual order first, then shape. The
shaper does not hand the renderer your original letters: it emits **Unicode
presentation forms**, the pre-joined initial/medial/final/isolated shapes in
U+FE70–U+FEFC, plus U+FB50–U+FBFF for the Perso-Arabic letters and
U+FEF5–U+FEFC for the lam-alef ligatures. So a font covering the Arabic block
U+0600–U+06FF and nothing else is covering the codepoints the *book* contains
and none of the ones the *device* asks for, and every word draws blank. The
built-in interface fonts do carry those forms; the built-in reading fonts
(Noto Sans/Serif 12–18) carry no Arabic at all, and nothing rescues them — the
only font redirect in the renderer fires on CJK codepoints. `NaskhFull` is the
fix, and the mechanism with source citations is in `../readers.md`.

**Why four sizes here (12, 14, 16, 18).** The small three exist in the CJK
families for 1.5.0's interface fallback, and that fallback is CJK-gated: it
probes 一/あ/ア/가 and gives up on a family that has none of them, so an Arabic
family would never be asked for 8/10/12 pt no matter what it ships. It is also
not needed — the built-in UI fonts already draw Arabic, which is why the menus
have always looked fine while the book body did not. Four files, 1.1 MB total,
against ~4 MB of dead weight.

**First thing to suspect if it misbehaves: the intervals.** `NaskhFull` is
built from four broad ranges rather than a preset, because the converter has
no `arabic` preset to use (it has `hebrew`, and that is the shape the Arabic
one would take). Four wide ranges is a world away from the sparse-subset trap
below — the built font has 18 intervals, next to WenZilla's 61 — but it is
still custom ranges, and that is the untested variable. If the family lists in
the picker and then reverts to Noto when you open a book, suspect the
intervals before anything else, and a truncated file second.

**Hard-won rule: build CJK fonts with broad preset intervals
(`latin-ext,cjk`), never with sparse custom ranges.** Sparse-interval
`.cpfont` files pass every format check but silently fail to load on the
device (the font setting reverts to built-in Noto). Found on CrossPoint
1.4.1 / X3, 2026-07, and not re-tested since — the converter and the loader
are unchanged in 1.5.0, so assume it still bites. Upstream issue candidate.
