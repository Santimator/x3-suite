# Target reader notes — Xteink X3 / CrossPoint

The deliverable EPUB is meant to drop into a Calibre-Web-Automated ingest folder
and ultimately render on an **Xteink X3** running **CrossPoint** firmware
(open-source replacement firmware, ESP32C3-based; CrossPoint 1.4 is current as of
mid-2026). A CJK-focused fork (`crosspoint-reader-cjk`) also exists.

## What is confirmed

- EPUB 2/3 rendering with embedded CSS, images, chapter navigation, footnotes,
  bookmarks, KOReader progress sync.
- CJK rendering works: a two-pass prewarm renderer makes Chinese viable on the
  hardware; the CJK fork specifically fixes spurious word-spacing gaps and
  spurious hyphenation at line breaks for CJK.
- Custom fonts can be sideloaded from SD for full Unicode/CJK coverage.

## What is NOT confirmed — ruby

Nothing in the CrossPoint 1.4 release notes, the user guide, or the CJK fork
mentions `<ruby>` / `<rt>` support. On a lightweight custom layout engine, ruby
(small annotation text laid out above a base run) is genuinely hard, and an
unsupported `<ruby>` element commonly degrades to rendering its `<rt>` text
**inline** — i.e. `汉<rt>hàn</rt>` shows as "汉hàn", pinyin jammed against the
character. The CJK fork's effort went into spacing/hyphenation, not ruby, which
is weak evidence ruby is not handled.

**Conclusion:** do not bet the EPUB structure on ruby. `build_epub.py` makes
pinyin display a parameter and ships an `interlinear` mode (CSS stacking, no
ruby tag) that renders on any CSS-capable engine, plus a `plain` mode.

## How to settle it (one device test)

```
python scripts/build_epub.py BOOK --out render-test.epub --diagnostic
```

This emits a single EPUB with chapter 1 rendered three ways on labeled pages:

1. **ruby** — if page 1 shows pinyin neatly above each character, ruby works →
   use `pinyin_mode: ruby` (most compact).
2. **interlinear** — pinyin stacked above hanzi via CSS. This should render even
   if ruby fails; it's the default fallback.
3. **plain** — hanzi only, pinyin in the glossary. Use if even stacked spans
   misbehave on the device.

Sideload once, flip the three pages, pick the cleanest, and set `pinyin_mode`
in the book's `book.json`. Re-test only if you change firmware.

## Sources

- https://github.com/crosspoint-reader/crosspoint-reader
- https://github.com/crosspoint-reader/crosspoint-reader/releases/tag/1.4.0
- https://github.com/aBER0724/crosspoint-reader-cjk
- https://crosspointreader.com/
