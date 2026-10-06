# Worked conversions — the pipeline's pseudo-proof

There is no deterministic "does the pipeline work" self-test, on purpose. The
pipeline is *a model writing chunks between deterministic gates*: whether it
works is whether a model driving it takes a real PDF to an EPUB a human,
reading it, finds sound. A frozen replay of one fixture's bytes would only
prove the scripts didn't change.

So the proof is this log: what each source was, which model drove it, what
the gates caught, and — most usefully — **where a tool was missing or rough**.

## The examples

| slug | source | class | committed? |
|---|---|---|---|
| `alcaldes-encontrados` | *Los alcaldes encontrados*, 1793 entremés, 16 pp. | D: scan, garbage OCR layer; verse | yes (public domain) |
| `amor-posmoderno` | *Mecanismos defensivos del amor posmoderno*, Omnia nº 281 (Mensa España), pp. 43–45 | A/E: born-digital, two columns, photos | no — gitignored test sample |

## Per-conversion notes

### alcaldes-encontrados (scan → vision transcription, before the chunk harness)
Transcribed by reading all 16 rendered pages by eye into `chapters/ch01.md`:
OCR fixed letter by letter, column-broken verse re-joined into whole metrical
lines, speaker labels and italic stage directions restored, 1793 orthography
kept, furniture not transcribed. It predates the chunk harness (2026-10) but
its output is the same contract: `chapters/*.md` passes today's format gate
and `verify.py`. Triage reads its text layer as `TEXT` — the garbage OCR case
`--untrusted-text` exists for. Its earlier rule-based conversion, faithful to
the OCR, was unreadable on-device; that is what motivated reading pages
instead.

### amor-posmoderno (born-digital article — first run of the chunk harness)
Three magazine pages, two columns, a title in capitals with a byline photo,
`##` sections, one large photo. Extracted for testing; not committed.

Findings while building the harness on it:
- **Columns interleave in the text layer.** pdfplumber merged left and right
  column lines sharing a baseline; any n-gram check against it was useless.
  Fixed: gutter detection in `extract_text.py` (`--columns auto`).
- **Words glued on tight justification** (gaps 2.99pt under a 3pt tolerance).
  Fixed: tolerance relative to font size.
- **Ligature leaks** in the text layer (`signififica`, `conflflicto`): the
  model fixes them from the image; the gate sees a one-word run, harmless.
- **The byline photo is detected twice** (as an image and as a ruled
  "table" around it); both flagged likely-decorative.
- **A mood photo split in two** under `double` is silly: added the per-figure
  override (`| inline`, `| single`).

Model-driven runs (Sonnet, as the CLI driver): X3/single and X4 Pro/double,
every chunk passing its gate first time. Read on the X3, two defects, both
since fixed in the tools:
- **Bold came out as italic.** FORMAT.md had only `*emphasis*`, so the writer
  mapped printed bold to `<em>`. Added `**strong**` (the firmware renders
  `<b>`/`<strong>`).
- **An invented caption under the photo.** writer.md asked for "a few words
  saying what it shows" when nothing was printed. Captions are now printed
  text only, and the gate checks every caption word is on the page.
- On the X4 Pro, `double` left the landscape photo *turned* instead of split
  (gain 1.15× < 1.25×). A split that spares turning the reader now wins at
  equal size.

## Tool gaps (for review)

Ranked roughly by value. ✅ = fixed, 🔧 = proposed.

1. ✅ Column-aware extraction (above).
2. ✅ Font-relative word gaps (above).
3. ✅ The old restore gate rejected legitimate de-hyphenation — gone with the
   restorer; the new gate joins line-break hyphens on both sides first.
4. 🔧 Vector figures and unruled tables are not detected (`figures.py add` by
   hand). Cluster `rects/lines/curves` into candidate regions.
5. 🔧 Footnote renumbering across chunks in `assemble.py`.
6. 🔧 Gate thresholds need more books behind them (DESIGN.md, open question 1).
7. 🔧 The text layer knows which words are bold or italic (font names:
   `DINPro-Bold`); the gate could check the chunk marks the same runs,
   instead of leaving it to the writer's eyes.
