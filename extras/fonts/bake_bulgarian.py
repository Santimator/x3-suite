#!/usr/bin/env python3
"""Bake a font's Bulgarian ``locl`` substitutions into its Unicode cmap.

CrossPoint's .cpfont converter rasterizes the glyph addressed directly by each
Unicode codepoint; it does not run OpenType language substitutions.  A normal
TTF therefore keeps showing the default/international Cyrillic forms even when
it contains the right Bulgarian glyphs behind ``locl``.  This helper redirects
the cmap to the glyphs selected by the font's own ``cyrl/BGR/locl`` feature.

The output is an intermediate build input.  The source font is left untouched.
"""

from pathlib import Path
import sys

from fontTools.ttLib import TTFont


SRC = Path("LinguisticsPro-Regular.ttf")
DST = Path("LinguisticsPro-Bulgarian.ttf")


def _single_substitutions(lookup):
    """Yield mappings from SingleSubst tables, including extension lookups."""
    for subtable in lookup.SubTable:
        if hasattr(subtable, "mapping"):
            yield from subtable.mapping.items()
        elif hasattr(subtable, "ExtSubTable"):
            extension = subtable.ExtSubTable
            if hasattr(extension, "mapping"):
                yield from extension.mapping.items()


def _bulgarian_locl_mapping(font):
    gsub = font["GSUB"].table
    feature_records = gsub.FeatureList.FeatureRecord
    cyrl = next(
        (record.Script for record in gsub.ScriptList.ScriptRecord if record.ScriptTag == "cyrl"),
        None,
    )
    if cyrl is None:
        sys.exit("source has no Cyrillic OpenType script")

    bgr = next(
        (record.LangSys for record in cyrl.LangSysRecord if record.LangSysTag.strip() == "BGR"),
        None,
    )
    if bgr is None:
        sys.exit("source has no cyrl/BGR OpenType language system")

    lookup_indexes = []
    for feature_index in bgr.FeatureIndex:
        record = feature_records[feature_index]
        if record.FeatureTag == "locl":
            lookup_indexes.extend(record.Feature.LookupListIndex)
    if not lookup_indexes:
        sys.exit("source has no cyrl/BGR/locl substitutions")

    mapping = {}
    for lookup_index in lookup_indexes:
        lookup = gsub.LookupList.Lookup[lookup_index]
        mapping.update(_single_substitutions(lookup))
    if not mapping:
        sys.exit("cyrl/BGR/locl contains no single-glyph substitutions")
    return mapping


def main():
    if not SRC.is_file():
        sys.exit(f"source not found: {SRC}")

    font = TTFont(SRC, recalcBBoxes=False, recalcTimestamp=False)
    substitutions = _bulgarian_locl_mapping(font)
    unicode_tables = [table for table in font["cmap"].tables if table.isUnicode()]

    remapped = set()
    for table in unicode_tables:
        for codepoint, glyph_name in list(table.cmap.items()):
            replacement = substitutions.get(glyph_name)
            if replacement is not None:
                table.cmap[codepoint] = replacement
                remapped.add(codepoint)

    # Linguistics Pro 1.088 localizes 27 Cyrillic codepoints.  Requiring a
    # meaningful set keeps an upstream layout change from producing a font that
    # looks successful but still contains international forms.
    if len(remapped) < 20:
        sys.exit(f"only {len(remapped)} Bulgarian glyphs were remapped; refusing output")

    font.save(DST, reorderTables=False)

    check = TTFont(DST, recalcTimestamp=False).getBestCmap()
    missed = [codepoint for codepoint in remapped if check.get(codepoint) not in substitutions.values()]
    if missed:
        sys.exit("saved cmap failed verification: " + ", ".join(f"U+{cp:04X}" for cp in missed))

    listed = ", ".join(f"U+{codepoint:04X}" for codepoint in sorted(remapped))
    print(f"baked {len(remapped)} Bulgarian glyphs -> {DST}: {listed}")


if __name__ == "__main__":
    main()
