#!/usr/bin/env python3
"""The gate: does the X3's own client see every book we serve?

Run after changing anything in this directory:

    .venv/bin/python opds-server/scripts/selftest.py

It builds a small library with the suite's real builder, serves it with the
real server on an ephemeral port, and then walks the whole catalog through
`crosspoint_client` — a port of the firmware's parser, URL joining and HTTP
behaviour (see that module for provenance). A generic OPDS validator would pass
feeds this device silently drops; this asks the only question that matters,
which is whether *that* client can reach every book and download it intact.

Checks, in order:
  1. the library scan finds the books and reads their metadata
  2. the root feed parses, and its navigation resolves as the device resolves it
  3. every navigation path leads somewhere real — no dead ends
  4. pagination round-trips: no book lost, none served twice
  5. search returns the right book through the templated URL
  6. a download arrives byte-identical and still passes the shared EPUB verifier
  7. the client-contract regressions (exact media types, .epub hrefs, no
     dc:identifier, XML escaping) that a valid-but-unreadable feed would trip
  8. failure modes: unknown ids, path-shaped ids, and Basic auth

Exit 0 = all good. No test framework needed.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from typing import List, Optional, Tuple

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parents[1]
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO / "epub-builder" / "scripts"))

import build_epub  # noqa: E402
import crosspoint_client as cc  # noqa: E402
import feeds  # noqa: E402
import library  # noqa: E402
import serve_opds  # noqa: E402
import verify_epub  # noqa: E402

# One fixture per hazard: CJK metadata, a plain Latin book, characters that must
# survive XML escaping, and a book with no author at all.
FIXTURES = [
    {"slug": "yugong", "title": "愚公移山", "author": "分级读物 (HSK 1-3)", "language": "zh"},
    {"slug": "alcaldes", "title": "Los alcaldes encontrados", "author": "Tirso de Molina",
     "language": "es"},
    {"slug": "escaping", "title": 'Ampersands & "angles" <tags>', "author": "Q & A",
     "language": "en"},
    {"slug": "anonymous", "title": "Anonymous notebook", "author": "", "language": "en"},
]

PAGE_SIZE = 2  # forces pagination over four books


# The server logs every request to stderr; that is right in production and pure
# noise inside a test that reports its own findings.
serve_opds.OpdsHandler.log_message = lambda *args, **kwargs: None


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'ok' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail and not ok else ""))
    return ok


def build_fixture_library(root: Path) -> None:
    """Real books from the real builder, in the suite's workspace layout."""
    for fixture in FIXTURES:
        book_dir = root / fixture["slug"]
        (book_dir / "chapters").mkdir(parents=True)
        (book_dir / "build").mkdir()
        (book_dir / "chapters" / "ch01.md").write_text(
            f"# {fixture['title']}\n\nOne short paragraph, enough to build.\n", encoding="utf-8")
        (book_dir / "book.json").write_text(json.dumps({
            "title": fixture["title"], "author": fixture["author"],
            "language": fixture["language"], "chapters": [{"source": "chapters/ch01.md"}],
        }, ensure_ascii=False), encoding="utf-8")

        # mode=None is the un-annotated path: no pinyin, and so no CJK
        # dependencies to install just to test a file server.
        chapters, meta = build_epub.assemble(book_dir, None)
        build_epub.write_epub(book_dir / "build" / f"{fixture['slug']}.epub",
                              meta["title"], meta.get("author", ""),
                              meta.get("language", "en"), chapters,
                              extended_css=True)


def start_server(root: Path, credentials: Optional[Tuple[str, str]] = None):
    cfg = {
        "library_roots": [root], "exclude": [], "host": "127.0.0.1", "port": 0,
        "page_size": PAGE_SIZE, "catalog_title": "Self-test library", "public_url": "",
    }
    httpd = serve_opds.create_server(cfg, credentials)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    host, port = httpd.server_address[0], httpd.server_address[1]
    return httpd, f"http://{host}:{port}"


def walk(server_url: str, start_path: str, credentials: Tuple[str, str] = ("", "")
         ) -> Tuple[List[cc.OpdsEntry], List[str], List[str]]:
    """Follow every navigation entry the way the device would. Returns
    (book entries, visited urls, errors)."""
    user, password = credentials
    books: List[cc.OpdsEntry] = []
    visited: List[str] = []
    errors: List[str] = []
    pending = [(start_path, cc.build_url(server_url, start_path))]
    seen = set()

    while pending:
        current_path, url = pending.pop(0)
        if url in seen or len(seen) > 50:
            continue
        seen.add(url)

        feed, error = cc.fetch_feed(url, user, password)
        if feed is None:
            errors.append(error)
            continue
        visited.append(url)

        for entry in feed.entries:
            target = cc.navigate(url, entry, server_url, current_path)
            if entry.type == cc.BOOK:
                books.append(entry)
            else:
                pending.append((target, target))
    return books, visited, errors


