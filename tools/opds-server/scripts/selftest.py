#!/usr/bin/env python3
"""The gate: does the X3's own client see every book we serve?

Run after changing anything in this directory:

    .venv/bin/python tools/opds-server/scripts/selftest.py

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
import zipfile
from pathlib import Path
from typing import List, Optional, Tuple

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parents[2]
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO / "epub-builder" / "scripts"))

import build_epub  # noqa: E402
import crosspoint_client as cc  # noqa: E402
import epub_metadata  # noqa: E402
import feeds  # noqa: E402
import ingest_book  # noqa: E402
import library  # noqa: E402
import serve_opds  # noqa: E402
import verify_epub  # noqa: E402

# One fixture per hazard: CJK metadata, a plain Latin book, characters that must
# survive XML escaping, a book with no author, and two ordered series volumes.
FIXTURES = [
    {"slug": "yugong", "title": "愚公移山", "author": "分级读物 (HSK 1-3)", "language": "zh"},
    {"slug": "alcaldes", "title": "Los alcaldes encontrados", "author": "Tirso de Molina",
     "language": "es"},
    {"slug": "escaping", "title": 'Ampersands & "angles" <tags>', "author": "Q & A",
     "language": "en"},
    {"slug": "anonymous", "title": "Anonymous notebook", "author": "", "language": "en"},
    {"slug": "fellowship", "title": "The Fellowship of the Ring",
     "author": "J. R. R. Tolkien", "language": "en",
     "series": "The Lord of the Rings", "series_index": "1"},
    {"slug": "two-towers", "title": "The Two Towers",
     "author": "J. R. R. Tolkien", "language": "en",
     "series": "The Lord of the Rings", "series_index": "2"},
]

PAGE_SIZE = 2  # forces pagination


def expected_title(fixture: dict) -> str:
    if fixture.get("series"):
        return library.catalog_title(fixture["title"], "LOTR", fixture["series_index"])
    return fixture["title"]


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
            "language": fixture["language"],
            "series": fixture.get("series", ""),
            "series_index": fixture.get("series_index", ""),
            "chapters": [{"source": "chapters/ch01.md"}],
        }, ensure_ascii=False), encoding="utf-8")

        # No glossary and no annotation pass: these fixtures exist to be
        # served, not to exercise the reader pipeline, so the builder runs with
        # zero CJK dependencies even for the CJK-titled one.
        chapters, meta = build_epub.assemble(book_dir)
        build_epub.write_epub(book_dir / "build" / f"{fixture['slug']}.epub",
                              meta["title"], meta.get("author", ""),
                              meta.get("language", "en"), chapters,
                              series=meta.get("series", ""),
                              series_index=meta.get("series_index", ""))
    (root / library.ALIAS_FILE).write_text(json.dumps({
        "version": 1, "aliases": {"The Lord of the Rings": "LOTR"},
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def make_metadata_epub(path: Path, title: str, author: str, series: str,
                       series_index: str, *, version: str = "2.0",
                       signed: bool = False) -> Path:
    """A small third-party-shaped EPUB for metadata-ingest regressions."""
    container = ('<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                 '<rootfiles><rootfile full-path="content.opf"/></rootfiles></container>')
    opf = (f'<package xmlns="http://www.idpf.org/2007/opf" version="{version}"><metadata '
           'xmlns:dc="http://purl.org/dc/elements/1.1/">'
           f'<dc:title>{title}</dc:title><dc:creator>{author}</dc:creator>'
           '<dc:language>en</dc:language>'
           f'<meta name="calibre:series" content="{series}"/>'
           f'<meta name="calibre:series_index" content="{series_index}"/>'
           '</metadata></package>')
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip",
                         compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("content.opf", opf)
        archive.writestr("chapter.xhtml", "<html><body>Keep me exactly.</body></html>")
        if signed:
            archive.writestr("META-INF/signatures.xml", "<signatures/>")
    return path


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

        # Every case below hands the scanner an explicit root, which is why the
        # default one could point at a directory that does not exist — after
        # this unit moved under tools/ — while every check here still passed
        # and the catalog served nothing.
        print("0. the default library root is inside the repo")
        import config as server_config
        all_ok &= check("REPO_ROOT holds AGENTS.md and epub-builder",
                        (server_config.REPO_ROOT / "AGENTS.md").exists()
                        and (server_config.REPO_ROOT / "epub-builder").is_dir(),
                        str(server_config.REPO_ROOT))
        all_ok &= check("the default root resolves to the repo's workspace/",
                        server_config.load_config()["library_roots"]
                        == [server_config.REPO_ROOT / "workspace"],
                        str(server_config.load_config()["library_roots"]))
        staged = root / "inbox" / "waiting-for-alias.epub"
        staged.parent.mkdir()
        shutil.copy2(root / "fellowship" / "build" / "fellowship.epub", staged)
        all_ok &= check("Telegram's pending/rejected inbox is never catalog content",
                        staged not in [Path(book.path) for book in library.scan(
                            [root], server_config.load_config()["exclude"])])
        old_config = tmp / "old-opds-config.json"
        old_config.write_text(json.dumps({"library_roots": [str(root)], "exclude": []}))
        all_ok &= check("an older local config cannot accidentally re-enable inbox",
                        "inbox/*" in server_config.load_config(old_config)["exclude"])
        staged.unlink()

        print("1. library scan")
        books = library.scan([root], [])
        all_ok &= check(f"found {len(FIXTURES)} books", len(books) == len(FIXTURES),
                        f"got {len(books)}")
        by_title = {b.base_title: b for b in books}
        for fixture in FIXTURES:
            book = by_title.get(fixture["title"])
            all_ok &= check(f"metadata from OPF: {fixture['title'][:24]}",
                            book is not None and book.author == fixture["author"]
                            and book.language == fixture["language"]
                            and book.title == expected_title(fixture),
                            "title, author, language or display title did not survive")
        groups = library.group_by_series(books)
        all_ok &= check("series metadata groups and sorts by volume",
                        len(groups) == 1 and groups[0][1:3] == (
                            "The Lord of the Rings", "LOTR")
                        and [b.series_index for b in groups[0][3]] == ["1", "2"],
                        str([(name, alias, [b.series_index for b in group])
                             for _, name, alias, group in groups]))
        all_ok &= check("a missing volume number is not invented",
                        library.catalog_title("Unknown volume", "SER", "")
                        == "SER - Unknown volume")
        all_ok &= check("series positions use natural numbers without zero padding",
                        library.catalog_title("Ninth", "SER", "09") == "SER 9 - Ninth"
                        and library.catalog_title("Tenth", "SER", "10")
                        == "SER 10 - Tenth"
                        and library.format_series_index("1.5") == "1.5")
        all_ok &= check("title cleanup leaves an unproved prefix alone",
                        library.clean_embedded_title(
                            "Anniversary Edition - Lonesome Dove", "Larry McMurtry",
                            "Lonesome Dove", "1.0")
                        == "Anniversary Edition - Lonesome Dove")
        all_ok &= check("book ids are stable across rescans",
                        [b.id for b in library.scan([root], [])] == [b.id for b in books])

        print("1b. metadata-driven ingest")
        catalog = tmp / "ingest-catalog"
        catalog.mkdir()
        source = tmp / "The_Lord_of_the_Rings_download-site-name-and-noise.epub"
        shutil.copy2(root / "fellowship" / "build" / "fellowship.epub", source)
        original_bytes = source.read_bytes()
        first = ingest_book.ingest(source, catalog)
        all_ok &= check("a new series pauses before filing",
                        first["status"] == "needs_alias" and source.exists(), str(first))
        all_ok &= check("the transparent alias suggestion is LOTR",
                        first.get("suggested_alias") == "LOTR", str(first))
        filed = ingest_book.ingest(source, catalog, "LOTR")
        destination = Path(filed.get("destination", "missing"))
        all_ok &= check("the confirmed alias files the book canonically",
                        filed["status"] == "filed" and destination.exists()
                        and destination.name
                        == "LOTR 1 - The Fellowship of the Ring - J. R. R. Tolkien.epub",
                        str(filed))
        all_ok &= check("ingest changes no EPUB bytes",
                        destination.read_bytes() == original_bytes
                        and not source.exists())
        duplicate = tmp / "duplicate.epub"
        duplicate.write_bytes(original_bytes)
        duplicate_report = ingest_book.ingest(duplicate, catalog)
        all_ok &= check("an identical destination is never overwritten or consumed",
                        duplicate_report["status"] == "already_present"
                        and duplicate.exists() and destination.read_bytes() == original_bytes,
                        str(duplicate_report))
        padded_destination = destination.with_name(
            "LOTR 01 - The Fellowship of the Ring - J. R. R. Tolkien.epub")
        destination.rename(padded_destination)
        padding_preview = ingest_book.normalize(catalog, dry_run=True)
        padding_report = ingest_book.normalize(catalog)
        all_ok &= check("catalog tidy migrates old zero-padded series filenames",
                        padding_preview["changed"] == 1
                        and padding_report["changed"] == 1
                        and destination.exists() and not padded_destination.exists()
                        and destination.read_bytes() == original_bytes,
                        f"preview={padding_preview}; applied={padding_report}")
        invalid = tmp / "broken.epub"
        invalid.write_bytes(b"not an epub")
        invalid_report = ingest_book.ingest(invalid, catalog)
        all_ok &= check("an invalid upload stays outside the catalog",
                        invalid_report["status"] == "invalid" and invalid.exists()
                        and len(library.scan([catalog], [])) == 1,
                        str(invalid_report))

        # A real-world dc:title can itself be a filename assembled from other
        # metadata. This is the exact Lonesome Dove report that exposed the
        # regression: both known prefixes must disappear, and nothing else.
        lonesome_source = tmp / "Lonesome_Dove_1_download-site.epub"
        make_metadata_epub(
            lonesome_source,
            "McMurtry, Larry - Lonesome Dove 01 - Lonesome Dove",
            "Larry McMurtry", "Lonesome Dove", "1.0")
        lonesome_bytes = lonesome_source.read_bytes()
        lonesome_pending = ingest_book.ingest(lonesome_source, catalog)
        all_ok &= check("the noisy embedded title still pauses for a human alias",
                        lonesome_pending["status"] == "needs_alias"
                        and lonesome_source.exists(), str(lonesome_pending))
        lonesome_report = ingest_book.ingest(lonesome_source, catalog, "LoDove")
        lonesome = catalog / "LoDove 1 - Lonesome Dove - Larry McMurtry.epub"
        all_ok &= check("known author and series labels are not duplicated in the title",
                        lonesome_report["status"] == "filed"
                        and lonesome_report["base_title"] == "Lonesome Dove"
                        and lonesome_report["title"] == "LoDove 1 - Lonesome Dove"
                        and lonesome.exists(), str(lonesome_report))
        all_ok &= check("the Lonesome Dove cleanup changes no EPUB bytes",
                        lonesome.read_bytes() == lonesome_bytes)

        old_lonesome = catalog / (
            "LoDove 1 - McMurtry, Larry - Lonesome Dove 01 - Lonesome Dove "
            "- Larry McMurtry.epub")
        lonesome.rename(old_lonesome)
        repair_preview = ingest_book.normalize(catalog, dry_run=True)
        repaired = ingest_book.normalize(catalog)
        all_ok &= check("normalization repairs the already-filed Lonesome Dove name",
                        repair_preview["changed"] == 1 and old_lonesome.exists() is False
                        and repaired["status"] == "normalized" and repaired["changed"] == 1
                        and lonesome.exists() and lonesome.read_bytes() == lonesome_bytes,
                        f"preview={repair_preview}; applied={repaired}")

        lonesome.rename(old_lonesome)
        repeat_upload = tmp / "Lonesome_Dove_repeat-upload.epub"
        repeat_upload.write_bytes(lonesome_bytes)
        repeat_report = ingest_book.ingest(repeat_upload, catalog)
        all_ok &= check("re-uploading the same book repairs its old catalog name",
                        repeat_report["status"] == "already_present"
                        and Path(repeat_report.get("renamed_from", "")) == old_lonesome
                        and lonesome.exists() and not old_lonesome.exists()
                        and repeat_upload.exists() and lonesome.read_bytes() == lonesome_bytes,
                        str(repeat_report))

        odd = catalog / "download-site--two-towers.epub"
        shutil.copy2(root / "two-towers" / "build" / "two-towers.epub", odd)
        dry = ingest_book.normalize(catalog, dry_run=True)
        all_ok &= check("normalization previews without moving anything",
                        dry["status"] == "dry_run" and dry["changed"] == 1
                        and odd.exists(), str(dry))
        normalized = ingest_book.normalize(catalog)
        towers = catalog / "LOTR 2 - The Two Towers - J. R. R. Tolkien.epub"
        all_ok &= check("normalization then renames metadata-only",
                        normalized["status"] == "normalized" and towers.exists()
                        and towers.read_bytes()
                        == (root / "two-towers" / "build" / "two-towers.epub").read_bytes(),
                        str(normalized))
        changed = ingest_book.set_alias(catalog, "The Lord of the Rings", "RINGS")
        all_ok &= check("changing an alias renames the whole series together",
                        changed["changed"] == 2
                        and (catalog / "RINGS 1 - The Fellowship of the Ring - J. R. R. Tolkien.epub").exists()
                        and (catalog / "RINGS 2 - The Two Towers - J. R. R. Tolkien.epub").exists(),
                        str(changed))

        series_names = tmp / "series-name-catalog"
        series_names.mkdir()
        old_one = make_metadata_epub(
            series_names / "old-one.epub", "First", "Series Author",
            "Old Cycle", "1", version="3.0")
        old_two = make_metadata_epub(
            series_names / "old-two.epub", "Second", "Series Author",
            "Old Cycle", "2", version="3.0")
        with zipfile.ZipFile(old_one) as archive:
            old_chapter = archive.read("chapter.xhtml")
        ingest_book.set_alias(series_names, "Old Cycle", "OLD")
        rename_preview = epub_metadata.rename_series(
            series_names, "Old Cycle", "New Cycle", dry_run=True)
        rename_report = epub_metadata.rename_series(
            series_names, "Old Cycle", "New Cycle",
            expected_sha256s=rename_preview["expected_sha256s"])
        renamed_one = series_names / "OLD 1 - First - Series Author.epub"
        renamed_two = series_names / "OLD 2 - Second - Series Author.epub"
        with zipfile.ZipFile(renamed_one) as archive:
            renamed_chapter = archive.read("chapter.xhtml")
        aliases_after_rename = library.load_aliases(series_names)
        all_ok &= check("a full series-name preview applies every volume together",
                        rename_preview["status"] == "dry_run"
                        and rename_report["status"] == "renamed"
                        and library.read_opf_metadata(renamed_one)["series"] == "New Cycle"
                        and library.read_opf_metadata(renamed_two)["series"] == "New Cycle"
                        and aliases_after_rename == {"New Cycle": "OLD"},
                        f"{rename_preview}; {rename_report}; {aliases_after_rename}")
        all_ok &= check("a full series rename preserves non-OPF book resources",
                        renamed_chapter == old_chapter)

        target = make_metadata_epub(
            series_names / "target.epub", "Third", "Series Author",
            "Existing Cycle", "3", version="3.0")
        ingest_book.set_alias(series_names, "Existing Cycle", "EXIST")
        merge_question = epub_metadata.rename_series(
            series_names, "New Cycle", "Existing Cycle", dry_run=True)
        merge_preview = epub_metadata.rename_series(
            series_names, "New Cycle", "Existing Cycle", merge=True, dry_run=True)
        merge_report = epub_metadata.rename_series(
            series_names, "New Cycle", "Existing Cycle", merge=True,
            expected_sha256s=merge_preview["expected_sha256s"])
        merged = library.group_by_series(library.scan([series_names], []))
        all_ok &= check("an existing name requires an explicit merge",
                        merge_question["status"] == "needs_merge"
                        and merge_preview["status"] == "dry_run"
                        and merge_preview["series_alias"] == "EXIST",
                        f"{merge_question}; {merge_preview}")
        all_ok &= check("an explicit merge uses the destination identity",
                        merge_report["status"] == "renamed"
                        and len(merged) == 1 and merged[0][1] == "Existing Cycle"
                        and len(merged[0][3]) == 3
                        and library.load_aliases(series_names)
                        == {"Existing Cycle": "EXIST"},
                        f"{merge_report}; {merged}")

        print("1c. catalog audit and explicit metadata editing")
        steward = tmp / "steward-catalog"
        steward.mkdir()
        volume_one = make_metadata_epub(
            steward / "download-one.epub", "Master and Commander",
            "Patrick O'Brian", "Aubrey-Maturin", "1")
        volume_two = make_metadata_epub(
            steward / "download-two.epub", "Post Captain",
            "Patrick O'Brian", "Aubrey-Maturin", "2")
        one_bytes, two_bytes = volume_one.read_bytes(), volume_two.read_bytes()
        damaged = steward / "damaged.epub"
        damaged.write_bytes(b"not a zip")
        audit_report = ingest_book.audit(steward)
        all_ok &= check("audit asks once for a series shared by two books",
                        audit_report["status"] == "needs_alias"
                        and len(audit_report["missing_series"]) == 1
                        and audit_report["missing_series"][0]["count"] == 2,
                        str(audit_report))
        all_ok &= check("audit reports an unreadable book and leaves it untouched",
                        len(audit_report["invalid"]) == 1
                        and damaged.read_bytes() == b"not a zip", str(audit_report))
        tidy_preview = ingest_book.reconcile(
            steward, {"Aubrey-Maturin": "A-M"}, dry_run=True)
        all_ok &= check("the whole tidy plan preflights before moving anything",
                        tidy_preview["status"] == "dry_run"
                        and tidy_preview["changed"] == 2
                        and volume_one.exists() and volume_two.exists(),
                        str(tidy_preview))
        tidy_answers = iter(["A-M", "yes"])
        tidy_prompts = []
        tidy_report = ingest_book.tidy_interactive(
            steward, input_fn=lambda prompt: (
                tidy_prompts.append(prompt), next(tidy_answers))[1])
        tidy_one = steward / "A-M 1 - Master and Commander - Patrick O'Brian.epub"
        tidy_two = steward / "A-M 2 - Post Captain - Patrick O'Brian.epub"
        all_ok &= check("tidy stores one alias and renames both readable volumes",
                        tidy_report["status"] == "reconciled"
                        and tidy_one.read_bytes() == one_bytes
                        and tidy_two.read_bytes() == two_bytes
                        and damaged.read_bytes() == b"not a zip", str(tidy_report))
        all_ok &= check("the terminal wizard asks once for the series, then once to apply",
                        len(tidy_prompts) == 2
                        and tidy_prompts[0].startswith("Short name for Aubrey-Maturin")
                        and tidy_prompts[1].startswith("Apply this catalog-only plan"),
                        str(tidy_prompts))

        editable = make_metadata_epub(
            steward / "wrong-download-name.epub", "Old title", "Old Author", "", "",
            version="3.0")
        with zipfile.ZipFile(editable) as archive:
            chapter_before = archive.read("chapter.xhtml")
        updates = {"title": "The New Title", "author": "New Author",
                   "series": "Earthsea Cycle", "series_index": "1",
                   "language": "en-GB"}
        edit_preview = epub_metadata.edit_metadata(
            editable, steward, updates, alias="EARTH", dry_run=True)
        all_ok &= check("metadata editing has a read-only exact preview",
                        edit_preview["status"] == "dry_run"
                        and set(edit_preview["changed_fields"]) == set(updates)
                        and editable.exists(), str(edit_preview))
        edit_report = epub_metadata.edit_metadata(
            editable, steward, updates, alias="EARTH")
        edited = steward / "EARTH 1 - The New Title - New Author.epub"
        edited_meta = library.read_opf_metadata(edited)
        with zipfile.ZipFile(edited) as archive:
            edited_opf = archive.read("content.opf").decode("utf-8")
            chapter_after = archive.read("chapter.xhtml")
        all_ok &= check("an explicit edit changes all five catalog fields",
                        edit_report["status"] == "updated" and edited.exists()
                        and edited_meta == updates, f"{edit_report}; {edited_meta}")
        all_ok &= check("EPUB 3 series metadata uses standard refinements plus compatibility fields",
                        'property="belongs-to-collection"' in edited_opf
                        and 'property="collection-type"' in edited_opf
                        and 'property="group-position"' in edited_opf
                        and 'name="calibre:series"' in edited_opf,
                        edited_opf[:500])
        all_ok &= check("editing preserves every non-OPF resource byte-for-byte",
                        chapter_after == chapter_before)
        all_ok &= check("the edited book is immediately canonical in the catalog",
                        not ingest_book.audit(steward)["renames"])

        builder_edit_dir = tmp / "builder-edit"
        builder_edit_dir.mkdir()
        builder_edit_source = builder_edit_dir / "builder-copy.epub"
        shutil.copy2(root / "alcaldes" / "build" / "alcaldes.epub",
                     builder_edit_source)
        builder_edit = epub_metadata.edit_metadata(
            builder_edit_source, builder_edit_dir,
            {"title": "Los alcaldes revisados"})
        builder_edited = builder_edit_dir / (
            "Los alcaldes revisados - Tirso de Molina.epub")
        builder_verify = verify_epub.verify_integrity(builder_edited)
        all_ok &= check("an edited suite-built EPUB still passes the strict builder gate",
                        builder_edit["status"] == "updated"
                        and builder_verify["pass"], str(builder_verify))

        signed = make_metadata_epub(
            steward / "signed.epub", "Signed", "Author", "", "",
            version="3.0", signed=True)
        signed_bytes = signed.read_bytes()
        refused = epub_metadata.edit_metadata(
            signed, steward, {"title": "Changed"})
        all_ok &= check("a signed EPUB is refused instead of silently invalidated",
                        refused["status"] == "invalid" and signed.read_bytes() == signed_bytes,
                        str(refused))
        all_ok &= check("required title and language cannot be cleared",
                        epub_metadata.edit_metadata(
                            edited, steward, {"title": ""})["status"] == "invalid"
                        and epub_metadata.edit_metadata(
                            edited, steward, {"language": ""})["status"] == "invalid")

        collision_source = make_metadata_epub(
            steward / "collision-source.epub", "Before", "Clash", "", "",
            version="3.0")
        collision_bytes = collision_source.read_bytes()
        collision_target = make_metadata_epub(
            steward / "Taken - Clash.epub", "Taken", "Clash", "", "",
            version="3.0")
        target_bytes = collision_target.read_bytes()
        collision_report = epub_metadata.edit_metadata(
            collision_source, steward, {"title": "Taken"})
        all_ok &= check("metadata filename collisions abort before any write",
                        collision_report["status"] == "conflict"
                        and collision_source.read_bytes() == collision_bytes
                        and collision_target.read_bytes() == target_bytes,
                        str(collision_report))

        rollback_dir = tmp / "metadata-rollback"
        rollback_dir.mkdir()
        rollback_source = make_metadata_epub(
            rollback_dir / "rollback.epub", "Rollback", "Author", "", "",
            version="3.0")
        rollback_bytes = rollback_source.read_bytes()
        original_alias_writer = library.write_alias_document
        try:
            library.write_alias_document = lambda *args, **kwargs: (
                _ for _ in ()).throw(OSError("simulated sidecar failure"))
            rollback_report = epub_metadata.edit_metadata(
                rollback_source, rollback_dir, {"series": "Rollback Cycle"},
                alias="ROLL")
        finally:
            library.write_alias_document = original_alias_writer
        all_ok &= check("a late sidecar failure restores the exact original EPUB",
                        rollback_report["status"] == "error"
                        and rollback_source.read_bytes() == rollback_bytes
                        and not list(rollback_dir.glob(".*metadata*")),
                        str(rollback_report))

        race_source = make_metadata_epub(
            steward / "race.epub", "Race", "Author", "", "", version="3.0")
        race_preview = epub_metadata.edit_metadata(
            race_source, steward, {"title": "Race changed"}, dry_run=True)
        race_source.write_bytes(race_source.read_bytes() + b"external change")
        race_bytes = race_source.read_bytes()
        race_report = epub_metadata.edit_metadata(
            race_source, steward, {"title": "Race changed"},
            expected_sha256=race_preview["sha256"])
        all_ok &= check("a book changed after preview is never overwritten",
                        race_report["status"] == "invalid"
                        and race_source.read_bytes() == race_bytes,
                        str(race_report))

        wizard_source = make_metadata_epub(
            steward / "wizard-download.epub", "Wizard Book", "Old Name", "", "",
            version="3.0")
        wizard_answers = iter(["", "New Name", "", "", "", "yes"])
        wizard_prompts = []
        wizard_report = ingest_book.edit_interactive(
            wizard_source, steward,
            input_fn=lambda prompt: (
                wizard_prompts.append(prompt), next(wizard_answers))[1])
        all_ok &= check("the terminal metadata wizard uses the shared editor and confirmation",
                        wizard_report["status"] == "updated"
                        and (steward / "Wizard Book - New Name.epub").exists()
                        and len(wizard_prompts) == 6,
                        f"{wizard_report}; {wizard_prompts}")

        httpd, server_url = start_server(root)
        try:
            print("2. root feed, read by the device's parser")
            root_feed, error = cc.fetch_feed(f"{server_url}/opds")
            all_ok &= check("root feed fetches and parses", root_feed is not None, error)
            if root_feed is None:
                print("FAIL")
                return 1
            all_ok &= check("five navigation entries", len(root_feed.navigation()) == 5,
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
                            {b.title for b in walked} == {expected_title(f) for f in FIXTURES},
                            f"reached {sorted(b.title for b in walked)}")
            all_ok &= check("author grouping is reachable",
                            any("/opds/authors/" in url for url in visited))
            all_ok &= check("language grouping is reachable",
                            any("/opds/languages/" in url for url in visited))
            all_ok &= check("series grouping is reachable",
                            any("/opds/series/" in url for url in visited))
            series_key = library.series_key("The Lord of the Rings")
            series_books, _, series_error = collect_pages(
                server_url, f"{server_url}/opds/series/{series_key}")
            all_ok &= check("series feed is in volume order",
                            not series_error and [b.title for b in series_books] == [
                                "LOTR 1 - The Fellowship of the Ring",
                                "LOTR 2 - The Two Towers",
                            ], series_error or str([b.title for b in series_books]))

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
            # "n" matches enough fixtures to paginate —
            # and page 2 must still be *the search*, not the whole library.
            wide_url = template.replace(cc.SEARCH_PLACEHOLDER, "n")
            wide, pages, wide_error = collect_pages(server_url, wide_url)
            expected = {expected_title(f) for f in FIXTURES
                        if "n" in f["title"].casefold()
                        or "n" in f["author"].casefold()
                        or "n" in f.get("series", "").casefold()}
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
                            feed is not None and len(feed.navigation()) == 5, error)
            books_seen, _, auth_errors = walk(secure_url, "/opds", ("reader", "s3cret"))
            all_ok &= check("authenticated walk reaches every book",
                            not auth_errors
                            and {b.title for b in books_seen}
                            == {expected_title(f) for f in FIXTURES},
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
