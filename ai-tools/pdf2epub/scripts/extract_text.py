#!/usr/bin/env python3
"""pdf2epub: pull per-page lines from a PDF's text layer, in reading order.

Deterministic extraction only -- this never repairs meaning, only recovers
what the text layer already encodes (glyph positions, sizes, fonts). Its output
has two readers: the model writing a chunk (a spelling reference next to the
page image) and check_chunk.py (the fidelity baseline the chunk is gated
against). Both need *reading order*, which a text layer does not promise.

Mechanical fixes, all geometric, none a content decision:
  - char dedupe (fake-bold double draw: coincident glyphs);
  - column split: a two-column page comes out of pdfplumber with the columns
    interleaved line by line ("left line 1 + right line 1"). A vertical gutter
    no body line crosses is detected per page; lines are cut at it and emitted
    left column, then right, between full-width lines (titles) that keep their
    place. `--columns off` disables it.
  - optional space recovery (`--space-recover`) for text layers with no space
    glyphs.

Writes EXTRACTDIR/pages.jsonl (lines with geometry; `col` L/R/F) and
EXTRACTDIR/text/pNNN.txt (the page's plain text in reading order).

Usage:
  extract_text.py SOURCE.pdf --out EXTRACTDIR [--pages A-B] [--dedupe auto|on|off]
                  [--columns auto|off]
"""
import argparse
import collections
import json
import re
import statistics
import sys
from pathlib import Path

import pdfplumber
from pdfplumber.utils import extract_text as chars_to_text

WHITESPACE = re.compile(r"\s+")

# Same rule triage.py uses to flag doubled_chars: dedupe collapsing more than
# 30% of chars means the page is fake-bold double-drawn.
DEDUPE_DROP_THRESHOLD = 0.7

# A word gap, as a fraction of the font size. pdfplumber's fixed 3pt default
# glues words on tightly justified 9pt text (gaps measured at 2.99pt).
X_TOLERANCE_RATIO = 0.15


def parse_pages(spec, n_pages):
    if not spec:
        return list(range(1, n_pages + 1))
    a, _, b = spec.partition("-")
    lo = int(a)
    hi = int(b) if b else lo
    return list(range(lo, hi + 1))


def should_dedupe(page, mode):
    if mode == "on":
        return True
    if mode == "off":
        return False
    raw = len(page.chars)
    if raw == 0:
        return False
    return len(page.dedupe_chars().chars) / raw < DEDUPE_DROP_THRESHOLD


def reconstruct_line_text(chars, ratio):
    """Rebuild a line's text from its glyph positions, inserting a space
    wherever the gap between two adjacent glyphs exceeds `ratio` times the
    line's median glyph width. For born-digital PDFs that render justified
    text with no space glyphs, pdfplumber's default line text comes out
    word-runtogether ('TheTransformerfollows...'); this recovers the spaces
    from geometry alone. Existing space glyphs are preserved (never doubled),
    so it's safe to apply, but it's opt-in — the agent turns it on when triage
    flags broken_spacing."""
    cs = sorted(chars, key=lambda c: c["x0"])
    widths = [c["x1"] - c["x0"] for c in cs if c["x1"] > c["x0"]]
    tol = ratio * (statistics.median(widths) if widths else 2.0)
    out = []
    prev = None
    for c in cs:
        if prev is not None and (c["x0"] - prev["x1"]) > tol \
                and not c["text"].isspace() and (not out or not out[-1].isspace()):
            out.append(" ")
        out.append(c["text"])
        prev = c
    return "".join(out)


