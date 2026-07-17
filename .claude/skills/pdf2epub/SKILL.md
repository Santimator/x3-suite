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

**Status: stage 0 (triage) implemented; stages 1–5 designed, pending
brainstorm sign-off.** Until the scripts exist, Claude Code performs the later
stages manually following the stage contracts below — that's the point of the
design: each stage has a file interface, so a human, a model, or a script can
fill any slot.

## Workspace convention

One folder per conversion job, mirroring graded-reader books:

```
workspace/<slug>/
  source.pdf          the input (never modified)
  build/triage.json   stage 0 output
  extract/            stage 1 output (raw per-page extraction)
  chapters/*.md       stage 2-3 output — the suite's common book format
  book.json           stage 3 output (metadata + spine), graded-reader schema
  build/<slug>.epub   stage 4 output
```

Converted books converge on the same `chapters/*.md + book.json` intermediate
that graded-reader produces, so the EPUB builder, `charset.py` font
subsetting, and device lore in `reference/readers.md` (repo root) are shared.

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

1. **Extract** (deterministic, planned) — route TEXT: pdfplumber with dedupe
   → per-page blocks with font/size/bbox. Route OCR: render pages →
   tesseract (lang from triage) or vision-model transcription. Never edits,
   only extracts.

2. **Restore** (deterministic `restore.py` driven by `policy.json`, planned)
   — reflow paragraphs across page breaks, dehyphenate, drop furniture,
   normalize punctuation, preserve deliberate line breaks (verse/drama), all
   as mechanical transforms configured by a policy file. The agent *verifies*
   the result on samples; on failure it diagnoses, edits the policy (e.g.
   flips a block to `verse`, adds a normalization entry), and re-runs.
   Span-scoped model patches are the last resort, gated by a fidelity check
   (length ratio + n-gram containment vs the raw extraction) and logged.

3. **Structure** (agent decisions applied by script, planned) — the agent
   proposes title, author, and chapter boundaries as *anchors* (page/line
   refs plus strings that must exist verbatim in the text); a script
   validates the anchors and cuts `book.json` + `chapters/*.md`. Gate: every
   source paragraph lands in exactly one chapter.

4. **Build** (deterministic, planned) — generalized sibling of
   graded-reader's `build_epub.py`: X3-friendly EPUB (simple CSS, no
   embedded fonts — see `reference/readers.md`).

5. **Verify** (deterministic, planned) — EPUB integrity + coverage report
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
