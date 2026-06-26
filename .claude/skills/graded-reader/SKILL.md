---
name: graded-reader
description: >-
  Generate Chinese graded readers chapter-by-chapter at a target HSK level.
  Use when the user wants to write, grade, or assemble leveled Chinese reading
  material: drafting chapters constrained to a vocabulary list, validating that
  a chapter stays within level, harvesting new words into per-chapter
  glossaries, or building a pinyin/ruby-annotated EPUB for an e-reader. Triggers
  include "graded reader", "HSK-level story", "分级读物", "leveled Chinese text",
  "pinyin EPUB", or pointing at a book directory with chapters/ and book.json.
---

# Chinese Graded Reader Pipeline

Generate Chinese graded readers one chapter at a time, validate each chapter's
vocabulary against a leveled list, harvest new words, and assemble a
pinyin-annotated EPUB. Generating chapter-by-chapter is deliberate: level drifts
upward over a whole book, so never generate a full book in one pass.

## Components

All scripts live in `scripts/` and import the shared `vocab.py`. Run them with a
Python that has `jieba` and `pypinyin` installed (see Setup).

- **`lists/`** — the source of truth, four TSVs sharing columns `word, level, pinyin, gloss`:
  - `hsk.tsv` — base leveled list (faithful copy of the HSK source; don't edit by hand).
  - `supplement.tsv` — high-frequency function/grammar words the word-based HSK
    list omits but any reader at level knows (pronoun plurals, demonstratives,
    measure combos, directional complements, conjunctions). Auditable layer.
  - `chengyu.tsv` — idioms / fixed expressions kept as whole tokens (tier b).
  - `personal.tsv` — personal known-words overlay **and** the sink for the
    add-and-gloss escalation (see the loop). Overrides others on conflict.
- **`scripts/vocab.py`** — loads + merges the lists, configures jieba so
  segmentation boundaries match the list (critical — otherwise the fail-rate
  lies), derives the known-character set, exposes pinyin lookup.
- **`scripts/validate.py`** — segments a chapter and runs the cascade; reports
  out-of-list rate per token; harvests stretch words. Exit 0 = pass, 1 = fail.
- **`scripts/build_epub.py`** — hand-built EPUB with selectable pinyin display
  (`ruby` / `interlinear` / `plain`) and per-chapter glossary. No epub library.

## The validation cascade

Each Han-containing token is classified:

| tier | rule | outcome |
|------|------|---------|
| a | token is a list word | **known** |
| b | token is a chengyu/expression | **known** (in list) |
| — | single character met in any known word | **known** |
| c | multi-char word, not listed, every char known | **stretch** (keep + gloss once) |
| d | otherwise (contains an unknown character) | **flagged** (out of list) |

Two gates, both per segmented token (not per character):
`out_of_list_rate = flagged / counted` (default cap **5%**) and
`stretch_rate = stretch / counted` (default cap **15%**). Either over its cap fails.

## The orchestration loop

For each chapter `N`:

1. **Plan.** Read `plan.json`: outline, target level, and the `introduced` set
   (words/characters already glossed). Pick chapter N's beat from the outline.
2. **Generate a chunk.** Write chapter N in the target level. Base it on
   well-known source material (西游记 episodes, fables, fairy tales) so effort
   goes into grading, not plot. Expect culturally loaded passages to pull toward
   classical/canonical vocab that fights the cap — those rework more.
3. **Validate.**
   ```
   python scripts/validate.py BOOK/chapters/chNN.md \
       --harvest-out BOOK/build/chNN-newwords.tsv --json
   ```
4. **Rework OR accept.**
   - **Pass** → accept.
   - **Fail** → look at `flagged_tokens` (worst first). Rewrite to avoid them,
     keeping the story. Re-validate. **Cap reworks at `rework_cap` (default 3).**
   - **After the cap**, if the same word keeps recurring because the topic
     genuinely demands it, do **not** keep rewriting: take the **add-and-gloss**
     path — add that word to `lists/personal.tsv` (with pinyin + gloss) and to
     `plan.json` → `introduced.add_and_gloss`. It is now known for all later
     chapters and gets glossed once here. This is the correct escape hatch, not
     a failure.
5. **Harvest vocabulary.** The harvest TSV from step 3 holds this chapter's
   stretch (and, with `--harvest-flagged`, flagged) words. Fill any blank
   glosses, drop words already in `introduced`, and save as the chapter glossary
   `BOOK/build/chNN-glossary.tsv`. Only first appearances are glossed.
6. **Update state.** Append newly introduced words to `plan.json` → `introduced.words`.
7. **Next chapter.** Repeat. Re-validation in later chapters uses the updated
   lists, so add-and-gloss words no longer flag.
8. **Assemble EPUB** (after chapters are accepted):
   ```
   python scripts/build_epub.py BOOK --out BOOK/build/book.epub --pinyin-mode interlinear
   ```

## Gates that are NOT automated — do these by hand

- **The validator checks vocabulary only, never quality.** Before trusting the
  loop to run unattended, a human must read **chapter 1** end to end: does it
  read naturally, is the story coherent, is the grading not just legal but
  *pleasant*? Do not batch-generate until chapter 1 passes this human QA gate.
- **Pinyin display depends on the target device.** Ruby (`<ruby>`) is compact
  and preferred, but a given e-reader may not support it (it can leak the `<rt>`
  text inline). Before scaling, build the diagnostic EPUB and confirm on the
  actual device:
  ```
  python scripts/build_epub.py BOOK --out BOOK/build/render-test.epub --diagnostic
  ```
  It renders chapter 1 three ways (ruby / interlinear / plain) on labeled pages.
  Sideload once, see which looks right, set `pinyin_mode` in `book.json`
  accordingly. `interlinear` is the safe fallback — CSS stacking, no ruby tag,
  renders anywhere. See `reference/readers.md` for the Xteink X3 / CrossPoint
  situation.

## Book layout

```
BOOK/
  book.json            {title, author, language, pinyin_mode, chapters:[{source, glossary}]}
  plan.json            outline + introduced set + validation params
  chapters/chNN.md     chapter source (# title, ## section, paragraphs)
  build/               harvest TSVs, glossaries, .epub output
```

`workspace/journey-west/` is a worked example: a validated HSK 1-3 chapter 1
with glossary, plan, and built EPUBs (including the diagnostic).

## Build order (when starting a new reader)

1. Scaffold against a small list and ONE chapter (already done for HSK 1-3).
2. Get validate + build_epub green on that one chapter end to end.
3. Confirm ruby/pinyin renders on the target reader via the diagnostic EPUB.
4. Only then run the full loop for the remaining chapters.

## Setup

```
python -m venv .venv && .venv/bin/pip install jieba pypinyin
```
On Debian/Ubuntu, install jieba inside a venv — the system setuptools breaks
jieba's legacy `setup.py` (`install_layout`). Run the scripts with `.venv/bin/python`.

## Adjusting level

Change which bands count as "known" by editing `lists/`. For a tighter cap, trim
`hsk.tsv`/`supplement.tsv`; for a higher target, append more bands. The pipeline
is level-agnostic — it grades against whatever the lists contain.
