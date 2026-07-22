#!/usr/bin/env python3
"""Stage 6 of pdf2epub: verify a built EPUB is structurally sound and that
its visible text still covers the restored source.

Two independent checks, both deterministic:
  1. Integrity -- delegated to the builder's shared verify_epub.py
     (mimetype first/stored, manifest <-> zip parity, internal links and
     fragments resolve, XHTML/OPF well-formed). One implementation, used by
     both suite tasks.
  2. Coverage -- strip tags from the spine's XHTML, normalize whitespace,
     and run it through the same fidelity gate restore.py uses
     (char_ratio, ngram_containment) against restore/restored.md. This is
     conversion-specific (it needs the restored source), so it stays here.
     If prepare.py silently dropped a paragraph, or the builder mangled
     something, this is what catches it.

Usage:
  verify.py workspace/<slug> --epub PATH [--restored RESTOREDIR]
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
sys.path.insert(0, str(Path(__file__).resolve().parent))
from restore import effective_restored  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "epub-builder" / "scripts"))
from verify_epub import check_integrity, load_opf, parse_manifest, parse_spine  # noqa: E402

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


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
def verify(epub_path: Path, restored_dir: Path) -> dict:
    z = zipfile.ZipFile(epub_path)
    opf_path, opf_dir, opf_xml = load_opf(z)
    manifest = parse_manifest(opf_xml)
    spine = parse_spine(opf_xml)

    integrity_errors = check_integrity(z, opf_path, opf_dir, manifest, spine)

    # Coverage compares the EPUB against the text it was actually built from:
    # corrected.md when the agent took the guarded-correction path, else
    # restored.md. review_edits.py separately bounds corrected vs restored.
    restored_text = effective_restored(restored_dir).read_text(encoding="utf-8")
    out_text = spine_text(z, opf_dir, manifest, spine)

    chars_in = nonwhitespace_chars(restored_text)
    chars_out = nonwhitespace_chars(out_text)
    char_ratio = (chars_out / chars_in) if chars_in else 1.0

    grams_in = word_ngrams(restored_text)
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
    ap.add_argument("--restored", type=Path, default=None, help="restore.py output dir (default: BOOKDIR/restore)")
    args = ap.parse_args()

    restored_dir = args.restored or (args.book_dir / "restore")
    report = verify(args.epub, restored_dir)

    (args.book_dir / "verify-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(summarize(report))
    print(f"wrote {args.book_dir / 'verify-report.json'}")
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
