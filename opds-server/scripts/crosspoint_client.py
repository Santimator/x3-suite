#!/usr/bin/env python3
"""A port of the X3's OPDS client — the oracle the self-test grades against.

Every service in this suite gates its output with a deterministic check, and the
check that matters for a catalog is not "is this valid OPDS?" but "will *this
reader* see every book?". Those differ: a feed can be perfectly valid and still
lose entries on the device, because the client matches on exact strings, joins
URLs with string concatenation rather than RFC 3986 resolution, and drops
anything it could not fully resolve — silently, with no error anywhere.

So this module reimplements the client's behaviour rather than the standard:

  `OpdsParser`     ← crosspoint-reader `lib/OpdsParser/OpdsParser.cpp`
  `build_url`      ← `src/util/UrlUtils.cpp`
  `fetch`          ← `src/network/HttpDownloader.cpp`
  `sd_filename`    ← the download path in
                     `src/activities/browser/OpdsBookBrowserActivity.cpp`
                     plus `src/util/StringUtils.cpp`

Ported from master as read on 2026-07-28; every source file above is identical
in the 1.5.0 tag, so this tracks **firmware 1.5.0**. It is a port, not the
device: it proves a feed satisfies the rules that firmware applies. On-device
confirmation is a separate claim, and `reference/readers.md` marks which
verdicts have it.

Two deliberate fidelities that look like bugs and are not:

- The parser matches namespaced elements with a bare substring test
  (`":id" in name`), so `dc:identifier` really does register as an id. The port
  keeps that, because the whole point is to catch a feed that trips it.
- `build_url` appends a relative href to the *current feed URL* rather than
  resolving it against the base, so a relative link one level down produces a
  deeper path than RFC 3986 would give. Kept, for the same reason.
"""
from __future__ import annotations

import base64
import urllib.error
import urllib.parse
import urllib.request
import xml.parsers.expat
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

NAVIGATION = "NAVIGATION"
BOOK = "BOOK"

ACQUISITION_MARKER = "opds-spec.org/acquisition"
EPUB_TYPE = "application/epub+zip"
ATOM_MARKER = "application/atom+xml"
SEARCH_PLACEHOLDER = "{searchTerms}"
MAX_REDIRECTS = 5
USER_AGENT = "CrossPoint-ESP32-1.5.0"


@dataclass
class OpdsEntry:
    type: str = NAVIGATION
    title: str = ""
    author: str = ""
    href: str = ""
    id: str = ""


@dataclass
class OpdsFeed:
    entries: List[OpdsEntry] = field(default_factory=list)
    search_template: str = ""
    next_page_url: str = ""
    prev_page_url: str = ""

    def books(self) -> List[OpdsEntry]:
        return [e for e in self.entries if e.type == BOOK]

    def navigation(self) -> List[OpdsEntry]:
        return [e for e in self.entries if e.type == NAVIGATION]

    def find(self, title: str) -> Optional[OpdsEntry]:
        return next((e for e in self.entries if e.title == title), None)


