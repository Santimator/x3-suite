#!/usr/bin/env python3
"""Stage 1 (scanned route) of pdf2epub: OCR page renders into the same
pages.jsonl shape extract_text.py produces, so restore.py doesn't care
which route filled it in.

Renders each page via render_pages.render_page() (import, not subprocess --
one rendering path for both the agent's eyes and OCR), then runs
pytesseract.image_to_data per page, grouping word boxes into lines by
tesseract's own (block, par, line) numbering. Pixel boxes are converted
back to PDF points (scale 72/dpi) so downstream geometry (restore.py's
reflow thresholds) means the same thing regardless of extraction route.

Usage:
  extract_ocr.py SOURCE.pdf --out EXTRACTDIR --lang spa [--dpi 300] [--psm 6] [--pages A-B]
"""
import argparse
import json
import shutil
import statistics
import sys
from pathlib import Path

import pypdfium2 as pdfium

sys.path.insert(0, str(Path(__file__).resolve().parent))
from render_pages import parse_pages, render_page  # noqa: E402


def get_pytesseract():
    """Return (module, None) or (None, one-line install hint)."""
    try:
        import pytesseract
    except ImportError:
        return None, "pytesseract not installed: .venv/bin/pip install pytesseract"
    if shutil.which("tesseract") is None:
        return None, "tesseract binary not found: apt install tesseract-ocr tesseract-ocr-<lang>"
    return pytesseract, None


def ocr_page(pytesseract, img, dpi: int, lang: str, psm: int):
    scale = 72.0 / dpi
    data = pytesseract.image_to_data(
        img, lang=lang, config=f"--psm {psm}", output_type=pytesseract.Output.DICT
    )
    n = len(data["text"])
    grouped = {}
    order = []
    for i in range(n):
        text = data["text"][i].strip()
        conf = float(data["conf"][i])
        if not text or conf < 0:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append({
            "text": text, "left": data["left"][i], "top": data["top"][i],
            "width": data["width"][i], "height": data["height"][i], "conf": conf,
        })

    lines = []
    for key in order:
        words = sorted(grouped[key], key=lambda w: w["left"])
        x0 = min(w["left"] for w in words)
        top = min(w["top"] for w in words)
        x1 = max(w["left"] + w["width"] for w in words)
        bottom = max(w["top"] + w["height"] for w in words)
        size = max(w["height"] for w in words)
        lines.append({
            "text": " ".join(w["text"] for w in words),
            "x0": round(x0 * scale, 1),
            "top": round(top * scale, 1),
            "x1": round(x1 * scale, 1),
            "bottom": round(bottom * scale, 1),
            "size": round(size * scale, 1),
            "font": "ocr",
            "conf": round(statistics.mean(w["conf"] for w in words), 1),
        })
    # Reading order isn't guaranteed by (block, par, line) iteration order for
    # every layout; sort top-to-bottom so it always is.
    lines.sort(key=lambda l: l["top"])

    all_confs = [w["conf"] for words in grouped.values() for w in words]
    mean_conf = round(statistics.mean(all_confs), 1) if all_confs else 0.0
    return lines, mean_conf


def run(pdf_path: Path, out_dir: Path, pages_spec, lang: str, dpi: int, psm: int) -> dict:
    pytesseract, err = get_pytesseract()
    if pytesseract is None:
        raise RuntimeError(err)

    out_dir.mkdir(parents=True, exist_ok=True)
    pages_path = out_dir / "pages.jsonl"
    report_path = out_dir / "extract-report.json"

    pdf = pdfium.PdfDocument(pdf_path)
    page_nums = parse_pages(pages_spec, len(pdf))

    per_page_chars = {}
    per_page_conf = {}
    total_lines = 0

    with pages_path.open("w", encoding="utf-8") as fh:
        for page_num in page_nums:
            width, height = pdf[page_num - 1].get_size()
            img = render_page(pdf, page_num, dpi)
            lines, mean_conf = ocr_page(pytesseract, img, dpi, lang, psm)
            record = {
                "page": page_num,
                "width": round(width, 1),
                "height": round(height, 1),
                "dedupe_applied": False,
                "lines": lines,
                "images": [],
            }
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            per_page_chars[str(page_num)] = sum(len(l["text"].replace(" ", "")) for l in lines)
            per_page_conf[str(page_num)] = mean_conf
            total_lines += len(lines)

    report = {
        "source": str(pdf_path),
        "lang": lang,
        "dpi": dpi,
        "psm": psm,
        "pages_processed": len(page_nums),
        "total_lines": total_lines,
        "total_chars": sum(per_page_chars.values()),
        "per_page_chars": per_page_chars,
        "per_page_conf": per_page_conf,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def summarize(report: dict) -> str:
    confs = list(report["per_page_conf"].values())
    mean_conf = round(statistics.mean(confs), 1) if confs else 0.0
    low = [p for p, c in report["per_page_conf"].items() if c < 60]
    return (
        f"{report['pages_processed']} pages OCR'd ({report['lang']}, {report['dpi']} dpi), "
        f"{report['total_lines']} lines, {report['total_chars']} chars total\n"
        f"mean confidence {mean_conf} -- low-confidence pages (<60): {low or 'none'}\n"
        f"wrote pages.jsonl + extract-report.json"
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--out", type=Path, required=True, help="extraction output directory")
    ap.add_argument("--lang", required=True, help="tesseract language code, e.g. spa")
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--psm", type=int, default=6, help="tesseract page segmentation mode")
    ap.add_argument("--pages", help="page range A-B, 1-indexed inclusive (default: all)")
    args = ap.parse_args()

    try:
        report = run(args.pdf, args.out, args.pages, args.lang, args.dpi, args.psm)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1

    print(summarize(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
