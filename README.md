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
  config.example.json   LLM runner config template (copy to config.json)
  lists/
    hsk.tsv             base leveled list, HSK 1-4 (word, level, pinyin, gloss)
    supplement.tsv      high-freq function/grammar words the word list omits
    chengyu.tsv         idioms / fixed expressions
    personal.tsv        personal known-words + add-and-gloss escalation sink
  scripts/
    vocab.py            list loader + jieba configuration (shared)
    gen_context.py      builds the scribe's writing brief for a chapter
    validate.py         cascade validator + glossary harvester
    update_state.py     deterministic post-accept bookkeeping
    build_epub.py       hand-built EPUB, selectable pinyin display
    llm.py              OpenAI-compatible chat client (the model seam)
    run_book.py         headless runner: drives the loop via any OpenAI API
    selftest.py         pipeline self-test (cascade, books, epub integrity)
  prompts/
    planner.md          planner role: source + level -> plan.json outline
    scribe.md           scribe role: write one chapter under constraints
    glossary_editor.md  glossary-editor role: prune + fill the chapter glossary
  reference/
    readers.md          Xteink X3 / CrossPoint ruby-support notes
workspace/
  journey-west/         first scaffold example (HSK 1-3, 2 chapters)
  yugong-mountain/      愚公移山 (HSK 1-3, 5 chapters) + built EPUB
  twelve-zodiac/        十二生肖 (HSK 1-4, 10 chapters) + built EPUB
  letter-writer/        写信的老人, original story (HSK 1-4, 7 ch) + built EPUB
```

## Quick start

```bash
python -m venv .venv
.venv/bin/pip install -r .claude/skills/graded-reader/requirements.txt
S=.claude/skills/graded-reader/scripts

# validate one chapter, or a whole book at once (gates come from its plan.json)
.venv/bin/python $S/validate.py workspace/twelve-zodiac/chapters/ch01.md
.venv/bin/python $S/validate.py workspace/twelve-zodiac

# build the EPUB (ruby pinyin), or a 3-mode render test for a new device
.venv/bin/python $S/build_epub.py workspace/twelve-zodiac \
    --out workspace/twelve-zodiac/build/book.epub --pinyin-mode ruby
.venv/bin/python $S/build_epub.py workspace/twelve-zodiac \
    --out workspace/twelve-zodiac/build/render-test.epub --diagnostic

# verify the whole pipeline after changing scripts or lists
.venv/bin/python $S/selftest.py
```

> On Debian/Ubuntu, install `jieba` inside a venv — the system setuptools breaks
> jieba's legacy `setup.py`.

## Key design points

- **Three LLM roles, deterministic glue.** A *planner* writes the outline once;
  a *scribe* writes (and reworks) each chapter from a mechanically-built brief;
  a *glossary editor* prunes the auto-proposed per-chapter glossary to the rows
  a learner actually needs. Scripts grade, track state, and assemble — they
  never invent, they segment, count, gloss, and record.
- **Two interchangeable drivers.** The model steps are a file-based seam, so the
  same scripts run either way: **Claude Code** drives the loop interactively on
  your subscription, or the headless **`run_book.py`** runner drives it via any
  OpenAI-compatible endpoint (NVIDIA NIM free tier with Kimi K2 by default; swap
  to Qwen/local Ollama in one config line). By default the runner generates the
  whole book in one go; `--pause-after 1` opts into a human QA stop after
  chapter 1. See `config.example.json`.
- **Writing is guided, not just checked.** `gen_context.py` front-loads the
  permitted vocabulary (grouped by band) into the scribe's brief, so it reaches
  for in-list words while writing instead of being reworked afterward.
- **Two-gate validation per segmented token** (not per character): out-of-list
  rate ≤ 5%, stretch rate ≤ 15% (both tunable, read from the book's plan.json).
- **jieba with the vocab list as its custom dictionary**, so segmentation
  boundaries match the list — otherwise the fail-rate is meaningless.
- **A character is "known" if it appears in any known word.** The official HSK
  list is word-based and omits standalone characters it already uses (e.g. `说话`
  but not `说`); crediting those keeps the rate honest.
- **Number grammar and known-word compounds are recognition, not stretch.**
  jieba merges 第五名, 十二个, 很快 into single tokens; the validator classifies
  them *composed* (in list) so the stretch budget only pays for genuine reaches
  like 山上 or 睡着 — the ones worth glossing.
- **Rework is capped** (default 3). When a topic genuinely demands an above-level
  word, the loop adds it to `personal.tsv` and glosses it once rather than
  rewriting forever.
- **Pinyin display is a parameter** (`ruby` / `interlinear` / `plain`) because
  the target reader's ruby support is unconfirmed — see `reference/readers.md`.
- **Two manual gates:** a human reads chapter 1 for quality before unattended
  runs, and confirms pinyin rendering on the device via the diagnostic EPUB.
