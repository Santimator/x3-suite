# CrossPoint SD-card fonts — copy this `fonts/` folder to the SD card root

Layout is exactly what CrossPoint expects
(`/fonts/<Family>/<Family>_<size>.cpfont`, scanned once at boot — power-cycle
after copying). Select under **Settings → Reader → Font Family**. "Manage
Fonts" is the WiFi download store; not needed for these.

## Latin (reading — these are known to work on the X3)

| Family | Style | Look |
|---|---|---|
| `EBGaramond` | classical serif, reg+bold+italic | Warm Garamond book face — softer, more organic than the built-in Noto. **Try for Latin reading.** |
| `ZLatinTest` | Noto Sans, single style | Plain control font (see diagnostics below) |

## Chinese (CJK — still being debugged on the X3, see readers.md)

| Family | Coverage | Note |
|---|---|---|
| `WenKaiFull` | full CJK, builder-faithful (100 intervals, 22.5k glyphs, 3-6.5 MB/size) | **Best CJK bet on CrossPoint** — built exactly like the official web builder (broad latin-ext+cjk presets), unlike our sparse subsets |
| `ZhTest` | letter-writer book only | Minimal CJK control — open 写信的老人 |
| `WenKaiHSK` | current books (533 glyphs) | kaiti, light |
| `NotoHSK` | current books (533 glyphs) | sans, light |
| `LXGWWenKai-GradedReader` | full HSK 1-4 (1223) | kaiti, reg+bold |
| `NotoSansSC-GradedReader` | full HSK 1-4 (1223) | sans, reg+bold |

## If Chinese fonts don't render (selection reverts to Noto)

This is an open problem — mainline CrossPoint ships **zero** CJK fonts and
CJK on the X3 is unvalidated. Diagnose in order:

1. **`ZLatinTest`** → open any English book. If it renders and sticks, SD
   fonts work → the issue is CJK-specific. If it *also* reverts, the whole
   SD-font path is broken (reflash / different SD card).
2. **`ZhTest`** → open 写信的老人. If English works but this reverts, it's a
   CJK render-path bug worth filing upstream.

Full analysis, firmware findings, and build recipe:
`reference/readers.md` (repo root — shared device notes for the whole suite).

Sources: EB Garamond, LXGW WenKai, Noto Sans SC — all SIL OFL. Subsets built
with CrossPoint's own `fontconvert_sdcard.py`.
