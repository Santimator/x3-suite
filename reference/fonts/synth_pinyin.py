#!/usr/bin/env python3
"""Add the 8 pinyin tone-vowels NV Zilla Slab lacks, drawn in Zilla's own
style as composite glyphs (base vowel + the font's existing spacing mark).

Missing: ǎ ǐ ǒ Ǎ (caron)  and  ǖ ǘ ǚ ǜ (ü + macron/acute/caron/grave).
Rule for every one: center the mark over the base horizontally, and sit its
bottom a small gap above the base's top (so ü-tones stack above the dots).
"""
from fontTools.ttLib import TTFont
from fontTools.pens.boundsPen import BoundsPen
from fontTools.ttLib.tables._g_l_y_f import Glyph, GlyphComponent
import sys

SRC, DST = "NV_Zilla_Slab-Regular.ttf", "NV_Zilla_Slab-Pinyin.ttf"

# target codepoint -> (base codepoint, mark codepoint)
# ü-tones build on the existing precomposed ü (U+00FC) so the diaeresis is
# already placed; we only stack the tone mark above it.
CARON, MACRON, ACUTE, GRAVE = 0x02C7, 0x00AF, 0x00B4, 0x0060
TARGETS = {
    0x01CE: (0x0061, CARON),   # ǎ  a + caron
    0x01D0: (0x0131, CARON),   # ǐ  dotless-i + caron
    0x01D2: (0x006F, CARON),   # ǒ  o + caron
    0x01CD: (0x0041, CARON),   # Ǎ  A + caron
    0x01D6: (0x00FC, MACRON),  # ǖ  ü + macron
    0x01D8: (0x00FC, ACUTE),   # ǘ  ü + acute
    0x01DA: (0x00FC, CARON),   # ǚ  ü + caron
    0x01DC: (0x00FC, GRAVE),   # ǜ  ü + grave
}

def main():
    f = TTFont(SRC)
    glyf, hmtx, cmap = f["glyf"], f["hmtx"], f.getBestCmap()
    gs = f.getGlyphSet()
    upm = f["head"].unitsPerEm
    gap = round(upm * 0.02)

    def bounds(cp_or_name):
        name = cmap[cp_or_name] if isinstance(cp_or_name, int) else cp_or_name
        pen = BoundsPen(gs)
        gs[name].draw(pen)
        return name, pen.bounds  # (xMin,yMin,xMax,yMax)

    # sanity: all needed components present
    need = set()
    for base, mark in TARGETS.values():
        need |= {base, mark}
    missing = [hex(c) for c in need if c not in cmap]
    if missing:
        sys.exit(f"source lacks components: {missing}")

    cmap_tables = [t for t in f["cmap"].tables if t.isUnicode()]
    added = []
    for target, (base_cp, mark_cp) in TARGETS.items():
        bname, (bxmin, bymin, bxmax, bymax) = bounds(base_cp)
        mname, (mxmin, mymin, mxmax, mymax) = bounds(mark_cp)
        dx = round((bxmin + bxmax) / 2 - (mxmin + mxmax) / 2)
        dy = round(bymax + gap - mymin)

        comp_base = GlyphComponent()
        comp_base.glyphName, comp_base.x, comp_base.y = bname, 0, 0
        comp_base.flags = 0x04  # ROUND_XY_TO_GRID
        comp_mark = GlyphComponent()
        comp_mark.glyphName, comp_mark.x, comp_mark.y = mname, dx, dy
        comp_mark.flags = 0x04

        g = Glyph()
        g.numberOfContours = -1
        g.components = [comp_base, comp_mark]
        gname = f"uni{target:04X}"
        glyf[gname] = g
        hmtx[gname] = hmtx[bname]           # advance = base vowel's
        for t in cmap_tables:
            t.cmap[target] = gname
        added.append(gname)

    f.save(DST)
    print(f"added {len(added)} glyphs -> {DST}: {', '.join(added)}")

if __name__ == "__main__":
    main()
