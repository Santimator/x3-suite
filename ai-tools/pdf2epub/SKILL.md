---
name: pdf2epub
description: >-
  Convert a PDF (born-digital or scanned) into a clean EPUB for an Xteink X3 or
  X4 Pro e-ink reader. Use when the user wants to turn a PDF into an EPUB,
  convert a scanned book, get a PDF's tables and figures readable on a small
  screen, or points at a workspace folder containing a source.pdf. Triggers
  include "pdf to epub", "convert this pdf", "ocr this book", "make this
  readable on the X3", "for the X4 Pro".
---

# pdf2epub — PDF → EPUB, a chunk at a time, gated

A PDF only says where ink goes; an EPUB needs to know what the text *is*. A
vision model is the best tool there is for that recovery: it reads a page the
way a person does, columns, headings, verse and all. What a model is not, left
alone, is reliable over a whole book: it drops a sentence, smooths a phrase,
forgets a closing fence. So the work is cut into small chunks, and **a
deterministic gate checks every chunk** before the next one starts:

- **format**: only markdown the builder renders (`epub-builder/FORMAT.md`),
  fences closed, words joined across line breaks, no page numbers left in;
- **fidelity**, where the PDF has a text layer: the chunk against that text,
  word by word. A dropped sentence or an invented / paraphrased one fails it,
  with the passage printed; a fixed typo or a dropped running header does not;
- **review**, where it has none (a scan): an AI reviewer compares the chunk
  with the page images, and assembly refuses a chunk without an "ok".

Then the book is assembled, built, verified, and **read on the device** — the
real last gate.

Figures and tables travel as **images sized for the reader's screen** (a 6"
panel cannot show them at print size, and the builder has no tables): turned
90° when that makes them much bigger, and optionally split over two
consecutive pages, so you page back and forth between the halves.

## 0. Ask first — always

Before planning, ask the user (both are their choice, not yours; `plan.py`
refuses to run without them):

