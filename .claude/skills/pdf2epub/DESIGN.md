# pdf2epub — design notes & open questions

Working document for the brainstorm. SKILL.md holds the settled parts; this
file holds the reasoning and the decisions still open.

## The problem, precisely

PDF is a *page description*: it says where ink goes, not what the text is.
EPUB is a *document*: ordered XHTML chapters, a nav, metadata. Conversion is
therefore **recovery of intent from ink** — and the amount of recovery needed
varies wildly by source. A taxonomy worth designing against:

| class | source | text layer | main difficulty |
|---|---|---|---|
| A | born-digital, clean | good | structure only (chapters, TOC) |
| B | born-digital, pathological | dirty | doubled glyphs, broken spacing, scrambled fonts |
| C | scan, no text layer | none | OCR everything |
| D | scan + embedded OCR | unknown quality | trust it or redo it? |
| E | complex layout | varies | columns, footnotes, tables, figures, verse |

Our fixture (`workspace/goya-sueno`) is class B with a class E twist (theatre
monologue — line breaks are authorial and must survive).

## Mindset (inherited from graded-reader)

- **Deterministic scripts** measure, transform, assemble, and *gate*. They
  never invent.
- **LLM roles** handle what varies: choosing the route, restoring prose,
  inferring structure.
- **Every LLM step is followed by a deterministic gate** that catches
  hallucination and omission — the pdf2epub analogue of graded-reader's
  vocabulary gates. Trust comes from the gate, not the model.
- **File-based seams between stages**, so any slot can be filled by a script,
  Claude Code interactively, or a headless runner later.

## What the fixture already proved

- The same PDF extracts differently per library: pypdf showed *line-level*
  doubling, pdfplumber *char-level* ("PPrreeffiieerroo"). The cause is fake
  bold: every glyph drawn twice, slightly offset.
- `pdfplumber.dedupe_chars()` fixes it **deterministically** — the recovered
  text is near-perfect (accents intact, spacing correct). A pathology we
  feared would need LLM cleanup costs zero tokens. Triage detects it
  (dedupe drop > 30%) and stage 1 applies it.
- Subset font names are scrambled (`HXCFJR+font1`) — font *names* carry no
  semantics; font *size/style metrics* still do, and stage 3 should use them
  as structure signals.

## Pipeline stages and the LLM/deterministic split

| stage | actor | in → out | gate |
|---|---|---|---|
| 0 triage | script (`triage.py`) ✅ | source.pdf → triage.json + route | model confirms route from samples |
| 1 extract | script | source.pdf → extract/pages (blocks w/ bbox, font, size) | none needed (no judgment) |
| 2 restore | **LLM restorer** | raw pages → clean text, chunked | fidelity: length ratio + n-gram containment vs raw |
| 3 structure | **LLM architect** | clean text + font signals → book.json + chapters/*.md | all paragraphs accounted for; no invented text |
| 4 build | script | chapters + book.json → EPUB | (deterministic) |
| 5 verify | script | EPUB → coverage + integrity report | human spot-check |

Deterministic pre-passes shrink the LLM's job in stage 2: furniture removal
(repetition across pages, from triage), char dedupe, provable dehyphenation
(wordlist check). The restorer only handles residual judgment: paragraph
reflow vs. deliberate breaks, ambiguous hyphens, OCR confusions.

## The common bus

Stage 3 emits the **same intermediate format graded-reader uses**:
`chapters/*.md + book.json`. Consequences:

- One EPUB builder for the whole suite (generalize `build_epub.py`: pinyin
  becomes an optional feature, not the spine of the script).
- `charset.py` subsetting works on converted books too — which matters the
  day we convert a *Chinese* PDF and need device fonts for exactly its glyphs.
- Workspace conventions, selftest patterns, and device lore
  (`reference/readers.md`) are shared instead of duplicated.

## Open questions

1. **OCR engine** (route OCR/HYBRID). Options: (a) tesseract — free,
   deterministic, mediocre on odd layouts; (b) vision-LLM transcription of
   rendered pages — excellent, costs tokens, *requires* the fidelity gate;
   (c) hybrid: tesseract first, vision only for pages below a confidence
   threshold. **Leaning (c).**
2. **Images/figures.** Keep (grayscale, downscaled to device width) or drop?
   E-ink + tiny screen argues for a size cap and dropping decorative images;
   content figures should survive. **Leaning: keep with cap, restorer flags
   decorative.**
3. **Footnotes.** EPUB3 noteref popups won't render on CrossPoint. Inline at
   paragraph end vs. per-chapter endnotes. **Leaning: endnotes with
   back-links** (the glossary link machinery in build_epub.py already does
   exactly this dance).
4. **Fidelity vs. readability.** Does the restorer fix punctuation (the
   fixture uses low-9 `‚` where `,` is meant) and obvious typos? **Leaning:
   a logged, deterministic normalization table; the LLM proposes entries,
   never silently edits.**
5. **Verse detection.** Reflow destroys drama/poetry. Triage could flag
   "verse-like" pages (short ragged lines, no justification); the restorer
   then defaults to preserving breaks. Needed for the fixture. **Leaning:
   yes, add to triage.**
6. **Headless runner.** graded-reader has `run_book.py` + `llm.py` parity.
   Same seam applies here, but **interactive-first**: prove the loop with
   Claude Code driving before wiring the runner.
7. **TOC without chapters.** The fixture is an unbroken monologue — no
   chapter headings at all. Architect options: single-chapter EPUB, or
   invented (clearly marked) navigation splits every ~N pages. **Leaning:
   single chapter for fidelity, optional `--split` for navigability.**
