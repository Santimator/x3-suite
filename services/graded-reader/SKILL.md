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
  - `hsk.tsv` — base leveled list, **generated** from the official **HSK 3.1**
    syllabus (the 2025 revision of HSK 3.0, in force 2026) + CC-CEDICT glosses.
    Never edit by hand — re-run `scripts/build_hsk_list.py` (see its docstring
    for sources and the band sizes: 300 / 500 / 1,000 / 2,000 / 3,600 / 5,400
    cumulative).
  - `supplement.tsv` — high-frequency function/grammar words the word-based HSK
    list omits but any reader at level knows (pronoun plurals, demonstratives,
    measure combos, directional complements, conjunctions). Auditable layer.
  - `chengyu.tsv` — the 410 chengyu of the HSK 3.1 syllabus, tagged with their
    real bands. **393 sit at HSK 7-9**: true 成语 are advanced vocabulary, so a
    low-level book's cap correctly hides them.
  - `expressions.tsv` — the everyday constructions that actually make graded
    prose sound Chinese, level-tagged: **patterns** with a regex (一…就…,
    虽然…但是…, 太…了) that the scribe wraps its own words inside, and **set
    phrases** (不好意思, 想办法) matched literally. The scribe must use a minimum
    number of distinct ones per chapter — see the gates below.
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
- **EPUB assembly** — done by the suite-shared builder skill
  (`epub-builder/`): hand-built EPUB with selectable pinyin
  display (five modes; `gloss-pinyin` is the X3 default — see the builder's
  FORMAT.md) and per-chapter glossary;
  glossed words in the text link to their glossary entry (and back).
  Annotation engages when `book.json` has `pinyin_mode`; the input contract
  is the builder's `FORMAT.md`.
- **`headless/`** — *optional* alternative driver, kept out of the core so the
  skill proper is just the briefing + deterministic tools. `run_book.py` drives
  the whole loop against any OpenAI-compatible endpoint (for running without
  Claude Code); `llm.py` is its stdlib model seam; `config.example.json` +
  `secrets/` configure it. Ignore it entirely when Claude Code is the driver.
