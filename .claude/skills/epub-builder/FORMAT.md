# The common book format — the builder's input contract

Every task in the suite converges on this format; the builder
(`scripts/build_epub.py` in this skill) consumes it and nothing else.
**This is the contract the orchestrating agent targets** when it prepares a
book — if it's not described here, the builder doesn't support it. The core
is implemented today; the "pdf2epub extensions" are the agreed target for
the builder to grow into.

## Layout

```
workspace/<slug>/
  book.json           metadata + spine (below)
  chapters/chNN.md    one markdown file per spine item, in order
  images/             (extension) prepared images, referenced from chapters
  build/              outputs only — never an input
```

## book.json — core (implemented)

```json
{
  "title":    "愚公移山",
  "author":   "分级读物 (HSK 1-3)",
  "language": "zh",
  "chapters": [
    { "source": "chapters/ch01.md", "glossary": "build/ch01-glossary.tsv" }
  ]
}
```

- `chapters` is the spine: order here is reading order, and each entry
  becomes a TOC entry titled by the chapter's `#` heading.
- `glossary` is optional and graded-reader-specific (TSV: word, pinyin,
  gloss; glossed words in the text link to their entry and back).
- `pinyin_mode` (`ruby` / `interlinear` / `plain`) is graded-reader-specific.

## Chapter markdown — core (implemented)

- First line: `# Chapter Title` — becomes the heading and the TOC label.
- Blank-line-separated paragraphs. Plain prose; no inline formatting is
  interpreted today.

## pdf2epub extensions (proposed — builder support pending)

The conversion draft needs to express things graded readers never had.
Chapter markdown gains a small, closed set of constructs:

- **Verse blocks** — lines that must keep their breaks (poetry, drama):
  fenced as ` ```verse … ``` `. The builder renders line-per-line with
  hanging indent; the restorer's `reflow: verse` policy emits these.
- **Images** — `![caption](../images/fig-03.png)` on its own paragraph.
  Files must already be *prepared*: grayscale, device-width (480 px max),
  PNG/JPEG. The prepare step does this; the builder only embeds.
- **Endnotes** — `[^n]` marker in text, `[^n]: note text` at chapter end.
  Rendered as per-chapter endnotes with back-links (same machinery as
  glossary links). CrossPoint can't do EPUB3 popups.
- **Emphasis** — `*em*` only. No bold (fake-bold PDFs are *pathology*, not
  semantics), no nested markup.

book.json gains optional fields:

- `"cover": "images/cover.png"` — prepared like any image.
- `"source": {"file": "source.pdf", "pages": 18}` — provenance record.
- `"toc_depth": 1` — flat TOC only for now; the X3 UI is shallow.

Anything else a conversion wants must be proposed here first — the builder
stays deliberately small, and the device (see `reference/readers.md` at the
repo root) rewards it: simple CSS, no embedded fonts, lean files.
