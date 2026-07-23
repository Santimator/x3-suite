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

**Status: fully implemented (stages 0-2 and 4-6). Proven by the worked
conversions under `workspace/` — OCR'd verse plays (`alcaldes-encontrados`,
`gurruminos`) and a longer 3-act comedia (`el-espanol-de-oran`), each with its
`policy.json`/`draft.json` and a built, verified EPUB. The pipeline strips
page-number/catchword furniture, normalizes OCR junk marks, recovers spacing
where the text layer dropped it, and reflows verse or prose — faithfully
preserving residual OCR noise rather than inventing corrections ("every byte
traces back to the extraction"). Stage 3 (draft) is always the agent, by
design. There is no deterministic self-test — see the "Verifying" section and
[`CONVERSIONS.md`](CONVERSIONS.md).**

## Workspace convention

One folder per conversion job, mirroring graded-reader books:

```
workspace/<slug>/
  source.pdf          the input (never modified)
  build/triage.json   stage 0 output
  extract/            stage 1 output (raw per-page extraction)
  policy.json         stage 2 input — the agent's restore decisions
  restore/            stage 2 output (restored.md + restore-report.json;
                      optional corrected.md from the guarded stage 2b)
  draft.json          stage 3 output — the agent's structured plan of the book
  chapters/*.md       stage 4 output ┐
  book.json           stage 4 output ├ the common book format (builder input)
  images/             stage 4 output ┘ prepared: grayscale, device-width
  build/<slug>.epub   stage 5 output
```

Converted books converge on the suite's common book format — **documented in
the epub-builder skill's contract,
[`epub-builder/FORMAT.md`](../../epub-builder/FORMAT.md); the
agent must know it when drafting** — so the EPUB builder, the device fonts
in `reference/fonts/`, and device lore in `reference/readers.md` are shared
with graded-reader.

## Stages

0. **Triage** (deterministic, implemented) — characterize the source, flag
   pathologies, recommend a route:

   ```bash
   .venv/bin/python services/pdf2epub/scripts/triage.py \
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
   `--dedupe`, `--pages`, `--space-recover`), `extract_ocr.py` (tesseract;
   `--lang`, `--dpi`, `--psm`), `render_pages.py` (page images, for the
   agent's own eyes or last-resort vision transcription). `--space-recover`
   rebuilds inter-word spaces from glyph gaps — reach for it when triage
   flags `broken_spacing` (born-digital PDFs that render justified text with
   no space glyphs come out word-runtogether otherwise; tune `--space-ratio`
   if a page over/under-splits). Per-page composable for HYBRID books. Tools
   never edit, only extract; the agent
   verifies output and reconsiders tool or parameters on failure.
   `extract_ocr.py` degrades gracefully (exit 1, one-line hint) when the
   `tesseract` binary or `pytesseract` module is absent — never auto-installs.

2. **Restore** (deterministic `restore.py` driven by `policy.json`,
   implemented) — reflow paragraphs across page breaks, dehyphenate, drop
   furniture, normalize punctuation, preserve deliberate line breaks
   (verse/drama), all as mechanical transforms configured by a policy file.
   The agent *verifies* the result on samples; on failure it diagnoses,
   edits the policy (e.g. flips a block to `verse`, adds a normalization
   entry), and re-runs. **Favour the policy** — a fix expressed as a rule
   (furniture, reflow, especially a `normalize` entry) replays from source
   and stays auditable. One-off OCR damage that isn't worth a rule takes the
   guarded-correction path (stage 2b).

   ```bash
   .venv/bin/python services/pdf2epub/scripts/restore.py \
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
   - `page_ranges`: every extracted line must be covered by exactly one
     range; overlaps or gaps are a restore error, never guessed. A range is
     either `"pages": "A-B"` (1-indexed, inclusive; also accepts a single
     `"N"`) — whole pages, the common case — or `"start_anchor"` +
     `"end_anchor"` (both required together), each matching one physical
     extracted line's text **exactly**: isolates a precise line span, e.g.
     a verse passage embedded mid-page that page-level granularity can't
     separate from the surrounding prose. If the anchor text repeats (a
     refrain), it's ambiguous by default — never guessed — until the
     policy disambiguates with an explicit 1-indexed
     `"start_anchor_occurrence"` / `"end_anchor_occurrence"` (the agent's
     decision, not the script's); an `end_anchor` search always starts
     from its own range's resolved `start_anchor`, so an earlier
     occurrence outside the range can't cause false ambiguity on its own.
     `treat`: `front_matter` — lines pass through **verbatim**, one
     paragraph per surviving physical line, no reflow/dehyphenation (the
     draft later decides what becomes title metadata, or drops them);
     `body` — dehyphenate (if on) then reflow; `skip` — the pages' content
     is dropped from the document (still counted in the fidelity gate's
     input baseline, so a skip that eats real text will fail the gate
     rather than silently vanishing).
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

2b. **Correct** (agent, guarded, optional) — for a genuine one-off the policy
   shouldn't carry (a single `teh`→`the`, a dropped accent, a name mangled in
   exactly one spot), the agent may hand-edit a copy of the restored text:
   copy `restore/restored.md` to `restore/corrected.md` and fix it directly.
   `restored.md` stays the immutable mechanical baseline. **Prefer the policy
   (`normalize`) for anything systematic** — corrected.md is for the local
   fix that's more natural to make by hand than to write a rule for.

   `review_edits.py` is the deterministic guard that keeps this honest: it
   bounds the diff to small, local corrections (default: ≤ 2% of chars
   changed, no single changed run > 24 chars, ≤ 1% net growth) and **prints
   it** for review. A rewrite or invented text trips the bound and exits 1 —
   the signal that the change belongs in the policy, not a free edit. The
   guarantee shifts from "replays byte-for-byte from the PDF" to "every
   change is small, local, and shown to you".

   ```bash
   .venv/bin/python services/pdf2epub/scripts/review_edits.py \
       workspace/<slug>/restore        # diffs corrected.md vs restored.md
   ```

   Downstream is transparent: if `corrected.md` exists, prepare cuts it and
   verify's coverage checks against it (review_edits having bounded it vs
   `restored.md`); if it doesn't, everything runs from `restored.md` as
   before. Skip this stage entirely on the policy-only path.

