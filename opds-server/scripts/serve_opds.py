#!/usr/bin/env python3
"""The OPDS server — the builder's output folder, over the network, to the X3.

The delivery half of the suite: `epub-builder` produces EPUBs into
`workspace/<slug>/build/`, and this serves that folder as an OPDS 1.2 catalog
the reader browses and downloads from directly, with nothing in between.

    python3 opds-server/scripts/serve_opds.py       # stdlib only — no venv needed

Then on the device: Settings → System → OPDS Servers → Add Server, and enter the
URL the server prints at startup (`http://<lan-ip>:6737/opds`).

Design notes:

- **Plain HTTP, LAN only.** Not laziness: CrossPoint builds esp-tls with
  `CONFIG_ESP_TLS_INSECURE` off and verifies against a bundled CA root store, so
  a self-signed certificate cannot complete a handshake. Either you have a
  publicly-trusted certificate or you serve `http://`. Anything sent — including
  Basic credentials, if you enable them — is cleartext on your network.
- **Ids, not paths.** A download URL carries a book id; the id is looked up in
  the scanned catalog and the file is served from the path recorded there. No
  request string ever reaches the filesystem, so path traversal is not defended
  against so much as unrepresentable.
- **Absolute URLs, built from the request's `Host`.** The client's URL joining
  is naive string concatenation rather than RFC 3986 resolution (see
  `feeds.py`), so nothing relative is ever emitted. Set `public_url` in the
  config to override, e.g. behind a reverse proxy.
- **HTTP/1.1 with an accurate `Content-Length` on every response.** The device
  keeps the connection alive; a missing length would hang it.
- **The catalog is rescanned on demand** (a short TTL), so a book built while
  the server is running shows up on the next page turn.
"""
from __future__ import annotations

import argparse
import base64
import hmac
import shutil
import socket
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, quote, unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

import feeds  # noqa: E402
import library  # noqa: E402
from config import ConfigError, load_config, resolve_auth  # noqa: E402

CATALOG_TTL_SECONDS = 2.0


class Catalog:
    """The scanned library, refreshed lazily so freshly-built books appear."""

    def __init__(self, roots: Sequence[Path], exclude: Sequence[str],
                 ttl: float = CATALOG_TTL_SECONDS):
        self.roots = list(roots)
        self.exclude = list(exclude)
        self.ttl = ttl
        self._books: List[library.Book] = []
        self._by_id: Dict[str, library.Book] = {}
        self._scanned_at = 0.0
        self._lock = threading.Lock()

    def refresh(self, force: bool = False) -> None:
        with self._lock:
            if not force and (time.monotonic() - self._scanned_at) < self.ttl:
                return
            self._books = library.scan(self.roots, self.exclude)
            self._by_id = {b.id: b for b in self._books}
            self._scanned_at = time.monotonic()

    def books(self) -> List[library.Book]:
        self.refresh()
        return self._books

    def recent(self) -> List[library.Book]:
        return sorted(self.books(), key=lambda b: b.mtime, reverse=True)

    def by_id(self, book_id: str) -> Optional[library.Book]:
        self.refresh()
        book = self._by_id.get(book_id)
        if book is None:  # a book built since the last scan
            self.refresh(force=True)
            book = self._by_id.get(book_id)
        return book


