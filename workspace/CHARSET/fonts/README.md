# CrossPoint SD-card fonts — copy this `fonts/` folder to the SD card root

The layout below is exactly what CrossPoint expects
(`/fonts/<Family>/<Family>_<size>.cpfont`, scanned once at boot — power-cycle
the reader after copying). Pick the family under **Settings → Reader → Font
Family**. "Manage Fonts" is the WiFi download store; not needed for these.

| Family | Style | Glyphs | Size/file | Notes |
|---|---|---|---|---|
| `WenKaiHSK` | kaiti (LXGW WenKai) | 533 (current books) | 71–140 KB | **Try first** — lightest, HSK-textbook look |
| `NotoHSK` | sans (Noto Sans SC) | 533 (current books) | 80–155 KB | Light backup |
| `LXGWWenKai-GradedReader` | kaiti, regular+bold | 1223 (full HSK 1-4) | 0.35–0.73 MB | Covers any future book; may exceed X3 RAM |
| `NotoSansSC-GradedReader` | sans, regular+bold | 1223 (full HSK 1-4) | 0.4–0.8 MB | Same, sans |

If a family "won't stick" (selection reverts to built-in Noto), the firmware
failed to load it at book-open — try a lighter family. Build details and
troubleshooting: `.claude/skills/graded-reader/reference/readers.md`.

Sources: LXGW WenKai (SIL OFL 1.1), Noto Sans SC (SIL OFL 1.1). Subsets built
with CrossPoint's own `fontconvert_sdcard.py`.
