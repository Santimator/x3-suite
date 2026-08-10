# Font attribution & licenses

The `.cpfont` files in this directory are bitmap conversions (for the Xteink
X3 / CrossPoint firmware) of third-party fonts. The conversion changes the
format, not the design — each remains a derivative of its source font and is
redistributed here under that font's original license, the **SIL Open Font
License 1.1** (full text in [`OFL.txt`](OFL.txt)). None of these fonts is
covered by the repository's top-level MIT license.

Per the OFL, the copyright notices and Reserved Font Names are reproduced
below. The `.cpfont` bitmaps are Modified Versions; the original fonts are
available from the sources listed.

## WenKaiFull/, WenZilla/ and CrimKai/ (CJK)

**LXGW WenKai (霞鹜文楷)** — the kaiti hanzi in all three families.
> Copyright 2021-2026 LXGW (https://github.com/lxgw/LxgwWenKai), with
> Reserved Font Name '霞鹜', '霞鶩', '落霞孤鹜', '落霞孤鶩' and 'LXGW'.

Source: https://github.com/lxgw/LxgwWenKai (also Ubuntu's `fonts-lxgw-wenkai`).

**Noto Sans CJK / Noto Sans SC** — CJK fallback used when building `WenKaiFull`.
> Copyright 2014-2021 Adobe (https://www.adobe.com/), with Reserved Font Name
> 'Source Han Sans'. Distributed by Google as Noto Sans CJK.

Source: https://github.com/notofonts/noto-cjk

## WenZilla/ (Latin)

**NV Zilla Slab** — the Latin/pinyin half of `WenZilla`. An e-reader-tuned
build (nicoverbruggen/ebook-fonts) of Mozilla's **Zilla Slab**, further
modified here: eight pinyin tone glyphs (ǎ ǐ ǒ Ǎ ǖ ǘ ǚ ǜ) were composited in
from the font's own marks (see [`synth_pinyin.py`](synth_pinyin.py)) before
conversion.
> Copyright 2017, The Mozilla Foundation, with Reserved Font Name 'Zilla Slab'.

Sources: https://github.com/mozilla/zilla-slab ·
https://github.com/nicoverbruggen/ebook-fonts (fonts/extra, `NV_Zilla_Slab-*`)

## CrimKai/ (Latin)

**NV Scarlet** — the Latin/pinyin half of `CrimKai`. An e-reader-tuned build
(nicoverbruggen/ebook-fonts) of **Cochineal**, Michael Sharpe's extension of
**Crimson** by Sebastian Kosch. Used unmodified: unlike Zilla it already draws
every pinyin tone vowel, so nothing was composited in.
> Copyright (c) 2010, Sebastian Kosch (sebastian@aldusleaf.org), Additions and
> modifications copyright (c) 2015--23, Michael Sharpe (msharpe@ucsd.edu)

No Reserved Font Name is declared by either notice.

Sources: https://github.com/skosch/Crimson · https://ctan.org/pkg/cochineal ·
https://github.com/nicoverbruggen/ebook-fonts (fonts/extra, `NV_Scarlet-*`)

