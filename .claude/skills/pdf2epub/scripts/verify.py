#!/usr/bin/env python3
"""Stage 6 of pdf2epub: verify a built EPUB is structurally sound and that
its visible text still covers the restored source.

Two independent checks, both deterministic:
  1. Integrity -- mimetype first and stored, every manifest item exists in
     the zip and vice versa, every internal href/fragment resolves, every
     XHTML/OPF entry is well-formed XML.
  2. Coverage -- strip tags from the spine's XHTML, normalize whitespace,
     and run it through the same fidelity gate restore.py uses
     (char_ratio, ngram_containment) against restore/restored.md. This is
     the last check in the pipeline: if prepare.py silently dropped a
     paragraph, or the builder mangled something, this is what catches it.

Usage:
  verify.py workspace/<slug> --epub PATH [--restored RESTOREDIR]
"""
import argparse
import html
import json
import posixpath
import re
import sys
import xml.dom.minidom
import zipfile
from pathlib import Path

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
# OPF parsing (the format is our own build_epub.py's output, so a light
# regex parse is fine -- no need for a full namespace-aware XML stack).
# --------------------------------------------------------------------------- #
def parse_manifest(opf_xml: str) -> dict:
    items = {}
    for m in re.finditer(r"<item\s+([^>]+?)/>", opf_xml):
        attrs = dict(re.findall(r'([\w:-]+)="([^"]*)"', m.group(1)))
        if "id" in attrs:
            items[attrs["id"]] = attrs
    return items


def parse_spine(opf_xml: str):
    return re.findall(r'<itemref\s+idref="([^"]+)"', opf_xml)


def load_opf(z: zipfile.ZipFile):
    container = z.read("META-INF/container.xml").decode("utf-8")
    m = re.search(r'full-path="([^"]+)"', container)
    if not m:
        raise ValueError("META-INF/container.xml has no rootfile full-path")
    opf_path = m.group(1)
    opf_dir = posixpath.dirname(opf_path)
    opf_xml = z.read(opf_path).decode("utf-8")
    return opf_path, opf_dir, opf_xml


# --------------------------------------------------------------------------- #
# Integrity
# --------------------------------------------------------------------------- #
def check_integrity(z: zipfile.ZipFile, opf_path: str, opf_dir: str, manifest: dict, spine: list) -> list:
    errors = []
    names = z.namelist()

    if not names or names[0] != "mimetype":
        errors.append("mimetype is not the first zip entry")
    elif z.getinfo("mimetype").compress_type != zipfile.ZIP_STORED:
        errors.append("mimetype is not stored uncompressed")

    expected = {"mimetype", "META-INF/container.xml", opf_path}
    for item in manifest.values():
        expected.add(posixpath.normpath(posixpath.join(opf_dir, item["href"])))
    actual = set(names)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing:
        errors.append(f"manifest items missing from zip: {missing}")
    if extra:
        errors.append(f"zip entries not registered in manifest: {extra}")

    for idref in spine:
        if idref not in manifest:
            errors.append(f"spine itemref {idref!r} has no matching manifest item")
    if not spine:
        errors.append("spine is empty")

    for n in names:
        if n.endswith((".xhtml", ".opf")):
            try:
                xml.dom.minidom.parseString(z.read(n))
            except Exception as e:
                errors.append(f"{n}: not well-formed XML ({e})")

    for n in names:
        if not n.endswith(".xhtml"):
            continue
        text = z.read(n).decode("utf-8")
        ids = set(re.findall(r'id="([^"]+)"', text))
        doc_dir = posixpath.dirname(n)
        for href in re.findall(r'(?:href|src)="([^"]+)"', text):
            if href.startswith(("#", "http://", "https://", "mailto:")):
                if href.startswith("#") and href[1:] not in ids:
                    errors.append(f"{n}: dead fragment link #{href[1:]}")
                continue
            target = posixpath.normpath(posixpath.join(doc_dir, href.split("#", 1)[0]))
            if target not in actual:
                errors.append(f"{n}: dead link {href!r} (resolved {target})")

    return errors


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

    restored_text = (restored_dir / "restored.md").read_text(encoding="utf-8")
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
