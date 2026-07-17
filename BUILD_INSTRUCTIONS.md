# BUILD_INSTRUCTIONS — implement the pdf2epub pipeline

Audience: a Claude (Sonnet 5) session in this repo with no prior context.
Everything you need is here and in the referenced docs. Read this file fully,
then read `.claude/skills/pdf2epub/SKILL.md`, its `DESIGN.md`, and
`.claude/skills/epub-builder/FORMAT.md` before writing any code.

## What exists / what you build

Done already (do not redo): stage 0 `triage.py`; the shared EPUB builder
(`.claude/skills/epub-builder/scripts/build_epub.py`) with its FORMAT.md
contract; the test fixture `workspace/goya-sueno/source.pdf` with its
`build/triage.json`.

You build, in this order (one commit per work item, WI-6 may be several):

| WI | deliverable | where |
|---|---|---|
| 1 | `extract_text.py` | `.claude/skills/pdf2epub/scripts/` |
| 2 | `render_pages.py` | same |
| 3 | `extract_ocr.py` | same |
| 4 | `restore.py` (+ policy.json schema) | same |
| 5 | `prepare.py` (+ draft.json schema) | same |
| 6 | FORMAT.md extensions in the builder | `.claude/skills/epub-builder/` |
| 7 | `verify.py` | `.claude/skills/pdf2epub/scripts/` |
| 8 | `selftest.py` for pdf2epub | same |
| 9 | end-to-end: the Goya EPUB | `workspace/goya-sueno/` |

## Ground rules (non-negotiable)

1. **Deterministic-first, agent-on-error.** Scripts never invent text. Every
   byte of book text must trace to the extraction. No LLM calls inside any
   script — the agent (you, or a later session) sits *between* scripts,
   reading reports and editing decision files (`policy.json`, `draft.json`).
2. **Contracts are authoritative.** The builder consumes only what
   `epub-builder/FORMAT.md` describes. If implementation forces a contract
   change, make the smallest change, update the doc in the same commit, and
   say so in the commit message.
