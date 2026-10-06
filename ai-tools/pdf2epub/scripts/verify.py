#!/usr/bin/env python3
"""pdf2epub, last step: verify a built EPUB is structurally sound and that
its visible text still covers the book's chapters.

Two independent checks, both deterministic:
  1. Integrity -- delegated to the builder's shared verify_epub.py
     (mimetype first/stored, manifest <-> zip parity, internal links and
     fragments resolve, XHTML/OPF well-formed). One implementation, used by
     both suite tasks.
  2. Coverage -- strip tags from the spine's XHTML and compare it with the
     book's own chapters/*.md (what went into the EPUB, markup stripped the
     same way check_chunk.py strips it): char_ratio and 5-gram containment.
     This catches a builder mangle/drop. The chunk gates already answered
     "is each chunk faithful to the page"; this answers "did the build keep it".

Usage:
  verify.py workspace/<slug> --epub PATH
"""
import argparse
import html
import json
import posixpath
import re
import sys
import zipfile
from pathlib import Path

# The EPUB integrity check + OPF parsing are builder-level infrastructure,
# shared with graded-reader; import them from the epub-builder skill.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "epub-builder" / "scripts"))
from verify_epub import check_integrity, load_opf, parse_manifest, parse_spine  # noqa: E402

from harness import markdown_to_text  # noqa: E402

WHITESPACE = re.compile(r"\s+")
TAG_RE = re.compile(r"<[^>]+>")


def nonwhitespace_chars(text: str) -> int:
    return len(WHITESPACE.sub("", text))


def word_ngrams(text: str, n: int = 5):
    words = text.split()
    if len(words) < n:
        return set()
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


# --------------------------------------------------------------------------- #
# Coverage
# --------------------------------------------------------------------------- #
def spine_text(z: zipfile.ZipFile, opf_dir: str, manifest: dict, spine: list) -> str:
    parts = []
    for idref in spine:
        if idref not in manifest:
            continue
        path = posixpath.normpath(posixpath.join(opf_dir, manifest[idref]["href"]))
        if path not in z.namelist():
            continue
        xhtml = z.read(path).decode("utf-8")
        m = re.search(r"<body[^>]*>(.*)</body>", xhtml, re.DOTALL)
        body = m.group(1) if m else xhtml
        parts.append(html.unescape(TAG_RE.sub(" ", body)))
    return "\n".join(parts)


def book_chapters_text(book_dir: Path) -> str:
    """The coverage baseline: the concatenated text of the chapters that went
    into the book (book.json's spine), with the markup the builder renders as
    structure stripped, so only reading text is compared."""
    book = json.loads((book_dir / "book.json").read_text(encoding="utf-8"))
    return "\n".join(markdown_to_text((book_dir / ch["source"]).read_text(encoding="utf-8"))
                     for ch in book["chapters"])


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
def verify(epub_path: Path, book_dir: Path) -> dict:
    z = zipfile.ZipFile(epub_path)
    opf_path, opf_dir, opf_xml = load_opf(z)
    manifest = parse_manifest(opf_xml)
    spine = parse_spine(opf_xml)

    integrity_errors = check_integrity(z, opf_path, opf_dir, manifest, spine)

    chapters_text = book_chapters_text(book_dir)
    out_text = spine_text(z, opf_dir, manifest, spine)

    chars_in = nonwhitespace_chars(chapters_text)
    chars_out = nonwhitespace_chars(out_text)
    char_ratio = (chars_out / chars_in) if chars_in else 1.0

    grams_in = word_ngrams(chapters_text)
    grams_out = word_ngrams(out_text)
    ngram_containment = (len(grams_in & grams_out) / len(grams_in)) if grams_in else 1.0

    coverage_pass = 0.98 <= char_ratio <= 1.02 and ngram_containment >= 0.995

    report = {
        "epub": str(epub_path),
        "integrity_errors": integrity_errors,
        "integrity_pass": not integrity_errors,
        "char_ratio": round(char_ratio, 4),
        "ngram_containment": round(ngram_containment, 4),
        "coverage_pass": coverage_pass,
    }
    report["pass"] = report["integrity_pass"] and coverage_pass
    return report


def summarize(report: dict) -> str:
    lines = [
        f"integrity: {'PASS' if report['integrity_pass'] else 'FAIL'}"
        + (f" -- {report['integrity_errors']}" if report["integrity_errors"] else ""),
        f"coverage:  {'PASS' if report['coverage_pass'] else 'FAIL'} "
        f"(char_ratio={report['char_ratio']}, ngram_containment={report['ngram_containment']})",
        f"overall: {'PASS' if report['pass'] else 'FAIL'}",
    ]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("book_dir", type=Path, help="workspace/<slug> directory")
    ap.add_argument("--epub", type=Path, required=True, help="built .epub to verify")
    args = ap.parse_args()

    report = verify(args.epub, args.book_dir)

    (args.book_dir / "verify-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(summarize(report))
    print(f"wrote {args.book_dir / 'verify-report.json'}")
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
