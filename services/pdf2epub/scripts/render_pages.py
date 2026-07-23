#!/usr/bin/env python3
"""pdf2epub toolbox: render PDF pages to grayscale PNGs.

This exists so the agent can *look* at a page (Read tool on the PNG) when
text extraction makes no sense, and as the rendering step OCR builds on.
No text is produced here -- render_page() is imported by extract_ocr.py
rather than shelled out to, so the two stay in lockstep on how a page
becomes pixels.

Usage:
  render_pages.py SOURCE.pdf --out DIR [--pages A-B] [--dpi 150]
"""
import argparse
import sys
from pathlib import Path

import pypdfium2 as pdfium


def parse_pages(spec, n_pages):
    if not spec:
        return list(range(1, n_pages + 1))
    a, _, b = spec.partition("-")
    lo = int(a)
    hi = int(b) if b else lo
    return list(range(lo, hi + 1))


def render_page(pdf: pdfium.PdfDocument, page_num: int, dpi: int):
    """Render one 1-indexed page to a grayscale PIL image."""
    page = pdf[page_num - 1]
    bitmap = page.render(scale=dpi / 72, grayscale=True)
    return bitmap.to_pil()


def run(pdf_path: Path, out_dir: Path, pages_spec, dpi: int) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf = pdfium.PdfDocument(pdf_path)
    page_nums = parse_pages(pages_spec, len(pdf))
    written = []
    for page_num in page_nums:
        img = render_page(pdf, page_num, dpi)
        out_path = out_dir / f"p{page_num:03d}.png"
        img.save(out_path)
        written.append(out_path.name)
    return {"source": str(pdf_path), "dpi": dpi, "pages": written}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--out", type=Path, required=True, help="output directory for pNNN.png files")
    ap.add_argument("--pages", help="page range A-B, 1-indexed inclusive (default: all)")
    ap.add_argument("--dpi", type=int, default=150)
    args = ap.parse_args()

    result = run(args.pdf, args.out, args.pages, args.dpi)
    print(f"rendered {len(result['pages'])} pages at {result['dpi']} dpi to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