def find_gutter(chars, width):
    """(g0, g1) of a vertical band, centred in the middle 40% of the page, that
    almost no glyph covers -- the gap between two text columns -- or None.

    Coverage is counted per 1pt bin. Body text covers every bin of its column
    once per line; a title crossing the gutter covers it a handful of times.
    So a bin counts as empty below 15% of the typical covered bin."""
    bins = [0] * (int(width) + 2)
    for c in chars:
        if c["text"].isspace():
            continue
        for x in range(max(0, int(c["x0"])), min(len(bins), int(c["x1"]) + 1)):
            bins[x] += 1
    covered = sorted(b for b in bins if b)
    if not covered:
        return None
    limit = 0.15 * covered[len(covered) // 2]
    best, start = None, None
    for x, b in enumerate(bins + [limit + 1]):
        if b <= limit and start is None:
            start = x
        elif b > limit and start is not None:
            g0, g1 = start, x
            centre = (g0 + g1) / 2
            if g1 - g0 >= 6 and 0.3 * width <= centre <= 0.7 * width:
                if best is None or g1 - g0 > best[1] - best[0]:
                    best = (g0, g1)
            start = None
    return best


def split_at_gutter(chars, gutter):
    """A pdfplumber line straddling the gutter is either a full-width line (a
    title: glyphs run continuously across) or two column lines that happen to
    share a baseline (a glyph gap spans the gutter). Returns [("F", chars)] or
    the non-empty of [("L", left), ("R", right)]."""
    g0, g1 = gutter
    mid = (g0 + g1) / 2
    left = [c for c in chars if (c["x0"] + c["x1"]) / 2 < mid]
    right = [c for c in chars if (c["x0"] + c["x1"]) / 2 >= mid]
    if not left or not right:
        return [("L" if left else "R", chars)]
    left_edge = max(c["x1"] for c in left if not c["text"].isspace()) \
        if any(not c["text"].isspace() for c in left) else max(c["x1"] for c in left)
    right_edge = min(c["x0"] for c in right if not c["text"].isspace()) \
        if any(not c["text"].isspace() for c in right) else min(c["x0"] for c in right)
    if right_edge - left_edge >= 0.6 * (g1 - g0):
        return [("L", left), ("R", right)]
    return [("F", chars)]


def line_record(chars, col, space_recover, space_ratio):
    chars = sorted(chars, key=lambda c: c["x0"])
    sizes = [c["size"] for c in chars]
    fonts = collections.Counter(c["fontname"] for c in chars)
    text = (reconstruct_line_text(chars, space_ratio) if space_recover
            else chars_to_text(chars, x_tolerance_ratio=X_TOLERANCE_RATIO)).strip()
    return {
        "text": text,
        "col": col,
        "x0": round(min(c["x0"] for c in chars), 1),
        "top": round(min(c["top"] for c in chars), 1),
        "x1": round(max(c["x1"] for c in chars), 1),
        "bottom": round(max(c["bottom"] for c in chars), 1),
        "size": round(statistics.median(sizes), 1) if sizes else 0.0,
        "font": fonts.most_common(1)[0][0] if fonts else "",
    }


def reading_order(lines):
    """Full-width lines keep their vertical place; between two of them, the
    left column's lines come first, then the right's."""
    out, band = [], []

    def flush():
        band.sort(key=lambda r: (r["col"] != "L", r["top"]))
        out.extend(band)
        band.clear()

    for r in sorted(lines, key=lambda r: r["top"]):
        if r["col"] == "F":
            flush()
            out.append(r)
        else:
            band.append(r)
    flush()
    return out


def extract_page(page, page_num, dedupe_mode, space_recover, space_ratio,
                 columns="auto"):
    applied = should_dedupe(page, dedupe_mode)
    work = page.dedupe_chars() if applied else page

    gutter = find_gutter(work.chars, page.width) if columns == "auto" else None
    lines = []
    for ln in work.extract_text_lines():
        parts = split_at_gutter(ln["chars"], gutter) if gutter else [("F", ln["chars"])]
        for col, chars in parts:
            rec = line_record(chars, col, space_recover, space_ratio)
            if rec["text"]:
                lines.append(rec)
    if gutter:
        lines = reading_order(lines)

    images = [
        {
            "x0": round(im["x0"], 1),
            "top": round(im["top"], 1),
            "x1": round(im["x1"], 1),
            "bottom": round(im["bottom"], 1),
        }
        for im in page.images
    ]

    chars_count = sum(len(WHITESPACE.sub("", ln["text"])) for ln in lines)

    record = {
        "page": page_num,
        "width": round(page.width, 1),
        "height": round(page.height, 1),
        "dedupe_applied": applied,
        "gutter": list(gutter) if gutter else None,
        "lines": lines,
        "images": images,
    }
    return record, chars_count


def run(pdf_path: Path, out_dir: Path, pages_spec, dedupe_mode,
        space_recover=False, space_ratio=0.4, columns="auto") -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "text").mkdir(exist_ok=True)
    pages_path = out_dir / "pages.jsonl"
    report_path = out_dir / "extract-report.json"

    dedupe_pages = []
    column_pages = []
    per_page_chars = {}
    total_lines = 0

    with pdfplumber.open(pdf_path) as pdf, pages_path.open("w", encoding="utf-8") as fh:
        page_nums = parse_pages(pages_spec, len(pdf.pages))
        for page_num in page_nums:
            page = pdf.pages[page_num - 1]
            record, chars_count = extract_page(page, page_num, dedupe_mode,
                                               space_recover, space_ratio, columns)
            (out_dir / "text" / f"p{page_num:03d}.txt").write_text(
                "\n".join(ln["text"] for ln in record["lines"]) + "\n", encoding="utf-8")
            if record["dedupe_applied"]:
                dedupe_pages.append(page_num)
            if record["gutter"]:
                column_pages.append(page_num)
            per_page_chars[str(page_num)] = chars_count
            total_lines += len(record["lines"])
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    report = {
        "source": str(pdf_path),
        "pages_processed": len(page_nums),
        "dedupe_pages": dedupe_pages,
        "two_column_pages": column_pages,
        "total_lines": total_lines,
        "total_chars": sum(per_page_chars.values()),
        "per_page_chars": per_page_chars,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def summarize(report: dict) -> str:
    return (
        f"{report['pages_processed']} pages processed, "
        f"{len(report['dedupe_pages'])} deduped, "
        f"{report['total_lines']} lines, {report['total_chars']} chars total\n"
        f"dedupe pages: {report['dedupe_pages'] or 'none'}\n"
        f"two-column pages: {report['two_column_pages'] or 'none'}\n"
        f"wrote pages.jsonl + text/pNNN.txt + extract-report.json"
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--out", type=Path, required=True, help="extraction output directory")
    ap.add_argument("--pages", help="page range A-B, 1-indexed inclusive (default: all)")
    ap.add_argument("--dedupe", choices=("auto", "on", "off"), default="auto",
                     help="auto: dedupe pages where it drops >30%% of chars (default)")
    ap.add_argument("--space-recover", action="store_true",
                     help="rebuild each line's spaces from glyph gaps — for born-digital "
                          "PDFs whose text layer runs words together (triage: broken_spacing)")
    ap.add_argument("--space-ratio", type=float, default=0.4,
                     help="space if a glyph gap exceeds this * median glyph width (default 0.4)")
    ap.add_argument("--columns", choices=("auto", "off"), default="auto",
                     help="auto: detect a two-column gutter per page and emit reading order")
    args = ap.parse_args()

    report = run(args.pdf, args.out, args.pages, args.dedupe,
                 args.space_recover, args.space_ratio, args.columns)
    print(summarize(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
