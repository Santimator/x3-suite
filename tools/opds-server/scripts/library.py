#!/usr/bin/env python3
"""The library model — what EPUBs exist, and what the device should call them.

Scans the configured roots (by default the builder's output, `workspace/`) for
`.epub` files and reads each one's metadata straight out of its OPF. The OPF is
authoritative on purpose: it is what *every* EPUB carries, so a book dropped in
by hand catalogs exactly as well as one this suite built, and what the catalog
shows is what the reader itself would show.

Metadata precedence, best source first:

  1. the EPUB's OPF (`dc:title`, `dc:creator`, `dc:language`, series metadata)
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
import unicodedata
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
    title: str      # catalog/device title; may carry a compact series prefix
    base_title: str # title embedded in the EPUB, without catalog decoration
    author: str
    language: str
    series: str
    series_index: str
    series_alias: str
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
    meta = {"title": "", "author": "", "language": "",
            "series": "", "series_index": ""}
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

    # EPUB 3 collection metadata. `belongs-to-collection` may describe several
    # kinds of collection, so prefer the one explicitly refined as a series.
    # A few publishers omit that refinement; a lone collection is still more
    # useful than throwing the metadata away.
    collections: List[Tuple[str, str]] = []
    refinements: Dict[str, Dict[str, str]] = {}
    calibre_series = calibre_index = ""
    for child in list(metadata):
        if child.tag.rsplit("}", 1)[-1] != "meta":
            continue
        prop = _clean(child.get("property", ""))
        name = _clean(child.get("name", "")).casefold()
        value = _text(child) or _clean(child.get("content", ""))
        if prop == "belongs-to-collection" and value:
            collections.append((_clean(child.get("id", "")), value))
        elif prop in ("collection-type", "group-position"):
            target = _clean(child.get("refines", "")).lstrip("#")
            if target:
                refinements.setdefault(target, {})[prop] = value
        elif name == "calibre:series":
            calibre_series = value
        elif name == "calibre:series_index":
            calibre_index = value

    for collection_id, name in collections:
        refined = refinements.get(collection_id, {})
        if refined.get("collection-type", "").casefold() == "series":
            meta["series"] = name
            meta["series_index"] = refined.get("group-position", "")
            break
    if not meta["series"] and len(collections) == 1:
        collection_id, name = collections[0]
        meta["series"] = name
        meta["series_index"] = refinements.get(collection_id, {}).get(
            "group-position", "")
    if not meta["series"] and calibre_series:
        meta["series"] = calibre_series
        meta["series_index"] = calibre_index
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
    return {k: _clean(str(data.get(k, "")))
            for k in ("title", "author", "language", "series", "series_index")}


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


ALIAS_FILE = ".series-aliases.json"
ALIAS_MAX_CHARS = 6


def _alias_path(directory: Path) -> Path:
    return Path(directory) / ALIAS_FILE


def load_aliases(directory: Path) -> Dict[str, str]:
    """Full series name -> confirmed short alias. Corrupt sidecars are ignored
    by the read-only catalog; the writer refuses them rather than compounding
    the damage."""
    try:
        data = json.loads(_alias_path(directory).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    raw = data.get("aliases", {}) if isinstance(data, dict) else {}
    if not isinstance(raw, dict):
        return {}
    return {_clean(str(name)): _clean(str(alias)) for name, alias in raw.items()
            if _clean(str(name)) and _clean(str(alias))}


def aliases_for(epub: Path, root: Path) -> Dict[str, str]:
    """Aliases visible to one book, nearest sidecar winning.

    The default OPDS root is `workspace/`, while Telegram files books into
    `workspace/library/`. Looking up through the parents lets that collection
    keep its sidecar beside its books without teaching the server a special
    directory name.
    """
    root = Path(root).resolve()
    here = Path(epub).resolve().parent
    merged: Dict[str, str] = {}
    chain = []
    while True:
        chain.append(here)
        if here == root or root not in here.parents:
            break
        here = here.parent
    for directory in reversed(chain):
        by_fold = {name.casefold(): name for name in merged}
        for name, alias in load_aliases(directory).items():
            old = by_fold.get(name.casefold())
            if old and old != name:
                merged.pop(old, None)
            merged[name] = alias
    return merged


def alias_for(series: str, aliases: Dict[str, str]) -> str:
    wanted = _clean(series).casefold()
    return next((alias for name, alias in aliases.items()
                 if name.casefold() == wanted), "")


def validate_alias(alias: str) -> str:
    alias = unicodedata.normalize("NFC", _clean(alias))
    if not alias:
        raise ValueError("the short series name cannot be empty")
    if len(alias) > ALIAS_MAX_CHARS:
        raise ValueError(f"the short series name may be at most {ALIAS_MAX_CHARS} characters")
    if any(c in "/\\" or unicodedata.category(c).startswith("C") for c in alias):
        raise ValueError("the short series name cannot contain slashes or control characters")
    return alias


def suggest_alias(series: str, used: Iterable[str] = ()) -> str:
    """A transparent suggestion, not a guess from a model.

    Multi-word names become initials, with a leading article omitted (`The
    Lord of the Rings` -> `LOTR`). A one-word name becomes its first six
    characters. Collisions get a numeric suffix. Telegram always asks the user
    to confirm this before it is stored.
    """
    words = re.findall(r"[^\W_]+", unicodedata.normalize("NFC", _clean(series)), re.UNICODE)
    if len(words) > 1:
        if words[0].casefold() in {"a", "an", "the", "el", "la", "los", "las",
                                  "le", "les", "un", "une", "der", "die", "das"}:
            words = words[1:] or words
        base = "".join(word[0] for word in words).upper()[:ALIAS_MAX_CHARS]
    else:
        base = (words[0] if words else "SERIES")[:ALIAS_MAX_CHARS].upper()
    taken = {a.casefold() for a in used}
    if base.casefold() not in taken:
        return base
    for n in range(2, 1000):
        suffix = str(n)
        candidate = base[:ALIAS_MAX_CHARS - len(suffix)] + suffix
        if candidate.casefold() not in taken:
            return candidate
    raise ValueError("could not make a unique short series name")


def format_series_index(value: str) -> str:
    """Preserve publisher-supplied subpositions while padding the main volume.

    `1` and `1.0` become `01`; `1.5` becomes `01.5`; `2.2.1` becomes
    `02.2.1`. Non-numeric positions are kept verbatim rather than invented.
    """
    value = _clean(value)
    if not value:
        return ""
    if re.fullmatch(r"\d+(?:\.\d+)*", value):
        parts = value.split(".")
        while len(parts) > 1 and parts[-1] == "0":
            parts.pop()
        parts[0] = parts[0].zfill(2)
        return ".".join(parts)
    return value


_TITLE_SEPARATOR = re.compile(r"\s+[-\u2013\u2014]\s+")


def _metadata_key(text: str) -> str:
    """Comparison key for metadata labels, never for the title we display."""
    text = unicodedata.normalize("NFKC", _clean(text)).casefold()
    return "".join(character for character in text if character.isalnum())


def clean_embedded_title(title: str, author: str = "", series: str = "",
                         series_index: str = "") -> str:
    """Remove only leading labels that the other OPF fields prove redundant.

    Some third-party EPUBs put a filename-shaped value in ``dc:title``, such
    as ``McMurtry, Larry - Lonesome Dove 01 - Lonesome Dove``.  The author,
    series and volume are already separate metadata, so carrying those labels
    into the catalog would add them a second time.  This deliberately is not a
    fuzzy title guess: a dash-delimited prefix is removed only when it exactly
    matches a form reconstructed from those known fields.
    """
    title = _clean(title)
    parts = [_clean(part) for part in _TITLE_SEPARATOR.split(title)]
    if len(parts) < 2:
        return title

    known_prefixes = {_metadata_key(author)} if author else set()
    author = _clean(author)
    if author and "," in author:
        family, given = (_clean(part) for part in author.split(",", 1))
        if family and given:
            known_prefixes.add(_metadata_key(f"{given} {family}"))
    elif author and not re.search(r"(?:\s(?:and|&)\s|[;/])", author, re.IGNORECASE):
        names = author.split()
        if len(names) > 1:
            known_prefixes.add(_metadata_key(f"{names[-1]}, {' '.join(names[:-1])}"))

    series = _clean(series)
    series_index = _clean(series_index)
    if series and series_index:
        positions = {series_index, format_series_index(series_index)}
        if re.fullmatch(r"\d+(?:\.\d+)*", series_index):
            numeric = series_index.split(".")
            while len(numeric) > 1 and numeric[-1] == "0":
                numeric.pop()
            numeric[0] = str(int(numeric[0]))
            positions.add(".".join(numeric))
        for position in positions:
            for label in (f"{series} {position}", f"{series} Book {position}",
                          f"{series} Volume {position}", f"{series} Vol. {position}"):
                known_prefixes.add(_metadata_key(label))

    known_prefixes.discard("")
    first_title_part = 0
    while (first_title_part < len(parts) - 1
           and _metadata_key(parts[first_title_part]) in known_prefixes):
        first_title_part += 1
    return " - ".join(parts[first_title_part:]) if first_title_part else title


def catalog_title(base_title: str, series_alias: str = "", series_index: str = "") -> str:
    base_title = _clean(base_title)
    if not series_alias:
        return base_title
    prefix = series_alias
    position = format_series_index(series_index)
    if position:
        prefix += " " + position
    return f"{prefix} - {base_title}"


def safe_filename(text: str, extension: str = ".epub", max_bytes: int = 240) -> str:
    """A portable, readable filename with its extension inside the byte cap."""
    text = unicodedata.normalize("NFC", _clean(text))
    text = re.sub(r"[<>:\"/\\|?*]", " ", text)
    text = " ".join(text.split()).strip(" .") or "book"
    budget = max(1, max_bytes - len(extension.encode("utf-8")))
    while len(text.encode("utf-8")) > budget:
        text = text[:-1]
    text = text.rstrip(" .") or "book"
    return text + extension


def canonical_filename(title: str, author: str) -> str:
    label = f"{title} - {author}" if author else title
    return safe_filename(label)


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
            # The OPF remains authoritative field by field. Read book.json
            # when a core field is thin *or* when it may be the only source of
            # optional series metadata.
            fallback = (read_book_json(resolved)
                        if not all(opf.get(k) for k in ("title", "author", "language"))
                        or not opf.get("series") else {})
            embedded_title = (opf["title"] or fallback.get("title")
                              or title_from_filename(resolved))
            author = opf["author"] or fallback.get("author") or ""
            language = opf["language"] or fallback.get("language") or ""
            series = opf.get("series") or fallback.get("series") or ""
            series_index = opf.get("series_index") or fallback.get("series_index") or ""
            title = clean_embedded_title(
                embedded_title, author, series, series_index)
            series_alias = alias_for(series, aliases_for(resolved, root)) if series else ""
            display_title = catalog_title(title, series_alias, series_index)

            books.append(Book(
                id=book_id,
                path=str(resolved),
                rel=rel,
                title=display_title,
                base_title=title,
                author=author,
                language=language,
                series=series,
                series_index=series_index,
                series_alias=series_alias,
                size=st.st_size,
                mtime=st.st_mtime,
                slug=slugify(display_title, resolved.stem),
            ))

    books.sort(key=lambda b: (b.title.casefold(), b.rel))
    return books


def author_key(author: str) -> str:
    """A stable, ascii-safe URL segment for an author. Hashed rather than
    escaped because authors here are routinely CJK, and CrossPoint's URL
    handling is naive string work, not RFC 3986 resolution."""
    return hashlib.sha1(author.encode("utf-8")).hexdigest()[:10]


def series_key(series: str) -> str:
    return hashlib.sha1(series.casefold().encode("utf-8")).hexdigest()[:10]


def series_sort_key(book: Book) -> tuple:
    value = book.series_index
    if re.fullmatch(r"\d+(?:\.\d+)*", value or ""):
        return (0, tuple(int(part) for part in value.split(".")), book.base_title.casefold())
    if value:
        return (1, value.casefold(), book.base_title.casefold())
    return (2, (), book.base_title.casefold())


def group_by_series(books: Iterable[Book]) -> List[Tuple[str, str, str, List[Book]]]:
    """[(key, full name, alias, books)] in series order.

    Books without series metadata are intentionally absent: "By series" is a
    grouping, not a second spelling of the entire catalog.
    """
    groups: Dict[str, Tuple[str, List[Book]]] = {}
    for book in books:
        if book.series:
            _, group = groups.setdefault(book.series.casefold(), (book.series, []))
            group.append(book)
    ordered = sorted((value for value in groups.values()),
                     key=lambda item: item[0].casefold())
    return [(series_key(name), name,
             next((b.series_alias for b in group if b.series_alias), ""),
             sorted(group, key=series_sort_key))
            for name, group in ordered]


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
    """Case-insensitive substring over title, author and full series — what a five-button
    on-screen keyboard can realistically drive."""
    q = query.strip().casefold()
    if not q:
        return []
    return [b for b in books
            if q in b.title.casefold() or q in b.base_title.casefold()
            or q in b.author.casefold() or q in b.series.casefold()]


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
        series = [{"key": key, "name": name, "alias": alias,
                   "count": len(group), "book_ids": [b.id for b in group]}
                  for key, name, alias, group in group_by_series(books)]
        json.dump({"roots": [str(r) for r in roots], "count": len(books),
                   "books": [b.as_dict() for b in books], "series": series},
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
