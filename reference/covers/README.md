# Default cover assets

Shared by both services through `epub-builder/scripts/prepare_cover.py`, used as
the last-resort cover when a book has no `source-cover.*` sidecar and no usable
cover inside its source. The title is drawn into the template's blank panel,
auto-sized to fit, rasterised into the PNG at build time (the font never ships
to the device — so any face works, we just bundle it for reproducibility).

## Templates

- **`default.png` + `default.json`** — Latin default (pdf2epub): a reading-nook
  scene with a blank parchment panel and a "SANTIMATOR" author nameplate.
- **`graded-default.png` + `graded-default.json`** — Chinese default
  (graded-reader): a matching scene in a Chinese key (pagoda, plum blossom,
  bamboo, 中文级 volumes).

Each `.json` places the title on its image: `title_box` (`[x0,y0,x1,y1]` as
fractions of the image), `color` (RGB ink), `uppercase`, and `font`. Swap an
image and adjust these to re-home the title; the JSON is what travels with it.

## Title fonts (bundled, OFL)

Both are baked into the cover PNG at build time and never reach the device.

- **`IMFellEnglish-Regular.ttf`** — Latin titles. A digital revival of the
  17th-century Fell types; suits period texts. © 2010 Igino Marini,
  SIL OFL 1.1 — [`IMFellEnglish-OFL.txt`](IMFellEnglish-OFL.txt).
- **`LXGWWenKai-Regular.ttf`** — CJK titles. LXGW WenKai, the kaiti hanzi that
  is WenZilla's Chinese half, so a cover title matches the reader's body face.
  © 2021-2026 LXGW, SIL OFL 1.1 — [`LXGWWenKai-OFL.txt`](LXGWWenKai-OFL.txt).

Neither is covered by the repository's top-level MIT license. To use a
different title face, drop its `.ttf`/`.otf` here (with its license), point a
template's `font` at it, or pass `--font` to `prepare_cover.py`. Title wrapping
is script-aware: it breaks on spaces for Latin and between characters for CJK.