3. **Never break graded-reader.** After any change under `epub-builder/`,
   run `.venv/bin/python .claude/skills/graded-reader/scripts/selftest.py`
   (must PASS) **and** rebuild `workspace/yugong-mountain` and diff every
   zip entry's bytes against the committed
   `workspace/yugong-mountain/build/yugong-mountain.epub` (zip *entry
   contents* must be identical; the annotated path's output is frozen).
4. **Determinism.** Same inputs → byte-identical outputs, always. Zip entry
   mtimes are pinned to `(2026, 1, 1, 0, 0, 0)` — keep that for anything new
   you write into the EPUB (images included). No wall-clock, no randomness,
   no dict-ordering surprises (sort where iteration order leaks into output).
5. **Dependencies.** Python via `.venv` (create if absent). Allowed:
   `pdfplumber`, `pypdf`, `pypdfium2` (page rendering), `Pillow` (image
   prep), `pytesseract` (OCR route only). Update
   `.claude/skills/pdf2epub/requirements.txt` as you use them. No epub
   libraries, no pandoc, nothing heavier.
6. **Style.** Match the existing scripts: stdlib argparse CLIs, module
   docstring explaining *why*, `--out` flags, human summary on stdout,
   machine JSON to files, exit 0/1 as pass/fail. Comments state constraints,
   not narration.
7. **Commits.** One per WI, imperative subject, body lists the verification
   you ran and its result. Push to the current branch
   (`git push -u origin <branch>`). Never push elsewhere.
8. Docs stay current: when a WI lands, flip its "planned" marker in
   `pdf2epub/SKILL.md` (and README where statuses appear) in the same commit.

## Setup

```bash
python3 -m venv .venv   # if missing
.venv/bin/pip install pdfplumber pypdf pypdfium2 Pillow
# graded-reader checks additionally need: jieba pypinyin
```

System `tesseract` may be absent; WI-3 must degrade gracefully (see below).

## Known facts about the fixture (measured — trust these)

- 18 pages, all with a usable text layer; route TEXT; language `es`.
- Every glyph is drawn twice ("PPrreeffiieerroo") — fake bold.
  `page.dedupe_chars()` fixes it; triage flags it as `doubled_chars`.
- **No geometric paragraph signal**: line gaps are uniform (~13.5pt) and all
  lines share x0 ≈ 87.6. Paragraph boundaries are recoverable only from
  punctuation: a line ending *without* terminal punctuation
  (`.` `!` `?` `…` `:` `”` `»` `)`) is a wrapped continuation of the next
  line; a line ending with one is a complete unit. The text is aphoristic
  theatre — one sentence per paragraph is the correct output shape.
- The text uses low-9 comma `‚` (U+201A) where `,` is meant → normalization
  table entry, applied by restore, logged.
- Page 1 is a title block (title, company "La Carniceria Teatro", author
  line "Texto y Dirección: Rodrigo García") followed by body text starting
  "Prefiero que me quite el sueño Goya…". There are no chapter headings
  anywhere — the book is one continuous monologue.

## WI-1 — `extract_text.py` (text-layer route)

CLI:
```
extract_text.py SOURCE.pdf --out EXTRACTDIR [--pages A-B] [--dedupe auto|on|off]
```

- `--dedupe auto` (default): apply `dedupe_chars()` on pages where it drops
  >30% of chars (same rule as triage).
- Writes `EXTRACTDIR/pages.jsonl` — one JSON object per page:
  ```json
  {"page": 2, "width": 595.3, "height": 841.9, "dedupe_applied": true,
   "lines": [{"text": "…", "x0": 87.6, "top": 74.1, "x1": 460.2,
              "bottom": 87.3, "size": 12.0, "font": "VAALBR+font2"}],
   "images": [{"x0": 0, "top": 0, "x1": 100, "bottom": 80}]}
  ```
  Use `page.extract_text_lines()`; `size` = median char size on the line;
  `font` = most common fontname. Round floats to 1 decimal (determinism +
  diff-ability).
- Writes `EXTRACTDIR/extract-report.json`: pages processed, dedupe pages,
  total lines/chars, per-page char counts. Print a 3-line summary.
- **Check:** run on the fixture → 18 pages, dedupe applied on all, page 2
  first line starts `"mismo la FNAC‚"` (not `"mmiissmmoo"`), total chars
  within 2% of triage's per-page sum.

## WI-2 — `render_pages.py` (the agent's eyes)

CLI: `render_pages.py SOURCE.pdf --out DIR [--pages A-B] [--dpi 150]`
- pypdfium2 → `DIR/pNNN.png`, grayscale. No OCR here; this exists so the
  agent can *look* at a page (Read tool on the PNG) when text output makes
  no sense, and as the input step for OCR.
- **Check:** render fixture pages 1-2; files exist, nonzero, look sane
  (open one yourself with Read).

## WI-3 — `extract_ocr.py` (scanned route)

CLI:
```
extract_ocr.py SOURCE.pdf --out EXTRACTDIR --lang spa [--dpi 300] [--psm 6] [--pages A-B]
```
- Render (reuse WI-2's code as an import, not a subprocess) → pytesseract
  `image_to_data` → reconstruct the same `pages.jsonl` shape as WI-1
  (line bboxes from word boxes; `size` from box height; `font` = `"ocr"`;
  add per-line `"conf"`: mean word confidence). Same report file, plus mean
  confidence per page — that number is how the agent decides a page needs
  the vision fallback.
- If `tesseract` binary or `pytesseract` is missing: exit 1 with a one-line
  install hint. Do NOT auto-install system packages.
- **Check (self-contained OCR roundtrip):** build a synthetic scanned PDF
  from the fixture — render pages 1-2 to PNG, wrap them into a new
  image-only PDF (Pillow `save(..., format="PDF")`) in the scratchpad — run
  triage on it (expect route OCR), then extract_ocr. Normalized text of
  page 1 must contain `"Prefiero que me quite"` (allow OCR noise
  elsewhere). If tesseract is unavailable in this environment, implement
  anyway, mark the check "not run — tesseract unavailable" in the commit
  body, and make selftest (WI-8) skip it gracefully.

## WI-4 — `restore.py` + `policy.json`

The heart. CLI:
```
restore.py EXTRACTDIR --policy workspace/<slug>/policy.json --out RESTOREDIR
```
Reads `pages.jsonl`, applies **only** what the policy says, writes
`RESTOREDIR/restored.md` + `RESTOREDIR/restore-report.json`.

`policy.json` schema (write this example into the SKILL.md when you land it):
```json
{
  "furniture": ["^\\d+$", "^kupdf\\.net"],
  "page_ranges": [
    {"pages": "1", "treat": "front_matter"},
    {"pages": "2-18", "treat": "body"}
  ],
  "reflow": "sentence",
  "normalize": {"‚": ","},
  "dehyphenate": true,
  "dehyphenate_exceptions": []
}
```
- `furniture`: regexes; matching *whole lines* are dropped (count them).
- `treat`: `front_matter` lines pass through preserved verbatim (the draft
  decides what becomes title metadata later); `body` lines get reflowed;
  `skip` drops the pages.
- `reflow` (also allowed per page_range): `prose` — join lines into
  paragraphs, breaking on vertical gap > 1.6× the page's median gap or on
  x0 indent > 10pt; `sentence` — join a line to the next while it lacks
  terminal punctuation (set above), each completed unit becomes a
  paragraph (correct for the fixture); `verse` — preserve every line break,
  emit as one ```` ```verse ```` block per page-range chunk.
- `dehyphenate`: line ending `-` + next line starting lowercase → join and
  drop the hyphen, unless the joined word matches a
  `dehyphenate_exceptions` entry (then keep the hyphen).
- `normalize`: exact string replacements, applied last, each with a count
  in the report.
- Report: lines in/out, furniture dropped (per pattern), joins made,
  hyphens resolved, normalizations (per entry), paragraphs emitted, and the
  **fidelity gate**: `char_ratio` = non-whitespace chars out / in (after
  removing furniture lines from "in") and `ngram_containment` = fraction of
  5-grams (word-level, on normalized text) of the input found in the
  output. Gate passes iff `0.98 ≤ char_ratio ≤ 1.02` and
  `ngram_containment ≥ 0.995`. Exit 1 on gate failure — that exit code is
  what tells the agent to look.
- **Check:** fixture run with the example policy above: gate passes; output
  has no `‚` left; no line ends mid-word; spot-read the first 20 paragraphs
  against `render_pages.py` output of pages 1-2 yourself.

## WI-5 — `prepare.py` + `draft.json`

CLI:
```
prepare.py workspace/<slug> [--restored RESTOREDIR]   # expects draft.json in the book dir
```
`draft.json` schema:
```json
{
  "title": "Prefiero que me quite el sueño Goya…",
  "author": "Rodrigo García",
  "language": "es",
  "chapters": [
    {"toc_label": "Monólogo", "start_anchor": "Prefiero que me quite el sueño Goya a que lo haga cualquier hijo"}
  ],
  "images": [],
  "front_matter": "drop"
}
```
- `start_anchor`: a verbatim substring of `restored.md`; must occur exactly
  once; chapters must appear in document order; chapter N runs to the line
  before chapter N+1's anchor (last chapter to EOF). Validation failures
  exit 1 with messages precise enough for the agent to fix the draft
  ("anchor not found", "anchor ambiguous (3 hits)", "anchors out of
  order") — never guess.
- `images[]` (may be empty): `{"page": 7, "index": 0, "anchor": "…",
  "caption": "…"}` — the image is cropped from the page render (bbox from
  extraction), grayscaled, downscaled to max width 480px, written to
  `images/figNN.png`, and an image paragraph is inserted after the
  paragraph containing `anchor`.
- Emits `book.json` + `chapters/chNN.md` exactly per
  `epub-builder/FORMAT.md` (`#` heading line from `toc_label`, then the
  paragraphs; do NOT set `pinyin_mode`), plus a `prepare-report.json`
  (paragraph count per chapter; every restored paragraph assigned exactly
  once — assert it).
- **Check:** fixture: draft above validates; `chapters/ch01.md` +
  `book.json` appear; total paragraphs in chapters == paragraphs in
  restored.md (minus dropped front matter).

## WI-6 — builder extensions (touching `epub-builder/` — ground rule 3 applies)

Implement the FORMAT.md "pdf2epub extensions" for the **un-annotated path
only** (mode `None`); the annotated path's behavior and output bytes are
frozen:

- ```` ```verse ```` fenced blocks → `<div class="verse"><p>line</p>…</div>`
  with CSS: no text-indent, `margin: 0 0 0.9em 1em`, lines
  `margin: 0; text-indent: -1em; padding-left: 1em` (hanging indent).
- Image paragraphs `![caption](../images/f.png)` → copy the file into
  `OEBPS/images/`, manifest entry with proper media-type,
  `<figure><img …/><figcaption>…</figcaption></figure>`; missing file =
  build error (prepare should have caught it).
- Endnotes: `[^n]` in a paragraph + `[^n]: text` lines at chapter end →
  superscript links to an end-of-chapter notes section with back-links
  (mirror the existing glossary link/back-link pattern and its id scheme).
- `*em*` → `<em>` (single asterisk pairs only, no nesting).
- `"cover"` in book.json → EPUB3 `cover-image` manifest property.
- New CSS is additive only. Existing CSS rules must not change.
- **Check:** graded-reader selftest PASS + yugong content-diff clean (rule
  3), plus a scratchpad book exercising every new construct builds and its
  XHTML parses (`xml.dom.minidom` on every entry, like selftest does).

## WI-7 — `verify.py`

CLI: `verify.py workspace/<slug> --epub PATH --restored RESTOREDIR`
- EPUB integrity: mimetype first + stored; every manifest item exists in
  the zip and vice versa (ignoring mimetype/container); every internal
  href/id resolves; every XHTML entry parses.
- Coverage: strip tags from spine XHTML, normalize whitespace, compare
  against restored.md the same way restore's gate does (`char_ratio`,
  `ngram_containment`, same thresholds). Report JSON + summary; exit 1 on
  any failure.
- **Check:** passes on the WI-9 EPUB; then prove it can fail: corrupt a
  copy (drop a chapter from the spine) and confirm exit 1 with a sane
  message.

## WI-8 — pdf2epub `selftest.py`

Mirror graded-reader's: no network, no LLM, runs the whole chain on the
fixture (triage → extract_text → restore with a committed
`workspace/goya-sueno/policy.json` → prepare with a committed `draft.json`
→ build → verify) into a temp dir, asserts every gate listed above, plus
the OCR roundtrip from WI-3 (skipped with a notice when tesseract is
absent). Single command, PASS/FAIL summary, exit code. Wire the fixture's
`policy.json` + `draft.json` into git as part of this WI.

## WI-9 — the deliverable

Run the pipeline for real: commit `workspace/goya-sueno/` with `policy.json`,
`draft.json`, `chapters/`, `book.json`, and `build/goya-sueno.epub`. Before
committing, personally read the first ~15 paragraphs of `ch01.md` against
the page renders — the fidelity gates catch loss, not stupidity; your eyes
are the last gate. Then update the status lines in `pdf2epub/SKILL.md` and
the README (stages implemented; fixture converted), and push.

## Definition of done

`pdf2epub/scripts/selftest.py` and `graded-reader/scripts/selftest.py` both
PASS from a clean venv; yugong-mountain content-diff against the committed
EPUB is clean; `workspace/goya-sueno/build/goya-sueno.epub` exists, passes
`verify.py`, and its chapter 1 opens with "Prefiero que me quite el sueño
Goya a que lo haga cualquier hijo de puta." as clean Spanish prose — one
sentence per paragraph, no doubled letters, no `‚`, no page numbers.
