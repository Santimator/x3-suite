# X3 suite

Tools that produce EPUBs for an e-ink reader — concretely an **Xteink X3**
running CrossPoint firmware, fed through a Calibre-Web-Automated ingest
folder. One mindset across all of them:

> **LLM roles for judgment, deterministic scripts for mechanics, and a
> deterministic gate after every LLM step.** Models handle what varies
> (writing prose, restoring mangled text, inferring structure); scripts
> measure, validate, track state, and assemble — they never invent. Trust
> comes from the gates, not the model.

The suite is one **builder** plus two **services** (the AI tools), each a
self-contained directory at the repo root. Every service converges on the
**common book format** — `chapters/*.md + book.json` in a `workspace/<slug>/`
folder — which the builder consumes. Each directory's `SKILL.md` is its
canonical documentation.

## Architecture

One builder, two services, both emitting the builder's format:

```
epub-builder/               infrastructure — the book format's contract
                            (FORMAT.md), build_epub.py, shared verify_epub.py
services/
  ├─ graded-reader/         service — writes leveled Chinese books
  └─ pdf2epub/              service — converts PDFs into clean EPUBs
workspace/<slug>/           one folder per book/job (inputs → build/ outputs)
reference/                  device notes + SD-ready fonts for the X3
```

The layout is agent-agnostic: `AGENTS.md` at the root is the entry point for
any coding agent, and `.claude/skills/` symlinks the three directories so
Claude Code still auto-loads them as skills.

Each AI tool has the **same shape**, and it's the shape worth copying for a
new one:

- **A briefing** — the `SKILL.md` (plus role prompts for graded-reader) that
  tells the model how to undertake the task and when to defer to a tool.
- **Deterministic tools for the parts models are bad at** — segmenting and
  grading vocabulary, extracting and OCR-ing a PDF, reflowing text, cutting
  chapters, assembling and verifying the EPUB. Scripts measure, transform,
  and check; they never invent.
- **A deterministic gate after every model step** — vocabulary rate gates for
  the writer; a restore fidelity gate and an EPUB coverage+integrity gate for
  the converter. The model proposes; a gate disposes.

So the division of labour is constant: **the model supplies judgement
(prose, a restore policy, chapter structure), scripts supply mechanics and
verification, and trust comes from the gates, not the model.** Where the
model needs to touch model-unfriendly ground directly — e.g. a one-off OCR
fix in pdf2epub — it does so through a *guarded* path: a deterministic check
bounds and prints the edit (see pdf2epub stage 2b). The model can adjust
parameters and re-run, or make a small, bounded correction; it can't quietly
rewrite.

The tools are plain CLI scripts with typed JSON/file I/O, so any agent that
can run a shell and read files drives them — Claude Code, or graded-reader's
optional headless runner against any OpenAI-compatible endpoint.

## Infrastructure

### epub-builder — the shared EPUB builder

One builder for every task, hand-built XHTML/OPF, deterministic output
(same source → byte-identical EPUB). Its input contract — what tasks are
allowed to hand it — is
[`epub-builder/FORMAT.md`](epub-builder/FORMAT.md).
Pinyin is marked by the graded-reader service before the build; generic
books build with zero CJK dependencies.

It also ships `verify_epub.py` — the one structural-integrity check (mimetype,
manifest⇄zip parity, well-formed XML, link resolution) that both tasks share,
so "is this a sound EPUB?" has a single implementation.

```bash
.venv/bin/python epub-builder/scripts/build_epub.py \
    workspace/<slug> --out workspace/<slug>/build/<slug>.epub
.venv/bin/python epub-builder/scripts/verify_epub.py \
    workspace/<slug>/build/<slug>.epub
```

## Tasks

### graded-reader — generate Chinese graded readers *(working)*

Write leveled Chinese books chapter-by-chapter: a *planner* outlines, a
*scribe* drafts each chapter against a mechanically-built vocabulary brief,
deterministic validation gates the HSK level (out-of-list ≤ 5%, stretch
≤ 15%), a *glossary editor* prunes the harvested glossary, and the builder
`annotate.py` marks each glossary word's first appearance with its pinyin, and
the generic builder assembles the EPUB.