def collect_pages(server_url: str, first_url: str) -> Tuple[List[cc.OpdsEntry], List[str], str]:
    """Follow rel=next to the end. Returns (entries, page urls, error)."""
    entries: List[cc.OpdsEntry] = []
    pages: List[str] = []
    url = first_url
    while url and len(pages) < 20:
        feed, error = cc.fetch_feed(url)
        if feed is None:
            return entries, pages, error
        pages.append(url)
        entries.extend(feed.books())
        url = feed.next_page_url
    return entries, pages, ""


def main() -> int:
    all_ok = True
    tmp = Path(tempfile.mkdtemp(prefix="x3-opds-selftest-"))
    try:
        root = tmp / "workspace"
        root.mkdir()
        build_fixture_library(root)

        print("1. library scan")
        books = library.scan([root], [])
        all_ok &= check(f"found {len(FIXTURES)} books", len(books) == len(FIXTURES),
                        f"got {len(books)}")
        by_title = {b.title: b for b in books}
        for fixture in FIXTURES:
            book = by_title.get(fixture["title"])
            all_ok &= check(f"metadata from OPF: {fixture['title'][:24]}",
                            book is not None and book.author == fixture["author"]
                            and book.language == fixture["language"],
                            "title, author or language did not survive the round trip")
        all_ok &= check("book ids are stable across rescans",
                        [b.id for b in library.scan([root], [])] == [b.id for b in books])

        httpd, server_url = start_server(root)
        try:
            print("2. root feed, read by the device's parser")
            root_feed, error = cc.fetch_feed(f"{server_url}/opds")
            all_ok &= check("root feed fetches and parses", root_feed is not None, error)
            if root_feed is None:
                print("FAIL")
                return 1
            all_ok &= check("four navigation entries", len(root_feed.navigation()) == 4,
                            f"got {len(root_feed.navigation())}")
            all_ok &= check("no entry was silently dropped",
                            all(e.title and e.href for e in root_feed.entries))
            all_ok &= check("search template is a URL, not a descriptor",
                            cc.SEARCH_PLACEHOLDER in root_feed.search_template,
                            f"got {root_feed.search_template!r}")

            print("3. navigation resolves everywhere")
            walked, visited, errors = walk(server_url, "/opds")
            all_ok &= check("every navigation target answers", not errors, "; ".join(errors))
            all_ok &= check("walk reached every book",
                            {b.title for b in walked} == {f["title"] for f in FIXTURES},
                            f"reached {sorted(b.title for b in walked)}")
            all_ok &= check("author grouping is reachable",
                            any("/opds/authors/" in url for url in visited))
            all_ok &= check("language grouping is reachable",
                            any("/opds/languages/" in url for url in visited))

            print("4. pagination")
            paged, pages, page_error = collect_pages(server_url, f"{server_url}/opds/all")
            all_ok &= check("all books listed across pages", not page_error, page_error)
            all_ok &= check(f"{len(FIXTURES)} books over "
                            f"{-(-len(FIXTURES) // PAGE_SIZE)} pages",
                            len(paged) == len(FIXTURES) and len(pages) == -(-len(FIXTURES) // PAGE_SIZE),
                            f"got {len(paged)} books over {len(pages)} pages")
            all_ok &= check("no book served twice",
                            len({b.href for b in paged}) == len(paged))
            second, _ = cc.fetch_feed(f"{server_url}/opds/all?page=2")
            all_ok &= check("page 2 offers a way back",
                            second is not None and second.prev_page_url.endswith("page=1"),
                            f"got {second.prev_page_url if second else 'no feed'!r}")

            print("5. search")
            template = root_feed.search_template
            search_url = template.replace(cc.SEARCH_PLACEHOLDER, "alcaldes")
            found, error = cc.fetch_feed(search_url)
            all_ok &= check("templated search URL answers", found is not None, error)
            all_ok &= check("search finds the one book",
                            found is not None and [b.title for b in found.books()]
                            == ["Los alcaldes encontrados"],
                            f"got {[b.title for b in found.books()] if found else None}")
            # "n" matches three of the four fixtures, so the results paginate —
            # and page 2 must still be *the search*, not the whole library.
            wide_url = template.replace(cc.SEARCH_PLACEHOLDER, "n")
            wide, pages, wide_error = collect_pages(server_url, wide_url)
            expected = {f["title"] for f in FIXTURES
                        if "n" in f["title"].casefold() or "n" in f["author"].casefold()}
            all_ok &= check("a paginated search keeps its query", not wide_error, wide_error)
            all_ok &= check(f"search paged over {len(pages)} page(s) without widening",
                            {b.title for b in wide} == expected and len(pages) > 1,
                            f"got {sorted({b.title for b in wide})} over {len(pages)} page(s), "
                            f"expected {sorted(expected)}")

            print("6. download")
            target = next((b for b in walked if b.title == "愚公移山"), None)
            all_ok &= check("the CJK book is downloadable", target is not None)
            if target is not None:
                status, payload, headers = cc.fetch(target.href)
                source = (root / "yugong" / "build" / "yugong.epub").read_bytes()
                all_ok &= check("HTTP 200", status == 200, f"got {status}")
                all_ok &= check("bytes are identical to the built file", payload == source,
                                f"{len(payload)} bytes vs {len(source)}")
                all_ok &= check("served as application/epub+zip",
                                headers.get("Content-Type") == feeds.EPUB_TYPE,
                                str(headers.get("Content-Type")))
                downloaded = tmp / "downloaded.epub"
                downloaded.write_bytes(payload)
                report = verify_epub.verify_integrity(downloaded)
                all_ok &= check("downloaded EPUB passes the shared verifier",
                                report["pass"], str(report["errors"]))
                all_ok &= check("lands on the SD card under a sane name",
                                cc.sd_filename(target).endswith(".epub")
                                and "愚公移山" in cc.sd_filename(target),
                                cc.sd_filename(target))

            print("7. client-contract regressions")
            status, raw, _ = cc.fetch(f"{server_url}/opds/all")
            raw_text = raw.decode("utf-8")
            all_ok &= check("acquisition type is exactly application/epub+zip",
                            f'type="{feeds.EPUB_TYPE}"' in raw_text)
            all_ok &= check("no dc:identifier inside entries (client reads it as the id)",
                            "dc:identifier" not in raw_text)
            all_ok &= check("acquisition hrefs end in .epub",
                            all(b.href.endswith(".epub") for b in paged))
            all_ok &= check("entry ids survive intact",
                            all(b.id.startswith("urn:x3:book:") for b in paged),
                            str([b.id for b in paged]))
            escaped = next((b for b in paged if "Ampersands" in b.title), None)
            all_ok &= check("XML-hostile title round-trips exactly",
                            escaped is not None
                            and escaped.title == 'Ampersands & "angles" <tags>',
                            escaped.title if escaped else "not found")
            anonymous = next((b for b in paged if b.title == "Anonymous notebook"), None)
            all_ok &= check("an authorless book carries no empty author",
                            anonymous is not None and anonymous.author == "",
                            anonymous.author if anonymous else "not found")

            print("8. failure modes")
            status, _, _ = cc.fetch(f"{server_url}/book/deadbeefcafe/nope.epub")
            all_ok &= check("unknown book id is 404", status == 404, f"got {status}")
            status, _, _ = cc.fetch(f"{server_url}/book/..%2F..%2Fetc%2Fpasswd/x.epub")
            all_ok &= check("a path-shaped id resolves to nothing", status == 404, f"got {status}")
            status, _, _ = cc.fetch(f"{server_url}/opds/authors/nosuchauthor")
            all_ok &= check("unknown author is 404", status == 404, f"got {status}")
        finally:
            httpd.shutdown()
            httpd.server_close()

        print("   (auth)")
        secure, secure_url = start_server(root, credentials=("reader", "s3cret"))
        try:
            status, _, headers = cc.fetch(f"{secure_url}/opds")
            all_ok &= check("no credentials is 401", status == 401, f"got {status}")
            all_ok &= check("401 names the scheme the device speaks",
                            headers.get("WWW-Authenticate", "").startswith("Basic"),
                            headers.get("WWW-Authenticate", ""))
            status, _, _ = cc.fetch(f"{secure_url}/opds", "reader", "wrong")
            all_ok &= check("wrong password is 401", status == 401, f"got {status}")
            status, _, _ = cc.fetch(f"{secure_url}/opds", "reader", "")
            all_ok &= check("half-configured credentials are not sent (as on the device)",
                            status == 401, f"got {status}")
            feed, error = cc.fetch_feed(f"{secure_url}/opds", "reader", "s3cret")
            all_ok &= check("correct credentials browse normally",
                            feed is not None and len(feed.navigation()) == 4, error)
            books_seen, _, auth_errors = walk(secure_url, "/opds", ("reader", "s3cret"))
            all_ok &= check("authenticated walk reaches every book",
                            not auth_errors
                            and {b.title for b in books_seen} == {f["title"] for f in FIXTURES},
                            "; ".join(auth_errors) or f"{sorted({b.title for b in books_seen})}")
        finally:
            secure.shutdown()
            secure.server_close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("PASS" if all_ok else "FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