class OpdsParser:
    """Line-for-line behavioural port of the firmware's expat handlers."""

    def __init__(self) -> None:
        self.feed = OpdsFeed()
        self._current = OpdsEntry()
        self._text = ""
        self._in_entry = False
        self._in_title = False
        self._in_author = False
        self._in_author_name = False
        self._in_id = False
        self.error: Optional[str] = None

        # No namespace separator, exactly as XML_ParserCreate(nullptr) — element
        # names arrive as written, prefix and all, which is what the firmware's
        # substring matching depends on.
        self._parser = xml.parsers.expat.ParserCreate()
        self._parser.StartElementHandler = self._start
        self._parser.EndElementHandler = self._end
        self._parser.CharacterDataHandler = self._chars

    def parse(self, data: bytes) -> bool:
        try:
            self._parser.Parse(data, True)
        except xml.parsers.expat.ExpatError as e:
            self.error = f"parse error: {e}"
            return False
        return True

    def _start(self, name: str, atts: Dict[str, str]) -> None:
        if name == "link" or ":link" in name:
            href = atts.get("href")
            if href:
                rel = atts.get("rel")
                type_ = atts.get("type")

                if rel == "search":
                    if SEARCH_PLACEHOLDER in href:
                        self.feed.search_template = href
                elif rel == "next" and not self._in_entry:
                    self.feed.next_page_url = href
                elif rel == "previous" and not self._in_entry:
                    self.feed.prev_page_url = href

                if self._in_entry:
                    if rel and type_ and ACQUISITION_MARKER in rel and type_ == EPUB_TYPE:
                        # Prefer a plain EPUB href when an entry offers several.
                        is_plain_epub = ".epub" in href or "/epub/" in href
                        already_plain = (self._current.type == BOOK
                                         and (".epub" in self._current.href
                                              or "/epub/" in self._current.href))
                        if self._current.type != BOOK or (is_plain_epub and not already_plain):
                            self._current.type = BOOK
                            self._current.href = href
                    elif type_ and ATOM_MARKER in type_:
                        if self._current.type != BOOK:
                            self._current.type = NAVIGATION
                            self._current.href = href

        if name == "entry" or ":entry" in name:
            self._in_entry = True
            self._current = OpdsEntry()
            return

        if not self._in_entry:
            return

        if name == "title" or ":title" in name:
            self._in_title = True
            self._text = ""
        elif name == "author" or ":author" in name:
            self._in_author = True
        elif self._in_author and (name == "name" or ":name" in name):
            self._in_author_name = True
            self._text = ""
        elif name == "id" or ":id" in name:
            self._in_id = True
            self._text = ""

    def _end(self, name: str) -> None:
        if name == "entry" or ":entry" in name:
            # The silent drop: no title or no href means the book never existed.
            if self._current.title and self._current.href:
                self.feed.entries.append(self._current)
            self._in_entry = False
        elif self._in_entry:
            if name == "title" or ":title" in name:
                if self._in_title:
                    self._current.title = self._text
                self._in_title = False
            elif name == "author" or ":author" in name:
                self._in_author = False
            elif self._in_author_name and (name == "name" or ":name" in name):
                self._current.author = self._text
                self._in_author_name = False
            elif name == "id" or ":id" in name:
                if self._in_id:
                    self._current.id = self._text
                self._in_id = False

    def _chars(self, data: str) -> None:
        if self._in_title or self._in_author_name or self._in_id:
            self._text += data


def ensure_protocol(url: str) -> str:
    return url if "://" in url else "http://" + url


def extract_host(url: str) -> str:
    protocol_end = url.find("://")
    if protocol_end == -1:
        first_slash = url.find("/")
        return url if first_slash == -1 else url[:first_slash]
    host_start = protocol_end + 3
    path_start = url.find("/", host_start)
    return url if path_start == -1 else url[:path_start]


UNSAFE_URL_CHARS = '"<>\\^`{|}'


def encode_unsafe_url_chars(url: str) -> str:
    """`UrlUtils::encodeUnsafeUrlChars` — percent-encode what esp_http_client
    will not accept raw. Byte-wise, like the firmware: an already-escaped `%XX`
    passes through, a lone `%` is encoded, and a non-ASCII character becomes one
    escape per UTF-8 byte. Added in firmware 1.5.0."""
    out = []
    i = 0
    while i < len(url):
        char = url[i]
        code = ord(char)
        escape = url[i + 1:i + 3]
        if (char == "%" and i + 2 < len(url)
                and all(c in "0123456789abcdefABCDEF" for c in escape)):
            out.append(url[i:i + 3])
            i += 3
            continue
        if char == "%" or code <= 0x20 or code >= 0x7F or char in UNSAFE_URL_CHARS:
            out.extend(f"%{byte:02X}" for byte in char.encode("utf-8"))
        else:
            out.append(char)
        i += 1
    return "".join(out)


def build_url(server_url: str, path: str) -> str:
    """`UrlUtils::buildUrl` — naive by design, and the reason this server emits
    only absolute URLs."""
    if "://" in path:
        return encode_unsafe_url_chars(path)
    url_with_protocol = ensure_protocol(server_url)
    if not path:
        return encode_unsafe_url_chars(url_with_protocol)
    if path[0] == "/":
        return encode_unsafe_url_chars(extract_host(url_with_protocol) + path)
    base = url_with_protocol.split("?", 1)[0]
    joined = base + path if base.endswith("/") else base + "/" + path
    return encode_unsafe_url_chars(joined)


