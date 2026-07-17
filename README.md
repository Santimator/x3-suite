# X3 suite

Tools that produce EPUBs for an e-ink reader — concretely an **Xteink X3**
running CrossPoint firmware, fed through a Calibre-Web-Automated ingest
folder. One mindset across all of them:

> **LLM roles for judgment, deterministic scripts for mechanics, and a
> deterministic gate after every LLM step.** Models handle what varies
> (writing prose, restoring mangled text, inferring structure); scripts
> measure, validate, track state, and assemble — they never invent. Trust
> comes from the gates, not the model.

Each tool is a Claude Code skill under `.claude/skills/`; its `SKILL.md` is
the orchestrator and canonical documentation. Tools converge on a **common
book format** — `chapters/*.md + book.json` in a `workspace/<slug>/` folder —
so EPUB building, font subsetting, and device knowledge are shared.

## Tools

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
.venv/bin/python $S/build_epub.py workspace/twelve-zodiac \
    --out workspace/twelve-zodiac/build/book.epub --pinyin-mode ruby
.venv/bin/python $S/selftest.py                               # full pipeline check
```

Two drivers: Claude Code interactively, or the headless `run_book.py` against
any OpenAI-compatible endpoint. On Debian/Ubuntu install `jieba` inside a
venv — system setuptools breaks its legacy `setup.py`.

### pdf2epub — convert PDFs into clean EPUBs *(stage 0 done, design open)*

PDFs are page descriptions (where ink goes); EPUB is a document (what the
text is). The pipeline recovers intent from ink, **deterministic-first,
agent-on-error**: triage characterizes the source, scripts extract and
restore the text on the happy path, and the agent orchestrates and
verifies — confirming routes, diagnosing failures, and emitting decisions
(policy switches, chapter anchors) that scripts apply. The model never bulk-
generates: every byte in the EPUB traces back to the extraction.

Docs: [`.claude/skills/pdf2epub/SKILL.md`](.claude/skills/pdf2epub/SKILL.md) ·
design + open questions: [`DESIGN.md`](.claude/skills/pdf2epub/DESIGN.md)

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
