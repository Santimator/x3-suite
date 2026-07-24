---
name: epub-builder
description: >-
  Assemble an EPUB from the suite's common book format (book.json +
  chapters/*.md) for the Xteink X3 e-reader. Use when a book directory is
  ready to be built into an EPUB, when changing EPUB output (CSS, nav,
  packaging), or when a task needs to know the builder's input contract.
  Triggers include "build the epub", "assemble the book", "rebuild
  workspace/<slug>".
---

# epub-builder — the suite's shared EPUB builder

Suite infrastructure, not a task: every task (graded-reader, pdf2epub, …)
prepares a book in the common format, and this skill turns it into an
X3-friendly EPUB. Hand-built XHTML/OPF, no epub library — the mimetype
ordering and annotation markup need exact control.

**The input contract is [`FORMAT.md`](FORMAT.md).** Tasks target it; the
builder consumes it and nothing else. If a construct isn't in FORMAT.md, the
builder doesn't support it — extend the contract first, then the builder.

## Usage

```bash
# generic book (no annotation — no CJK dependencies needed)
.venv/bin/python epub-builder/scripts/build_epub.py \
    workspace/<slug> --out workspace/<slug>/build/<slug>.epub

# graded-reader book (pinyin_mode in book.json engages annotation)
.venv/bin/python epub-builder/scripts/build_epub.py \
    workspace/being-earnest --out workspace/being-earnest/build/book.epub

# pinyin render test for a new device (one EPUB, chapter 1 in all 3 modes)
.venv/bin/python epub-builder/scripts/build_epub.py \
    workspace/being-earnest --out render-test.epub --diagnostic
```

## Behavior

- **Annotation is opt-in.** `pinyin_mode` in `book.json` (or `--pinyin-mode`)
  engages pinyin rendering (`ruby` / `interlinear` / `plain`) and glossary
  linking; only then are jieba/pypinyin (graded-reader's `vocab.py`) imported.
  Books without it build as plain escaped text with zero CJK dependencies.
- **Deterministic output.** Fixed timestamp + title-derived UUID: the same
  source builds a byte-identical EPUB, and re-sideloading replaces instead
  of duplicating.
- **Device-shaped.** Simple CSS, no embedded fonts (the X3 can't rasterize
  them — see `reference/readers.md` at the repo root), lean files.

## Consumers

- graded-reader: `run_book.py` shells out to it; `selftest.py` imports
  `build_epub` for in-memory assembly.
- pdf2epub: stage 5 of its pipeline (its `prepare.py` emits FORMAT.md-shaped
  input).
