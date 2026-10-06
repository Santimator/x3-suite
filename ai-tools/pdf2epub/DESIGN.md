# pdf2epub — design notes

SKILL.md holds how to run it; this file holds why it is shaped this way, and
what is still open.

## The problem

PDF is a *page description*: where ink goes. EPUB is a *document*: ordered
chapters, paragraphs, headings, a nav. Conversion is recovery of intent from
ink, and the sources vary wildly:

| class | source | text layer | main difficulty |
|---|---|---|---|
| A | born-digital, clean | good | structure (chapters, columns, figures) |
| B | born-digital, pathological | dirty | doubled glyphs, no space glyphs, ligature leaks |
| C | scan, no text layer | none | read everything from pixels |
| D | scan + embedded OCR | unknown, often garbage | trust it or not? |
| E | complex layout | any | columns, footnotes, tables, figures, verse |

## History: two routes, then one (2026-10)

The first design was "deterministic first, generation never": extract the text
layer, restore it with a rule file (`policy.json`: furniture regexes, reflow
modes, normalizations), cut chapters from verbatim anchors (`draft.json`),
model only as orchestrator. It was faithful and it was fragile: a policy is
hand-authored per book, its own fidelity gate rejected ordinary
de-hyphenation, and on a class D scan it faithfully carried garbage OCR onto
the device. So a second route was added (2026-07) in which the agent read the
page images and transcribed scans directly — and it was the one that produced
readable books.

The 2026-10 redesign drops the first route and generalizes the second:

- **The model writes every chunk, for every class.** It reads the page image
  (ground truth) with the text layer beside it as a spelling reference. Rule
  files, tesseract, the restorer, the anchor cutter are gone: ~1,000 lines a
  model does better and more cheaply in tokens than a human does in
  rule-writing time.
- **Small chunks** (2 pages by default) bound what one mistake can cost and
  what one context must hold, and make the work resumable and parallelizable
  across drivers.
- **A deterministic gate after every chunk** keeps the old principle — trust
  comes from checks, not from the model — but applies it to the model's output
  instead of to a rule engine's.

## The gate, and why it is shaped like this

*Format* is easy and catches the cheap failures (unclosed fence, HTML, a
markdown table the builder cannot render, a word left split at a line end, a
page number left in, a blank line inside a verse fence — which the builder
would split).

*Fidelity* is the interesting part. Where a text layer exists it is a
near-perfect word list of the page, even when its order is wrong. So both
sides are reduced to lowercase words (line-break hyphens joined, NFKC) and a
word counts as covered when a word 5-gram containing it occurs on the other
side. That makes the comparison:

- **order-tolerant between blocks** (a heading the text layer put elsewhere,
  columns) but **order-sensitive inside them** (a shuffled sentence fails);
- **proportionate**: one fixed typo uncovers one word; a dropped sentence
  uncovers a run as long as the sentence. The gate fails on the *longest
  run* (≥12 missing, ≥8 invented words) and on overall recall/precision
  (0.95/0.97), and prints the runs — the report is the rework instruction.

A first version measured n-gram *sets*; one fixed word then cost five n-grams
and harmless fixes dragged precision under the bar. Word coverage fixed that.

Text layers interleave columns line by line ("left line 1 + right line 1"),
which would break every 5-gram crossing a line. `extract_text.py` finds a
vertical gutter no body line crosses and emits left column, then right,
between full-width lines (titles). Word gaps are judged relative to font size
(`x_tolerance_ratio`): pdfplumber's fixed 3pt glued words on tightly justified
9pt text whose gaps measured 2.99pt.

Where there is no text layer, no deterministic content check exists, so the
gate is a **review**: a second model pass (or a fresh subagent) comparing the
chunk with the pages, recorded in `review.json` against the chunk's hash;
assembly refuses without it. A writer reviewing its own output in the same
context catches little — keep them apart.

## Figures and tables on a 6" panel

CrossPoint draws an image at its own pixel size, only shrinking it to the
viewport, never enlarging it, and gives an image too tall for the rest of the
page a page of its own (read from the firmware source: `extras/readers.md`).
So size is decided at preparation, from the vector source: every placed
figure is rendered to fill the panel. The panel is portrait; most tables are
landscape — turning one 90° often makes it 1.5× larger. And a dense figure can
be split in two with a small overlap, each half a page: back-and-forth between
two pages beats squinting at one. Gains under 1.25× are not worth the
reader's effort, so they are not taken — except that a split needing no turn
beats a turned single image as soon as it is as large: turning the reader
costs more than flipping a page (found on the X4 Pro, whose narrower panel
left a landscape photo turned instead of split at a 1.15× gain). Captions go before page-sized images:
after one, a caption would sit alone on the next page.

The mode is the user's choice (`single` / `double`), per figure overridable
(`inline` for decoration), and the device too (X3 528×792, X4 Pro 480×800 —
`epub-builder/scripts/devices.py`). Changing either re-renders figures and
cover, never the text.

## Drivers

The seams are files, so two drivers fill the same slots:

- **Claude Code** following SKILL.md: the quality path, interactive (it asks
  the device and figure mode), can add figure regions, judge a garbage OCR
  layer, take a user transcript.
- **`headless/run_conversion.py`**: unattended, any OpenAI-compatible vision
  model, device/mode from config. Stops rather than guesses; resumes from
  files. `tools/tgbot/` calls it.

Chunk state is computed from files (`out.md` hash vs `check.json` and
`review.json`), so either can pick up where the other stopped.

## Open questions

1. **Thresholds** (12/8 words, 0.95/0.97) are set from one born-digital
   article. They need a few more conversions — a novel, a paper with
   footnotes, a two-column scan — before they are trusted.
2. **Footnotes** are written as endnotes per chunk and numbered on by the
   writer; assembly only checks uniqueness per chapter. A renumbering pass in
   assembly would remove a source of mistakes.
3. **Vector figures** (diagrams drawn with lines, unruled tables) are not
   detected; they are added by hand with `figures.py add`. Clustering
   `page.rects/lines/curves` into regions would make that automatic.
4. **Device rendering of large images** — the shrink-to-viewport quality on
   dense tables, and whether a full-panel image leaves room for the status bar
   without a second shrink — is inferred from source, not confirmed on the
   panel.
5. **Chunk size.** Two pages keeps a model's context and a mistake's cost
   small; dense pages might want one, light ones three. Untested.
