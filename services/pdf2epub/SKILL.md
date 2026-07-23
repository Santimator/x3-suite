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

Recover a *document* from a *page description*. A PDF only says where ink
goes; an EPUB needs to know what the text *is* (paragraphs, chapters, a nav).
How hard that recovery is depends entirely on the source, so the pipeline
sorts every job by one principle:

**"Lo bueno, barato; lo malo, posible."** A clean source is converted
*cheaply* — deterministic extraction plus light, rule-based cleanup, no model
in the loop for bulk text. A bad source — a raw scan, or a scan whose embedded
OCR is garbage — is converted *at all*, by the agent **reading the rendered
page images with its own vision and writing the clean text directly**. Direct
scans are practically impassable to automatic extraction; vision transcription
is the unlock that makes them possible.

The dividing question is never "what does the text layer say?" but **"is the
text layer trustworthy?"** — and the agent answers it by rendering a few pages
and *looking*. The ground truth is always the printed page, never the OCR.

## The two routes

| | **cheap route** — "bueno, barato" | **vision route** — "malo, posible" |
|---|---|---|
| source | born-digital, or scan with clean OCR (class A/B) | scan, or scan with garbage OCR (class C/D/E) |
| ground truth | the text layer | the **rendered page image** |
| who writes the text | scripts (`extract` → `restore`) | the **agent**, reading pages, into `chapters/*.md` |
| the agent's job | pick tools, verify completeness, diagnose | **transcribe**: fix OCR errors, re-join column-broken lines, rebuild real paragraphs, keep verse lines whole, drop furniture |
| the gate | fidelity to the text layer (char/ngram) + read | **completeness** (nothing whole dropped) + **a human reading it** |

The old contract — *"every byte traces back to the extraction"* — holds **only
on the cheap route**, where the extraction is trustworthy. On the vision route
it is explicitly *wrong*: faithfully carrying OCR garbage into the EPUB is what
puts `qdueejaáqmuiepfouir` and column-shredded half-lines on the device. There
the **agent's vision *is* the restoration engine**; the deterministic tools are
mechanics *around* it — render the pages, build the EPUB, check nothing was
dropped. Both routes converge on the same output (`chapters/*.md` +
`book.json`) and the same builder.

**Deciding the route.** Triage recommends one, but the agent confirms it the
only reliable way: `render_pages.py` a handful of pages, read them, and compare
to what `extract_text.py` pulled. Extraction clean and faithful → cheap route.
Extraction is a jumble of split words, scrambled columns, or nonsense letters →
**vision route, and do not fight it** — no policy of furniture regexes and
normalize entries will rescue a garbage OCR layer; transcribe instead.

Full design rationale + open questions: [`DESIGN.md`](DESIGN.md).

**Status: fully implemented. The cheap route is deterministic scripts (stages
0–2, 4–6); the vision route is the agent writing `chapters/*.md` directly
(see "Vision transcription" below) and the same build/verify. Proven by the
worked conversions under `workspace/` — `alcaldes-encontrados` is a full
vision transcription of a 1793 entremés; `el-espanol-de-oran` a longer comedia.
There is no deterministic self-test — see the "Verifying" section and
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

On the **vision route** the workspace is minimal: `source.pdf`, the rendered
`pages/*.png` you read from, and the `chapters/*.md` + `book.json` you write by
hand. No `extract/`, `policy.json`, `restore/`, `draft.json`, or `prepare` step
— you *are* the extract-through-prepare pipeline.

## Vision transcription (the "malo, posible" route)

When triage or your own eyes say the OCR is untrustworthy, transcribe. This is
not a last resort or a span-scoped patch — for a scan it is the *primary* path,
and the agent's own reading is the whole engine.

**Workflow:**

1. **Render.** `render_pages.py source.pdf --out pages --dpi 200` (200 dpi is
   plenty to read 18th-c. type; go 300 for tiny footnotes). Read the images in
   batches with your vision — the page is the ground truth.
2. **Transcribe into `chapters/*.md` directly.** Write clean Markdown as you
   read. You are not copying the OCR; you are reading the *page* and writing
   what it says. Actively:
   - **Fix OCR errors** — `qdueeja…` back to real words, restore dropped
     accents, un-scramble letters. You can see the glyphs; use that.
   - **Re-join column/line-wrap breaks** into whole units — a metrical verse
     line or a real prose sentence, never the printer's short column-width
     fragments.
   - **Rebuild real paragraphs** from reflowed prose; **keep verse lines whole**
     inside ` ```verse ` fences.
   - **Drop furniture** — running page numbers, catchwords, signature marks,
     the publisher's colophon.
   - **Keep the author's orthography.** Fix what the *scanner* got wrong, not
     what the *author* wrote: period spelling (`felíz`, `judio`, `christiano`),
     archaic forms and punctuation stay. You are restoring the page, not
     modernizing the text.
3. **Structure for the small screen.** ~12–20 lines per screen
   (`reference/readers.md`) means structure carries the read: `#` headings per
   chapter/act/scene, `*italic*` paragraphs for stage directions / section
   breaks, speaker labels prefixing each turn. Give the reader landmarks.
4. **Write `book.json` by hand** — title, author, language, and the chapter
   list (see the epub-builder `FORMAT.md`). Skip `draft.json`/`prepare.py`
   entirely; those exist to *cut* a machine-restored blob into chapters, and
   you've already written the chapters.
