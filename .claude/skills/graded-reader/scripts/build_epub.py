#!/usr/bin/env python3
"""Assemble a ruby/pinyin-annotated EPUB by hand (no epub library).

Hand-controlled XHTML is required because pinyin annotation modes differ at the
markup level, and because the mimetype must be stored first and uncompressed --
things heavy epub libraries hide from you.

Pinyin display is a parameter (the X3/CrossPoint renderer's ruby support is
unconfirmed, so we don't bet the structure on it):

  ruby         <ruby>汉<rt>hàn</rt></ruby>   -- compact, standard, needs a
               renderer that supports <ruby>. If the reader doesn't, the <rt>
               text typically leaks inline ("汉hàn").
  interlinear  stacked spans, pinyin on a line above the hanzi via CSS only --
               no <ruby> tag, so it renders anywhere. Safe e-ink fallback.
  plain        hanzi only; pinyin appears solely in the per-chapter glossary.

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
import sys
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vocab as vocab_mod  # noqa: E402

MODES = ("ruby", "interlinear", "plain")


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
            out.append((ch, s[0] if vocab_mod._is_han(ch) else ""))
    else:  # safety net: annotate Han chars individually
        for ch in word:
            if vocab_mod._is_han(ch):
                out.append((ch, pinyin(ch, style=Style.TONE)[0][0]))
            else:
                out.append((ch, ""))
    return out


def render_word(word: str, mode: str) -> str:
    """Render one segmented word as annotated XHTML for the given mode."""
    if not any(vocab_mod._is_han(c) for c in word):
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
    """
    words = vocab_mod.segment(text)
    parts: List[str] = []
    for w in words:
        rendered = render_word(w, mode)
        if link_ctx:
            prefix, link_map, linked = link_ctx
            idx = link_map.get(w)
            if idx is not None and idx not in linked:
                linked.add(idx)
                rendered = (
                    f'<a class="gl" id="{prefix}-r{idx}" href="#{prefix}-g{idx}">'
                    f"{rendered}</a>"
                )
        parts.append(rendered)
    return "".join(parts)


# --------------------------------------------------------------------------- #
# Chapter markdown -> XHTML body
# --------------------------------------------------------------------------- #
def chapter_body(md: str, mode: str, link_ctx: Optional[Tuple] = None) -> Tuple[str, str]:
    """Return (title, body_html). First '# ' line is the chapter title.

    Glossary links (link_ctx) are applied only inside paragraphs, not headings.
    """
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
.glossary { margin-top: 2em; border-top: 1px solid #999; padding-top: 0.5em; }
.glossary ul { list-style: none; padding-left: 0; }
.glossary li { margin: 0.2em 0; }
.gw { font-weight: bold; }
.gp { color: #555; margin: 0 0.4em; }
/* tappable glossary links: dotted underline under the word, hanzi color kept */
a.gl { color: inherit; text-decoration: underline; text-decoration-style: dotted; }
a.gback { text-decoration: none; color: #888; margin-left: 0.4em; }
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


def build_opf(title: str, author: str, lang: str, chapters: List[Dict]) -> str:
    manifest = ['<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
                '<item id="css" href="style.css" media-type="text/css"/>']
    spine = []
    for ch in chapters:
        manifest.append(f'<item id="{ch["id"]}" href="{ch["file"]}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="{ch["id"]}"/>')
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


def write_epub(out: Path, title: str, author: str, lang: str, chapters: List[Dict]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w") as z:
        # mimetype MUST be first and stored (uncompressed)
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", CONTAINER_XML, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/style.css", CSS, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/content.opf", build_opf(title, author, lang, chapters), compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/nav.xhtml", build_nav(title, lang, chapters), compress_type=zipfile.ZIP_DEFLATED)
        for ch in chapters:
            xhtml = XHTML_TMPL.format(lang=lang, title=html.escape(ch["title"]), body=ch["body"])
            z.writestr("OEBPS/" + ch["file"], xhtml, compress_type=zipfile.ZIP_DEFLATED)


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
        link_map = {w: i for i, (w, _, _) in enumerate(gloss_rows)}
        linked: set = set()
        title, body = chapter_body(md, mode, link_ctx=(prefix, link_map, linked))
        body += glossary_section(gloss_rows, mode, prefix=prefix, linked=linked)
        chapters.append({"id": prefix, "file": f"{prefix}.xhtml", "title": title, "body": body})
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
    }
    for idx, mode in enumerate(MODES, start=1):
        title, body = chapter_body(md, mode)
        banner = f'<h1>{labels[mode]}</h1><p style="text-indent:0;color:#777">If this page looks wrong, this mode is unsupported on your device.</p>'
        body = banner + body + glossary_section(gloss_rows, mode)
        chapters.append({"id": f"diag{idx}", "file": f"diag{idx}.xhtml", "title": labels[mode].split("—")[0].strip(), "body": body})
    return chapters, meta


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Assemble a pinyin-annotated EPUB by hand.")
    ap.add_argument("book_dir", type=Path, help="book directory containing book.json")
    ap.add_argument("--out", type=Path, required=True, help="output .epub path")
    ap.add_argument("--pinyin-mode", choices=MODES, default=None, help="override book.json pinyin_mode")
    ap.add_argument("--diagnostic", action="store_true", help="emit one epub with all three modes (X3 render test)")
    ap.add_argument("--lists", type=Path, default=vocab_mod.LISTS_DIR, help="lists directory")
    args = ap.parse_args(argv)

    vocab_mod.load_vocab(args.lists)  # configures jieba segmenter

    if args.diagnostic:
        chapters, meta = assemble_diagnostic(args.book_dir)
        title = meta.get("title", "Book") + " — pinyin render test"
    else:
        mode = args.pinyin_mode or json.loads((args.book_dir / "book.json").read_text(encoding="utf-8")).get("pinyin_mode", "interlinear")
        chapters, meta = assemble(args.book_dir, mode)
        title = meta.get("title", "Book")

    write_epub(args.out, title, meta.get("author", "Graded Reader Pipeline"), meta.get("language", "zh-CN"), chapters)
    print(f"wrote {args.out}  ({len(chapters)} sections, {'diagnostic' if args.diagnostic else args.pinyin_mode or meta.get('pinyin_mode','interlinear')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
