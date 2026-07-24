# The common book format — the builder's input contract

The builder consumes **a prepared book directory and nothing else**. It knows
nothing about who prepared it: not Chinese, not PDFs, not vocabulary levels.
Every service in the suite does its own thinking, writes the result into this
shape, and then calls the builder "dumbly".

So the division is:

| | decides | examples |
|---|---|---|
| **the service** | *what the book says* | which words carry a pronunciation guide and what it reads, what's in the glossary, where chapters break, which images survive |
| **the builder** | *how it is presented* | markdown → XHTML, spine and TOC, CSS, packaging into a valid EPUB |

If a construct isn't described below, the builder doesn't support it — and
adding one is a change to *this file* first.

## Layout

```
workspace/<slug>/
  book.json           metadata + spine (below)
  chapters/chNN.md    one markdown file per spine item, in order
  images/             prepared images (grayscale, ≤480px wide), referenced from chapters
  build/              outputs — and any generated inputs the service prepares
```

## book.json

```json
{
  "title":    "诚实的重要",
  "author":   "改编自 Oscar Wilde",
  "language": "zh",
  "cover":    "images/cover.png",
  "reading_style": "after",
  "line_spacing":  "normal",
  "chapters": [
    { "source": "build/annotated/ch01.md", "glossary": "build/ch01-glossary.tsv" }
  ]
}
```

| key | meaning |
|---|---|
| `title`, `author`, `language` | metadata; `language` is the EPUB `dc:language` |
| `chapters` | the spine — order here is reading order; each entry's `#` heading becomes its TOC label |
| `chapters[].source` | the markdown the builder renders. Point it wherever the prepared file lives |
| `chapters[].glossary` | optional TSV (`word`, `pinyin`, `gloss`) rendered as an end-of-chapter list; annotated words link to their entry and back |
| `cover` | optional; path relative to the book directory. Prepare it to device spec first (`prepare_cover.py`) |
| `reading_style` | how `{word\|reading}` is presented: `after` (default, 猴子hóuzi), `ruby` (`<ruby>`; device-confirmed broken on the X3, fine on phones), `none` (drop readings) |
| `line_spacing` | `normal` (default) or `tight` — minimal leading, for small e-ink screens |

## Chapter markdown

The complete construct set. Everything else is literal text.

| construct | renders as |
|---|---|
| `# Title` | `<h1>` — the first one is the chapter title and TOC label |
| `## Section` | `<h2>` |
| blank-line-separated block | `<p>` |
| `*emphasis*` | `<em>` |
| ` ```verse ` … ` ``` ` | `<div class="verse">` with one `<p>` per line, hanging indent — line breaks preserved (poetry, drama) |
| `![caption](../images/f1.png)` | `<figure><img><figcaption>` — the file must already exist, prepared |
| `text[^1]` + `[^1]: note` | numbered endnote link + an endnotes section at the chapter end |
| `{word\|reading}` | the word with a pronunciation guide, presented per `reading_style` |

### `{word|reading}` — the annotation construct

```
今天{阿龙|Ā Lóng}一个人在家。
```

This is how a service passes a reading (pinyin, furigana, any phonetic hint)
without the builder knowing the language. **The service decides which
occurrences get marked** — mark only the first if you want gloss-once
behaviour — and if the word matches a glossary row, the builder additionally
links it to that entry and back.

The builder does no segmentation and generates no readings; it renders exactly
what it is handed.

## Preparing a book — the short version

1. Write `chapters/*.md` using only the constructs above.
2. Prepare anything that needs computing — readings, glossaries, images,
   cover — and write it into the book directory.
3. Fill in `book.json`, pointing each `source` at the file the builder should
   read (keep your human-readable original separate if you generate an
   annotated copy — graded-reader does exactly this, via `annotate.py`).
4. `build_epub.py BOOKDIR --out book.epub`, then `verify_epub.py book.epub`.

The builder takes no flags that change the book: everything it needs is
declared in `book.json`, so the same directory always produces the same EPUB.
