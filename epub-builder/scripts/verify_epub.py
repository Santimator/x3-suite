#!/usr/bin/env python3
"""Structural integrity check for an EPUB the builder produced.

This is builder-level infrastructure: it knows only the EPUB format, nothing
about how the book was authored. Both suite tasks lean on it —
graded-reader's self-test and pdf2epub's verify.py — so the "is this a
well-formed EPUB" question has one implementation, not one per task.

Checks (all deterministic, no dependencies beyond the stdlib):
  - mimetype is the first zip entry and stored uncompressed;
  - the OPF manifest and the zip's entries are in exact correspondence
    (every manifest item present, no stray files);
  - every spine itemref resolves to a manifest item, and the spine is
    non-empty;
  - every .xhtml / .opf entry is well-formed XML;
  - every internal href/src and every '#fragment' resolves.

The OPF here is our own build_epub.py's output, so a light regex parse is
fine — no namespace-aware XML stack needed.

Usage:
  verify_epub.py path/to/book.epub          # prints report, exit 0/1
  # or, from another script:
  from verify_epub import verify_integrity
  report = verify_integrity(Path("book.epub"))   # {"pass": bool, "errors": [...]}
"""
from __future__ import annotations

import argparse
import posixpath
import re
import sys
import xml.dom.minidom
import zipfile
from pathlib import Path


# --------------------------------------------------------------------------- #
# OPF parsing (shared with the coverage check in pdf2epub's verify.py)
# --------------------------------------------------------------------------- #
def load_opf(z: zipfile.ZipFile):
    """Return (opf_path, opf_dir, opf_xml) by following container.xml."""
    container = z.read("META-INF/container.xml").decode("utf-8")
    m = re.search(r'full-path="([^"]+)"', container)
    if not m:
        raise ValueError("META-INF/container.xml has no rootfile full-path")
    opf_path = m.group(1)
    opf_dir = posixpath.dirname(opf_path)
    opf_xml = z.read(opf_path).decode("utf-8")
    return opf_path, opf_dir, opf_xml


def parse_manifest(opf_xml: str) -> dict:
    items = {}
    for m in re.finditer(r"<item\s+([^>]+?)/>", opf_xml):
        attrs = dict(re.findall(r'([\w:-]+)="([^"]*)"', m.group(1)))
        if "id" in attrs:
            items[attrs["id"]] = attrs
    return items


def parse_spine(opf_xml: str):
    return re.findall(r'<itemref\s+idref="([^"]+)"', opf_xml)


# --------------------------------------------------------------------------- #
# Integrity
# --------------------------------------------------------------------------- #
def check_integrity(z: zipfile.ZipFile, opf_path: str, opf_dir: str,
                    manifest: dict, spine: list) -> list:
    """Return a list of human-readable error strings (empty == sound)."""
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


def verify_integrity(epub_path: Path) -> dict:
    """Open an EPUB and run every structural check. Returns
    {"pass": bool, "errors": [...]}."""
    with zipfile.ZipFile(epub_path) as z:
        opf_path, opf_dir, opf_xml = load_opf(z)
        manifest = parse_manifest(opf_xml)
        spine = parse_spine(opf_xml)
        errors = check_integrity(z, opf_path, opf_dir, manifest, spine)
    return {"pass": not errors, "errors": errors}


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify an EPUB's structural integrity.")
    ap.add_argument("epub", type=Path)
    args = ap.parse_args()

    report = verify_integrity(args.epub)
    if report["pass"]:
        print(f"ok  {args.epub.name}: structurally sound")
        return 0
    print(f"FAIL  {args.epub.name}:")
    for e in report["errors"]:
        print(f"  - {e}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
