#!/usr/bin/env python3
"""Stage 0 of pdf2epub: characterize a source PDF and recommend a route.

PDFs are wildly diverse; everything downstream depends on knowing which kind
this one is. This script measures — it never guesses about content:

  - per-page text-layer density and image coverage
  - font inventory
  - pathology heuristics: doubled lines (fake-bold double draw), broken
    intra-word spacing, repeated page furniture (headers/footers/page numbers)
  - crude stopword-based language guess (feeds the OCR lang parameter)

It emits triage.json (machine) and a summary (human/LLM). The route it
recommends — TEXT / OCR / HYBRID — is advisory: the orchestrating model reads
the summary plus a couple of sample pages and makes the final call.

Usage:
  triage.py SOURCE.pdf [--out triage.json] [--samples 3]
"""

import argparse
import collections
import json
import re
import statistics
import sys
from pathlib import Path

import pdfplumber

# Pages with fewer extractable characters than this are treated as image-only.
MIN_TEXT_CHARS = 100

STOPWORDS = {
    "es": {"que", "de", "la", "el", "en", "los", "una", "por", "con", "para"},
    "en": {"the", "and", "of", "to", "in", "that", "for", "with", "was", "his"},
    "fr": {"les", "des", "une", "que", "dans", "pour", "est", "qui", "pas", "sur"},
    "de": {"der", "die", "und", "das", "nicht", "ein", "mit", "ist", "von", "sich"},
    "it": {"che", "di", "il", "non", "per", "una", "con", "del", "gli", "sono"},
    "pt": {"que", "de", "não", "uma", "para", "com", "os", "do", "da", "em"},
}

# Real one-letter words; anything else of length 1 suggests broken spacing.
OK_SINGLE = set("aeoyiu")

# A user may drop a better transcription next to the PDF to bypass our OCR
# entirely: <stem>-transcript.<ext> (e.g. source-transcript.md). Priority
# order when several exist — the first wins, the rest are reported as extras.
TRANSCRIPT_EXTS = ("md", "markdown", "txt", "text")


def find_transcripts(pdf_path: Path):
    """Sibling transcript files `<stem>-transcript.<ext>`, priority-ordered.

    Lets the user bring their own OCR / hand transcription; when present the
    whole triage→extract→OCR front end is skipped and the agent works from
    this text. Returns a list (may be empty)."""
    stem = pdf_path.stem
    return [
        cand
        for ext in TRANSCRIPT_EXTS
        if (cand := pdf_path.with_name(f"{stem}-transcript.{ext}")).exists()
    ]


# A user may also drop a cover image next to the PDF: <stem>-cover.<ext>.
# prepare_cover.py validates/fixes it; detection here only surfaces it.
COVER_EXTS = ("png", "jpg", "jpeg", "webp", "tiff", "tif", "bmp", "gif")


def find_cover(pdf_path: Path):
    """Sibling cover image `<stem>-cover.<ext>`, if any (first match wins)."""
    stem = pdf_path.stem
    for ext in COVER_EXTS:
        cand = pdf_path.with_name(f"{stem}-cover.{ext}")
        if cand.exists():
            return cand
    return None


def transcript_report(paths):
    """Describe the chosen transcript for the agent — content is *not*
    interpreted here; the agent reads it and decides how to structure it.
    `looks_structured` is only an advisory hint, never a routing decision."""
    chosen = paths[0]
    text = chosen.read_text(encoding="utf-8", errors="replace")
    return {
        "path": str(chosen),
        "format": chosen.suffix.lstrip("."),
        "chars": len(text),
        "lines": text.count("\n") + 1,
        "empty": not text.strip(),
        "looks_structured": bool(re.search(r"(?m)^#{1,6}\s|```", text)),
        "preview": text[:1200],
        "extras": [str(p) for p in paths[1:]],
    }


def normalized(line: str) -> str:
    return re.sub(r"\s+", "", line)


