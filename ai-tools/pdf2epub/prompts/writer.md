# Writing a chunk

You turn a few printed pages into clean chunk markdown for an EPUB read on a
small e-ink screen (~36 characters a line, ~20 lines a screen). The page
images are the ground truth. The text layer, when you are given one, is a
spelling reference: exact words, accents, numbers, but often in the wrong
order, with running headers and page numbers mixed in, and words split at line
ends.

A deterministic gate checks what you write against that text layer, word by
word: leaving out a sentence fails it, and so does writing words the page does
not print. Paraphrasing, summarizing, modernizing or "improving" the text also
fail it. Transcribe the page.

## What to write

- **Every word of the body text on your pages, in reading order.** Follow the
  printed columns (all of the left column, then the right), not the text
  layer's line order.
- **Real paragraphs.** Join the printed lines of a paragraph into one line.
  Separate paragraphs with a blank line.
- **Join words split across a line break** (`sustancial-` / `mente` →
  `sustancialmente`). Keep the hyphen in real compounds (`socio-cultural`).
- **Fix extraction damage you can see is wrong on the page image**: doubled
  letters (`signififica` → `significa`), glued or split words, wrong
  characters. Fix what the *scanner or text layer* got wrong, never what the
  *author* wrote: keep period spelling, odd punctuation, the author's own
  capitalization and dashes.
- **Leave out page furniture**: running headers and footers, page numbers,
  the magazine or book name repeated on every page, catchwords, scan stamps.
- **The title** of the work, if it starts on your pages, is a `# ` heading,
  written in normal sentence case even when printed in capitals. A subtitle
  printed with it goes in the same heading after a colon. A byline (the
  author's name next to the title) is left out: the book's metadata carries it.
- **Section headings** printed inside the text are `## ` headings, in
  sentence case, without a final period. A bold sentence that opens a
  paragraph (a run-in lead) is not a heading: it stays in its paragraph, as
  `*emphasis*`. Start a new chapter (`# `) only where the printed work starts
  one: a new chapter, act, or article.
- **Emphasis**: `*italic*` for italic or bold words in running text. Not for
  headings, not `**bold**`.
- **Verse** (poetry, drama in verse): inside a ` ```verse ` fence, one printed
  metrical line per line, a column-wrapped line re-joined, no blank line
  inside a fence (a new stanza is a new fence). Speaker labels begin their
  turn's first line.
- **Footnotes**: `word[^1]` where the mark is, and `[^1]: the note` on its own
  line at the end of your chunk. Number them on from the previous chunk.

## Figures and tables

You are told the figure candidates on your pages (embedded images, ruled
tables, regions someone added), each with an id and a preview. For each one:

- **Content** (a chart, diagram, map, table, a photo the text talks about):
  place it where it belongs in the reading order, as a paragraph of its own:
  `[[fig:p044-1 | Tabla 2. Ventas por año]]`. The caption is the printed
  caption if there is one, else a few words saying what it shows. The text
  inside the figure (table cells, labels) stays in the image: do not
  transcribe it.
- **Decoration** (an author's thumbnail, ornaments, logos): leave it out. An
  illustration the text never refers to (a mood photo) may stay if it adds to
  the page, with a third field: `[[fig:p045-1 | Dos sillas vacías | inline]]`.
  `inline` keeps it at most page width with its caption below — never turned,
  split or blown up to a page of its own.
- **Captions** are in the book's language. Use the printed caption if there
  is one; otherwise write a few plain words saying what the image shows.
- **Tables are never markdown tables.** No construct for them exists: a table
  is a figure, or, if it is really just a list, paragraphs.

## Where your pages end

If your last paragraph runs on past your last page, write what is on your
pages and end the chunk with a line `[[continues]]` right after it, with no
blank line in between. If the previous chunk ended that way, you are told
how, and your first paragraph begins mid-sentence: start it exactly where the
page does, the two are joined later. A word split across the page break is
written with its hyphen at the end of the earlier chunk (`postmo-`); joining
it is done for you.

## Allowed markdown, complete

`# ` and `## ` headings, paragraphs, `*italic*`, ` ```verse ` fences,
footnotes `[^n]`, `[[fig:ID | caption]]` (optionally `| single`, `| double`,
`| inline`), and a final `[[continues]]`. Anything else (HTML, lists, tables,
bold, links, `###`) is not rendered and fails the gate.

Return only the chunk markdown: no commentary, no code fence around it.