class OpdsHandler(BaseHTTPRequestHandler):
    # Set per-server in create_server().
    catalog: Catalog = None          # type: ignore[assignment]
    credentials: Optional[Tuple[str, str]] = None
    catalog_title: str = "Library"
    page_size: int = 25
    public_url: str = ""

    protocol_version = "HTTP/1.1"
    server_version = "x3-opds"
    sys_version = ""

    # ---------------------------------------------------------------- plumbing

    def version_string(self) -> str:
        return self.server_version  # the default appends a trailing sys_version

    def handle_one_request(self) -> None:
        """Diagnose a TLS handshake rather than answering it with binary noise.

        A device configured with an `https://` URL opens TLS against this plain
        HTTP port. Left alone, http.server reads the ClientHello as a request
        line and logs `Bad HTTP/0.9 request type ('\\x16\\x03\\x03\\x01')`,
        which is true, unreadable, and three steps from the actual problem —
        while the reader just says "Failed to fetch feed". So catch the one
        byte that identifies it (0x16 = TLS handshake record) and say what to
        change instead.
        """
        try:
            if self.rfile.peek(1)[:1] == b"\x16":
                self.log_message(
                    "%s", "TLS handshake on a plain-HTTP port — the OPDS server URL "
                    "stored on the device starts with https://. Change it to http:// "
                    "(the X3 verifies certificates against a bundled CA store, so no "
                    "self-signed certificate can connect; plain HTTP on the LAN is the "
                    "supported transport).")
                self.close_connection = True
                return
        except (OSError, ValueError):
            pass  # nothing buffered, or a closed socket — let the base class deal
        super().handle_one_request()

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(f"{self.address_string()} {fmt % args}\n")

    def base_url(self) -> str:
        if self.public_url:
            return self.public_url
        host = self.headers.get("Host")
        if not host:  # HTTP/1.1 requires Host; fall back to the bound socket
            host = f"{self.server.server_address[0]}:{self.server.server_address[1]}"
        return f"http://{host}"

    def authorized(self) -> bool:
        if not self.credentials:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        username, _, password = decoded.partition(":")
        expected_user, expected_pass = self.credentials
        # Both compared, both in constant time, so neither leaks by timing.
        return (hmac.compare_digest(username, expected_user)
                and hmac.compare_digest(password, expected_pass))

    def _send(self, status: HTTPStatus, content_type: str, body: bytes,
              extra: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_xml(self, body: str, content_type: str) -> None:
        self._send(HTTPStatus.OK, f"{content_type}; charset=utf-8", body.encode("utf-8"))

    def _send_text(self, status: HTTPStatus, message: str,
                   extra: Optional[Dict[str, str]] = None) -> None:
        self._send(status, "text/plain; charset=utf-8",
                   (message + "\n").encode("utf-8"), extra)

    def _unauthorized(self) -> None:
        self._send_text(
            HTTPStatus.UNAUTHORIZED, "authentication required",
            {"WWW-Authenticate": f'Basic realm="{self.catalog_title}", charset="UTF-8"'})

    # ------------------------------------------------------------------ routes

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        if not self.authorized():
            self._unauthorized()
            return

        split = urlsplit(self.path)
        # Split the *raw* path, then unquote each segment on its own: unquoting
        # first would turn an escaped slash inside a segment (a language tag, a
        # slug) into a path separator and route the request somewhere else.
        segments = [unquote(s) for s in split.path.split("/") if s]
        query = parse_qs(split.query)

        try:
            self.route(segments, query)
        except BrokenPipeError:
            pass  # the device walked away mid-download; nothing to report
        except ConnectionResetError:
            pass

    def route(self, segments: List[str], query: Dict[str, List[str]]) -> None:
        page = self._page(query)

        if not segments:
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", f"{self.base_url()}/opds")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if segments[0] == "book":
            self._send_book(segments)
            return

        if segments[0] != "opds":
            self._send_text(HTTPStatus.NOT_FOUND, "not found")
            return

        tail = segments[1:]
        if not tail:
            self._send_xml(self.root_feed(), feeds.NAV_TYPE)
        elif tail == ["opensearch.xml"]:
            self._send_xml(feeds.opensearch_description(self.base_url(), self.catalog_title),
                           feeds.OPENSEARCH_TYPE)
        elif tail == ["all"]:
            self._send_books("all", "All books", self.catalog.books(), page)
        elif tail == ["recent"]:
            self._send_books("recent", "Recently built", self.catalog.recent(), page)
        elif tail == ["authors"]:
            self._send_xml(self.authors_feed(), feeds.NAV_TYPE)
        elif len(tail) == 2 and tail[0] == "authors":
            self._author_feed(tail[1], page)
        elif tail == ["languages"]:
            self._send_xml(self.languages_feed(), feeds.NAV_TYPE)
        elif len(tail) == 2 and tail[0] == "languages":
            self._language_feed(tail[1], page)
        elif tail == ["search"]:
            term = (query.get("q") or [""])[0]
            self._send_books(f"search:{term}", f"Search: {term}" if term else "Search",
                             library.search(self.catalog.books(), term), page,
                             extra_query=f"q={quote(term, safe='')}")
        else:
            self._send_text(HTTPStatus.NOT_FOUND, "not found")

    @staticmethod
    def _page(query: Dict[str, List[str]]) -> int:
        try:
            return max(1, int((query.get("page") or ["1"])[0]))
        except ValueError:
            return 1

    # ------------------------------------------------------------------- feeds

    def root_feed(self) -> str:
        base = self.base_url()
        books = self.catalog.books()
        authors = library.group_by_author(books)
        languages = library.group_by_language(books)

        sections = [
            ("recent", "Recently built", f"{len(books)} book(s), newest first",
             f"{base}/opds/recent", feeds.ACQ_TYPE),
            ("all", "All books", f"{len(books)} book(s), by title",
             f"{base}/opds/all", feeds.ACQ_TYPE),
            ("authors", "By author", f"{len(authors)} author(s)",
             f"{base}/opds/authors", feeds.NAV_TYPE),
            ("languages", "By language", f"{len(languages)} language(s)",
             f"{base}/opds/languages", feeds.NAV_TYPE),
        ]
        entries = [feeds.navigation_entry(title=title, href=href, summary=summary,
                                          entry_id=f"urn:x3:nav:{key}", kind_type=kind)
                   for key, title, summary, href, kind in sections]

        return feeds.navigation_feed(
            feed_id="urn:x3:catalog:root", title=self.catalog_title,
            self_url=f"{base}/opds", start_url=f"{base}/opds", base_url=base,
            entries=entries)

    def authors_feed(self) -> str:
        base = self.base_url()
        entries = [
            feeds.navigation_entry(
                title=author or "Unknown author",
                href=f"{base}/opds/authors/{key}",
                summary=f"{len(group)} book(s)",
                entry_id=f"urn:x3:author:{key}",
                kind_type=feeds.ACQ_TYPE)
            for key, author, group in library.group_by_author(self.catalog.books())
        ]
        return feeds.navigation_feed(
            feed_id="urn:x3:catalog:authors", title="By author",
            self_url=f"{base}/opds/authors", start_url=f"{base}/opds",
            base_url=base, entries=entries)

    def languages_feed(self) -> str:
        base = self.base_url()
        entries = [
            feeds.navigation_entry(
                title=lang.upper() if lang else "Unknown language",
                href=f"{base}/opds/languages/{quote(lang or 'unknown')}",
                summary=f"{len(group)} book(s)",
                entry_id=f"urn:x3:language:{lang or 'unknown'}",
                kind_type=feeds.ACQ_TYPE)
            for lang, group in library.group_by_language(self.catalog.books())
        ]
        return feeds.navigation_feed(
            feed_id="urn:x3:catalog:languages", title="By language",
            self_url=f"{base}/opds/languages", start_url=f"{base}/opds",
            base_url=base, entries=entries)

    def _author_feed(self, key: str, page: int) -> None:
        for candidate, author, group in library.group_by_author(self.catalog.books()):
            if candidate == key:
                self._send_books(f"author:{key}", author or "Unknown author", group, page,
                                 path=f"/opds/authors/{key}")
                return
        self._send_text(HTTPStatus.NOT_FOUND, "no such author")

    def _language_feed(self, code: str, page: int) -> None:
        wanted = "" if code == "unknown" else code.lower()
        for lang, group in library.group_by_language(self.catalog.books()):
            if lang == wanted:
                self._send_books(f"language:{lang}", lang.upper() or "Unknown language",
                                 group, page,
                                 path=f"/opds/languages/{quote(code, safe='')}")
                return
        self._send_text(HTTPStatus.NOT_FOUND, "no such language")

    def _send_books(self, key: str, title: str, books: Sequence[library.Book], page: int,
                    path: Optional[str] = None, extra_query: str = "") -> None:
        base = self.base_url()
        path = path or f"/opds/{key.split(':')[0]}"
        window, has_prev, has_next = feeds.paginate(books, page, self.page_size)

        def page_url(n: int) -> str:
            query = f"page={n}" + (f"&{extra_query}" if extra_query else "")
            return f"{base}{path}?{query}"

        self._send_xml(feeds.acquisition_feed(
            feed_id=f"urn:x3:catalog:{key}",
            title=f"{title} ({page})" if (has_prev or has_next) else title,
            self_url=page_url(page), start_url=f"{base}/opds", base_url=base,
            books=window,
            next_url=page_url(page + 1) if has_next else None,
            prev_url=page_url(page - 1) if has_prev else None,
        ), feeds.ACQ_TYPE)

    # ---------------------------------------------------------------- download

    def _send_book(self, segments: List[str]) -> None:
        """`/book/<id>/<slug>.epub`. Only the id is honoured — the slug is
        decoration, and no part of the URL is ever joined onto a path."""
        if len(segments) < 2:
            self._send_text(HTTPStatus.NOT_FOUND, "not found")
            return

        book = self.catalog.by_id(segments[1])
        if book is None:
            self._send_text(HTTPStatus.NOT_FOUND, "no such book")
            return

        source = Path(book.path)
        try:
            size = source.stat().st_size
            handle = source.open("rb")
        except OSError:
            self._send_text(HTTPStatus.NOT_FOUND, "book file is gone")
            return

        with handle:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", feeds.EPUB_TYPE)
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition",
                             f'attachment; filename="{book.slug}.epub"')
            self.send_header("Accept-Ranges", "none")
            self.end_headers()
            if self.command != "HEAD":
                shutil.copyfileobj(handle, self.wfile, 64 * 1024)


