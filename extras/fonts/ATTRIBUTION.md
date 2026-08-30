# Font attribution & licenses

The `.cpfont` files in this directory are bitmap conversions (for CrossPoint
on the Xteink X3 and X4 Pro) of third-party fonts. The conversion changes the
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

## NaskhFull/ (Arabic)

**Noto Naskh Arabic** — the Arabic half of `NaskhFull`, and the reason the
family exists: it encodes the Arabic presentation forms (U+FB50–U+FDFF,
U+FE70–U+FEFC) that CrossPoint's shaper emits. Version 2.021, used unmodified.
> Copyright 2022 The Noto Project Authors
> (https://github.com/notofonts/arabic)

No Reserved Font Name is declared by the notice.

**Noto Sans** — the fallback half of `NaskhFull`, supplying the Latin and
punctuation codepoints Noto Naskh Arabic does not carry. Version 2.015, used
unmodified.
> Copyright 2022 The Noto Project Authors
> (https://github.com/notofonts/latin-greek-cyrillic)

Sources: https://github.com/notofonts/notofonts.github.io
(`fonts/NotoNaskhArabic/hinted/ttf/`, `fonts/NotoSans/hinted/ttf/`)

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

## LinguisticsPro/ (Bulgarian Cyrillic)

**Linguistics Pro** — the primary face in `LinguisticsPro`, version 1.088. It
is a LOCALFONTS book serif with modern Bulgarian Cyrillic forms, based on
Utopia Nova / Lingua Franca. The source TTF's own `cyrl/BGR/locl`
substitutions were redirected into an intermediate cmap by
[`bake_bulgarian.py`](bake_bulgarian.py) before bitmap conversion; no outlines
were redrawn and the intermediate TTF is not distributed here.
> Copyright 2026 The Linguistics Pro Project Authors
> (https://github.com/StefanPeev/Linguistics-Pro). Earlier design contributions
> are credited to the Utopia Project / Adobe Systems (1989, 1991), the
> GUTenberg Project (2003–2004), Han The Thanh (2006), Andrey V. Panov
> (2008–2014), Michael Sharpe (2014), Andreas Nolda (2015), and Stefan Peev
> (2016).

No Reserved Font Name is declared by the current OFL notice.

Source: https://github.com/StefanPeev/Linguistics-Pro (official CI regular TTF
from commit `1b8580fbc25d2f3e2a92bf587a134d74214fb61a`)

**Noto Serif** — fallback for replacement glyphs and source-coverage gaps.
Version 2.015, used unmodified before bitmap conversion.
> Copyright 2022 The Noto Project Authors
> (https://github.com/notofonts/latin-greek-cyrillic)

No Reserved Font Name is declared by the notice.

Source: https://github.com/crosspoint-reader/crosspoint-reader
(`lib/EpdFont/builtinFonts/source/NotoSerif/NotoSerif-Regular.ttf`, tag
`v1.5.0`)