Docs: [`services/graded-reader/SKILL.md`](services/graded-reader/SKILL.md)

```bash
python3 -m venv .venv
.venv/bin/pip install -r services/graded-reader/requirements.txt
S=services/graded-reader/scripts

.venv/bin/python $S/validate.py workspace/being-earnest       # grade a book
.venv/bin/python $S/selftest.py                               # full pipeline check
# EPUB assembly: the shared epub-builder (see Infrastructure above)
```

Two drivers: Claude Code interactively, or the optional headless runner in
`services/graded-reader/headless/` (`run_book.py`) against any
OpenAI-compatible endpoint — kept out of the core so the skill is just the
briefing plus deterministic tools. On Debian/Ubuntu install `jieba` inside a
venv — system setuptools breaks its legacy `setup.py`.

### pdf2epub — convert PDFs into clean EPUBs *(fully implemented and proven end-to-end on its test fixture)*

PDFs are page descriptions (where ink goes); EPUB is a document (what the
text is). The pipeline recovers intent from ink, **deterministic-first,
agent-on-error**: triage characterizes the source, scripts extract and
restore the text on the happy path, and the agent orchestrates and
verifies — confirming routes, diagnosing failures, and emitting decisions
(policy switches, chapter anchors) that scripts apply. The model never bulk-
generates: every byte in the EPUB traces back to the extraction.

Docs: [`services/pdf2epub/SKILL.md`](services/pdf2epub/SKILL.md) ·
design + open questions: [`DESIGN.md`](services/pdf2epub/DESIGN.md)

```bash
.venv/bin/pip install -r services/pdf2epub/requirements.txt
```

Proof by worked conversion, not self-test: whether pdf2epub works is whether an
agent can turn a real PDF into a faithful EPUB a human finds sound, so the proof
is the committed sample under `workspace/` — the public-domain Spanish
entremés `alcaldes-encontrados`, a full vision transcription with a built,
verified EPUB — annotated,
with the tool gaps they surfaced, in
[`services/pdf2epub/CONVERSIONS.md`](services/pdf2epub/CONVERSIONS.md). The
pipeline strips furniture, normalizes OCR marks, recovers spacing, and reflows
verse or prose, faithfully preserving residual OCR noise — never inventing
corrections.

## Shared ground

```
reference/readers.md    Xteink X3 / CrossPoint device notes: confirmed
                        rendering verdicts, font build rules, SD layout —
                        read before touching anything device-facing
workspace/<slug>/       one folder per book/job (source, chapters/, book.json,
                        build/ outputs)
reference/fonts/        SD-ready .cpfont families for the device: WenZilla
                        (recommended Chinese hybrid — WenKai kaiti + Zilla Slab
                        Latin/pinyin), WenKaiFull (pure kaiti, confirmed
                        working)
```

Device facts that shape every tool (details in `reference/readers.md`):
embedded EPUB fonts are useless (the reader rasterizes only pre-converted
`.cpfont` bitmaps), ruby and interlinear pinyin are confirmed broken (use the
`reading_style: after`), RAM is ~400 KB — keep books lean and CSS simple.

## License

The code and documentation are **MIT** licensed — see [`LICENSE`](LICENSE).

> Offered in the spirit of a shift already underway: as AI lets anyone
> generate software tailored to their own needs, the scarcity that
> intellectual property was built to protect is fading. Take it, adapt it,
> make it yours.

Two kinds of bundled third-party content keep their own terms:

- **Fonts** (`reference/fonts/*.cpfont`) are conversions of open-source fonts
  (LXGW WenKai, Zilla Slab, Noto CJK) under the **SIL Open Font
  License 1.1** — notices, sources, and the license text in
  [`reference/fonts/ATTRIBUTION.md`](reference/fonts/ATTRIBUTION.md) and
  [`reference/fonts/OFL.txt`](reference/fonts/OFL.txt).
- **Sample book texts** under `workspace/` are either original to this project
  or public-domain source material (e.g. 西游记, 愚公移山, O. Henry's *The Gift
  of the Magi*, the 1793 entremés *Los alcaldes encontrados*), retold or
  converted as pipeline demonstrations.