def lan_addresses(port: int) -> List[str]:
    """Best-effort list of URLs to type into the device.

    Two independent guesses, because neither is reliable alone: the address the
    routing table would use to reach the outside world, and whatever the
    hostname resolves to. A UDP `connect` sends no packets — it only asks the
    kernel which interface it would pick — and some sandboxes answer it with the
    destination itself, so a TEST-NET-1 reply is discarded rather than printed.
    """
    candidates: List[str] = []
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.settimeout(0.2)
        probe.connect(("192.0.2.1", 9))  # TEST-NET-1: reserved, routed nowhere
        candidates.append(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            candidates.append(info[4][0])
    except (OSError, socket.gaierror):
        pass

    urls = []
    for address in candidates:
        if address.startswith(("127.", "192.0.2.")) or address in urls:
            continue
        urls.append(address)
    urls.append("127.0.0.1")
    return [f"http://{address}:{port}/opds" for address in dict.fromkeys(urls)]


def create_server(cfg: Dict, credentials: Optional[Tuple[str, str]] = None
                  ) -> ThreadingHTTPServer:
    """Build a bound server. Split out from main() so the self-test can drive a
    real server on an ephemeral port instead of a mock."""
    catalog = Catalog(cfg["library_roots"], cfg["exclude"])

    class BoundHandler(OpdsHandler):
        pass

    BoundHandler.catalog = catalog
    BoundHandler.credentials = credentials
    BoundHandler.catalog_title = cfg["catalog_title"]
    BoundHandler.page_size = cfg["page_size"]
    BoundHandler.public_url = cfg["public_url"]

    httpd = ThreadingHTTPServer((cfg["host"], cfg["port"]), BoundHandler)
    httpd.daemon_threads = True
    return httpd


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Serve the suite's EPUBs as an OPDS catalog.")
    ap.add_argument("--config", type=Path, default=None, help="path to config.json")
    ap.add_argument("--root", action="append", default=[], metavar="DIR",
                    help="library root (repeatable); overrides the config")
    ap.add_argument("--host", default=None, help="bind address (default 0.0.0.0)")
    ap.add_argument("--port", type=int, default=None,
                    help="bind port (default 6737; 0 lets the OS pick a free one)")
    ap.add_argument("--page-size", type=int, default=None, help="entries per feed page")
    args = ap.parse_args(argv)

    try:
        cfg = load_config(args.config)
        if args.root:
            cfg["library_roots"] = [Path(r).expanduser().resolve() for r in args.root]
        if args.host:
            cfg["host"] = args.host
        if args.port is not None:
            cfg["port"] = args.port
        if args.page_size is not None:
            cfg["page_size"] = max(1, args.page_size)
        credentials = resolve_auth(cfg)
    except ConfigError as e:
        print(f"opds-server: {e}", file=sys.stderr)
        return 1

    try:
        httpd = create_server(cfg, credentials)
    except OSError as e:
        print(f"opds-server: cannot bind {cfg['host']}:{cfg['port']}: {e}", file=sys.stderr)
        return 1

    # Ask the socket, don't echo the config: with `--port 0` the kernel picks
    # the port, and the number you need to type into the device is only knowable
    # after the bind.
    bound_port = httpd.server_address[1]

    books = library.scan(cfg["library_roots"], cfg["exclude"])
    print(f"opds-server: {len(books)} book(s) from "
          f"{', '.join(str(r) for r in cfg['library_roots'])}")
    print(f"opds-server: config {cfg['_config_path'] or '(defaults)'}")
    for group in library.duplicate_labels(books):
        print(f"opds-server: {', '.join(b.rel for b in group)} share an author and title — "
              f"the device saves by author+title, so one will overwrite the other on the SD "
              f"card; exclude the variant you don't want in the config")
    if credentials:
        print(f"opds-server: HTTP Basic auth as {credentials[0]!r} "
              f"(cleartext on the wire — plain HTTP is the only transport the X3 accepts)")
    else:
        print("opds-server: open — anyone on this network can read and download the library")
    for url in lan_addresses(bound_port):
        print(f"opds-server: add this on the device → {url}")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nopds-server: stopped")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
