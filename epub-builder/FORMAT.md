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
    { "source": "chapters/ch01.md", "glossary": "build/ch01-glossary.tsv",
      "glossary_position": "before" }
  ]
}
```

- `chapters` is the spine: order here is reading order, and each entry
  becomes a TOC entry titled by the chapter's `#` heading.
- `glossary` is optional and graded-reader-specific (TSV: word, pinyin,
  gloss; glossed words in the text link to their entry and back).
- `glossary_position` is optional, per-chapter-entry, graded-reader-specific:
  `"before"` (default) or `"after"`. Controls whether the glossary section
  renders ahead of the chapter body (student previews new words before
  reading — the pedagogical default) or after it (the original layout).
  Omitting the field is the same as `"before"`; set it to `"after"` on a
  chapter-by-chapter basis to opt back into the old order.
- `pinyin_mode` is graded-reader-specific. Five values:
  `ruby` / `interlinear` (device-confirmed broken on the X3: rt leaks
  inline / stacking collapses — kept for capable readers like Apple Books),
  `plain` (hanzi only), and the X3-recommended marked-plain pair:
  `gloss-underline` (glossary words' first occurrences underlined) and
  `gloss-pinyin` (first occurrences followed by word-level pinyin, 猴子hóuzi,
  taken from the curated glossary row with spaces stripped).

## Chapter markdown — core (implemented)

- First line: `# Chapter Title` — becomes the heading and the TOC label.
- Blank-line-separated paragraphs. Plain prose; no inline formatting is
  interpreted today.

## pdf2epub extensions (implemented, un-annotated path only)

The conversion draft needs to express things graded readers never had.
Chapter markdown gains a small, closed set of constructs. These only exist
on the un-annotated path (no `pinyin_mode`) — the annotated path's behavior
and output bytes are frozen and never see this code.

- **Verse blocks** — lines that must keep their breaks (poetry, drama):
  fenced as ` ```verse … ``` `. Rendered `<div class="verse"><p>line</p>…
  </div>`, hanging indent via CSS; the restorer's `reflow: verse` policy
  emits these.
- **Images** — `![caption](../images/fig-03.png)` on its own paragraph
  (path relative to `chapters/`, i.e. `../images/<file>`). Files must
  already be *prepared*: grayscale, device-width (480 px max), PNG/JPEG.
  The prepare step does this; the builder only embeds (`<figure><img/>
  <figcaption>`) and errors out if the file is missing.
- **Endnotes** — `[^n]` marker in text, `[^n]: note text` on its own line
  (defs are stripped wherever they appear, not just at chapter end).
  Rendered as a per-chapter endnotes section with back-links (mirrors the
  glossary link/back-link id scheme). An `[^n]` with no matching def is a
  build error. CrossPoint can't do EPUB3 popups.
- **Emphasis** — `*em*` only. No bold (fake-bold PDFs are *pathology*, not
  semantics), no nested markup.

book.json gains optional fields:

- `"cover": "images/cover.png"` — path relative to the book directory
  (not `chapters/`). Prepared like any image; becomes an EPUB3
  `cover-image` manifest property. **Implemented.**
- `"source": {"file": "source.pdf", "pages": 18}` — provenance record.
  **Not yet implemented** — no conversion has needed it read back.
- `"toc_depth": 1` — flat TOC only for now; the X3 UI is shallow.
  **Not yet implemented** — the builder's TOC is already flat by default.

Anything else a conversion wants must be proposed here first — the builder
stays deliberately small, and the device (see `reference/readers.md` at the
repo root) rewards it: simple CSS, no embedded fonts, lean files.