- **`scripts/selftest.py`** — pipeline self-test: cascade examples, every
  workspace book against its gates, epub build + link integrity. Run it after
  changing scripts or lists. (Device fonts are prebuilt in `reference/fonts/`;
  charset-subsetting was retired — see readers.md for why sparse fonts fail.)
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
- **`headless/run_book.py` drives** (headless, any model) — the optional
  driver in `headless/`, for running without Claude Code. Calls an
  OpenAI-compatible endpoint for the model steps. Default is NVIDIA NIM (free
  tier) with Kimi K2 (most natural Chinese prose; one-line swap to Qwen / a
  local Ollama or vLLM server). By default it runs the **whole book in one
  go** — readers are cheap to regenerate, so the convenience wins. Config
  lives in `headless/` (found by absolute path, so run from the repo root):
  ```
  H=services/graded-reader/headless
  cp $H/config.example.json $H/config.json      # then set model / base_url
  printf '%s' 'nvapi-...' > $H/secrets/nim.key   # gitignored
  .venv/bin/python $H/run_book.py workspace/BOOK                 # whole book, unattended
  .venv/bin/python $H/run_book.py workspace/BOOK --pause-after 1 # opt-in QA stop after ch1
  .venv/bin/python $H/run_book.py workspace/BOOK --from 3        # resume from chapter 3
  ```
  `headless/config.json` and `headless/secrets/` are gitignored. The runner
  stops (doesn't guess) if a chapter still fails after the rework cap —
  add-and-gloss stays a human/Claude-Code judgement call.

## The validation cascade

Each Han-containing token is classified:

| tier | rule | outcome |
|------|------|---------|
| a | token is a list word | **known** |
| b | token is a chengyu/expression | **known** (in list) |
| c | single character met in any known word | **known** |
| d | number grammar (第五名, 十二个) or a concatenation of list words (很快, 只能) | **composed** (in list) |
| e | multi-char word, not listed, every char known | **stretch** (keep + gloss once) |
| f | otherwise (contains an unknown character) | **flagged** (out of list) |

Tier (d) exists because jieba merges frequent collocations into single tokens.
Without it, ordinals and known-word compounds eat the stretch budget and the
rate stops measuring genuine reach — a learner who knows 很 and 快 *recognizes*
很快; only combinations like 山上 or 睡着 are real (gloss-worthy) stretches.

**Five gates.** The rate gates are per segmented token (not per character);
`out_of_list_rate = flagged / counted`, `stretch_rate = stretch / counted`:

| gate | default | why |
|---|---|---|
| `threshold` — max out-of-list | 5% | above it the text stops being readable at level |
| `min_out_of_list` — **min** out-of-list | 0 (books opt in, ~1.5%) | **a floor, not a typo.** Text that is 100% in-list is too easy to learn from; a little new vocabulary in context is where acquisition happens (i+1). A chapter at 0% fails as *too easy*. |
| `max_stretch` | 15% | compositional guesses shouldn't carry the text |
| `min_chars` | 0 (books opt in, ~800) | a chapter must be a real episode |
| `min_expressions` | 0 (books opt in, ~5) | distinct `expressions.tsv` constructions used |

The floors default to 0 so older books keep passing; new books set them in
`plan.json` → `validation`. They exist because **only what a script measures
actually happens**: length and expression targets lived in the prompts for a
long time and were quietly missed every single chapter, while the script-checked
vocabulary gates were met 100% of the time. Gates default to the book's `plan.json` validation params; CLI flags
override. `validate.py BOOKDIR` checks every chapter in `book.json` at once.

## The orchestration loop

**Once per book — Planner.** Produce `plan.json` per `prompts/planner.md`:
**research the source first** (don't plan from memory), write a **story bible**
(cast, relationships, setting, motifs, and the full event chain), and only then
divide that chain into chapters with a length budget. Seed obvious story names
into `lists/personal.tsv`.
Aim for a **substantial book**: follow the source story's events across enough
chapters (roughly 8–12 for a short tale, more for a longer source) and make each
a meaty episode (~450+ chars), not a summary — see planner.md's "Make the book
substantial".

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
   python ../../epub-builder/scripts/build_epub.py BOOK --out BOOK/build/book.epub
   ```
9. **Verify the EPUB** (deterministic, shared with pdf2epub): confirm the
   output is a structurally sound EPUB — mimetype first/stored, manifest ⇄
   zip parity, well-formed XHTML/OPF, and every glossary link/fragment
   resolves.
   ```
   python ../../epub-builder/scripts/verify_epub.py BOOK/build/book.epub
   ```

## Gates that are NOT automated

- **The validator checks vocabulary only, never quality.** The QA read is
  *optional and off by default* — readers are cheap to regenerate, so the
  default is to run the whole book unattended and just reread/regenerate if a
  result disappoints. When you do want the safety check (a new source or level
  you haven't tried), opt in with `--pause-after 1`: the runner stops after
  chapter 1 so a human can confirm it reads naturally and the grading is
  *pleasant*, not merely legal, before the rest of the book generates.
- **Pinyin display depends on the target device.** On the X3, ruby and
  interlinear are device-confirmed broken; ship `gloss-pinyin` (the books'
  default), `gloss-underline`, or `plain`. Ruby is kept for capable readers
  (phones). For a NEW device, settle it empirically with the diagnostic EPUB
  (chapter 1 rendered in all five modes on labeled pages), then set
  `pinyin_mode` in `book.json`:
  ```
  python ../../epub-builder/scripts/build_epub.py BOOK --out BOOK/build/render-test.epub --diagnostic
  ```
  Device notes: `reference/readers.md` at the repo root.

## Book layout

```
BOOK/
  book.json            {title, author, language, pinyin_mode, cover?, chapters:[{source, glossary}]}
  images/cover.png     optional cover (prepare_cover.py; see "Cover")
  plan.json            outline + introduced set + validation params
  chapters/chNN.md     chapter source (# title, ## section, paragraphs)
  build/               harvest TSVs, glossaries, .epub output
```

Worked example under `workspace/`: `being-earnest` (诚实的重要, HSK 3, 10 ch) —
plan with story bible, per-chapter glossaries, and a built EPUB. Superseded
books are deleted rather than kept: once the generator improves, older output
is noise, and git history holds it if we ever want to look back.

## Cover

Give each reader a cover with the shared tool
`epub-builder/scripts/prepare_cover.py` (same one pdf2epub uses). The default
template is Chinese-themed — a parchment panel over a study scene — and the
book's title is drawn into the panel in **LXGW WenKai** (the kaiti hanzi that is
WenZilla's Chinese half, so the cover matches the reader's body face). The font
is rasterised into the PNG at build time; wrapping is CJK-aware (breaks between
hanzi).

```bash
.venv/bin/python epub-builder/scripts/prepare_cover.py \
    reference/covers/graded-default.png --title "愚公移山" \
    --title-config reference/covers/graded-default.json \
    --out BOOK/images/cover.png
```

Then set `"cover": "images/cover.png"` in `book.json`; the builder embeds it.
A user can override by dropping their own image and pointing `--title-config` at
it (or its own JSON), or skip the title for a cover that already has one.

## Build order (when starting a new reader)

1. Scaffold against a small list and ONE chapter (already done for HSK 1-3).
2. Get validate + build_epub green on that one chapter end to end.
3. Confirm the pinyin mode renders on the target reader (diagnostic EPUB;
   on the X3 that's settled: gloss-pinyin / gloss-underline / plain).
4. Only then run the full loop for the remaining chapters.

## Setup

```
python -m venv .venv && .venv/bin/pip install jieba pypinyin
```
On Debian/Ubuntu, install jieba inside a venv — the system setuptools breaks
jieba's legacy `setup.py` (`install_layout`). Run the scripts with `.venv/bin/python`.

## Adjusting level

Change which bands count as "known" by editing `lists/`. `hsk.tsv` currently
carries HSK 1-4; for a tighter target, trim bands out, and for a higher one,
append more (mirror any new band in `gen_context.py`'s `BAND_ORDER`). The
pipeline is level-agnostic — it grades against whatever the lists contain.
