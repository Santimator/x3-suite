# X3 suite

Tools that produce EPUBs for an e-ink reader — concretely an **Xteink X3**
running CrossPoint firmware, fed over WiFi by the suite's own OPDS server. One
mindset across all of them:

> **LLM roles for judgment, deterministic scripts for mechanics, and a
> deterministic gate after every LLM step.** Models handle what varies
> (writing prose, restoring mangled text, inferring structure); scripts
> measure, validate, track state, and assemble — they never invent. Trust
> comes from the gates, not the model.

The suite is **infrastructure** — a builder and a server — plus two
**services** (the AI tools), each a self-contained directory at the repo root.
Every service converges on the **common book format** —
`chapters/*.md + book.json` in a `workspace/<slug>/` folder — which the builder
consumes and the server delivers. Each directory's `SKILL.md` is its canonical
documentation.

## Architecture

Two pieces of infrastructure, two services emitting the builder's format:

```
epub-builder/               infrastructure — the book format's contract
                            (FORMAT.md), build_epub.py, shared verify_epub.py
opds-server/                infrastructure — serves build/ output to the X3
                            over WiFi as an OPDS catalog
services/
  ├─ graded-reader/         service — writes leveled Chinese books
  └─ pdf2epub/              service — converts PDFs into clean EPUBs
workspace/<slug>/           one folder per book/job (inputs → build/ outputs)
reference/                  device notes + SD-ready fonts for the X3
```

The split is by what's in the loop, not by what the thing does: services carry
a model and gate it; infrastructure is pure mechanics — the builder produces,
the server delivers, both deterministic.

The layout is agent-agnostic: `AGENTS.md` at the root is the entry point for
any coding agent, and `.claude/skills/` symlinks the four directories so
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
Pinyin annotation is an opt-in feature (`pinyin_mode` in book.json); generic
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

### opds-server — the built books, over WiFi *(contract-verified against the firmware source; not yet device-confirmed)*

Serves `workspace/*/build/*.epub` as an **OPDS 1.2** catalog the X3 browses and
downloads from directly — no SD card shuffling, and no library manager in
between. Build a book and it is on the reader by the next page turn. Stdlib
only, no dependencies.

Docs: [`opds-server/SKILL.md`](opds-server/SKILL.md)

```bash
python3 opds-server/scripts/serve_opds.py     # serve workspace/ on :8080
python3 opds-server/scripts/library.py        # what would be served
python3 opds-server/scripts/selftest.py       # the gate
```

Then on the device: Settings → System → OPDS Servers → Add Server, and enter the
URL the server prints. Local config and the optional Basic-auth password live in
gitignored `config.json` / `secrets/` (copy `config.example.json`); with no
config at all it serves the builder's output folder, open on the LAN.

Its design is dictated by what the firmware's client *actually* parses, read
from source rather than docs: an entry with no title or unresolvable href is
dropped **silently**, an acquisition link needs `type` exactly
`application/epub+zip`, `rel="search"` must carry the `{searchTerms}` template
inline (the conventional OpenSearch descriptor is never fetched), relative hrefs
are appended rather than resolved, and self-signed HTTPS cannot connect at all.
The table lives in [`reference/readers.md`](reference/readers.md).

So the gate here isn't "is this valid OPDS?" — a valid feed can lose books on
this device. `selftest.py` serves a real library and walks it through a port of
**the firmware's own client**, checking that every book is reachable, that
pagination loses nothing, and that a download arrives byte-identical and still
passes the shared `verify_epub.py`.

## Tasks

### graded-reader — generate Chinese graded readers *(working)*

Write leveled Chinese books chapter-by-chapter: a *planner* outlines, a
*scribe* drafts each chapter against a mechanically-built vocabulary brief,
deterministic validation gates the HSK level (out-of-list ≤ 5%, stretch
≤ 15%), a *glossary editor* prunes the harvested glossary, and the builder
emits a pinyin-annotated EPUB (`gloss-pinyin` on the X3: plain hanzi with
word-level pinyin on each glossary word's first appearance).

Docs: [`services/graded-reader/SKILL.md`](services/graded-reader/SKILL.md)

```bash
python3 -m venv .venv
.venv/bin/pip install -r services/graded-reader/requirements.txt
S=services/graded-reader/scripts

.venv/bin/python $S/validate.py workspace/twelve-zodiac       # grade a book
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
is the committed samples under `workspace/` — public-domain Spanish plays
(`alcaldes-encontrados`, `gurruminos`, the 3-act `el-espanol-de-oran`), each
with its `policy.json`/`draft.json` and a built, verified EPUB — annotated,
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
                        working) + EBGaramond (Latin)
```

Device facts that shape every tool (details in `reference/readers.md`):
embedded EPUB fonts are useless (the reader rasterizes only pre-converted
`.cpfont` bitmaps), ruby and interlinear pinyin are confirmed broken (use the
`gloss-*` modes), RAM is ~400 KB — keep books lean and CSS simple.

## License

The code and documentation are **MIT** licensed — see [`LICENSE`](LICENSE).

> Offered in the spirit of a shift already underway: as AI lets anyone
> generate software tailored to their own needs, the scarcity that
> intellectual property was built to protect is fading. Take it, adapt it,
> make it yours.

Two kinds of bundled third-party content keep their own terms:

- **Fonts** (`reference/fonts/*.cpfont`) are conversions of open-source fonts
  (LXGW WenKai, Zilla Slab, EB Garamond, Noto CJK) under the **SIL Open Font
  License 1.1** — notices, sources, and the license text in
  [`reference/fonts/ATTRIBUTION.md`](reference/fonts/ATTRIBUTION.md) and
  [`reference/fonts/OFL.txt`](reference/fonts/OFL.txt).
- **Sample book texts** under `workspace/` are either original to this project
  or public-domain source material (e.g. 西游记, 愚公移山, O. Henry's *The Gift
  of the Magi*, the 1793 entremés *Los alcaldes encontrados*), retold or
  converted as pipeline demonstrations.