def is_self_doubled(line: str) -> bool:
    """True if the line is its own text twice (fake-bold double draw)."""
    n = normalized(line)
    if len(n) < 16 or len(n) % 2:
        return False
    return n[: len(n) // 2] == n[len(n) // 2 :]


def dedoubled(line: str) -> str:
    if not is_self_doubled(line):
        return line
    # Cut the raw line near its midpoint at the start of the second copy.
    n = normalized(line)
    half = n[: len(n) // 2]
    seen = 0
    for i, ch in enumerate(line):
        if not ch.isspace():
            seen += 1
        if seen == len(half):
            return line[: i + 1].strip()
    return line


def page_stats(page):
    # Fake bold, variant A: every glyph drawn twice at the same spot.
    # dedupe_chars() collapses coincident chars; a big drop means doubling.
    raw_chars = len(page.chars)
    deduped = page.dedupe_chars()
    char_doubled = raw_chars > 0 and len(deduped.chars) / raw_chars < 0.7
    if char_doubled:
        page = deduped

    text = page.extract_text() or ""
    lines = [ln for ln in text.splitlines() if ln.strip()]

    doubled = sum(1 for ln in lines if is_self_doubled(ln))
    clean_lines = [dedoubled(ln) for ln in lines]
    words = re.findall(r"[^\W\d_]+", " ".join(clean_lines).lower())
    singles = [w for w in words if len(w) == 1 and w not in OK_SINGLE]

    page_area = float(page.width * page.height) or 1.0
    img_area = 0.0
    for im in page.images:
        img_area += abs((im["x1"] - im["x0"]) * (im["bottom"] - im["top"]))

    return {
        "chars": len(normalized(text)),
        "lines": len(lines),
        "char_doubled": char_doubled,
        "doubled_lines": doubled,
        "words": len(words),
        "stray_singles": len(singles),
        "image_count": len(page.images),
        "image_coverage": round(min(img_area / page_area, 1.0), 3),
        "first_line": clean_lines[0][:80] if clean_lines else "",
        "last_line": clean_lines[-1][:80] if clean_lines else "",
        "clean_text": "\n".join(clean_lines),
    }


def guess_language(words):
    counts = collections.Counter(words)
    scores = {
        lang: sum(counts[w] for w in sw) for lang, sw in STOPWORDS.items()
    }
    best = max(scores, key=scores.get)
    return best if scores[best] >= 5 else "unknown"


def detect_furniture(pages):
    """Lines repeating on many pages (headers/footers) — drop candidates."""
    edge_lines = collections.Counter()
    for p in pages:
        for key in ("first_line", "last_line"):
            ln = re.sub(r"\d+", "#", p[key]).strip()
            if ln:
                edge_lines[ln] += 1
    threshold = max(3, len(pages) // 3)
    return [ln for ln, c in edge_lines.items() if c >= threshold]


def triage(pdf_path: Path, samples: int):
    with pdfplumber.open(pdf_path) as pdf:
        pages = [page_stats(p) for p in pdf.pages]
        fonts = sorted({c["fontname"] for p in pdf.pages for c in p.chars})
        meta = {k: str(v) for k, v in (pdf.metadata or {}).items()}

    text_pages = [p for p in pages if p["chars"] >= MIN_TEXT_CHARS]
    image_pages = [p for p in pages if p["chars"] < MIN_TEXT_CHARS and p["image_coverage"] > 0.3]

    all_words = re.findall(
        r"[^\W\d_]+", " ".join(p["clean_text"] for p in pages).lower()
    )

    total_lines = sum(p["lines"] for p in pages) or 1
    total_words = sum(p["words"] for p in pages) or 1
    doubled_ratio = sum(p["doubled_lines"] for p in pages) / total_lines
    stray_ratio = sum(p["stray_singles"] for p in pages) / total_words

    flags = []
    if sum(p["char_doubled"] for p in pages) > len(pages) / 2:
        flags.append("doubled_chars")
    if doubled_ratio > 0.10:
        flags.append("doubled_lines")
    if stray_ratio > 0.02:
        flags.append("broken_spacing")
    furniture = detect_furniture(pages)
    if furniture:
        flags.append("page_furniture")

    transcripts = find_transcripts(pdf_path)
    cover_sidecar = find_cover(pdf_path)
    if transcripts:
        # A user-supplied transcript overrides our own OCR. We still analyse
        # the PDF (page count, language, images remain useful context and the
        # visual ground truth), but the text comes from the sidecar and the
        # agent, not from extract/OCR.
        route = "TRANSCRIPT"
    elif len(text_pages) >= 0.9 * len(pages):
        route = "TEXT"
    elif len(text_pages) <= 0.1 * len(pages):
        route = "OCR"
    else:
        route = "HYBRID"

    sample_pages = {}
    step = max(1, len(pages) // max(samples, 1))
    for idx in list(range(0, len(pages), step))[:samples]:
        sample_pages[idx + 1] = pages[idx]["clean_text"][:1200]

    report = {
        "source": str(pdf_path),
        "pages": len(pages),
        "metadata": meta,
        "fonts": fonts,
        "route": route,
        "flags": flags,
        "language_guess": guess_language(all_words),
        "text_pages": len(text_pages),
        "image_only_pages": len(image_pages),
        "median_chars_per_page": int(statistics.median(p["chars"] for p in pages)),
        "doubled_line_ratio": round(doubled_ratio, 3),
        "stray_single_ratio": round(stray_ratio, 3),
        "furniture_candidates": furniture,
        "transcript": transcript_report(transcripts) if transcripts else None,
        "cover_sidecar": str(cover_sidecar) if cover_sidecar else None,
        "per_page": [
            {k: v for k, v in p.items() if k != "clean_text"} for p in pages
        ],
        "sample_pages": sample_pages,
    }
    return report


def summarize(r):
    lines = [
        f"{r['source']}: {r['pages']} pages, route {r['route']}"
        f" (text pages: {r['text_pages']}, image-only: {r['image_only_pages']})",
        f"language guess: {r['language_guess']}   fonts: {', '.join(r['fonts']) or '-'}",
        f"median chars/page: {r['median_chars_per_page']}",
        f"flags: {', '.join(r['flags']) or 'none'}"
        f"  (doubled lines {r['doubled_line_ratio']:.0%},"
        f" stray singles {r['stray_single_ratio']:.1%})",
    ]
    if r["furniture_candidates"]:
        lines.append("furniture: " + " | ".join(r["furniture_candidates"][:5]))
    if r.get("transcript"):
        t = r["transcript"]
        if t["empty"]:
            note = "!! transcript is EMPTY — falling back means re-triaging without it"
        else:
            hint = ("looks structured (has headings/fences)"
                    if t["looks_structured"] else "looks like raw text")
            note = (f"TRANSCRIPT provided: {t['path']} "
                    f"({t['chars']} chars, .{t['format']}, {hint}). "
                    "OCR/extract skipped — read it and decide how to turn it "
                    "into chapters (see SKILL.md § sidecar).")
            if t["extras"]:
                note += f"  [ignoring extra transcripts: {', '.join(t['extras'])}]"
        lines.insert(0, note)
    if r.get("cover_sidecar"):
        lines.append(f"cover sidecar: {r['cover_sidecar']} "
                     "(run prepare_cover.py to validate/fix)")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--out", type=Path, help="write triage.json here")
    ap.add_argument("--samples", type=int, default=3,
                    help="sample pages to include as cleaned text (default 3)")
    args = ap.parse_args()

    report = triage(args.pdf, args.samples)
    print(summarize(report))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
