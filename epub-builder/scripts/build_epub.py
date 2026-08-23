#!/usr/bin/env python3
"""Assemble an EPUB by hand (no epub library) from the common book format.

The builder is deliberately ignorant of its callers: it renders exactly the
construct set FORMAT.md documents and nothing else. Deciding *what* to say —
which words carry a pronunciation guide, what the glossary contains, how the
text was produced — belongs to the calling service, which prepares the book
directory. The builder only turns that directory into a valid EPUB.

Hand-controlled XHTML is required because pinyin annotation modes differ at the
markup level, and because the mimetype must be stored first and uncompressed --
things heavy epub libraries hide from you.

Pinyin display is a parameter (the X3/CrossPoint renderer's ruby support is
unconfirmed, so we don't bet the structure on it):

  ruby            <ruby>汉<rt>hàn</rt></ruby>  -- compact, standard, needs a
                  renderer that supports <ruby>. On the X3 the <rt> text
                  leaks inline ("汉hàn") -- device-confirmed unsupported.
  interlinear     stacked spans via CSS inline-block. On the X3 the stacking
                  collapses inline -- device-confirmed unsupported.
  plain           hanzi only; pinyin appears solely in the per-chapter
                  glossary. Renders everywhere.
  gloss-underline plain body, but each glossary word's first occurrence is
                  underlined (<u>), signalling "this word is in the glossary".
  gloss-pinyin    plain body, but each glossary word's first occurrence gets
                  word-level pinyin right after it: 猴子hóuzi (curated
                  glossary pinyin, spaces stripped).

The first occurrence of each glossary word in the chapter text is a tappable
link (dotted underline) to its entry in the end-of-chapter glossary; each entry
has a ↩ back-link to jump back to the reading position.

A "book" is a directory:
  book.json            {title, author, language, reading_style?, line_spacing?,
                        cover?, chapters:[{source, glossary?}]}
  chapters/<f>.md      chapter source (markdown-ish: # / ## headings + paragraphs)
  <glossary>.tsv       optional per-chapter new-word list (word,pinyin,gloss)

Usage:
  build_epub.py BOOKDIR --out book.epub
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Pinyin modes; None = no annotation. The gloss-* modes render plain hanzi
# but mark each glossary word's first occurrence: underlined, or followed by
# word-level pinyin (猴子hóuzi). Device-tested on the X3: ruby leaks <rt>
# inline and interlinear's CSS stacking collapses, so plain/gloss-* are the
# CrossPoint-safe modes (see extras/readers.md).
# How a `{word|reading}` annotation is *presented*. Presentation is the
# builder's business; deciding which words carry a reading, and what the
# reading says, is the calling service's (see FORMAT.md).
READING_STYLES = ("after", "ruby", "none")


class BuildError(Exception):
    """An authoring error the builder catches instead of a raw traceback --
    a missing image or an undefined footnote ref should have been caught by
    prepare.py, but the builder is the last line of defense."""


# --------------------------------------------------------------------------- #
# Chapter markdown -> XHTML body
# --------------------------------------------------------------------------- #
# --- Inline and block constructs (all documented in FORMAT.md) ------------- #
FOOTNOTE_REF_RE = re.compile(r"\[\^([^\]]+)\]")
FOOTNOTE_DEF_RE = re.compile(r"^\[\^([^\]]+)\]:\s*(.*)$")
EMPHASIS_RE = re.compile(r"\*([^*]+)\*")
# {word|reading}: a word carrying a pronunciation guide (pinyin, furigana...).
# The service decides which words get one; the builder only presents it.
READING_RE = re.compile(r"\{([^{}|]+)\|([^{}|]*)\}")
IMAGE_RE = re.compile(r"^!\[([^\]]*)\]\(([^)]+)\)$")


def split_blocks(md: str) -> List[str]:
    """Blank-line-separated blocks. A ```verse fenced block has no internal
    blank lines (restore.py never inserts one), so it always survives as a
    single block here -- no special-casing needed at this stage."""
    return [b for b in re.split(r"\n\s*\n", md.strip()) if b.strip()]


def apply_emphasis(escaped_text: str) -> str:
    """*em* -> <em>, single asterisk pairs only, no nesting. Must run on
    already-HTML-escaped text (the markup chars * are untouched by escape)."""
    return EMPHASIS_RE.sub(lambda m: f"<em>{m.group(1)}</em>", escaped_text)


def render_reading(word: str, reading: str, style: str) -> str:
    """Present one {word|reading} annotation. Already HTML-escaped."""
    if style == "none" or not reading:
        return word
    if style == "ruby":
        return f"<ruby>{word}<rt>{reading}</rt></ruby>"
    return f'{word}<span class="rd">{reading}</span>'  # "after": 猴子hóuzi


def render_inline(text: str, footnote_order: Dict[str, int], prefix: str,
                  link_ctx: Optional[Tuple] = None,
                  reading_style: str = "after") -> str:
    """Emphasis, `{word|reading}` annotations, glossary links and footnote refs
    — the closed inline set FORMAT.md documents. No segmentation: the caller
    already marked the words it wants annotated."""
    escaped = apply_emphasis(html.escape(text))

    def sub_reading(m: "re.Match") -> str:
        word, reading = m.group(1), m.group(2)
        out = render_reading(word, reading, reading_style)
        if link_ctx:
            gprefix, link_map, linked = link_ctx
            hit = link_map.get(word)
            if hit is not None and hit[0] not in linked:
                idx = hit[0]
                linked.add(idx)
                out = (f'<a class="glm" id="{gprefix}-r{idx}" '
                       f'href="#{gprefix}-g{idx}">{out}</a>')
        return out

    escaped = READING_RE.sub(sub_reading, escaped)

    def sub_footnote(m: "re.Match") -> str:
        marker = m.group(1)
        idx = footnote_order.setdefault(marker, len(footnote_order) + 1)
        return (f'<a class="fnref" id="{prefix}-fr{idx}" href="#{prefix}-fn{idx}">'
                f"<sup>{idx}</sup></a>")

    return FOOTNOTE_REF_RE.sub(sub_footnote, escaped)


def endnotes_section(footnote_defs: Dict[str, str], footnote_order: Dict[str, int], prefix: str) -> str:
    if not footnote_order:
        return ""
    items = []
    for marker, idx in sorted(footnote_order.items(), key=lambda kv: kv[1]):
        if marker not in footnote_defs:
            raise BuildError(f"{prefix}: undefined footnote reference [^{marker}]")
        note_html = apply_emphasis(html.escape(footnote_defs[marker]))
        items.append(
            f'<li id="{prefix}-fn{idx}"><sup>{idx}</sup> {note_html} '
            f'<a class="fnback" href="#{prefix}-fr{idx}">↩</a></li>'
        )
    return ('<section class="endnotes" epub:type="endnotes"><h2>Notes</h2>'
            "<ul>" + "".join(items) + "</ul></section>")


def image_paragraph(caption: str, rel_path: str, prefix: str,
                     images_out: Optional[List[Tuple[str, str]]]) -> str:
    if not rel_path.startswith("../images/"):
        raise BuildError(f"{prefix}: image path must be under ../images/: {rel_path!r}")
    basename = rel_path.rsplit("/", 1)[-1]
    epub_src = f"images/{basename}"
    if images_out is not None:
        images_out.append((rel_path, epub_src))
    return (f'<figure><img src="{epub_src}" alt="{html.escape(caption)}"/>'
            f"<figcaption>{html.escape(caption)}</figcaption></figure>")


def strip_footnote_defs(md: str) -> Tuple[str, Dict[str, str]]:
    """Pull out "[^n]: text" lines (one def per physical line -- markdown
    footnote defs are conventionally adjacent, no blank line between them,
    so block-splitting alone can't isolate them). Skips lines inside a
    ```verse fence, where a poem could coincidentally start with "[^"."""
    footnote_defs: Dict[str, str] = {}
    kept: List[str] = []
    in_verse = False
    for line in md.split("\n"):
        if line.strip().startswith("```verse"):
            in_verse = True
        elif in_verse and line.strip() == "```":
            in_verse = False
        elif not in_verse:
            m = FOOTNOTE_DEF_RE.match(line)
            if m:
                footnote_defs[m.group(1)] = m.group(2)
                continue
        kept.append(line)
    return "\n".join(kept), footnote_defs


def chapter_body(md: str, prefix: str,
                 images_out: Optional[List[Tuple[str, str]]] = None,
                 link_ctx: Optional[Tuple] = None,
                 reading_style: str = "after") -> Tuple[str, str]:
    """Markdown -> (title, body XHTML). One renderer for every caller.

    The builder understands exactly the construct set FORMAT.md documents --
    headings, paragraphs, verse blocks, images, endnotes, emphasis and
    `{word|reading}` annotations -- and nothing about who produced them."""
    md, footnote_defs = strip_footnote_defs(md)
    content_blocks = split_blocks(md)
    title = ""
    if content_blocks and content_blocks[0].startswith("# "):
        title = content_blocks[0][2:].strip()
        content_blocks = content_blocks[1:]

    footnote_order: Dict[str, int] = {}
    html_blocks: List[str] = []
    if title:
        html_blocks.append(f"<h1>{render_inline(title, footnote_order, prefix)}</h1>")

    for b in content_blocks:
        if b.startswith("```verse") and b.rstrip().endswith("```"):
            inner = b.split("\n", 1)[1] if "\n" in b else ""
            inner = inner.rsplit("```", 1)[0].rstrip("\n")
            lines_html = "".join(
                f"<p>{render_inline(ln, footnote_order, prefix, link_ctx, reading_style)}</p>"
                for ln in inner.split("\n")
            )
            html_blocks.append(f'<div class="verse">{lines_html}</div>')
        elif b.startswith("!["):
            m = IMAGE_RE.match(b.strip())
            if not m:
                raise BuildError(f"{prefix}: malformed image paragraph: {b!r}")
            html_blocks.append(image_paragraph(m.group(1), m.group(2), prefix, images_out))
        elif b.startswith("## "):
            html_blocks.append(f"<h2>{render_inline(b[3:].strip(), footnote_order, prefix)}</h2>")
        elif b.startswith("# "):
            html_blocks.append(f"<h1>{render_inline(b[2:].strip(), footnote_order, prefix)}</h1>")
        else:
            html_blocks.append(
                f"<p>{render_inline(b, footnote_order, prefix, link_ctx, reading_style)}</p>")

    html_blocks.append(endnotes_section(footnote_defs, footnote_order, prefix))
    return title or "Chapter", "\n".join(b for b in html_blocks if b)


def glossary_section(
    rows: List[Tuple[str, str, str]],
    prefix: str = "",
    linked: Optional[set] = None,
) -> str:
    """Render the end-of-chapter glossary. Each entry gets an id so words in the
    text can link to it; entries whose word was actually linked in the body get
    a ↩ back-link to the reading position.
    """
    if not rows:
        return ""
    items = []
    for idx, (word, pinyin, gloss) in enumerate(rows):
        back = (
            f' <a class="gback" href="#{prefix}-r{idx}">↩</a>'
            if linked is not None and idx in linked
            else ""
        )
        items.append(
            f'<li id="{prefix}-g{idx}"><span class="gw">{html.escape(word)}</span> '
            f'<span class="gp">{html.escape(pinyin)}</span> '
            f'<span class="gg">{html.escape(gloss)}</span>{back}</li>'
        )
    return (
        '<section class="glossary" epub:type="glossary">'
        "<h2>生词 New words</h2><ul>" + "".join(items) + "</ul></section>"
    )


# --------------------------------------------------------------------------- #
# EPUB packaging
# --------------------------------------------------------------------------- #
CSS = """\
html { -epub-hyphens: none; hyphens: none; }
body { font-family: serif; line-height: 1.9; margin: 1em; }
h1 { font-size: 1.4em; line-height: 2.4; }
h2 { font-size: 1.15em; line-height: 2.2; }
p { margin: 0 0 0.9em 0; text-indent: 2em; }
.w { white-space: nowrap; }              /* keep a word from breaking mid-token */
ruby rt { font-size: 0.5em; }
/* interlinear: stack pinyin over hanzi, each character a cell */
.c { display: inline-block; text-align: center; vertical-align: bottom; }
.c .py { display: block; font-size: 0.55em; line-height: 1.1; color: #444; }
.c .hz { display: block; }
.glossary { margin-top: 2em; border-top: 1px solid #999; padding-top: 0.5em; }
.glossary ul { list-style: none; padding-left: 0; }
.glossary li { margin: 0.2em 0; }
.gw { font-weight: bold; }
.gp { color: #555; margin: 0 0.4em; }
/* tappable glossary links: dotted underline under the word, hanzi color kept */
a.gl { color: inherit; text-decoration: underline; text-decoration-style: dotted; }
a.gback { text-decoration: none; color: #888; margin-left: 0.4em; }
/* gloss-* modes: marked glossary words in otherwise-plain text.
   The anchor carries no decoration; the <u> element (gloss-underline) or the
   trailing .wpy pinyin (gloss-pinyin) is the visible mark — chosen because
   they degrade gracefully on minimal engines like the X3's. */
a.glm { color: inherit; text-decoration: none; }
.wpy { font-size: 0.7em; color: #444; }
"""

# Appended when book.json asks for line_spacing: "tight".
TIGHT_SPACING_CSS = """\
/* On a small e-ink screen the reader adds space by choosing a bigger font or
   rotating to landscape; our job is to waste no vertical room. Overrides the
   base 1.9/2.4/2.2. */
body { line-height: 1.3; }
h1 { line-height: 1.3; margin: 0.3em 0 0.6em; }
h2 { line-height: 1.25; margin: 0.6em 0 0.3em; }
.verse { text-indent: 0; margin: 0 0 0.7em 1em; }
.verse p { margin: 0; text-indent: -1em; padding-left: 1em; }
figure { margin: 1em 0; text-align: center; }
figure img { max-width: 100%; }
figcaption { font-size: 0.85em; color: #555; margin-top: 0.3em; }
.endnotes { margin-top: 2em; border-top: 1px solid #999; padding-top: 0.5em; }
.endnotes ul { list-style: none; padding-left: 0; }
.endnotes li { margin: 0.2em 0; }
a.fnref { text-decoration: none; }
a.fnback { text-decoration: none; color: #888; margin-left: 0.4em; }
"""

XHTML_TMPL = """\
<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="{lang}" lang="{lang}">
<head><meta charset="utf-8"/><title>{title}</title>
<link rel="stylesheet" type="text/css" href="style.css"/></head>
<body>{body}</body>
</html>
"""

CONTAINER_XML = """\
<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>
"""


def build_opf(title: str, author: str, lang: str, chapters: List[Dict],
              images: Optional[Dict[str, Dict]] = None,
              series: str = "", series_index: str = "") -> str:
    manifest = ['<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
                '<item id="css" href="style.css" media-type="text/css"/>']
    spine = []
    for ch in chapters:
        manifest.append(f'<item id="{ch["id"]}" href="{ch["file"]}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="{ch["id"]}"/>')
    # Sorted so manifest order never depends on dict/insertion order.
    for epub_src in sorted(images or {}):
        info = images[epub_src]
        props = ' properties="cover-image"' if info.get("cover") else ""
        manifest.append(f'<item id="{info["id"]}" href="{epub_src}" media-type="{info["media_type"]}"{props}/>')
    import uuid
    # Stable id (derived from the title): rebuilding doesn't change the book's
    # identity, so readers replace a re-sideloaded copy instead of duplicating
    # it, and identical sources build byte-identical epubs.
    book_id = "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, "epub-builder:" + title))
    series_meta = ""
    if series:
        series_meta = (
            f'    <meta property="belongs-to-collection" id="series">'
            f'{html.escape(str(series))}</meta>\n'
            f'    <meta refines="#series" property="collection-type">series</meta>\n')
        if str(series_index).strip():
            series_meta += (f'    <meta refines="#series" property="group-position">'
                            f'{html.escape(str(series_index).strip())}</meta>\n')
    return f"""\
<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" xml:lang="{lang}">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">{book_id}</dc:identifier>
    <dc:title>{html.escape(title)}</dc:title>
    <dc:creator>{html.escape(author)}</dc:creator>
    <dc:language>{lang}</dc:language>
{series_meta}\
    <!-- fixed timestamp: same book source always builds a byte-identical epub -->
    <meta property="dcterms:modified">2026-01-01T00:00:00Z</meta>
  </metadata>
  <manifest>{"".join(manifest)}</manifest>
  <spine>{"".join(spine)}</spine>
</package>
"""


def build_nav(title: str, lang: str, chapters: List[Dict]) -> str:
    items = "".join(f'<li><a href="{c["file"]}">{html.escape(c["title"])}</a></li>' for c in chapters)
    body = f'<nav epub:type="toc" id="toc"><h1>{html.escape(title)}</h1><ol>{items}</ol></nav>'
    return XHTML_TMPL.format(lang=lang, title="Contents", body=body)


IMAGE_MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


def image_id(epub_src: str) -> str:
    return "img-" + re.sub(r"[^A-Za-z0-9]+", "-", epub_src).strip("-")


def write_epub(out: Path, title: str, author: str, lang: str, chapters: List[Dict],
               cover: Optional[Tuple[str, Path]] = None, line_spacing: str = "normal",
               series: str = "", series_index: str = "") -> None:
    out.parent.mkdir(parents=True, exist_ok=True)

    # Fixed zip mtimes (matching dcterms:modified) so identical sources really
    # do build byte-identical epubs — writestr's default stamps wall-clock time.
    def entry(name: str) -> zipfile.ZipInfo:
        return zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))

    # Images referenced from chapter bodies (empty for
    # annotated books, which never populate a chapter's "images" key) plus
    # an optional book.json cover, deduped by their in-EPUB path.
    images: Dict[str, Dict] = {}
    for ch in chapters:
        for epub_src, abs_path in ch.get("images", []):
            images.setdefault(epub_src, {
                "path": abs_path, "id": image_id(epub_src),
                "media_type": IMAGE_MEDIA_TYPES.get(Path(abs_path).suffix.lower(), "application/octet-stream"),
                "cover": False,
            })
    if cover is not None:
        epub_src, abs_path = cover
        info = images.setdefault(epub_src, {
            "path": abs_path, "id": image_id(epub_src),
            "media_type": IMAGE_MEDIA_TYPES.get(Path(abs_path).suffix.lower(), "application/octet-stream"),
            "cover": False,
        })
        info["cover"] = True

    css_text = CSS + TIGHT_SPACING_CSS if line_spacing == "tight" else CSS

    with zipfile.ZipFile(out, "w") as z:
        # mimetype MUST be first and stored (uncompressed)
        z.writestr(entry("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr(entry("META-INF/container.xml"), CONTAINER_XML, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr(entry("OEBPS/style.css"), css_text, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr(entry("OEBPS/content.opf"),
                   build_opf(title, author, lang, chapters, images,
                             series=series, series_index=series_index),
                   compress_type=zipfile.ZIP_DEFLATED)
        z.writestr(entry("OEBPS/nav.xhtml"), build_nav(title, lang, chapters), compress_type=zipfile.ZIP_DEFLATED)
        for ch in chapters:
            xhtml = XHTML_TMPL.format(lang=lang, title=html.escape(ch["title"]), body=ch["body"])
            z.writestr(entry("OEBPS/" + ch["file"]), xhtml, compress_type=zipfile.ZIP_DEFLATED)
        for epub_src in sorted(images):
            info = images[epub_src]
            z.writestr(entry("OEBPS/" + epub_src), Path(info["path"]).read_bytes(), compress_type=zipfile.ZIP_DEFLATED)


# --------------------------------------------------------------------------- #
# Glossary loading
# --------------------------------------------------------------------------- #
def load_glossary(path: Optional[Path]) -> List[Tuple[str, str, str]]:
    rows: List[Tuple[str, str, str]] = []
    if not path or not path.exists():
        return rows
    for i, raw in enumerate(path.read_text(encoding="utf-8").splitlines()):
        if not raw.strip() or raw.startswith("#"):
            continue
        parts = raw.split("\t")
        if i == 0 and parts[0].strip() == "word":
            continue
        w = parts[0].strip() if parts else ""
        if not w:
            continue
        p = parts[1].strip() if len(parts) > 1 else ""
        g = parts[2].strip() if len(parts) > 2 else ""
        rows.append((w, p, g))
    return rows


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
def assemble(book_dir: Path) -> Tuple[List[Dict], Dict]:
    """Build the chapter dicts. Everything the builder needs is in the book."""
    meta = json.loads((book_dir / "book.json").read_text(encoding="utf-8"))
    reading_style = meta.get("reading_style", "after")
    if reading_style not in READING_STYLES:
        raise BuildError(f"book.json reading_style must be one of {READING_STYLES}")
    chapters = []
    for idx, ch in enumerate(meta["chapters"], start=1):
        md = (book_dir / ch["source"]).read_text(encoding="utf-8")
        gloss_rows = load_glossary(book_dir / ch["glossary"]) if ch.get("glossary") else []
        prefix = f"ch{idx:02d}"
        link_map = {w: (i, p) for i, (w, p, _) in enumerate(gloss_rows)}
        linked: set = set()
        chapter_images: List[Tuple[str, str]] = []
        title, body = chapter_body(md, prefix, images_out=chapter_images,
                                    link_ctx=(prefix, link_map, linked),
                                    reading_style=reading_style)
        body += glossary_section(gloss_rows, prefix=prefix, linked=linked)
        resolved_images = []
        for rel_path, epub_src in chapter_images:
            src_path = ((book_dir / ch["source"]).parent / rel_path).resolve()
            if not src_path.is_file():
                raise BuildError(f"{ch['source']}: missing image {rel_path} (resolved {src_path})")
            resolved_images.append((epub_src, src_path))
        chapters.append({"id": prefix, "file": f"{prefix}.xhtml", "title": title, "body": body,
                          "images": resolved_images})
    return chapters, meta



def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Assemble an EPUB from the common book format (see FORMAT.md).")
    ap.add_argument("book_dir", type=Path, help="book directory containing book.json")
    ap.add_argument("--out", type=Path, required=True, help="output .epub path")
    args = ap.parse_args(argv)

    try:
        chapters, meta = assemble(args.book_dir)
        cover = None
        if meta.get("cover"):
            cover_path = (args.book_dir / meta["cover"]).resolve()
            if not cover_path.is_file():
                raise BuildError(f"book.json cover not found: {meta['cover']} (resolved {cover_path})")
            cover = (f"images/{cover_path.name}", cover_path)
    except BuildError as e:
        print(f"build error: {e}", file=sys.stderr)
        return 1

    write_epub(args.out, meta.get("title", "Book"), meta.get("author", ""),
               meta.get("language", "en"), chapters, cover=cover,
               line_spacing=meta.get("line_spacing", "normal"),
               series=meta.get("series", ""),
               series_index=meta.get("series_index", ""))
    print(f"wrote {args.out}  ({len(chapters)} sections, "
          f"readings: {meta.get('reading_style', 'after')}, "
          f"spacing: {meta.get('line_spacing', 'normal')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
