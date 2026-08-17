---
name: epub-slimmer
description: >-
  Strip a third-party EPUB down to what the Xteink X3 can actually render —
  drop embedded fonts, re-encode images to the panel, keep every word. Use when
  a downloaded book is far bigger than it needs to be, when changing what gets
  stripped, or when a slimmed book renders differently from the original.
  Triggers include "shrink this epub", "the book is 22MB", "strip the fonts",
  "optimize for the reader".
---

# epub-slimmer — the same book, without what the reader ignores

A commercial EPUB is built for devices this one is not. It carries typefaces
the renderer will never load and colour plates at four times the resolution the
panel can show. Stripping those is not a compromise: **on this device the
output is pixel-for-pixel what the fat file would have drawn**, because the
firmware was going to throw the difference away anyway.

Two facts do all the work, both device-confirmed in
[`extras/readers.md`](../../extras/readers.md):

- **Embedded fonts are ignored.** The renderer only rasterizes pre-converted
  `.cpfont` bitmaps. `@font-face` is dead weight, and on a typical shop-bought
  book it is most of the file.
- **Colour is dropped, detail is wasted.** 528×792, four grey levels. A
  2000px full-colour plate is decoded, converted and discarded on every draw.

## Usage

```bash
.venv/bin/python tools/epub-slimmer/scripts/slim_epub.py BOOK.epub --out slim.epub
.venv/bin/python tools/epub-slimmer/scripts/slim_epub.py BOOK.epub --probe   # measure only
.venv/bin/python tools/epub-slimmer/scripts/selftest.py                      # the gate
```

Needs Pillow — it is the only unit here besides `wallpaper-maker` that decodes
images.

## What it does, and what it refuses to do

| Kept exactly | Changed | Dropped |
|---|---|---|
| Text, spine, nav, metadata, filenames | Images → grayscale, capped at 480 px wide (cover: the 528×792 panel), re-encoded in the format they arrived in | Embedded fonts |
| Everything else in the CSS | Progressive JPEG → baseline | `@font-face` and `font-family` |
| | | Scripts, audio, video |

**Not a word of text is touched**, and no file is renamed. That last one is
deliberate: keeping `cover.jpg` a JPEG rather than converting it to a smaller
PNG means every reference in the XHTML and CSS keeps working without this tool
rewriting markup it does not understand. A book that renders differently
afterwards is not the same book, whatever it weighs.

**GIF and SVG are left alone**, and reported. Both already draw as an
`[Image]` placeholder on this device, and fixing that would mean renaming
files and rewriting references — a bigger promise than this tool makes.

**An image that re-encodes larger keeps its original bytes.** Small line art
often does. The rule is never to hand back something worse than what arrived.

## Two properties worth relying on

**It verifies before it writes.** The output goes through the same
`verify_epub.py` both AI tools use — manifest⇄zip parity, well-formed XML,
links resolve. A book that fails is *deleted rather than written*, so a
slimmed file on disk is one that passed. That also catches the mistake this
tool could most easily make: dropping a font while leaving its manifest entry
behind, which would break the book in a way no size check would notice.

**It is deterministic.** Same EPUB in, byte-identical EPUB out — `mimetype`
first and stored, everything else sorted and deflated at one fixed level, one
fixed timestamp. That is what lets a slimmed copy be cached by content hash and
reused on every later push instead of rebuilt.

The caveat is the same one the font recipe carries: determinism holds *for a
given Pillow*. Image encoders may change their output between versions, and
nothing here can notice if they do.

## What it will not save you

Books built by this suite are already lean — `epub-builder` never embeds a
font and `prepare_cover.py` has already sized the cover. Slimming
`workspace/being-earnest` recovers 72 bytes. That is the expected result, and
the reason the bot only substitutes the slim copy when the saving is real.

## The gate

[`scripts/selftest.py`](scripts/selftest.py) builds a deliberately fat book —
two embedded fonts, a 1400×2100 colour cover, a colour plate, a progressive
JPEG, a stylesheet full of `@font-face` — slims it, and checks that the fonts
and the script left the zip *and the manifest*, that the CSS lost only its font
rules, that the text and nav are byte-identical, that the cover fits the panel
in grayscale, that a progressive JPEG comes back baseline, that two runs give
one file, and that a zip which is not an EPUB is refused without writing
anything.

## Files

```
SKILL.md                 this file
scripts/
  slim_epub.py           the tool
  selftest.py            the gate
```