3. **Draft** (agent) — the agent authors `draft.json`, the structured plan
   of the book: title/author, chapter boundaries as *verbatim anchors*, TOC
   labels, image placements, front-matter handling. Its whole creative
   output — and every claim in it is checkable.

   `draft.json` schema:
   ```json
   {
     "title": "Los alcaldes encontrados",
     "author": "Tirso de Molina",
     "language": "es",
     "chapters": [
       {"toc_label": "Los alcaldes encontrados", "start_anchor": "PERSONAS."}
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
   .venv/bin/python services/pdf2epub/scripts/prepare.py \
       workspace/<slug>   # expects draft.json; restore/ defaults to workspace/<slug>/restore
   ```

5. **Build** (deterministic, exists) — the suite-shared **epub-builder**
   skill (`epub-builder/`): X3-friendly EPUB, no CJK
   dependencies for generic books. The FORMAT.md extensions (verse blocks,
   images, endnotes, emphasis, cover) are implemented on the un-annotated
   path; the annotated (graded-reader) path is untouched and frozen.

6. **Verify** (deterministic `verify.py`, implemented) — EPUB integrity
   (mimetype first/stored, manifest ⇄ zip parity, every href/fragment
   resolves, every XHTML/OPF entry well-formed) plus a coverage report:
   strip tags from the spine, run it through the same fidelity gate
   restore.py uses against `restore/restored.md`. Exits 1 on any failure —
   the last check in the pipeline, catching what prepare.py or the builder
   silently dropped.

   ```bash
   .venv/bin/python services/pdf2epub/scripts/verify.py \
       workspace/<slug> --epub workspace/<slug>/build/<slug>.epub
   ```

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r services/pdf2epub/requirements.txt
```

## Verifying — there is no self-test, by design

Whether this pipeline "works" is whether an agent can drive the tools to a
faithful EPUB that a human, reading it, finds sound. A frozen replay of one
fixture's bytes would only prove the scripts didn't change — so there isn't
one. The proof is the **worked conversions**: the committed samples under
`workspace/` (their `source.pdf` + `policy.json` + `draft.json` and the built
`build/<slug>.epub`), annotated in [`CONVERSIONS.md`](CONVERSIONS.md) — which
also lists the tool gaps those conversions surfaced.

To sanity-check the scripts after editing them, re-run a sample by hand
(`triage → extract_text → restore → prepare → build_epub → verify`, all exit
0 on success) and read the EPUB. Structural soundness alone is one command:
`epub-builder/scripts/verify_epub.py workspace/<slug>/build/<slug>.epub`.

Changes under `epub-builder/` are shared infrastructure, so also re-run the
graded-reader check (`services/graded-reader/scripts/selftest.py`) and keep the
annotated path's output byte-identical — `workspace/yugong-mountain` is the
canary.

## Test fixture

`workspace/alcaldes-encontrados/source.pdf` — *Los alcaldes encontrados*, a
16-page 1793 printing of a Spanish entremés (attributed to Tirso de Molina;
public domain),
converted (`build/alcaldes-encontrados.epub`). It is a scanned book with an
ABBYY-FineReader OCR text layer — a different, and more common, kind of hard
source than a born-digital PDF: the pathology isn't doubled glyphs but
**OCR noise**. Triage routes it TEXT and flags `page_furniture` +
`broken_spacing`.

The committed `policy.json` handles it deterministically: **furniture**
patterns drop the per-page numbers (many OCR-mangled — `ΙΟ`, `T2`, `τβ`, bare
`s`/`f`/`l`) and the printer's catchwords at each page foot (`Ver`, `Ino-`,
`Sa-`…); **normalize** maps the pervasive OCR middot `·`→`.` and strips the
stray `*` marks (which would otherwise be eaten as Markdown emphasis by the
builder); the body is a verse play, so it reflows `verse` (line structure
kept) while the three decorative title lines are dropped as front matter via
`draft.json`. Dehyphenation is deliberately left **off** — on a short verse
text, merging the handful of wrapped words moves more of the fidelity gate's
n-gram budget than it's worth, and keeping the physical lines is truer to the
verse anyway.

Residual OCR errors in the letters themselves (`vues^rced`, `Escribana` for
*Escribano*) are **preserved, not guessed away** — that is the pipeline's
whole contract. The fixture proves the mechanics end to end; it is not
claimed to be a clean scholarly read. Triage output lives next to the source.