5. **Build and verify** exactly as the cheap route does (stages 5–6). On this
   route `verify.py`'s coverage compares the EPUB against *your* `chapters/*.md`
   — a completeness/self-consistency check (did the build drop anything?), not
   a fidelity-to-OCR check. The n-gram number against the old OCR is meaningless
   here and is not the gate. **The gate is you reading the EPUB** and, ideally,
   the user reading it on-device.

**Markdown conventions** (what the builder understands — see `FORMAT.md`):

- `# Title` / `## Act` — chapter and section headings.
- ` ```verse ` … ` ``` ` — a run of lines whose breaks must survive (poetry,
  drama). Merge column-wraps *before* fencing; each metrical line is one line.
- `*italic*` on its own paragraph — stage directions (`*Salen los dos
  Alcaldes.*`), sung-section markers (`*Canta.*`), editorial breaks.
- Speaker labels (`Vej.`, `Dom.`, `Esc.`) prefix the first line of each turn,
  inside the verse fence — abbreviate consistently, matching the source.

**Interlineado / screen.** Do *not* cap line length — the reader controls that
with font size and landscape mode. Our job is the opposite: waste no vertical
space. The un-annotated build path already minimizes `line-height`
(`PDF2EPUB_CSS`); you just supply clean structure. See
`reference/readers.md` § "Screen text capacity".

## Stages (the "bueno, barato" route)

These stages are the deterministic cheap route — a clean text layer flows
through them with no model writing bulk text. On the vision route you skip
stages 1–4 (you replace them) and use only 0 (triage, to decide), 5 (build),
6 (verify).


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
   strip tags from the spine and compare it against the book's own
   `chapters/*.md`. **What that coverage means depends on the route.** On the
   cheap route the chapters descend mechanically from the trusted extraction,
   so coverage is a genuine fidelity gate (nothing lost extract→EPUB). On the
   **vision route the chapters are the agent's transcription**, so coverage is
   a *completeness / self-consistency* check — `char_ratio ≈ 1` confirms the
   build dropped nothing; the n-gram number will dip wherever `*emphasis*`
   markers render as `<em>` and is *not* a fidelity signal against the OCR.
   Integrity always exits 1 on failure; on the vision route read the coverage
   as "did anything whole disappear", and let a human reading the EPUB be the
   real gate.

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

Whether this pipeline "works" is whether an agent can drive the tools to an
EPUB that a human, reading it, finds sound — and on the vision route that
reading *is* the test, not a byte-replay. A frozen replay of one fixture's
bytes would only prove the scripts didn't change, and would actively mislead on
the vision route (it would demand fidelity to the OCR garbage) — so there isn't
one. The proof is the **worked conversions**: the committed samples under
`workspace/` (their `source.pdf`, whatever route artifacts they used — a
`policy.json`/`draft.json` on the cheap route, hand-written `chapters/*.md` on
the vision route — and the built `build/<slug>.epub`), annotated in
[`CONVERSIONS.md`](CONVERSIONS.md) — which also lists the tool gaps those
conversions surfaced.

To sanity-check the cheap-route scripts after editing them, re-run a sample by
hand (`triage → extract_text → restore → prepare → build_epub → verify`, all
exit 0 on success) and read the EPUB. For the vision route the "test" is
inherently a read: render, transcribe a chapter, build, and *look* at the
result on-device or in a reader. Structural soundness alone is one command:
`epub-builder/scripts/verify_epub.py workspace/<slug>/build/<slug>.epub`.

Changes under `epub-builder/` are shared infrastructure, so also re-run the
graded-reader check (`services/graded-reader/scripts/selftest.py`) and keep the
annotated path's output byte-identical — `workspace/yugong-mountain` is the
canary.

## Worked fixture — the vision route end to end

`workspace/alcaldes-encontrados/source.pdf` — *Los alcaldes encontrados*, a
16-page 1793 printing of a Spanish entremés (public domain) — is the reference
vision-route conversion. It is a scan with an OCR text layer so noisy the
extraction reads `Ve]. ^ TO me ga` for `Vej. No me tenga` and shreds every
verse line at the column width. This is exactly the source class where the
cheap route *cannot* win: no furniture regex or normalize table reconstructs
letters the OCR never got right.

So `chapters/ch01.md` is a **full vision transcription** — read off the
rendered pages (`render_pages.py … --dpi 200`), all 16, by eye. It fixes the
OCR letter by letter, re-joins the column-broken verse into whole metrical
lines, labels every turn (`Vej.`/`Dom.`/`Esc.`/`Pre.`/`Muj.`/`Gra.`), sets the
stage directions as `*italic*` paragraphs and the closing tonadillas as
`*Canta.*`/`*Estrivillo.*` blocks — while keeping the 1793 orthography
(`felíz`, `judio`, `Christiano`) untouched. Furniture (page numbers,
catchwords, the Quiroga colophon) is simply not transcribed. There is no
`policy.json`, `restore/`, or `draft.json`: on this route the agent replaces
those stages.

`verify.py` reports `char_ratio ≈ 0.99` (complete — nothing whole dropped) and
a lower n-gram containment (the `*emphasis*`/`<em>` artifact described in stage
6, not a defect). The real proof is that the EPUB *reads* — which is the whole
point of the redesign: the earlier faithful-to-OCR build produced on-device
garbage; this one is a clean read. Triage output lives next to the source.