def sanitize_filename(name: str, max_bytes: int = 100) -> str:
    """`StringUtils::sanitizeFilename` — byte-budgeted, codepoint-aware."""
    text = name.lstrip(" .")
    result = b""
    for char in text:
        cp = ord(char)
        if char in '/\\:*?"<>|':
            if len(result) + 1 > max_bytes:
                break
            result += b"_"
        elif cp >= 128 or 32 <= cp < 127:
            encoded = char.encode("utf-8")
            if len(result) + len(encoded) > max_bytes:
                break
            result += encoded
    # The firmware trims trailing spaces and dots after the budget is spent —
    # so a title truncated mid-way can lose one more character here — and never
    # returns empty.
    trimmed = result.decode("utf-8", "ignore").rstrip(" .")
    return trimmed or "book"


# `OpdsFilenameFormat` in src/util/OpdsFilename.h. 1.5.0 made this a setting
# (`opdsFilenameFormat`); before that the layout was always AuthorTitle, which
# is why 0 is the default here as it is there.
FILENAME_AUTHOR_TITLE = 0
FILENAME_TITLE_AUTHOR = 1
FILENAME_TITLE_ONLY = 2


def opds_book_filename(author: str, title: str, fmt: int = FILENAME_AUTHOR_TITLE) -> str:
    """`opdsBookFilename` — the name a download lands under on the SD card.

    Ported rather than approximated because it is the one string that decides
    whether two delivery paths agree. A book pushed over the file-transfer API
    and the same book pulled through this catalog only end up as *one* file on
    the card if both spell it identically, down to the byte budget and the
    trailing-dot trim; get it wrong and the reader quietly holds two copies of
    everything.

    With no author, every format collapses to the title alone.
    """
    if fmt == FILENAME_TITLE_AUTHOR:
        base = f"{title} - {author}" if author else title
    elif fmt == FILENAME_TITLE_ONLY:
        base = title
    else:
        base = f"{author} - {title}" if author else title
    # .epub is appended after sanitizing, so the extension is never truncated.
    return sanitize_filename(base) + ".epub"


def sd_filename(entry: OpdsEntry, fmt: int = FILENAME_AUTHOR_TITLE) -> str:
    """What the book will be called on the SD card, as an absolute path."""
    return "/" + opds_book_filename(entry.author, entry.title, fmt)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The firmware opens with `esp_http_client_open`, which does not follow
    redirects — it steps them by hand, capped at 5 hops. So do we."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(url: str, username: str = "", password: str = "",
          timeout: float = 30.0) -> Tuple[int, bytes, Dict[str, str]]:
    """GET with preemptive Basic auth and manual redirects. Returns
    (status, body, headers); the firmware treats anything but 200 as failure."""
    opener = urllib.request.build_opener(_NoRedirect)
    current = url

    for _ in range(MAX_REDIRECTS + 1):
        request = urllib.request.Request(current, method="GET")
        request.add_header("User-Agent", USER_AGENT)
        if username and password:  # only when both are non-empty, as the device does
            token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
            request.add_header("Authorization", f"Basic {token}")
        try:
            with opener.open(request, timeout=timeout) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as e:
            status = e.code
            headers = dict(e.headers)
            body = e.read()
            if status in (301, 302, 303, 307, 308) and headers.get("Location"):
                current = urllib.parse.urljoin(current, headers["Location"])
                continue
            return status, body, headers

    return 0, b"", {}


def fetch_feed(url: str, username: str = "", password: str = "") -> Tuple[Optional[OpdsFeed], str]:
    """Fetch and parse one feed the way the browser activity does. Returns
    (feed, error) — a non-200 or a parse failure yields (None, reason)."""
    status, body, _ = fetch(url, username, password)
    if status != 200:
        return None, f"HTTP {status} from {url}"
    parser = OpdsParser()
    if not parser.parse(body):
        return None, f"{parser.error} ({url})"
    return parser.feed, ""


def navigate(feed_url: str, entry: OpdsEntry, server_url: str, current_path: str) -> str:
    """The URL the device would open for `entry`, resolved as it resolves it."""
    resolved_feed = build_url(server_url, current_path)
    return build_url(resolved_feed, entry.href)
