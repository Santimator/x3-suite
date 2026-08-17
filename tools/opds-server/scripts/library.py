#!/usr/bin/env python3
"""The library model — what EPUBs exist, and what the device should call them.

Scans the configured roots (by default the builder's output, `workspace/`) for
`.epub` files and reads each one's metadata straight out of its OPF. The OPF is
authoritative on purpose: it is what *every* EPUB carries, so a book dropped in
by hand catalogs exactly as well as one this suite built, and what the catalog
shows is what the reader itself would show.

Metadata precedence, best source first:

  1. the EPUB's OPF (`dc:title`, `dc:creator`, `dc:language`)
  2. `book.json` two levels up (`workspace/<slug>/book.json` for a build under
     `workspace/<slug>/build/`) — covers an EPUB whose OPF is thin
  3. the filename stem

Metadata matters more here than in a normal catalog: CrossPoint names the
downloaded file `<author> - <title>.epub` on the SD card (see
`extras/readers.md`), so a missing author is a permanently worse filename.

Book ids are `sha1(path relative to its root)[:12]` — stable across restarts and
rescans, so a device bookmark or a half-finished download still resolves after
the server is restarted. Nothing in the URL space is ever a filesystem path.

Usage:
  library.py                       # scan the default roots, print a report
  library.py --json                # the same library as typed JSON
  library.py --root DIR [--root D] # override the configured roots
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

CONTAINER_NS = "{urn:oasis:names:tc:opendocument:xmlns:container}"
OPF_NS = "{http://www.idpf.org/2007/opf}"
DC_NS = "{http://purl.org/dc/elements/1.1/}"

# (path, mtime_ns, size) -> parsed OPF metadata. A rescan is cheap; re-reading
# and re-parsing every OPF on every feed request is not.
_META_CACHE: Dict[Tuple[str, int, int], Dict[str, str]] = {}


@dataclass(frozen=True)
class Book:
    id: str
    path: str      # absolute path on disk — never appears in a URL
    rel: str       # path relative to its root, for display and id derivation
    title: str
    author: str
    language: str
    size: int
    mtime: float
    slug: str      # url-safe decoration for the download URL

    def as_dict(self) -> dict:
        return asdict(self)


# XML 1.0 forbids most control characters, and a single one in a `dc:creator`
# would make the whole feed unparseable on the device. Cleaned here, at the one
# point metadata enters the model, rather than at each place it is rendered.
_ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clean(text: str) -> str:
    return " ".join(_ILLEGAL_XML.sub("", text or "").split())


def _text(el: Optional[ET.Element]) -> str:
    if el is None or el.text is None:
        return ""
    return _clean(el.text)


def read_opf_metadata(epub: Path) -> Dict[str, str]:
    """Title/author/language from the EPUB's OPF. Never raises — a book with an
    unreadable OPF still belongs in the catalog, just under weaker metadata."""
    meta = {"title": "", "author": "", "language": ""}
    try:
        with zipfile.ZipFile(epub) as z:
            container = ET.fromstring(z.read("META-INF/container.xml"))
            rootfile = container.find(f"{CONTAINER_NS}rootfiles/{CONTAINER_NS}rootfile")
            if rootfile is None:
                return meta
            opf_path = rootfile.get("full-path")
            if not opf_path:
                return meta
            opf = ET.fromstring(z.read(opf_path))
    except (OSError, KeyError, zipfile.BadZipFile, ET.ParseError):
        return meta

    metadata = opf.find(f"{OPF_NS}metadata")
    if metadata is None:
        return meta
    meta["title"] = _text(metadata.find(f"{DC_NS}title"))
    meta["author"] = _text(metadata.find(f"{DC_NS}creator"))
    meta["language"] = _text(metadata.find(f"{DC_NS}language"))
    return meta


def read_book_json(epub: Path) -> Dict[str, str]:
    """Fallback metadata from the suite's common book format. A build lives at
    `workspace/<slug>/build/<slug>.epub`, so book.json is two levels up."""
    candidate = epub.parent.parent / "book.json"
    if not candidate.exists():
        return {}
    try:
        data = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: _clean(str(data.get(k, ""))) for k in ("title", "author", "language")}


def _cached_metadata(epub: Path, stat_result) -> Dict[str, str]:
    key = (str(epub), stat_result.st_mtime_ns, stat_result.st_size)
    cached = _META_CACHE.get(key)
    if cached is None:
        cached = read_opf_metadata(epub)
        _META_CACHE[key] = cached
    return cached


def title_from_filename(epub: Path) -> str:
    stem = _clean(epub.stem.replace("_", " ").replace("-", " "))
    return stem or epub.name


def slugify(text: str, fallback: str = "") -> str:
    """URL-safe decoration for a download URL. Ascii-only by design: the id in
    the same URL is what actually resolves the book, so this only has to be
    readable and safe, never faithful. A wholly non-Latin title (the common case
    here) leaves nothing behind, so fall back to the filename before "book"."""
    for candidate in (text, fallback):
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", candidate or "").strip("-.")
        if slug:
            return slug[:60]
    return "book"


def _iter_epubs(root: Path, exclude: Iterable[str]) -> Iterable[Path]:
    patterns = list(exclude)
    for path in sorted(root.rglob("*.epub")):
        if not path.is_file():
            continue
        name = path.name
        rel = path.relative_to(root).as_posix()
        if any(fnmatch.fnmatch(name, p) or fnmatch.fnmatch(rel, p) for p in patterns):
            continue
        yield path


def scan(roots: Iterable[Path], exclude: Iterable[str] = ()) -> List[Book]:
    """Every EPUB under the roots, best metadata first, sorted by title."""
    exclude = list(exclude)
    books: List[Book] = []
    seen_ids: Dict[str, str] = {}
    seen_paths = set()

    for root in roots:
        root = Path(root).resolve()
        if not root.is_dir():
            continue
        for path in _iter_epubs(root, exclude):
            resolved = path.resolve()
            if str(resolved) in seen_paths:  # overlapping roots
                continue
            seen_paths.add(str(resolved))

            try:
                st = resolved.stat()
            except OSError:
                continue

            rel = resolved.relative_to(root).as_posix()
            book_id = hashlib.sha1(rel.encode("utf-8")).hexdigest()[:12]
            if book_id in seen_ids:  # same relative path under two roots
                book_id = hashlib.sha1(str(resolved).encode("utf-8")).hexdigest()[:12]
            seen_ids[book_id] = rel

            opf = _cached_metadata(resolved, st)
            fallback = read_book_json(resolved) if not all(opf.values()) else {}
            title = opf["title"] or fallback.get("title") or title_from_filename(resolved)
            author = opf["author"] or fallback.get("author") or ""
            language = opf["language"] or fallback.get("language") or ""

            books.append(Book(
                id=book_id,
                path=str(resolved),
                rel=rel,
                title=title,
                author=author,
                language=language,
                size=st.st_size,
                mtime=st.st_mtime,
                slug=slugify(title, resolved.stem),
            ))

    books.sort(key=lambda b: (b.title.casefold(), b.rel))
    return books


def author_key(author: str) -> str:
    """A stable, ascii-safe URL segment for an author. Hashed rather than
    escaped because authors here are routinely CJK, and CrossPoint's URL
    handling is naive string work, not RFC 3986 resolution."""
    return hashlib.sha1(author.encode("utf-8")).hexdigest()[:10]


def group_by_author(books: Iterable[Book]) -> List[Tuple[str, str, List[Book]]]:
    """[(key, author, books)] sorted by author; unattributed books last."""
    groups: Dict[str, List[Book]] = {}
    for book in books:
        groups.setdefault(book.author, []).append(book)
    ordered = sorted(groups.items(), key=lambda kv: (kv[0] == "", kv[0].casefold()))
    return [(author_key(a), a, bs) for a, bs in ordered]


def group_by_language(books: Iterable[Book]) -> List[Tuple[str, List[Book]]]:
    """[(language, books)] sorted by language code; unlabelled books last."""
    groups: Dict[str, List[Book]] = {}
    for book in books:
        groups.setdefault(book.language.lower(), []).append(book)
    ordered = sorted(groups.items(), key=lambda kv: (kv[0] == "", kv[0]))
    return [(lang, bs) for lang, bs in ordered]


def duplicate_labels(books: Iterable[Book]) -> List[List[Book]]:
    """Groups of books the *device* cannot tell apart.

    CrossPoint names a download `<author> - <title>.epub` on the SD card, so two
    books with the same author and title overwrite each other there however
    distinct they are here — which is exactly what variant builds of one book
    (`letter-writer.epub` and `letter-writer-underline.epub`) are. Reported, not
    fixed: renaming someone's book to dodge a filename collision is the sort of
    invention this suite keeps scripts out of.
    """
    groups: Dict[Tuple[str, str], List[Book]] = {}
    for book in books:
        groups.setdefault((book.author.casefold(), book.title.casefold()), []).append(book)
    return [g for g in groups.values() if len(g) > 1]


def search(books: Iterable[Book], query: str) -> List[Book]:
    """Case-insensitive substring over title and author — what a five-button
    on-screen keyboard can realistically drive."""
    q = query.strip().casefold()
    if not q:
        return []
    return [b for b in books if q in b.title.casefold() or q in b.author.casefold()]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Scan the library the OPDS server serves.")
    ap.add_argument("--root", action="append", default=[], metavar="DIR",
                    help="library root (repeatable); defaults to the configured roots")
    ap.add_argument("--exclude", action="append", default=None, metavar="GLOB",
                    help="skip matching filenames (repeatable)")
    ap.add_argument("--json", action="store_true", help="emit the library as JSON")
    args = ap.parse_args(argv)

    if args.root:
        roots = [Path(r) for r in args.root]
        exclude = args.exclude if args.exclude is not None else []
    else:
        from config import load_config  # local import: only needed without --root
        cfg = load_config()
        roots = cfg["library_roots"]
        exclude = args.exclude if args.exclude is not None else cfg["exclude"]

    books = scan(roots, exclude)

    if args.json:
        json.dump({"roots": [str(r) for r in roots], "count": len(books),
                   "books": [b.as_dict() for b in books]},
                  sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0

    print(f"{len(books)} book(s) in {', '.join(str(r) for r in roots)}")
    for b in books:
        author = b.author or "—"
        print(f"  {b.id}  {b.title}  · {author} · {b.language or '—'} · "
              f"{b.size / 1024:.0f} KB · {b.rel}")
    for group in duplicate_labels(books):
        print(f"  note: {len(group)} books share author+title "
              f"({', '.join(b.rel for b in group)}) — the device saves both as "
              f"the same SD filename, so one overwrites the other")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
