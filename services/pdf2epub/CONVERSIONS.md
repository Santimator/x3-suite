# Worked conversions — the pipeline's pseudo-proof

There is no deterministic "does the pipeline work" self-test, on purpose. The
pipeline's whole design is *an AI wielding deterministic tools*: whether it
works is whether an agent can take a real PDF and produce a faithful EPUB, and
whether a human, reading the result, finds it sound. A frozen replay of one
fixture's exact bytes would only prove the scripts didn't change — not that the
toolbox is *capable*.

So the proof is this: the committed sample conversions under `workspace/` —
their `source.pdf`, the agent's decisions (a `policy.json`/`draft.json` on the
cheap route, or a hand-written `chapters/*.md` transcription on the vision
route), and the built `build/<slug>.epub` (the result). This file is the
annotated log of producing them: what each source was, which route it took and
why, which tools the agent reached for, and — most usefully — **where a tool
was missing or rough**, so we can add or polish it. Re-run a cheap-route one by
hand to check the scripts still behave (`triage → extract_text → restore →
prepare → build_epub → verify`); on the vision route the test is a read. Either
way, reading the EPUB is the final judge. That *is* the test.

## The examples (four classes on purpose)

| slug | source | class | committed? |
|---|---|---|---|
| `alcaldes-encontrados` | *Los alcaldes encontrados*, 1793 entremés | OCR'd verse, short, single piece | yes (public domain) |
| *attention-is-all-you-need* | *Attention Is All You Need* (2017) | **born-digital**, prose, dense | **no** — copyrighted; converted as a demo only |

Only conversions produced by the *current* pipeline are kept. The earlier
OCR-route books (`gurruminos`, `el-espanol-de-oran`) were retired when the
vision route replaced them: they were faithful to a garbage text layer, which
is precisely the philosophy this service abandoned, so keeping them would
document the wrong thing.

## Per-conversion notes

### alcaldes-encontrados (scan → **vision route**)
The reference vision-route conversion. Originally taken through the cheap route
(policy furniture regexes + normalize + verse reflow); the result read faithful
to the OCR — and the OCR was garbage (`Ve]. ^ TO me ga` for `Vej. No me tenga`,
every verse line shredded at the column width), so on-device it was unreadable.
Re-done by **reading all 16 rendered pages by eye** and writing
`chapters/ch01.md` directly: OCR fixed letter by letter, column-broken verse
re-joined into whole metrical lines, speaker labels and italic stage directions
restored, 1793 orthography kept, furniture simply not transcribed. No
`policy.json`/`draft.json` — the agent replaces those stages. `verify.py` shows
`char_ratio ≈ 0.99` (complete); the read is the gate. This is the conversion
that motivated the two-route redesign.



### attention-is-all-you-need (born-digital paper — demo, not committed)
A genuinely different class, and the best stress test. Findings:
- **Single-column**, so no column-interleave disaster.
- **Severe broken spacing** — the text layer renders justified text with no
  space glyphs, so pdfplumber's lines came out `TheTransformerfollows...`.
  `--space-recover` fixed it cleanly (gap #1). This is *why* that tool exists.
- `prose` reflow; page-number + arXiv-stamp furniture dropped; one chapter.
- Quality is faithful-but-imperfect: math/tables flatten to inline text,
  ligatures leak (`fifigures`), and it's one long chapter (sectioning would
  help). All *faithful to the extraction* — the contract holds; beauty is a
  separate axis the source and policy drive.
- **Not committed**: the paper is copyrighted. For a committed born-digital
  fixture we'd want a public-domain or CC-licensed one.

## Tool gaps found (for review)

Ranked roughly by value. ✅ = fixed this pass, 🔧 = proposed.

1. ✅ **Space recovery for born-digital PDFs.** Justified text with no space
   glyphs extracted word-runtogether. Added `extract_text.py --space-recover`
   (rebuilds spaces from glyph gaps; adaptive threshold, opt-in for
   `broken_spacing`). Without it the paper was unreadable.
2. 🔧 **The restore fidelity gate rejects legitimate dehyphenation.** Merging
   line-break hyphens changes >0.5% of 5-grams on any prose with a normal
   hyphenation rate, so `ngram_containment ≥ 0.995` fails and I had to run with
   `dehyphenate: false` everywhere. The pipeline currently *cannot* emit
   dehyphenated prose. Fix: make the gate dehyphenation-aware (apply the same
   hyphen-join to the input baseline before comparing), so a correct transform
   isn't punished.
3. ✅ **Coverage counted `` ```verse `` fences as content.** They're builder
   markup (rendered as `<div class="verse">`), but verify compared them against
   the EPUB and short verse books failed on the two fence tokens alone. Fixed:
   strip fences before comparing.
4. ✅ **Coverage penalized intentionally-dropped front matter.** verify compared
   the EPUB against *all* of `restored.md`, so dropped front matter read as
   "missing" (fine at 3 lines, failed orán at 13). Fixed: compare against the
   **chapters** (what actually went into the book); prepare's paragraph
   accounting separately guards the restored→chapters hop.
5. 🔧 **Furniture authoring is manual and error-prone on messy OCR.** Page
   numbers and catchwords had to be enumerated as exact regexes, one by one,
   dodging real verse lines. A *furniture-candidate detector* (short lines that
   recur as the first/last line of pages but not in the body) would propose the
   set for the agent to confirm. triage has a stub; make it real.
6. 🔧 **Cutting a verse work into chapters means hunting boundary lines.**
   `verse` reflow emits one block per range, so a 3-act play needs three ranges
   with exact `end_anchor` lines found by hand. A `split_on` option ("start a
   new range at each line matching /JORNADA/") would make act/scene cutting a
   one-liner.
7. 🔧 **Furniture vs range coverage is a confusing interaction.** Furniture is
   dropped *inside* a chunk, but `page_ranges` must still cover the furniture
   lines — so a front-matter range that "starts at the title" left the page-1
   header line uncovered and errored. Clearer error text (or letting a leading
   furniture line sit outside the ranges) would save a debug cycle.
8. 🔧 **Ligature artifacts in born-digital extraction** (`fifigures`). A small
   ligature-normalization pass (or an extraction flag) would clean these.
