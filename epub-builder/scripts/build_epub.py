#!/usr/bin/env python3
"""Assemble a ruby/pinyin-annotated EPUB by hand (no epub library).

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

Use --diagnostic to emit ONE epub containing the first chapter rendered all
three ways (labeled), so you can sideload once and see what the device does.

A "book" is a directory:
  book.json            {title, author, language, pinyin_mode, chapters:[...]}
  chapters/<f>.md      chapter source (markdown-ish: # / ## headings + paragraphs)
  <glossary>.tsv       optional per-chapter new-word list (word,pinyin,gloss)

Usage:
  build_epub.py BOOKDIR --out book.epub [--pinyin-mode ruby|interlinear|plain]
  build_epub.py BOOKDIR --out diag.epub --diagnostic
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

# Pinyin annotation lives in the graded-reader task; the builder only pulls it
# in (jieba + pypinyin) when a book actually asks for a pinyin mode. Generic
# books (e.g. pdf2epub output) build with no CJK dependencies at all.
_GRADED_READER_SCRIPTS = Path(__file__).resolve().parents[2] / "graded-reader" / "scripts"
_vocab_mod = None


def vocab():
    global _vocab_mod
    if _vocab_mod is None:
        sys.path.insert(0, str(_GRADED_READER_SCRIPTS))
        import vocab as vocab_mod  # noqa: E402
        _vocab_mod = vocab_mod
    return _vocab_mod


# Pinyin modes; None = no annotation. The gloss-* modes render plain hanzi
# but mark each glossary word's first occurrence: underlined, or followed by
# word-level pinyin (猴子hóuzi). Device-tested on the X3: ruby leaks <rt>
# inline and interlinear's CSS stacking collapses, so plain/gloss-* are the
# CrossPoint-safe modes (see reference/readers.md).
MODES = ("ruby", "interlinear", "plain", "gloss-underline", "gloss-pinyin")
GLOSS_MODES = ("gloss-underline", "gloss-pinyin")


class BuildError(Exception):
    """An authoring error the builder catches instead of a raw traceback --
    a missing image or an undefined footnote ref should have been caught by
    prepare.py, but the builder is the last line of defense."""


# Where the per-chapter glossary sits relative to the chapter body. "before"
# is the pedagogical default (words previewed ahead of the story); a book or
# a single chapter entry can opt into "after" via book.json's
# glossary_position (see FORMAT.md).
GLOSSARY_POSITIONS = ("before", "after")


def chapter_glossary_position(ch: Dict, prefix: str) -> str:
    pos = ch.get("glossary_position", "before")
    if pos not in GLOSSARY_POSITIONS:
        raise BuildError(
            f"{prefix}: glossary_position must be 'before' or 'after', got {pos!r}")
    return pos


# --------------------------------------------------------------------------- #
# Annotation
# --------------------------------------------------------------------------- #
def char_pinyin(word: str) -> List[Tuple[str, str]]:
    """Per-character (char, syllable) pairs. Word-context tones via pypinyin.

    Non-Han characters come back with an empty syllable so callers can pass
    them through unannotated.
    """
    from pypinyin import pinyin, Style

    out: List[Tuple[str, str]] = []
    sylls = pinyin(word, style=Style.TONE, heteronym=False, errors="default")
    # pypinyin groups by character for Han; lengths line up for CJK text.
    if len(sylls) == len(word):
        for ch, s in zip(word, sylls):
            out.append((ch, s[0] if vocab()._is_han(ch) else ""))
    else:  # safety net: annotate Han chars individually
        for ch in word:
            if vocab()._is_han(ch):
                out.append((ch, pinyin(ch, style=Style.TONE)[0][0]))
            else:
                out.append((ch, ""))
    return out


def render_word(word: str, mode: str) -> str:
    """Render one segmented word as annotated XHTML for the given mode."""
    if not any(vocab()._is_han(c) for c in word):
        return html.escape(word)

    pairs = char_pinyin(word)
    if mode == "plain":
        return f'<span class="w">{html.escape(word)}</span>'

    if mode == "ruby":
        inner = "".join(
            f"<ruby>{html.escape(ch)}<rt>{html.escape(s)}</rt></ruby>" if s else html.escape(ch)
            for ch, s in pairs
        )
        return f'<span class="w">{inner}</span>'

    # interlinear: pinyin span stacked above hanzi span, no <ruby>
    cells = "".join(
        (
            f'<span class="c"><span class="py">{html.escape(s)}</span>'
            f'<span class="hz">{html.escape(ch)}</span></span>'
        )
        if s
        else f'<span class="c"><span class="py"></span>'
             f'<span class="hz">{html.escape(ch)}</span></span>'
        for ch, s in pairs
    )
    return f'<span class="w">{cells}</span>'


def render_paragraph(text: str, mode: str, link_ctx: Optional[Tuple] = None) -> str:
    """Render a paragraph; if link_ctx is given, hyperlink the first occurrence
    of each glossary word to its entry at the end of the chapter.

    link_ctx = (prefix, link_map, linked): link_map maps a glossary word to its
    row index; `linked` is a shared mutable set so only the FIRST occurrence in
    the chapter becomes a link (matches the gloss-once policy).

    mode None = no annotation: plain escaped text, no segmentation, no CJK
    dependencies. Glossary links need segmentation, so they only apply in
    annotated modes.
    """
    if mode is None:
        return html.escape(text)
    # gloss-* modes render body words plain; only the marking differs below.
    word_mode = "plain" if mode in GLOSS_MODES else mode
    words = vocab().segment(text)
    parts: List[str] = []
    for w in words:
        rendered = render_word(w, word_mode)
        if link_ctx:
            prefix, link_map, linked = link_ctx
            hit = link_map.get(w)
            if hit is not None and hit[0] not in linked:
                idx, row_pinyin = hit
                linked.add(idx)
                if mode == "gloss-underline":
                    rendered = (
                        f'<a class="glm" id="{prefix}-r{idx}" href="#{prefix}-g{idx}">'
                        f"<u>{rendered}</u></a>"
                    )
                elif mode == "gloss-pinyin":
                    # word-level pinyin, spaces stripped: 猴子hóuzi. Prefer the
                    # curated glossary pinyin; fall back to pypinyin.
                    py = (row_pinyin or "").replace(" ", "") or "".join(
                        s for _, s in char_pinyin(w))
                    rendered = (
                        f'<a class="glm" id="{prefix}-r{idx}" href="#{prefix}-g{idx}">'
                        f'{rendered}<span class="wpy">{html.escape(py)}</span></a>'
                    )
                else:
                    rendered = (
                        f'<a class="gl" id="{prefix}-r{idx}" href="#{prefix}-g{idx}">'
                        f"{rendered}</a>"
                    )
        parts.append(rendered)
    return "".join(parts)


# --------------------------------------------------------------------------- #
# Chapter markdown -> XHTML body
# --------------------------------------------------------------------------- #
def chapter_body(md: str, mode: str, link_ctx: Optional[Tuple] = None,
                  prefix: str = "ch01", images_out: Optional[List[Tuple[str, str]]] = None
                  ) -> Tuple[str, str]:
    """Return (title, body_html). First '# ' line is the chapter title.

    Glossary links (link_ctx) are applied only inside paragraphs, not headings.

    mode is None (no pinyin annotation) dispatches to chapter_body_plain(),
    which understands the pdf2epub extensions (verse/images/endnotes/
    emphasis) documented in epub-builder/FORMAT.md. The annotated path below
    is frozen -- its behavior and output bytes must not change.
    """
    if mode is None:
        return chapter_body_plain(md, prefix, images_out)

    title = ""
    blocks: List[str] = []
    for raw in md.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith("## "):
            blocks.append(f"<h2>{render_paragraph(line[3:].strip(), mode)}</h2>")
        elif line.startswith("# "):
            t = line[2:].strip()
            if not title:
                title = t
            blocks.append(f"<h1>{render_paragraph(t, mode)}</h1>")
        else:
            blocks.append(f"<p>{render_paragraph(line.strip(), mode, link_ctx)}</p>")
    return title or "Chapter", "\n".join(blocks)


# --- pdf2epub extensions (un-annotated / mode=None path only) -------------- #
FOOTNOTE_REF_RE = re.compile(r"\[\^([^\]]+)\]")
FOOTNOTE_DEF_RE = re.compile(r"^\[\^([^\]]+)\]:\s*(.*)$")
EMPHASIS_RE = re.compile(r"\*([^*]+)\*")
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


def render_inline_plain(text: str, footnote_order: Dict[str, int], prefix: str) -> str:
    """Emphasis + footnote refs for the un-annotated path. No segmentation,
    no glossary linking -- just the closed set FORMAT.md documents."""
    escaped = apply_emphasis(html.escape(text))

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


def chapter_body_plain(md: str, prefix: str,
                        images_out: Optional[List[Tuple[str, str]]]) -> Tuple[str, str]:
    """chapter_body() for mode=None: the pdf2epub construct set -- verse
    blocks, images, endnotes, *em* -- on top of the same heading/paragraph
    shape the annotated path uses."""
    md, footnote_defs = strip_footnote_defs(md)
    content_blocks = split_blocks(md)
    title = ""
    if content_blocks and content_blocks[0].startswith("# "):
        title = content_blocks[0][2:].strip()
        content_blocks = content_blocks[1:]

    footnote_order: Dict[str, int] = {}
    html_blocks: List[str] = []
    if title:
        html_blocks.append(f"<h1>{render_inline_plain(title, footnote_order, prefix)}</h1>")

    for b in content_blocks:
        if b.startswith("```verse") and b.rstrip().endswith("```"):
            inner = b.split("\n", 1)[1] if "\n" in b else ""
            inner = inner.rsplit("```", 1)[0].rstrip("\n")
            lines_html = "".join(
                f"<p>{render_inline_plain(ln, footnote_order, prefix)}</p>"
                for ln in inner.split("\n")
            )
            html_blocks.append(f'<div class="verse">{lines_html}</div>')
        elif b.startswith("!["):
            m = IMAGE_RE.match(b.strip())
            if not m:
                raise BuildError(f"{prefix}: malformed image paragraph: {b!r}")
            html_blocks.append(image_paragraph(m.group(1), m.group(2), prefix, images_out))
        elif b.startswith("## "):
            html_blocks.append(f"<h2>{render_inline_plain(b[3:].strip(), footnote_order, prefix)}</h2>")
        elif b.startswith("# "):
            html_blocks.append(f"<h1>{render_inline_plain(b[2:].strip(), footnote_order, prefix)}</h1>")
        else:
            html_blocks.append(f"<p>{render_inline_plain(b, footnote_order, prefix)}</p>")

    html_blocks.append(endnotes_section(footnote_defs, footnote_order, prefix))
    return title or "Chapter", "\n".join(b for b in html_blocks if b)


def glossary_section(
    rows: List[Tuple[str, str, str]],
    mode: str,
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
/* border/margin on both sides: the glossary can sit before or after the
   chapter body (book.json glossary_position), so it needs a divider on
   whichever edge touches the story text. */
.glossary { margin: 2em 0; border-top: 1px solid #999; border-bottom: 1px solid #999; padding: 0.5em 0; }
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

# Appended only for un-annotated (mode=None / pdf2epub) builds -- annotated
# books never see these bytes, so their style.css stays byte-identical.
PDF2EPUB_CSS = """\
.verse { text-indent: 0; margin: 0 0 0.9em 1em; }
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
              images: Optional[Dict[str, Dict]] = None) -> str:
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
    book_id = "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, "graded-reader:" + title))
    return f"""\
<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" xml:lang="{lang}">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">{book_id}</dc:identifier>
    <dc:title>{html.escape(title)}</dc:title>
    <dc:creator>{html.escape(author)}</dc:creator>
    <dc:language>{lang}</dc:language>
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
               cover: Optional[Tuple[str, Path]] = None, extended_css: bool = False) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)

    # Fixed zip mtimes (matching dcterms:modified) so identical sources really
    # do build byte-identical epubs — writestr's default stamps wall-clock time.
    def entry(name: str) -> zipfile.ZipInfo:
        return zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))

    # Images referenced from chapter bodies (pdf2epub extension; empty for
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

    css_text = CSS + PDF2EPUB_CSS if extended_css else CSS

    with zipfile.ZipFile(out, "w") as z:
        # mimetype MUST be first and stored (uncompressed)
        z.writestr(entry("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr(entry("META-INF/container.xml"), CONTAINER_XML, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr(entry("OEBPS/style.css"), css_text, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr(entry("OEBPS/content.opf"), build_opf(title, author, lang, chapters, images), compress_type=zipfile.ZIP_DEFLATED)
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
def assemble(book_dir: Path, mode: str) -> Tuple[List[Dict], Dict]:
    """Build the chapter dicts for a single-mode book."""
    meta = json.loads((book_dir / "book.json").read_text(encoding="utf-8"))
    chapters = []
    for idx, ch in enumerate(meta["chapters"], start=1):
        md = (book_dir / ch["source"]).read_text(encoding="utf-8")
        gloss_rows = load_glossary(book_dir / ch["glossary"]) if ch.get("glossary") else []
        prefix = f"ch{idx:02d}"
        link_map = {w: (i, p) for i, (w, p, _) in enumerate(gloss_rows)}
        linked: set = set()
        # images_out is only populated on the mode=None (pdf2epub) path;
        # chapter_body() ignores it entirely for annotated modes.
        chapter_images: List[Tuple[str, str]] = []
        title, body = chapter_body(md, mode, link_ctx=(prefix, link_map, linked),
                                    prefix=prefix, images_out=chapter_images)
        glossary_html = glossary_section(gloss_rows, mode, prefix=prefix, linked=linked)
        if chapter_glossary_position(ch, ch["source"]) == "before":
            body = glossary_html + body
        else:
            body = body + glossary_html
        resolved_images = []
        for rel_path, epub_src in chapter_images:
            src_path = ((book_dir / ch["source"]).parent / rel_path).resolve()
            if not src_path.is_file():
                raise BuildError(f"{ch['source']}: missing image {rel_path} (resolved {src_path})")
            resolved_images.append((epub_src, src_path))
        chapters.append({"id": prefix, "file": f"{prefix}.xhtml", "title": title, "body": body,
                          "images": resolved_images})
    return chapters, meta


def assemble_diagnostic(book_dir: Path) -> Tuple[List[Dict], Dict]:
    """One epub: first chapter rendered in all three modes, labeled."""
    meta = json.loads((book_dir / "book.json").read_text(encoding="utf-8"))
    ch = meta["chapters"][0]
    md = (book_dir / ch["source"]).read_text(encoding="utf-8")
    gloss_rows = load_glossary(book_dir / ch["glossary"]) if ch.get("glossary") else []
    chapters = []
    labels = {
        "ruby": "Mode 1 — RUBY (needs &lt;ruby&gt; support)",
        "interlinear": "Mode 2 — INTERLINEAR (CSS stacking, no ruby)",
        "plain": "Mode 3 — PLAIN (pinyin only in glossary)",
        "gloss-underline": "Mode 4 — GLOSS-UNDERLINE (glossary words underlined)",
        "gloss-pinyin": "Mode 5 — GLOSS-PINYIN (glossary words + word pinyin)",
    }
    for idx, mode in enumerate(MODES, start=1):
        # gloss-* modes need the link machinery to show their marking.
        link_ctx = None
        linked: set = set()
        if mode in GLOSS_MODES:
            link_map = {w: (i, p) for i, (w, p, _) in enumerate(gloss_rows)}
            link_ctx = (f"diag{idx}", link_map, linked)
        title, body = chapter_body(md, mode, link_ctx=link_ctx)
        banner = f'<h1>{labels[mode]}</h1><p style="text-indent:0;color:#777">If this page looks wrong, this mode is unsupported on your device.</p>'
        glossary_html = glossary_section(gloss_rows, mode, prefix=f"diag{idx}",
                                          linked=linked if mode in GLOSS_MODES else None)
        core = glossary_html + body if chapter_glossary_position(ch, ch["source"]) == "before" else body + glossary_html
        body = banner + core
        chapters.append({"id": f"diag{idx}", "file": f"diag{idx}.xhtml", "title": labels[mode].split("—")[0].strip(), "body": body})
    return chapters, meta


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Assemble an EPUB from the common book format (see FORMAT.md).")
    ap.add_argument("book_dir", type=Path, help="book directory containing book.json")
    ap.add_argument("--out", type=Path, required=True, help="output .epub path")
    ap.add_argument("--pinyin-mode", choices=MODES, default=None, help="override book.json pinyin_mode")
    ap.add_argument("--diagnostic", action="store_true", help="emit one epub with all three pinyin modes (X3 render test)")
    ap.add_argument("--lists", type=Path, default=None, help="vocab lists directory (annotated books only)")
    args = ap.parse_args(argv)

    meta_peek = json.loads((args.book_dir / "book.json").read_text(encoding="utf-8"))
    # Annotation is opt-in: book.json carries pinyin_mode (or the CLI forces
    # one). Books without it build plain — and never import jieba/pypinyin.
    mode = args.pinyin_mode or meta_peek.get("pinyin_mode")

    if mode is not None or args.diagnostic:
        vocab().load_vocab(args.lists or vocab().LISTS_DIR)  # configures jieba segmenter

    try:
        if args.diagnostic:
            chapters, meta = assemble_diagnostic(args.book_dir)
            title = meta.get("title", "Book") + " — pinyin render test"
            cover = None
        else:
            chapters, meta = assemble(args.book_dir, mode)
            title = meta.get("title", "Book")
            cover = None
            if meta.get("cover"):
                cover_path = (args.book_dir / meta["cover"]).resolve()
                if not cover_path.is_file():
                    raise BuildError(f"book.json cover not found: {meta['cover']} (resolved {cover_path})")
                cover = (f"images/{cover_path.name}", cover_path)
    except BuildError as e:
        print(f"build error: {e}", file=sys.stderr)
        return 1

    write_epub(args.out, title, meta.get("author", ""), meta.get("language", "en"), chapters,
               cover=cover, extended_css=(not args.diagnostic and mode is None))
    print(f"wrote {args.out}  ({len(chapters)} sections, {'diagnostic' if args.diagnostic else mode or 'plain (no annotation)'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
