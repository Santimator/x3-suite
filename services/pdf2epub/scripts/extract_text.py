#!/usr/bin/env python3
"""Stage 1 (text-layer route) of pdf2epub: pull per-page lines from a PDF's
text layer.

Deterministic extraction only -- this never repairs meaning, only recovers
what the text layer already encodes (glyph positions, sizes, fonts) into a
per-page line structure the agent and restore.py can reason about. Char
dedupe (fake-bold double-draw) is the one mechanical fix applied here,
because it is purely geometric (coincident glyphs), not a content decision.

Usage:
  extract_text.py SOURCE.pdf --out EXTRACTDIR [--pages A-B] [--dedupe auto|on|off]
"""
import argparse
import collections
import json
import re
import statistics
import sys
from pathlib import Path

import pdfplumber

WHITESPACE = re.compile(r"\s+")

# Same rule triage.py uses to flag doubled_chars: dedupe collapsing more than
# 30% of chars means the page is fake-bold double-drawn.
DEDUPE_DROP_THRESHOLD = 0.7


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


def extract_page(page, page_num, dedupe_mode, space_recover, space_ratio):
    applied = should_dedupe(page, dedupe_mode)
    work = page.dedupe_chars() if applied else page

    lines = []
    for ln in work.extract_text_lines():
        chars = ln["chars"]
        sizes = [c["size"] for c in chars]
        fonts = collections.Counter(c["fontname"] for c in chars)
        text = reconstruct_line_text(chars, space_ratio) if space_recover else ln["text"]
        lines.append({
            "text": text,
            "x0": round(ln["x0"], 1),
            "top": round(ln["top"], 1),
            "x1": round(ln["x1"], 1),
            "bottom": round(ln["bottom"], 1),
            "size": round(statistics.median(sizes), 1) if sizes else 0.0,
            "font": fonts.most_common(1)[0][0] if fonts else "",
        })

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
        "lines": lines,
        "images": images,
    }
    return record, chars_count


def run(pdf_path: Path, out_dir: Path, pages_spec, dedupe_mode,
        space_recover=False, space_ratio=0.4) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    pages_path = out_dir / "pages.jsonl"
    report_path = out_dir / "extract-report.json"

    dedupe_pages = []
    per_page_chars = {}
    total_lines = 0

    with pdfplumber.open(pdf_path) as pdf, pages_path.open("w", encoding="utf-8") as fh:
        page_nums = parse_pages(pages_spec, len(pdf.pages))
        for page_num in page_nums:
            page = pdf.pages[page_num - 1]
            record, chars_count = extract_page(page, page_num, dedupe_mode,
                                               space_recover, space_ratio)
            if record["dedupe_applied"]:
                dedupe_pages.append(page_num)
            per_page_chars[str(page_num)] = chars_count
            total_lines += len(record["lines"])
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    report = {
        "source": str(pdf_path),
        "pages_processed": len(page_nums),
        "dedupe_pages": dedupe_pages,
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
        f"wrote pages.jsonl + extract-report.json"
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
    args = ap.parse_args()

    report = run(args.pdf, args.out, args.pages, args.dedupe,
                 args.space_recover, args.space_ratio)
    print(summarize(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
