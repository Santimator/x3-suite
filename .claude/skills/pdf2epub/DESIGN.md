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

## Mindset: deterministic-first, agent-on-error

The suite's split (LLM judgment / deterministic mechanics) gets a sharper
formulation here, because conversion — unlike graded-reader — starts from
text that already exists. The model is excellent at *reading* — verifying
that text is complete and correct, recognizing what kind of problem a page
has — and unrigorous at *bulk generation*. So generation is never its job:

- **The happy path is fully deterministic.** Extract → restore → build, no
  model in the loop. Scripts transform; they never invent.
- **The agent is the orchestrator and verifier, not the generator.** It
  reads triage output and text samples, confirms the route, checks the
  extraction is complete and faithful, and *diagnoses* failures ("this is
  char-doubling", "these pages need OCR", "this block is verse").
- **On error, the agent reaches for tools, not for prose.** Its output is
  small structured *decisions* — a route, a per-block reflow policy, a
  normalization-table entry, chapter anchors — which the deterministic
  scripts then apply to the whole text. **The agent writes decisions, not
  text**; every byte in the EPUB traces back to the extraction.
- **Where generation is unavoidable** (vision transcription of a page
  tesseract mangled; patching a span no tool can fix), it is last on the
  escalation ladder, span-scoped, and sits behind a deterministic fidelity
  gate that catches omission and invention. Trust comes from the gate.
- **File-based seams between stages**, so any slot can be filled by a
  script, Claude Code interactively, or a headless runner later.

### The escalation ladder

1. Deterministic extraction + deterministic fixes (char dedupe, furniture
   removal, wordlist dehyphenation, heuristic reflow).
2. Agent verification pass: read samples + coverage stats; pass → build.
3. On failure: agent diagnoses, adjusts a decision (re-route pages to OCR,
   flip a block to verse policy, add a normalization entry), re-runs step 1.
4. Last resort, span-scoped: model transcribes or patches the specific
   broken text, gated by the fidelity check, logged as a patch — never a
   silent rewrite.

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

| stage | actor | in → out | agent's role |
|---|---|---|---|
| 0 triage | script (`triage.py`) ✅ | source.pdf → triage.json + route | confirm route from samples; re-route on evidence |
| 1 extract | **toolbox** (agent-picked tool + params) | source.pdf → extract/pages (blocks w/ bbox, font, size) | pick the tool and its parameters; verify output; on failure reconsider tool or re-parameterize and re-run |
| 2 restore | script (`restore.py`, policy-driven) | raw pages + policy.json → clean text | **verify** completeness/faithfulness on samples; on failure edit policy.json and re-run; span-scoped patches only as last resort (gated) |
| 3 draft | **agent** | clean text + triage signals → draft.json | author the structured draft: what text goes where, chapter anchors, nav entries, image placements |
| 4 prepare | script | draft.json → book.json + chapters/*.md + images/ | none — the draft is validated (anchors must exist verbatim) and cut mechanically; images resized/converted to spec |
| 5 build | script | the book-format contract → EPUB | none |
| 6 verify | script | EPUB → coverage + integrity report | read the report; human spot-check |

### The toolbox (stage 1)

"The agent verifies and reconsiders" only works if it has real alternatives
to reach for. Extraction is therefore not one script but a small toolbox of
parameterized, deterministic CLI tools, and the agent's moves are: pick →
run → verify → reconsider (different tool) or re-parameterize (same tool,
different flags):

- `extract_text.py` — text-layer route. Flags: `--dedupe` (char-doubling
  fix), `--layout/--no-layout`, `--pages A-B`.
- `extract_ocr.py` — scanned route: render + tesseract. Flags: `--lang`
  (from triage's guess), `--dpi`, `--psm` (layout mode), `--pages`.
- `render_pages.py` — pages → PNG, so the agent can *look* at what a page
  actually shows when text output makes no sense, and for last-resort
  vision transcription of a page tesseract mangled.
- Tools are per-page composable: a HYBRID book can use the text layer for
  the body and OCR for two image-only pages.

### The draft and the prepare step (stages 3–4)

Between "clean text" and "EPUB" the agent authors **draft.json** — the
structured plan of the book: spine order, chapter boundaries as verbatim
anchors, TOC labels, which images survive and where they land, front-matter
handling. It is the agent's whole creative output, and it is *checkable*:
`prepare.py` validates every anchor exists in the text, every image ref
resolves, every paragraph lands in exactly one chapter — then cuts chapters,
resizes/grayscales images to device spec, and emits the builder's input.

`policy.json` (stage 2) is the same idea one level down: per-block
`reflow: prose|verse|preserve`, furniture patterns to drop, a punctuation
normalization table, dehyphenation exceptions — switches the restorer
applies mechanically. The deterministic pre-passes (char dedupe,
repetition-based furniture removal, wordlist dehyphenation) handle
everything provable before the agent ever looks.

### The builder contract

The builder is now its own skill — suite infrastructure at
`.claude/skills/epub-builder/` — and consumes exactly one thing: the common
book format **documented in its [`FORMAT.md`](../epub-builder/FORMAT.md)** —
book.json + chapters/*.md + prepared images/. That document *is* the
contract the agent must know when drafting and preparing; if a construct
isn't in it, the builder doesn't support it. pdf2epub's needs (verse blocks,
images, endnotes, cover) are specified there as extensions for the builder
to implement.

## The common bus

Stage 3 emits the **same intermediate format graded-reader uses**:
`chapters/*.md + book.json`. Consequences:

- One EPUB builder for the whole suite (generalize `build_epub.py`: pinyin
  becomes an optional feature, not the spine of the script).
- Device fonts are shared too: `reference/fonts/WenKaiFull` covers full CJK,
  so a converted *Chinese* PDF needs no per-book font work (charset-level
  subsetting was retired — sparse-interval fonts fail on-device; readers.md).
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
4. **Fidelity vs. readability.** ~~Open~~ **Settled by the mindset:** all
   edits (the fixture's low-9 `‚` for `,`, OCR confusions) go through the
   normalization table in `policy.json` — the agent proposes entries, the
   restorer applies them mechanically and logs them. No silent edits.
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
