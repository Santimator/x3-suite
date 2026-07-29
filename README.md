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

## Before you trust any of this

This is vibe-coded software. Every line was written by an LLM at my direction,
and the "testing" is my own sparse use — one person, one device, the handful of
books I actually wanted to read. No CI, no test matrix, no second pair of eyes.

That isn't quite the same as untested, and the difference is the useful part:

- The **self-tests are real, and they gate real things** — vocabulary level,
  EPUB structural integrity, whether the reader's own OPDS client can see every
  book. Where something is machine-checked, it stays checked.
- They cover **mechanics, not taste**. Nothing verifies that a book is pleasant
  to read on the device. That part is me, squinting at an e-ink screen.
- Each tool carries its **own status marker** below — *working*,
  *contract-verified but not device-confirmed*. Those are meant literally, and
  they are the honest ones.

So: expect edges, and read any claim here as "worked for me on an X3" rather
than "verified". Issues and pull requests are welcome — very much including the
ones that just say this is wrong.

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

### opds-server — the built books, over WiFi *(device-confirmed, 2026-07)*

Serves `workspace/*/build/*.epub` as an **OPDS 1.2** catalog the X3 browses and
downloads from directly — no SD card shuffling, and no library manager in
between. Build a book and it is on the reader by the next page turn. Stdlib
only, no dependencies.

Docs: [`opds-server/SKILL.md`](opds-server/SKILL.md)

```bash
python3 opds-server/scripts/serve_opds.py     # serve workspace/ on :6737
python3 opds-server/scripts/library.py        # what would be served
python3 opds-server/scripts/selftest.py       # the gate
```

Then on the device: Settings → System → OPDS Servers → Add Server, and enter the
URL the server prints — **starting with `http://`**, since the reader only does
verified HTTPS and can't be given a self-signed certificate.

**You don't need a config file.** Every setting has a working default, and the
startup banner says `config (defaults)` when you're using them:

| | default | why |
|---|---|---|
| library roots | `workspace/` | the builder's output; resolved against the repo, not your shell's directory |
| excludes | `*-DIAGNOSTIC.epub` | the builder's render-test EPUBs aren't books |
| bind | `0.0.0.0:6737` | every interface, so the reader can reach it. 6737 is "OPDS" on a phone keypad — not 8080, which everything else wants |
| auth | off | open on your LAN, and the banner says so at startup |
| page size | 25 entries | a feed page the device holds comfortably |

For a one-off change, use a flag:

```bash
python3 opds-server/scripts/serve_opds.py --root ~/Books --port 8123
```

`--root DIR` (repeatable) serves somewhere else · `--port` (`0` picks any free
one and prints it) · `--host` · `--page-size` · `--config` for a config file
elsewhere.

To make a change permanent, copy `opds-server/config.example.json` to
`config.json` — it's gitignored, and every key is annotated in place. That's
also where you turn on Basic auth: set `auth.username`, put the password in
gitignored `secrets/`, and read [`secrets/README.md`](opds-server/secrets/README.md)
first — over plain HTTP it's cleartext, which is a real limit and not a
sloppy one.

To leave it running, `opds-server/opds-server.service` is a systemd unit — edit
two lines, `systemctl enable --now opds-server`, done; the commands and what
they do are in [`opds-server/helper-info.txt`](opds-server/helper-info.txt).
Note the unit runs the script with **no flags**, so under systemd anything
non-default belongs in `config.json`. It's written for Debian; on anything else,
hand the file to your favourite AI and ask for the equivalent.

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
                        build/ outputs). Gitignored apart from the committed
                        samples below — your books are yours, not the repo's
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

- **Fonts** are under the **SIL Open Font License 1.1**: the `.cpfont`
  families in `reference/fonts/` (conversions of LXGW WenKai, Zilla Slab, Noto
  CJK), and `reference/covers/IMFellEnglish-Regular.ttf`, which sets Latin
  cover titles. Notices, sources, and the license texts in
  [`reference/fonts/ATTRIBUTION.md`](reference/fonts/ATTRIBUTION.md),
  [`reference/fonts/OFL.txt`](reference/fonts/OFL.txt) and
  [`reference/covers/IMFellEnglish-OFL.txt`](reference/covers/IMFellEnglish-OFL.txt).
- **Sample book texts** under `workspace/` are either original to this project
  or public-domain source material — Oscar Wilde's *The Importance of Being
  Earnest*, retold in Chinese as a graded reader, and the 1793 entremés
  *Los alcaldes encontrados* — retold or converted as pipeline demonstrations.
