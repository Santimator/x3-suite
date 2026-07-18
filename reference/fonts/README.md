# SD-card fonts for the X3 (CrossPoint firmware)

Copy the family folders onto the SD card under `/fonts/` — the layout below is
exactly what CrossPoint expects (`/fonts/<Family>/<Family>_<size>.cpfont`).
Fonts are scanned **once at boot**, so power-cycle the reader after copying,
then pick the family under **Settings → Reader → Font Family**. ("Manage
Fonts" is the WiFi download store; not needed for these.)

| Family | Use for | What it is |
|---|---|---|
| `WenKaiFull` | **Chinese books** | LXGW WenKai 霞鹜文楷 (kaiti / brush style, like HSK textbooks) + Noto fallback. Full CJK — 22.5k glyphs, streamed from SD. **Device-confirmed working.** |
| `EBGaramond` | Latin/English books | Classical Garamond book serif, regular + bold + italic. Warmer and easier on the eye than the built-in Noto. |

Both are SIL OFL licensed. Built with CrossPoint's own
`fontconvert_sdcard.py`; the reproducible recipe (and the full device
debugging record, including why charset-subsetted fonts must NOT be used) is
in `../readers.md`.

**Hard-won rule: build CJK fonts with broad preset intervals
(`latin-ext,cjk`), never with sparse custom ranges.** Sparse-interval
`.cpfont` files pass every format check but silently fail to load on the
device (the font setting reverts to built-in Noto). Confirmed on CrossPoint
1.4.1 / X3, 2026-07 — upstream issue candidate.
