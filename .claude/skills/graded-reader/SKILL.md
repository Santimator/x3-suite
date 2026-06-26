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
- **`scripts/gen_context.py`** — builds the scribe's writing brief for chapter N
  (beat + story-so-far + constraints + introduced set + topic words + the
  permitted vocabulary grouped by band). This is what makes writing *guided*
  rather than write-then-rework.
- **`scripts/validate.py`** — segments a chapter and runs the cascade; reports
  out-of-list rate per token; harvests stretch words. Exit 0 = pass, 1 = fail.
- **`scripts/update_state.py`** — deterministic bookkeeping after a chapter is
  accepted: writes the gloss-once chapter glossary, appends newly-glossed words
  to `introduced`, files the recap, marks the outline entry, wires `book.json`.
- **`scripts/build_epub.py`** — hand-built EPUB with selectable pinyin display
  (`ruby` / `interlinear` / `plain`) and per-chapter glossary; glossed words in
  the text link to their glossary entry (and back). No epub library.
- **`scripts/llm.py`** — minimal OpenAI-compatible chat client (stdlib only).
  The swappable model seam for the headless runner. Reads `config.json`.
- **`scripts/run_book.py`** — headless runner: drives the whole loop by calling
  an OpenAI-compatible endpoint for the model steps (see "Two drivers").
- **`prompts/planner.md`, `prompts/scribe.md`, `prompts/glossary_editor.md`** —
  the three LLM-role briefs.

## Three roles + deterministic scripts

The pipeline is one model wearing three hats, with scripts doing everything
mechanical between them:

- **Planner (LLM, once per book)** — turns source material + level into
  `plan.json` (outline with per-chapter beats). See `prompts/planner.md`.
- **Scribe (LLM, once per chapter)** — writes chapter N from the brief
  `gen_context.py` produces (and reworks it from flagged tokens on a fail).
  See `prompts/scribe.md`.
- **Glossary editor (LLM, once per chapter)** — `update_state.py` proposes every
  gloss-worthy first appearance (over-inclusive on purpose); the model prunes
  compositionally-transparent rows, keeps topic words, and fills blank glosses.
  See `prompts/glossary_editor.md`.
- **Deterministic scripts** — `gen_context.py` (brief), `validate.py` (grade),
  `update_state.py` (track used words + what happened + propose glossary),
  `build_epub.py` (assemble). No LLM judgement.

The planner invents structure; the scribe invents prose; the glossary editor
applies pedagogical judgement; the scripts never invent anything — they segment,
count, gloss, and record.

## Two drivers (the model seam is file-based)

The LLM steps read and write files (brief in → chapter out; raw glossary in →
curated glossary out), so *who fills them* is pluggable. Two ways to drive the
exact same scripts:

- **Claude Code drives** (interactive, rides your Claude subscription) — Claude
  Code follows the orchestration loop below itself: runs the scripts as Bash
  tools, writes each chapter and curates each glossary inline. No `config.json`
  needed. Best for chapter 1 and the human-QA gate.
- **`run_book.py` drives** (headless, any model) — calls an OpenAI-compatible
  endpoint for the model steps. Default is NVIDIA NIM (free tier) with Kimi K2
  (most natural Chinese prose; one-line swap to Qwen / a local Ollama or vLLM
  server). By default it runs the **whole book in one go** — readers are cheap
  to regenerate, so the convenience wins. Setup:
  ```
  cp config.example.json config.json            # then set model / base_url
  printf '%s' 'nvapi-...' > secrets/nim.key      # gitignored
  python scripts/run_book.py BOOK                # whole book, unattended
  python scripts/run_book.py BOOK --pause-after 1  # opt-in QA stop after ch1
  python scripts/run_book.py BOOK --from 3       # resume from chapter 3
  ```
  `config.json` and `secrets/` are gitignored. The runner stops (doesn't guess)
  if a chapter still fails after the rework cap — add-and-gloss stays a
  human/Claude-Code judgement call.

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

**Once per book — Planner.** Produce `plan.json` per `prompts/planner.md`
(outline + per-chapter beats; seed obvious story names into `lists/personal.tsv`).

**Then, for each chapter `N`:**

1. **Build the scribe brief** (deterministic):
   ```
   python scripts/gen_context.py BOOK --chapter N --out BOOK/build/chNN-brief.md
   ```
   It pulls the beat, the story-so-far recaps, the `introduced` set, the topic
   words, and the permitted vocabulary grouped by band.
2. **Write the chunk — Scribe.** Following the brief and `prompts/scribe.md`,
   write `BOOK/chapters/chNN.md` (a `# 第N章` title, paragraphs, then a final
   `RECAP:` line). Stay inside the permitted vocabulary; prefer the lowest band;
   reuse words. Base it on well-known source material so effort goes into
   grading, not plot. Expect culturally loaded passages to rework more.
3. **Validate** (deterministic):
   ```
   python scripts/validate.py BOOK/chapters/chNN.md --json
   ```
4. **Rework OR accept.**
   - **Pass** → accept.
   - **Fail** → take `flagged_tokens` (worst first) and rewrite to avoid them,
     keeping the story. Re-validate. **Cap reworks at `rework_cap` (default 3).**
   - **After the cap**, if a word keeps recurring because the topic genuinely
     demands it, do **not** keep rewriting: take the **add-and-gloss** path — add
     it to `lists/personal.tsv` (pinyin + gloss) and to `plan.json` →
     `introduced.add_and_gloss`. It becomes known for all later chapters and is
     glossed once here. (A content word that recurs many times but squeaks under
     the threshold is also an add-and-gloss candidate, not a pass to ignore.)
5. **Update state** (deterministic — does the harvest, glossary, and tracking):
   ```
   python scripts/update_state.py BOOK --chapter N
   ```
   It writes `BOOK/build/chNN-glossary.tsv` (gloss-worthy first appearances:
   topic words + compositional stretch, minus anything already in `introduced`),
   appends them to `introduced.words`, files the `RECAP:` line into the outline
   (and strips it from the chapter), and wires `book.json`.
6. **Curate the glossary — Glossary editor (LLM).** The proposed TSV is
   over-inclusive on purpose. Per `prompts/glossary_editor.md`, prune
   compositionally-transparent rows (山上, 很多, 一天…), keep topic words, and
   fill any blank gloss. This is the model's judgement call, not a human's — the
   `run_book.py` driver does it automatically; Claude Code does it inline.
7. **Next chapter.** Repeat. Later chapters re-segment against the updated lists,
   so add-and-gloss words no longer flag and introduced words aren't re-glossed.
8. **Assemble EPUB** (after chapters are accepted):
   ```
   python scripts/build_epub.py BOOK --out BOOK/build/book.epub --pinyin-mode interlinear
   ```

## Gates that are NOT automated

- **The validator checks vocabulary only, never quality.** The QA read is
  *optional and off by default* — readers are cheap to regenerate, so the
  default is to run the whole book unattended and just reread/regenerate if a
  result disappoints. When you do want the safety check (a new source or level
  you haven't tried), opt in with `--pause-after 1`: the runner stops after
  chapter 1 so a human can confirm it reads naturally and the grading is
  *pleasant*, not merely legal, before the rest of the book generates.
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
