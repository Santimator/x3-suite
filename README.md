# X3 suite

Tools that produce EPUBs for an e-ink reader — concretely an **Xteink X3**
running CrossPoint firmware, fed through a Calibre-Web-Automated ingest
folder. One mindset across all of them:

> **LLM roles for judgment, deterministic scripts for mechanics, and a
> deterministic gate after every LLM step.** Models handle what varies
> (writing prose, restoring mangled text, inferring structure); scripts
> measure, validate, track state, and assemble — they never invent. Trust
> comes from the gates, not the model.

The suite splits into **tasks** and **infrastructure**, each a Claude Code
skill under `.claude/skills/` whose `SKILL.md` is its canonical
documentation. Tasks (graded-reader, pdf2epub) own their steps and tools;
they all converge on the **common book format** — `chapters/*.md +
book.json` in a `workspace/<slug>/` folder — which the shared
**epub-builder** consumes.

## Infrastructure

### epub-builder — the shared EPUB builder

One builder for every task, hand-built XHTML/OPF, deterministic output
(same source → byte-identical EPUB). Its input contract — what tasks are
allowed to hand it — is
[`.claude/skills/epub-builder/FORMAT.md`](.claude/skills/epub-builder/FORMAT.md).
Pinyin annotation is an opt-in feature (`pinyin_mode` in book.json); generic
books build with zero CJK dependencies.

```bash
.venv/bin/python .claude/skills/epub-builder/scripts/build_epub.py \
    workspace/<slug> --out workspace/<slug>/build/<slug>.epub
```

## Tasks

### graded-reader — generate Chinese graded readers *(working)*

Write leveled Chinese books chapter-by-chapter: a *planner* outlines, a
*scribe* drafts each chapter against a mechanically-built vocabulary brief,
deterministic validation gates the HSK level (out-of-list ≤ 5%, stretch
≤ 15%), a *glossary editor* prunes the harvested glossary, and the builder
emits a pinyin-annotated EPUB (`ruby` / `interlinear` / `plain`).

Docs: [`.claude/skills/graded-reader/SKILL.md`](.claude/skills/graded-reader/SKILL.md)

```bash
python3 -m venv .venv
.venv/bin/pip install -r .claude/skills/graded-reader/requirements.txt
S=.claude/skills/graded-reader/scripts

.venv/bin/python $S/validate.py workspace/twelve-zodiac       # grade a book
.venv/bin/python $S/selftest.py                               # full pipeline check
# EPUB assembly: the shared epub-builder (see Infrastructure above)
```

Two drivers: Claude Code interactively, or the headless `run_book.py` against
any OpenAI-compatible endpoint. On Debian/Ubuntu install `jieba` inside a
venv — system setuptools breaks its legacy `setup.py`.

### pdf2epub — convert PDFs into clean EPUBs *(stages 0-2, 4-6 done: triage, extract, restore, prepare, build, verify; selftest + real conversion remain)*

PDFs are page descriptions (where ink goes); EPUB is a document (what the
text is). The pipeline recovers intent from ink, **deterministic-first,
agent-on-error**: triage characterizes the source, scripts extract and
restore the text on the happy path, and the agent orchestrates and
verifies — confirming routes, diagnosing failures, and emitting decisions
(policy switches, chapter anchors) that scripts apply. The model never bulk-
generates: every byte in the EPUB traces back to the extraction.

Docs: [`.claude/skills/pdf2epub/SKILL.md`](.claude/skills/pdf2epub/SKILL.md) ·
design + open questions: [`DESIGN.md`](.claude/skills/pdf2epub/DESIGN.md) ·
implementation plan for the remaining stages:
[`BUILD_INSTRUCTIONS.md`](BUILD_INSTRUCTIONS.md)

```bash
.venv/bin/pip install -r .claude/skills/pdf2epub/requirements.txt
.venv/bin/python .claude/skills/pdf2epub/scripts/triage.py \
    workspace/goya-sueno/source.pdf --out workspace/goya-sueno/build/triage.json
```

Test fixture: `workspace/goya-sueno/` — an 18-page Spanish theatre text with
a deliberately pathological text layer (fake-bold double-drawn glyphs).

## Shared ground

```
reference/readers.md    Xteink X3 / CrossPoint device notes: fonts, CJK saga,
                        ruby support, SD-card font layout — read before
                        touching anything device-facing
workspace/<slug>/       one folder per book/job (source, chapters/, book.json,
                        build/ outputs)
workspace/CHARSET/      exact-charset font subsetting output + prebuilt
                        .cpfont families for the device (see its README)
```

Device facts that shape every tool (details in `reference/readers.md`):
embedded EPUB fonts are useless (the reader rasterizes only pre-converted
`.cpfont` bitmaps), ruby annotation support is unconfirmed, RAM is ~400 KB —
keep books lean and CSS simple.
