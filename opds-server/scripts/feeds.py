#!/usr/bin/env python3
"""Atom feed generation — OPDS 1.2, shaped by what CrossPoint actually parses.

Every non-obvious choice in this file is a consequence of the firmware's client,
read from source (`lib/OpdsParser/OpdsParser.cpp`,
`src/activities/browser/OpdsBookBrowserActivity.cpp`, `src/util/UrlUtils.cpp` in
crosspoint-reader, master @ 2026-07). `reference/readers.md` carries the summary;
the rules that shape the bytes below are:

  - **Atom XML only.** The client is expat over an Atom feed. There is no OPDS
    2.0 / JSON path.
  - **An entry needs a non-empty title *and* a resolved href**, or it is dropped
    without a word. Everything emitted here is checked for both.
  - **An acquisition link needs `rel` containing `opds-spec.org/acquisition` and
    `type` *exactly* `application/epub+zip`.** No `;profile=` suffix, no other
    media type — an entry whose only link is, say, a PDF simply is not a book.
  - **A navigation link needs `type` containing `application/atom+xml`.**
  - **Acquisition hrefs should end in `.epub`.** With several acquisition links
    on one entry the client prefers an href containing `.epub` or `/epub/`.
  - **Search is a template, not a descriptor.** The client takes the `rel=search`
    href *literally* and substitutes `{searchTerms}`; it never fetches an
    OpenSearch description document. A conventional descriptor-only link
    silently disables search. We emit both — the descriptor for other clients,
    the template for this one — and the client picks the templated href because
    it ignores any `rel=search` without the placeholder.
  - **Pagination is `rel="next"` / `rel="previous"`, feed level only.** Spelled
    `previous`; `prev` is not recognised, and either inside an entry is ignored.
  - **URLs are emitted absolute.** `UrlUtils::buildUrl` is naive string work
    rather than RFC 3986 resolution — a relative href is appended to the current
    feed URL, so `.../authors/ab12` + `2` yields `.../authors/ab12/2`. Absolute
    URLs are passed through untouched, so we only ever emit those.
  - **No covers.** The parser has no thumbnail handling whatsoever, and the
    device has ~400 KB of RAM; a cover link would be bytes it reads and drops.
  - **No `dc:identifier` inside an entry.** The client matches element names
    with `strstr(name, ":id")`, which `dc:identifier` satisfies — it would
    overwrite the entry's id with the book's ISBN/UUID.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Sequence, Tuple
from xml.sax.saxutils import escape, quoteattr

# The exact strings the client matches on. Do not "tidy" these.
ACQUISITION_REL = "http://opds-spec.org/acquisition"
EPUB_TYPE = "application/epub+zip"
NAV_TYPE = "application/atom+xml;profile=opds-catalog;kind=navigation"
ACQ_TYPE = "application/atom+xml;profile=opds-catalog;kind=acquisition"
OPENSEARCH_TYPE = "application/opensearchdescription+xml"

FEED_OPEN = ('<feed xmlns="http://www.w3.org/2005/Atom" '
             'xmlns:dc="http://purl.org/dc/elements/1.1/" '
             'xmlns:opds="http://opds-spec.org/2010/catalog">')

# XML 1.0 forbids most control characters outright; a stray one in a title would
# make the whole feed unparseable on the device.
_ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _clean(text: str) -> str:
    return _ILLEGAL_XML.sub("", text or "").strip()


def rfc3339(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _link(rel: str, href: str, type_: Optional[str] = None, **attrs: str) -> str:
    parts = [f"<link rel={quoteattr(rel)} href={quoteattr(href)}"]
    if type_:
        parts.append(f"type={quoteattr(type_)}")
    for key, value in attrs.items():
        parts.append(f"{key}={quoteattr(str(value))}")
    return " ".join(parts) + "/>"


def search_links(base_url: str) -> List[str]:
    """Descriptor first for conventional clients, template second for this one.
    Both carry `rel="search"`; CrossPoint keeps only the href containing the
    `{searchTerms}` placeholder, so the pair cannot confuse it."""
    return [
        _link("search", f"{base_url}/opds/opensearch.xml", OPENSEARCH_TYPE),
        _link("search", f"{base_url}/opds/search?q={{searchTerms}}", "application/atom+xml"),
    ]


def book_url(base_url: str, book) -> str:
    """`/book/<id>/<slug>.epub` — the id resolves it, the slug is decoration,
    and the `.epub` suffix is what makes the client prefer this link."""
    return f"{base_url}/book/{book.id}/{book.slug}.epub"


def _feed_header(*, feed_id: str, title: str, updated: str, self_url: str,
                 self_type: str, start_url: str, base_url: str,
                 next_url: Optional[str], prev_url: Optional[str]) -> List[str]:
    out = ["<?xml version=\"1.0\" encoding=\"UTF-8\"?>", FEED_OPEN,
           f"  <id>{escape(feed_id)}</id>",
           f"  <title>{escape(_clean(title))}</title>",
           f"  <updated>{updated}</updated>",
           # Ignored by CrossPoint (feed-level metadata is skipped outside
           # entries) but required for a valid Atom feed.
           "  <author><name>x3-suite opds-server</name></author>",
           "  " + _link("self", self_url, self_type),
           "  " + _link("start", f"{start_url}", NAV_TYPE)]
    for link in search_links(base_url):
        out.append("  " + link)
    # Feed level only: inside an entry the client ignores these.
    if prev_url:
        out.append("  " + _link("previous", prev_url, self_type))
    if next_url:
        out.append("  " + _link("next", next_url, self_type))
    return out


def navigation_entry(*, title: str, href: str, entry_id: str, summary: str = "",
                     kind_type: str = NAV_TYPE) -> str:
    """A catalog link. Dropped by the client unless title and href are both
    non-empty, so callers must not pass blanks."""
    title = _clean(title)
    if not title or not href:
        raise ValueError("navigation entry needs both a title and an href")
    lines = ["  <entry>",
             f"    <title>{escape(title)}</title>",
             f"    <id>{escape(entry_id)}</id>",
             f"    <updated>{rfc3339(datetime.now(tz=timezone.utc).timestamp())}</updated>"]
    if summary:
        lines.append(f"    <content type=\"text\">{escape(_clean(summary))}</content>")
    lines.append("    " + _link("subsection", href, kind_type))
    lines.append("  </entry>")
    return "\n".join(lines)


def book_entry(book, base_url: str) -> str:
    """One downloadable book. `<author><name>` is omitted when unknown rather
    than emitted empty: the client builds the SD-card filename as
    `<author> - <title>.epub` and an empty author would leave a dangling dash."""
    title = _clean(book.title) or book.slug
    href = book_url(base_url, book)
    lines = ["  <entry>",
             f"    <title>{escape(title)}</title>",
             # urn:x3:book:<id>, never dc:identifier — see this module's docstring.
             f"    <id>urn:x3:book:{escape(book.id)}</id>",
             f"    <updated>{rfc3339(book.mtime)}</updated>"]
    author = _clean(book.author)
    if author:
        lines.append(f"    <author><name>{escape(author)}</name></author>")
    language = _clean(book.language)
    if language:
        lines.append(f"    <dc:language>{escape(language)}</dc:language>")
    lines.append(f"    <content type=\"text\">{escape(f'{book.size / 1024:.0f} KB · {book.rel}')}</content>")
    lines.append("    " + _link(ACQUISITION_REL, href, EPUB_TYPE, length=str(book.size)))
    lines.append("  </entry>")
    return "\n".join(lines)


def navigation_feed(*, feed_id: str, title: str, self_url: str, start_url: str,
                    base_url: str, entries: Sequence[str],
                    updated: Optional[str] = None) -> str:
    updated = updated or rfc3339(datetime.now(tz=timezone.utc).timestamp())
    out = _feed_header(feed_id=feed_id, title=title, updated=updated, self_url=self_url,
                       self_type=NAV_TYPE, start_url=start_url, base_url=base_url,
                       next_url=None, prev_url=None)
    out.extend(entries)
    out.append("</feed>")
    return "\n".join(out) + "\n"


def acquisition_feed(*, feed_id: str, title: str, self_url: str, start_url: str,
                     base_url: str, books: Iterable, next_url: Optional[str] = None,
                     prev_url: Optional[str] = None, updated: Optional[str] = None) -> str:
    books = list(books)
    if updated is None:
        newest = max((b.mtime for b in books), default=datetime.now(tz=timezone.utc).timestamp())
        updated = rfc3339(newest)
    out = _feed_header(feed_id=feed_id, title=title, updated=updated, self_url=self_url,
                       self_type=ACQ_TYPE, start_url=start_url, base_url=base_url,
                       next_url=next_url, prev_url=prev_url)
    out.extend(book_entry(b, base_url) for b in books)
    out.append("</feed>")
    return "\n".join(out) + "\n"


def opensearch_description(base_url: str, catalog_title: str) -> str:
    """For clients that do fetch a descriptor. CrossPoint never asks for this."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/">\n'
        f'  <ShortName>{escape(_clean(catalog_title))}</ShortName>\n'
        f'  <Description>Search {escape(_clean(catalog_title))}</Description>\n'
        '  <InputEncoding>UTF-8</InputEncoding>\n'
        f'  <Url type={quoteattr(ACQ_TYPE)} '
        f'template={quoteattr(f"{base_url}/opds/search?q={{searchTerms}}")}/>\n'
        '</OpenSearchDescription>\n'
    )


def paginate(items: Sequence, page: int, page_size: int) -> Tuple[Sequence, bool, bool]:
    """(page_items, has_prev, has_next) for a 1-based page number."""
    page = max(1, page)
    start = (page - 1) * page_size
    window = items[start:start + page_size]
    return window, page > 1, start + page_size < len(items)
