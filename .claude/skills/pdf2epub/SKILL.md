---
name: pdf2epub
description: >-
  Convert a PDF (born-digital or scanned) into a clean EPUB for an e-ink
  reader. Use when the user wants to turn a PDF into an EPUB, fix a messy PDF
  text layer, OCR a scanned book, or points at a workspace folder containing a
  source.pdf. Triggers include "pdf to epub", "convert this pdf", "ocr this
  book", "make this readable on the X3".
---

# pdf2epub — PDF → EPUB conversion pipeline

Recover a *document* from a *page description*. PDFs only say where ink goes;
EPUB needs to know what the text is (paragraphs, chapters, a nav). The
pipeline is **deterministic-first, agent-on-error**: the happy path is pure
scripts (extract → restore → build); the agent orchestrates, verifies
completeness, and diagnoses failures — and when it intervenes it emits
*decisions* (a route, a policy switch, an anchor) that scripts apply, never
bulk text. Every byte in the EPUB traces back to the extraction.
Full design rationale + open questions: [`DESIGN.md`](DESIGN.md).

**Status: stage 0 (triage) implemented; stage 1 toolbox partially implemented
(`extract_text.py`, `render_pages.py`); stage 5 (build) exists as the shared
epub-builder skill; the rest is specified for implementation in
`BUILD_INSTRUCTIONS.md` at the repo root.** Until the remaining scripts
exist, Claude Code performs those stages manually following the stage
contracts below — that's the point of the design: each stage has a file
interface, so a human, a model, or a script can fill any slot.

## Workspace convention

One folder per conversion job, mirroring graded-reader books:

```
workspace/<slug>/
  source.pdf          the input (never modified)
  build/triage.json   stage 0 output
  extract/            stage 1 output (raw per-page extraction)
  policy.json         stage 2 input — the agent's restore decisions
  draft.json          stage 3 output — the agent's structured plan of the book
  chapters/*.md       stage 4 output ┐
  book.json           stage 4 output ├ the common book format (builder input)
  images/             stage 4 output ┘ prepared: grayscale, device-width
  build/<slug>.epub   stage 5 output
```

Converted books converge on the suite's common book format — **documented in
the epub-builder skill's contract,
[`.claude/skills/epub-builder/FORMAT.md`](../epub-builder/FORMAT.md); the
agent must know it when drafting** — so the EPUB builder, `charset.py` font
subsetting, and device lore in `reference/readers.md` are shared with
graded-reader.

## Stages

0. **Triage** (deterministic, implemented) — characterize the source, flag
   pathologies, recommend a route:

   ```bash
   .venv/bin/python .claude/skills/pdf2epub/scripts/triage.py \
       workspace/<slug>/source.pdf --out workspace/<slug>/build/triage.json
   ```

   Routes: `TEXT` (usable text layer), `OCR` (scanned), `HYBRID` (per-page
   mix). Flags: `doubled_chars` / `doubled_lines` (fake-bold double draw —
   fixed deterministically via char dedupe), `broken_spacing`,
   `page_furniture` (repeating headers/footers → drop candidates). The
   orchestrating model reads the summary + sample pages and confirms the
   route.

1. **Extract** (toolbox, `extract_text.py` + `render_pages.py` implemented;
   `extract_ocr.py` planned) — parameterized deterministic tools the
   agent picks between and re-runs: `extract_text.py` (pdfplumber;
   `--dedupe`, `--pages`), `extract_ocr.py` (tesseract;
   `--lang`, `--dpi`, `--psm`), `render_pages.py` (page images, for the
   agent's own eyes or last-resort vision transcription). Per-page
   composable for HYBRID books. Tools never edit, only extract; the agent
   verifies output and reconsiders tool or parameters on failure.

2. **Restore** (deterministic `restore.py` driven by `policy.json`, planned)
   — reflow paragraphs across page breaks, dehyphenate, drop furniture,
   normalize punctuation, preserve deliberate line breaks (verse/drama), all
   as mechanical transforms configured by a policy file. The agent *verifies*
   the result on samples; on failure it diagnoses, edits the policy (e.g.
   flips a block to `verse`, adds a normalization entry), and re-runs.
   Span-scoped model patches are the last resort, gated by a fidelity check
   (length ratio + n-gram containment vs the raw extraction) and logged.

3. **Draft** (agent, planned) — the agent authors `draft.json`, the
   structured plan of the book: title/author, chapter boundaries as
   *verbatim anchors*, TOC labels, image placements, front-matter handling.
   Its whole creative output — and every claim in it is checkable.

4. **Prepare** (deterministic `prepare.py`, planned) — validate the draft
   (anchors exist, image refs resolve, every paragraph lands in exactly one
   chapter), then cut `chapters/*.md` + `book.json` and resize/grayscale
   images to device spec — emitting exactly the format the epub-builder
   skill's `FORMAT.md` specifies.

5. **Build** (deterministic, exists) — the suite-shared **epub-builder**
   skill (`.claude/skills/epub-builder/`): X3-friendly EPUB, no CJK
   dependencies for generic books. The FORMAT.md extensions (verse blocks,
   images, endnotes) still need implementing there.

6. **Verify** (deterministic, planned) — EPUB integrity + coverage report
   (source text in vs. text out, per chapter).

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r .claude/skills/pdf2epub/requirements.txt
```

## Test fixture

`workspace/goya-sueno/source.pdf` — *Prefiero que me quite el sueño Goya a
que lo haga cualquier hijo de puta* (Rodrigo García / La Carnicería Teatro),
18 pages, Spanish theatre monologue. Deliberately pathological: every glyph
double-drawn (fake bold), scrambled subset font names, verse-like line
breaks that must NOT be reflowed. Triage output lives next to it.