1. **Which reader?** `x3` — Xteink X3, 528×792 (the suite's default) — or
   `x4pro` — Xteink X4 Pro, 480×800.
2. **Tables and figures: single or double?** `single` — each on one page,
   turned 90° when that makes it ≥1.25× bigger. `double` — additionally split
   in two halves on consecutive pages when that makes it ≥1.25× bigger than
   single (dense tables, detailed diagrams).

Also confirm title/author if the PDF's metadata looks wrong.

## 1. Plan

```bash
.venv/bin/python ai-tools/pdf2epub/scripts/plan.py workspace/<slug> \
    --device x3 --figures single --title "…" --author "…" [--language es]
```

`workspace/<slug>/source.pdf` must exist. One command runs triage, extracts
the text layer (column-aware: a two-column page comes out in reading order),
renders the pages to `pages/pNNN.png`, detects figure candidates
(`figures/candidates.json`, previews in `figures/previews/`), and writes
`job.json` and `chunks/cNN/input.json` (2 pages per chunk; `--pages-per-chunk`).

**Look at a page or two before going on.** If the PDF has a text layer that is
garbage (a scan with bad embedded OCR: `Ve]. ^ TO me ga`), re-plan with
`--untrusted-text --force`: those chunks lose the fidelity baseline and need a
review instead. Triage cannot tell good OCR from bad; your eyes can.

## 2. Write each chunk, gate it

For each chunk in order, read **`ai-tools/pdf2epub/prompts/writer.md`** (the
writing rules, shared with the headless runner — read it once, follow it for
every chunk), then:

1. Look at the chunk's page images (`input.json` → `page_images`), read its
   text layer (`text`), look at its figure candidates' previews.
2. Write `chunks/cNN/out.md`: the pages' body text in reading order, real
   paragraphs, furniture dropped, `#`/`##` headings, figures placed as
   `[[fig:ID | caption]]`, `[[continues]]` as the last line if the last
   paragraph runs on into the next chunk.
3. Gate it:

   ```bash
   .venv/bin/python ai-tools/pdf2epub/scripts/check_chunk.py workspace/<slug> cNN
   ```

   It fails on a run of ≥12 source words missing or ≥8 words not in the
   source, or recall < 0.95 / precision < 0.97 (shares of words covered).
   Short runs are expected and harmless: a dropped running header, a word
   you corrected (text-layer ligature leaks like `signififica` show up as a
   one-word "missing" + "invented" pair). On FAIL, the report says what:
   format errors by line, or the missing / invented passages. Fix *those* against the page image and re-run. Do not
   edit around the gate (re-wording until it passes is the failure it exists
   to catch); if you believe the gate is wrong about a passage, look at the
   page again, and say so to the user if it still is.

`check_chunk.py workspace/<slug>` (no ids) gates every written chunk and prints
each one's state: `todo`, `unchecked`, `failed`, `needs-review`, `done`. The
state lives in files, so a conversion can stop and resume at any point, and be
finished by a different driver.

**Chunks without a text layer** (`needs-review`): after the gate passes, review
the chunk against its pages following **`prompts/reviewer.md`** — ideally as
a fresh pass (a subagent with no memory of writing it), since reviewing your
own transcription in the same breath catches little. Fix what it finds, re-gate,
and write `chunks/cNN/review.json`:

```json
{"sha256": "<sha256 of out.md, as in check.json>", "verdict": "ok", "issues": [], "reviewer": "…"}
```

### Figures and tables

A candidate is an embedded image or a ruled table pdfplumber found. Content
(charts, diagrams, tables, photos the text refers to) goes in with a caption;
decoration (author thumbnails, ornaments) is left out or kept small with
`| inline`. A third field overrides the job's mode for one figure:
`| single`, `| double`, `| inline` (page width, never turned or split).

A figure the detector missed (a vector diagram, an unruled table) is added by
hand, then placed like any other:

```bash
.venv/bin/python ai-tools/pdf2epub/scripts/figures.py add workspace/<slug> \
    --page 12 --bbox 40,120,440,380 --kind table   # PDF points; px * 72 / dpi on pages/*.png
```

Look at `figures/previews/<id>.png` to check the box. The text inside a placed
figure is taken out of the chunk's fidelity baseline automatically.

## 3. Assemble, build, verify

```bash
.venv/bin/python ai-tools/pdf2epub/scripts/figures.py prepare workspace/<slug>
.venv/bin/python ai-tools/pdf2epub/scripts/assemble.py workspace/<slug>
.venv/bin/python epub-builder/scripts/build_epub.py workspace/<slug> \
    --out workspace/<slug>/build/<slug>.epub
.venv/bin/python ai-tools/pdf2epub/scripts/verify.py workspace/<slug> \
    --epub workspace/<slug>/build/<slug>.epub
```

- `figures.py prepare` renders every placed figure for the job's device and
  mode into `images/` and prints each one's layout (`single`, `single, turned
  90°`, `split-x`, `split-y`, `inline`) and pixel size.
- `assemble.py` refuses (listing why) unless every chunk is `done` and the
  figures were prepared for the current device/mode. It joins the chunks
  (`[[continues]]` paragraphs re-joined, split words de-hyphenated), expands
  figures, cuts chapters at each `# `, writes `book.json` (tight line spacing)
  and a cover for the device (a `source-cover.*` sidecar if present, else the
  default template with the title).
- `verify.py` checks EPUB integrity and that the build kept every word of the
  chapters.

Changing device or figure mode later needs no rewriting: `plan.py … --force`
with the new flags keeps every chunk whose pages did not change; then prepare,
assemble, build again.

**Then read it — on the device if possible.** The gates catch what a machine
can see; whether it reads well is the user's call. Record what the conversion
taught you in `CONVERSIONS.md`.

## Bring your own transcript

If the user has a better transcription (their own OCR, a hand transcription),
it goes next to the PDF as `source-transcript.{md,txt}`; triage reports route
`TRANSCRIPT`. Plan as usual, then write each chunk *from the transcript*,
checking it against the page images, and shaping it as `writer.md` says
(drop furniture, rebuild paragraphs, headings, figures). Don't re-flow what
the user laid out on purpose. Gates and review apply as for any chunk. The
headless runner does not take this route: matching a transcript to chunks is
judgement work.

## Headless

`headless/run_conversion.py workspace/<slug>` does sections 1–3 unattended with
any OpenAI-compatible **vision** model (`headless/config.example.json` →
`config.json`, key in `headless/secrets/`). It cannot ask, so device and figure
mode come from its config. Per chunk: write → gate → on failure, rewrite with
the gate's report (up to `max_attempts`) → review where required. It **stops
rather than guesses**: a chunk that keeps failing ends the run with the report,
progress saved; re-running resumes. Exit 0 built and verified, 1 stopped, 2 not
configured. `tools/tgbot/` calls it for a PDF sent from the phone.

## Workspace

```
workspace/<slug>/
  source.pdf                 the input, never modified
  source-cover.*             optional: your cover
  source-transcript.md       optional: your transcription
  job.json                   device, figure mode, metadata, chunks
  build/triage.json          what triage measured
  extract/                   text layer: pages.jsonl (geometry), text/pNNN.txt
  pages/pNNN.png             what the model reads
  figures/                   candidates.json, previews/, prepared.json
  chunks/cNN/                input.json, out.md, check.json, review.json
  images/                    prepared figures + cover.png
  chapters/, book.json       the common book format (epub-builder/FORMAT.md)
  build/<slug>.epub          the result
```

`workspace/` is gitignored except allowlisted samples (AGENTS.md).

## Scripts

| script | does |
|---|---|
| `plan.py` | triage + extract + render + detect figures → `job.json`, chunk inputs |
| `triage.py` | measure the PDF: text layer per page, pathologies, language, sidecars |
| `extract_text.py` | text layer per page in reading order (dedupe, columns, space recovery) |
| `render_pages.py` | pages → grayscale PNG |
| `figures.py` | `detect` / `add` candidates, `prepare` device-sized images |
| `check_chunk.py` | the gate: format + fidelity; chunk states |
| `assemble.py` | chunks → `chapters/` + `book.json` + cover |
| `verify.py` | EPUB integrity + build coverage |
| `harness.py` | shared: job file, chunk states, placeholders, text normalization |

Setup: `.venv/bin/pip install -r ai-tools/pdf2epub/requirements.txt` (pdfplumber, pypdf,
pypdfium2, Pillow). No OCR engine: the model reads the pages.

## Verifying changes to this unit

No self-test, by design: whether the pipeline works is whether a model driving
it turns a real PDF into an EPUB a human finds sound ([`CONVERSIONS.md`](CONVERSIONS.md)).
After changing a script, re-run a conversion by hand (plan → chunks → gate →
prepare → assemble → build → verify, all exit 0) and read the result. Changes
under `epub-builder/` also need the graded-reader self-test with the canary
byte-identical, and the opds-server self-test (AGENTS.md).

Design and the reasoning behind it: [`DESIGN.md`](DESIGN.md).
