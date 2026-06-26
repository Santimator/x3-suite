# graded-reader

Generate Chinese **graded readers** chapter-by-chapter: draft a chapter
constrained to a target HSK level, validate its vocabulary against a leveled
list, harvest new words into a per-chapter glossary, and assemble a
pinyin-annotated EPUB ready for a Calibre-Web-Automated ingest folder and an
e-ink reader.

The whole pipeline is packaged as a Claude Code skill at
[`.claude/skills/graded-reader/`](.claude/skills/graded-reader/SKILL.md) — that
`SKILL.md` is the orchestrator and the canonical documentation. This README is a
quick map.

## Why chapter-by-chapter

Level drifts upward over a whole book, so the generator never writes a full book
in one pass. Each chapter is graded, reworked if needed, and only then accepted.

## Layout

```
.claude/skills/graded-reader/
  SKILL.md              orchestration loop + full docs
  requirements.txt      jieba, pypinyin
  lists/
    hsk.tsv             base HSK 1-3 list (word, level, pinyin, gloss)
    supplement.tsv      high-freq function/grammar words the word list omits
    chengyu.tsv         idioms / fixed expressions
    personal.tsv        personal known-words + add-and-gloss escalation sink
  scripts/
    vocab.py            list loader + jieba configuration (shared)
    gen_context.py      builds the scribe's writing brief for a chapter
    validate.py         cascade validator + glossary harvester
    update_state.py     deterministic post-accept bookkeeping
    build_epub.py       hand-built EPUB, selectable pinyin display
  prompts/
    planner.md          planner role: source + level -> plan.json outline
    scribe.md           scribe role: write one chapter under constraints
  reference/
    readers.md          Xteink X3 / CrossPoint ruby-support notes
workspace/
  journey-west/         worked example: validated HSK 1-3 chapter 1 + EPUBs
```

## Quick start

```bash
python -m venv .venv
.venv/bin/pip install -r .claude/skills/graded-reader/requirements.txt
cd .claude/skills/graded-reader

# validate a chapter against the HSK 1-3 lists
.venv/bin/python scripts/validate.py ../../../workspace/journey-west/chapters/ch01.md

# build the EPUB (interlinear pinyin), and a 3-mode render test for the device
.venv/bin/python scripts/build_epub.py ../../../workspace/journey-west \
    --out ../../../workspace/journey-west/build/book.epub --pinyin-mode interlinear
.venv/bin/python scripts/build_epub.py ../../../workspace/journey-west \
    --out ../../../workspace/journey-west/build/render-test.epub --diagnostic
```

> On Debian/Ubuntu, install `jieba` inside a venv — the system setuptools breaks
> jieba's legacy `setup.py`.

## Key design points

- **Two LLM roles, deterministic glue.** A *planner* writes the outline once; a
  *scribe* writes each chapter from a mechanically-built brief; scripts grade,
  track state, and assemble. The model invents structure and prose; the scripts
  never invent — they segment, count, gloss, and record.
- **Writing is guided, not just checked.** `gen_context.py` front-loads the
  permitted vocabulary (grouped by band) into the scribe's brief, so it reaches
  for in-list words while writing instead of being reworked afterward.
- **Two-gate validation per segmented token** (not per character): out-of-list
  rate ≤ 5%, stretch rate ≤ 15% (both tunable).
- **jieba with the vocab list as its custom dictionary**, so segmentation
  boundaries match the list — otherwise the fail-rate is meaningless.
- **A character is "known" if it appears in any known word.** The official HSK
  list is word-based and omits standalone characters it already uses (e.g. `说话`
  but not `说`); crediting those keeps the rate honest.
- **Rework is capped** (default 3). When a topic genuinely demands an above-level
  word, the loop adds it to `personal.tsv` and glosses it once rather than
  rewriting forever.
- **Pinyin display is a parameter** (`ruby` / `interlinear` / `plain`) because
  the target reader's ruby support is unconfirmed — see `reference/readers.md`.
- **Two manual gates:** a human reads chapter 1 for quality before unattended
  runs, and confirms pinyin rendering on the device via the diagnostic EPUB.
