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

**Status: stages 0-2 and 4 (triage, extract toolbox, restore, prepare)
implemented; stage 5 (build) exists as the shared epub-builder skill; stage
3 (draft) is always the agent, by design; stage 6 (verify) is specified for
implementation in `BUILD_INSTRUCTIONS.md` at the repo root.**

## Workspace convention

One folder per conversion job, mirroring graded-reader books:

```
workspace/<slug>/
  source.pdf          the input (never modified)
  build/triage.json   stage 0 output
  extract/            stage 1 output (raw per-page extraction)
  policy.json         stage 2 input — the agent's restore decisions
  restore/            stage 2 output (restored.md + restore-report.json)
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

1. **Extract** (toolbox, implemented) — parameterized deterministic tools
   the agent picks between and re-runs: `extract_text.py` (pdfplumber;
   `--dedupe`, `--pages`), `extract_ocr.py` (tesseract;
   `--lang`, `--dpi`, `--psm`), `render_pages.py` (page images, for the
   agent's own eyes or last-resort vision transcription). Per-page
   composable for HYBRID books. Tools never edit, only extract; the agent
   verifies output and reconsiders tool or parameters on failure.
   `extract_ocr.py` degrades gracefully (exit 1, one-line hint) when the
   `tesseract` binary or `pytesseract` module is absent — never auto-installs.

2. **Restore** (deterministic `restore.py` driven by `policy.json`,
   implemented) — reflow paragraphs across page breaks, dehyphenate, drop
   furniture, normalize punctuation, preserve deliberate line breaks
   (verse/drama), all as mechanical transforms configured by a policy file.
   The agent *verifies* the result on samples; on failure it diagnoses,
   edits the policy (e.g. flips a block to `verse`, adds a normalization
   entry), and re-runs. Span-scoped model patches are the last resort, gated
   by a fidelity check (length ratio + n-gram containment vs the raw
   extraction) and logged.

   ```bash
   .venv/bin/python .claude/skills/pdf2epub/scripts/restore.py \
       workspace/<slug>/extract --policy workspace/<slug>/policy.json \
       --out workspace/<slug>/restore
   ```

   `policy.json` schema:
   ```json
   {
     "furniture": ["^\\d+$", "^kupdf\\.net"],
     "page_ranges": [
       {"pages": "1", "treat": "front_matter"},
       {"pages": "2-18", "treat": "body"}
     ],
     "reflow": "sentence",
     "normalize": {"‚": ","},
     "dehyphenate": true,
     "dehyphenate_exceptions": []
   }
   ```
   - `furniture`: regexes (`re.search`); a line matching any of them is
     dropped entirely, everywhere (all treats) — counted per pattern.
   - `page_ranges`: every extracted page must be covered by exactly one
     range (1-indexed, inclusive `"A-B"` or single `"N"`); overlaps or gaps
     are a restore error, never guessed. `treat`: `front_matter` — lines
     pass through **verbatim**, one paragraph per surviving physical line,
     no reflow/dehyphenation (the draft later decides what becomes title
     metadata, or drops them); `body` — dehyphenate (if on) then reflow;
     `skip` — the pages' content is dropped from the document (still counted
     in the fidelity gate's input baseline, so a skip that eats real text
     will fail the gate rather than silently vanishing).
   - `reflow` (top-level default, overridable per `page_ranges` entry):
     `prose` — join lines into paragraphs, breaking on vertical gap > 1.6×
     the chunk's median line gap or on x0 indent drifting > 10pt from the
     chunk's modal left margin; `sentence` — join a line to the next while
     it lacks terminal punctuation (`.` `!` `?` `…` `:` `”` `»` `)`), each
     completed unit becomes a paragraph; `verse` — preserve every line
     break, emit the whole chunk as one ` ```verse ` fenced block.
   - `dehyphenate`: a line ending `-` immediately followed by a line
     starting lowercase is merged, hyphen dropped — unless the joined word
     matches `dehyphenate_exceptions` (then merged but the hyphen is kept).
     Applies within `body` chunks (all reflow modes), never to
     `front_matter`.
   - `normalize`: exact string replacements, applied last (after
     reflow/dehyphenate), each occurrence counted per entry.
   - `restore-report.json`: lines in/out, furniture dropped (per pattern),
     joins made, hyphens resolved, normalizations (per entry), paragraphs
     emitted, and the fidelity gate — `char_ratio` (non-whitespace chars
     out / in, furniture excluded from "in") and `ngram_containment`
     (fraction of the input's word 5-grams, on normalized text, found in
     the output). Gate passes iff `0.98 ≤ char_ratio ≤ 1.02` and
     `ngram_containment ≥ 0.995`; restore.py exits 1 on gate failure — that
     exit code is the signal to look at the report and edit the policy.

3. **Draft** (agent) — the agent authors `draft.json`, the structured plan
   of the book: title/author, chapter boundaries as *verbatim anchors*, TOC
   labels, image placements, front-matter handling. Its whole creative
   output — and every claim in it is checkable.

   `draft.json` schema:
   ```json
   {
     "title": "Prefiero que me quite el sueño Goya…",
     "author": "Rodrigo García",
     "language": "es",
     "chapters": [
       {"toc_label": "Monólogo", "start_anchor": "Prefiero que me quite el sueño Goya a que lo haga cualquier hijo"}
     ],
     "images": [],
     "front_matter": "drop"
   }
   ```
   - `chapters[].start_anchor`: a verbatim substring of `restore/restored.md`;
     must occur exactly once; chapters must appear in document order.
     Chapter N's content runs from the paragraph containing its anchor to
     the paragraph before chapter N+1's anchor (last chapter to EOF).
   - `images[]` (may be empty): `{"page": 7, "index": 0, "anchor": "…",
     "caption": "…"}` — `page`/`index` select a bbox from the extraction's
     `pages.jsonl`; `anchor` (verbatim, unique, must land inside some
     chapter) places the image paragraph immediately after that paragraph.
   - `front_matter`: only `"drop"` is implemented — paragraphs before
     chapter 1's anchor are excluded from the book.

4. **Prepare** (deterministic `prepare.py`, implemented) — validate the
   draft (anchors exist and are unique and ordered, image refs resolve,
   every paragraph lands in exactly one chapter or the dropped front
   matter — asserted, not trusted), then cut `chapters/*.md` + `book.json`
   and crop/grayscale/downscale (480px max width) images to device spec —
   emitting exactly the format the epub-builder skill's `FORMAT.md`
   specifies. Validation failures exit 1 with a precise, fixable message
   ("anchor not found", "anchor ambiguous (N hits)", "anchors out of
   order") — prepare.py never guesses.

   ```bash
   .venv/bin/python .claude/skills/pdf2epub/scripts/prepare.py \
       workspace/<slug>   # expects draft.json; restore/ defaults to workspace/<slug>/restore
   ```

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
