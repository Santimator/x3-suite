#!/usr/bin/env python3
"""Stage 3->4 boundary of pdf2epub: validate draft.json against restored.md
and cut chapters/*.md + book.json + images/, exactly the shape
epub-builder/FORMAT.md specifies.

draft.json is the agent's whole creative output for this pipeline -- title,
author, chapter boundaries as verbatim text anchors, image placements. This
script does not interpret intent; it *checks* the draft's claims (anchors
exist, are unique, are in order; image refs resolve) and then cuts text
mechanically. A bad anchor is a validation error precise enough to fix, not
a guess.

Usage:
  prepare.py workspace/<slug> [--restored RESTOREDIR]   # expects draft.json in the book dir
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from render_pages import render_page  # noqa: E402


class PrepareError(Exception):
    pass


REQUIRED_DRAFT_KEYS = ("title", "author", "language", "chapters", "front_matter")


def load_paragraphs(restored_md_path: Path):
    """Recover restore.py's paragraph/verse units. render_markdown() joins
    them with a blank line and a trailing newline; a verse block's internal
    newlines are never doubled, so splitting on "\\n\\n" recovers exactly
    the original unit list."""
    text = restored_md_path.read_text(encoding="utf-8").rstrip("\n")
    return text.split("\n\n") if text else []


def validate_draft(draft: dict):
    missing = [k for k in REQUIRED_DRAFT_KEYS if k not in draft]
    if missing:
        raise PrepareError(f"draft.json missing required keys: {missing}")
    if draft["front_matter"] != "drop":
        raise PrepareError(
            f"unsupported front_matter value {draft['front_matter']!r} (only 'drop' is implemented)"
        )
    if not draft["chapters"]:
        raise PrepareError("draft.json has no chapters")
    for i, ch in enumerate(draft["chapters"]):
        for key in ("toc_label", "start_anchor"):
            if key not in ch:
                raise PrepareError(f"chapter {i + 1} missing {key!r}")


def locate_anchor(text: str, anchor: str, label: str) -> int:
    count = text.count(anchor)
    if count == 0:
        raise PrepareError(f"{label}: anchor not found: {anchor!r}")
    if count > 1:
        raise PrepareError(f"{label}: anchor ambiguous ({count} hits): {anchor!r}")
    return text.index(anchor)


def offset_to_paragraph(paragraphs, offsets, char_offset: int) -> int:
    """Which paragraph index contains a character offset into the joined
    (by "\\n\\n") restored text."""
    for idx in range(len(paragraphs) - 1, -1, -1):
        if char_offset >= offsets[idx]:
            return idx
    return 0


def paragraph_offsets(paragraphs):
    offsets = []
    pos = 0
    for p in paragraphs:
        offsets.append(pos)
        pos += len(p) + 2  # + the "\n\n" separator render_markdown used
    return offsets


def assign_chapters(draft, paragraphs, offsets, restored_text):
    """Validate chapter anchors and return (start_para_idx per chapter,
    front_matter_paragraph_count)."""
    starts = []
    for i, ch in enumerate(draft["chapters"], start=1):
        char_off = locate_anchor(restored_text, ch["start_anchor"], f"chapter {i}")
        starts.append(offset_to_paragraph(paragraphs, offsets, char_off))

    for i in range(1, len(starts)):
        if starts[i] <= starts[i - 1]:
            raise PrepareError(
                f"anchors out of order: chapter {i + 1}'s anchor does not come after "
                f"chapter {i}'s in restored.md"
            )

    front_matter_count = starts[0]
    return starts, front_matter_count


def load_extract_images(extract_dir: Path, page: int):
    pages_path = extract_dir / "pages.jsonl"
    for raw in pages_path.read_text(encoding="utf-8").splitlines():
        rec = json.loads(raw)
        if rec["page"] == page:
            return rec["images"]
    raise PrepareError(f"page {page} not found in {pages_path}")


def prepare_image(source_pdf: Path, extract_dir: Path, images_dir: Path,
                   image_entry: dict, fig_num: int, dpi: int = 300, max_width: int = 480) -> str:
    import pypdfium2 as pdfium
    from PIL import Image

    page_num = image_entry["page"]
    idx = image_entry["index"]
    page_images = load_extract_images(extract_dir, page_num)
    if idx >= len(page_images):
        raise PrepareError(f"image index {idx} out of range on page {page_num} (has {len(page_images)})")
    bbox = page_images[idx]

    pdf = pdfium.PdfDocument(source_pdf)
    rendered = render_page(pdf, page_num, dpi)
    scale = dpi / 72.0
    crop_box = (
        round(bbox["x0"] * scale), round(bbox["top"] * scale),
        round(bbox["x1"] * scale), round(bbox["bottom"] * scale),
    )
    cropped = rendered.crop(crop_box).convert("L")
    if cropped.width > max_width:
        new_height = round(cropped.height * max_width / cropped.width)
        cropped = cropped.resize((max_width, new_height), Image.LANCZOS)

    images_dir.mkdir(parents=True, exist_ok=True)
    filename = f"fig{fig_num:02d}.png"
    cropped.save(images_dir / filename)
    return filename


def prepare(book_dir: Path, restored_dir: Path):
    draft = json.loads((book_dir / "draft.json").read_text(encoding="utf-8"))
    validate_draft(draft)

    restored_text_raw = (restored_dir / "restored.md").read_text(encoding="utf-8")
    paragraphs = load_paragraphs(restored_dir / "restored.md")
    offsets = paragraph_offsets(paragraphs)
    restored_text = restored_text_raw.rstrip("\n")

    starts, front_matter_count = assign_chapters(draft, paragraphs, offsets, restored_text)
    ends = starts[1:] + [len(paragraphs)]

    # Image insertions: anchor -> paragraph index -> list of markdown lines,
    # inserted (in draft order) right after that paragraph.
    insertions = {}
    images_report = []
    extract_dir = book_dir / "extract"
    source_pdf = book_dir / "source.pdf"
    images_dir = book_dir / "images"
    for n, img in enumerate(draft.get("images", []), start=1):
        char_off = locate_anchor(restored_text, img["anchor"], f"image {n}")
        para_idx = offset_to_paragraph(paragraphs, offsets, char_off)
        if para_idx < front_matter_count:
            raise PrepareError(f"image {n} anchor falls in dropped front matter: {img['anchor']!r}")
        filename = prepare_image(source_pdf, extract_dir, images_dir, img, n)
        insertions.setdefault(para_idx, []).append(f"![{img.get('caption', '')}](../images/{filename})")
        images_report.append({"file": f"images/{filename}", "page": img["page"], "anchor": img["anchor"]})

    chapters_dir = book_dir / "chapters"
    chapters_dir.mkdir(parents=True, exist_ok=True)
    book_chapters = []
    report_chapters = []

    for i, ch in enumerate(draft["chapters"], start=1):
        lo, hi = starts[i - 1], ends[i - 1]
        blocks = [f"# {ch['toc_label']}"]
        for p in range(lo, hi):
            blocks.append(paragraphs[p])
            for extra in insertions.get(p, []):
                blocks.append(extra)
        filename = f"ch{i:02d}.md"
        (chapters_dir / filename).write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
        book_chapters.append({"source": f"chapters/{filename}"})
        report_chapters.append({"file": f"chapters/{filename}", "toc_label": ch["toc_label"], "paragraphs": hi - lo})

    book = {
        "title": draft["title"],
        "author": draft["author"],
        "language": draft["language"],
        "chapters": book_chapters,
    }
    (book_dir / "book.json").write_text(json.dumps(book, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    total_assigned = sum(rc["paragraphs"] for rc in report_chapters) + front_matter_count
    if total_assigned != len(paragraphs):
        raise PrepareError(
            f"paragraph accounting mismatch: {total_assigned} assigned "
            f"(chapters + dropped front matter) != {len(paragraphs)} total in restored.md"
        )

    report = {
        "total_paragraphs_in_restored": len(paragraphs),
        "front_matter_dropped_paragraphs": front_matter_count,
        "chapters": report_chapters,
        "images_prepared": images_report,
    }
    (book_dir / "prepare-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def summarize(report: dict) -> str:
    lines = [
        f"{len(report['chapters'])} chapters, "
        f"{report['total_paragraphs_in_restored']} paragraphs total "
        f"({report['front_matter_dropped_paragraphs']} dropped as front matter)",
    ]
    for ch in report["chapters"]:
        lines.append(f"  {ch['file']}: {ch['toc_label']!r} -- {ch['paragraphs']} paragraphs")
    if report["images_prepared"]:
        lines.append(f"{len(report['images_prepared'])} images prepared")
    lines.append("wrote book.json + chapters/*.md + prepare-report.json")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("book_dir", type=Path, help="workspace/<slug> directory, must contain draft.json")
    ap.add_argument("--restored", type=Path, default=None, help="restore.py output dir (default: BOOKDIR/restore)")
    args = ap.parse_args()

    restored_dir = args.restored or (args.book_dir / "restore")
    try:
        report = prepare(args.book_dir, restored_dir)
    except PrepareError as e:
        print(f"prepare error: {e}", file=sys.stderr)
        return 1

    print(summarize(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
