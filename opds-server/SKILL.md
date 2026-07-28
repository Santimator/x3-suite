---
name: opds-server
description: >-
  Serve the suite's built EPUBs to the Xteink X3 over WiFi as an OPDS catalog,
  straight from the builder's output folder. Use when the reader should fetch
  books over the network instead of an SD card, when changing what the catalog
  exposes (sections, metadata, search, auth), or when a book is missing or
  unreadable on the device. Triggers include "serve the books", "opds", "put
  the library on the network", "the X3 can't see my book".
---

# opds-server — the suite's EPUB delivery

Suite infrastructure, not a task. `epub-builder` produces EPUBs into
`workspace/<slug>/build/`; this serves that folder as an **OPDS 1.2** catalog
the X3 browses and downloads from directly. No SD card shuffling, and no
library manager in between — build a book, and it is on the reader by the next
page turn.

Deterministic like the builder: stdlib only, no dependencies, no model in the
loop. What stands in for this suite's "gate after every model step" is
[`scripts/selftest.py`](scripts/selftest.py), which grades the served catalog
against a port of the **device's own client** rather than against the OPDS
standard — see *The gate* below for why those are not the same test.

## Usage

```bash
python3 opds-server/scripts/serve_opds.py          # serve workspace/ on :8080
python3 opds-server/scripts/library.py             # what would be served
python3 opds-server/scripts/library.py --json      # ... as typed JSON
python3 opds-server/scripts/selftest.py            # the gate; exit 0 = sound
```

Then on the device: **Settings → System → OPDS Servers → Add Server**, and
enter the URL the server prints at startup (`http://<lan-ip>:8080/opds`). The
reader stores up to 8 servers. Books land on the SD card as
`<author> - <title>.epub`.

Useful flags: `--root DIR` (repeatable) to serve somewhere else, `--port`,
`--host`, `--page-size`.

## Configuration

`config.example.json` → copy to `config.json`, which is **gitignored**, as is
`secrets/`. Everything has a working default, so no config is needed to start.
The credential seam is the same one `services/graded-reader/headless/` uses —
the secret is never in the config file, only a path to a gitignored file
holding it, with an environment variable as the fallback. Details in
[`secrets/README.md`](secrets/README.md).

Auth is **off by default**: the server runs open on your LAN and says so at
startup. Set `auth.username` plus a password file to turn on HTTP Basic, and
`require_auth: true` to refuse to start without them.

The default root, `workspace/`, is gitignored apart from the repo's samples, so
what this serves is your own library — which is the point, but worth knowing
before you open a port on it. Add absolute paths to `library_roots` to serve
books kept elsewhere.

## The catalog

```
/opds                    root: Recently built · All books · By author · By language
/opds/all                every book, by title, paginated
/opds/recent             newest build first
/opds/authors[/<key>]    grouped by author
/opds/languages[/<code>] grouped by language
/opds/search?q=...       title and author substring
/book/<id>/<slug>.epub   the download
```

Metadata comes from each EPUB's **OPF** first (`dc:title`, `dc:creator`,
`dc:language`), then `book.json` two levels up, then the filename. The OPF is
authoritative on purpose: a book dropped in by hand catalogs exactly as well as
one this suite built. That matters more than it sounds — the device builds the
SD-card filename out of author and title, so thin metadata is a permanently
worse filename.

Book ids are `sha1(path relative to its root)[:12]`, stable across restarts, and
a download URL carries **only an id**. The id is looked up in the scanned
catalog and the file served from the path recorded there, so no request string
ever reaches the filesystem — traversal isn't defended against, it's
unrepresentable.

The library is rescanned on demand (short TTL), so a book built while the server
is running appears without a restart.

## What the device's client actually requires

The whole design is downstream of CrossPoint's OPDS client, read from source
(`lib/OpdsParser/`, `src/network/HttpDownloader.cpp`, `src/util/UrlUtils.cpp`,
`src/activities/browser/`, master @ 2026-07). `reference/readers.md` at the repo
root carries the summary table; the consequences that shape the bytes:

- **Atom XML only.** There is no OPDS 2.0 / JSON path.
- **An entry with no title or no resolvable href is dropped silently.** No error
  on the device, no error in the feed — the book simply isn't there.
- **An acquisition link needs `rel` containing `opds-spec.org/acquisition` and
  `type` *exactly* `application/epub+zip`.** A `;profile=` suffix, or any other
  media type, means "not a book".
- **Search is a URL template, not a descriptor.** Pointing `rel="search"` at an
  OpenSearch description document — the conventional thing to do — silently
  disables search, because the client never fetches it. It wants
  `{searchTerms}` literally in the href. We emit both links; the client ignores
  the one without the placeholder.
- **Pagination is `rel="next"` / `rel="previous"`, feed level.** Spelled
  `previous`; `prev` is not recognised.
- **Relative hrefs are appended, not resolved.** The client's URL joining is
  string work, not RFC 3986, so a relative link one level down lands somewhere
  deeper than you meant. Everything here is emitted absolute, built from the
  request's `Host`.
- **`dc:identifier` inside an entry corrupts the entry id** — the client matches
  element names by substring, and `":id"` is inside `":identifier"`.
- **No covers.** The parser has no thumbnail handling at all; a cover link is
  bytes the device reads and throws away.
- **Plain HTTP.** Not a shortcut: esp-tls is built with insecure mode off and
  verifies against a bundled CA store, so a self-signed certificate cannot
  complete a handshake. Either a publicly-trusted certificate or `http://`.

## The gate

A generic OPDS validator would pass feeds this device silently drops. So
`selftest.py` builds a small library with the real builder, serves it with the
real server on an ephemeral port, and walks the whole catalog through
[`scripts/crosspoint_client.py`](scripts/crosspoint_client.py) — a behavioural
port of the firmware's parser, URL joining, and HTTP client, quirks deliberately
included. It checks that navigation resolves everywhere, that pagination loses
and duplicates nothing, that search works through the templated URL, that a
download arrives byte-identical and still passes the **shared**
`epub-builder/scripts/verify_epub.py`, that the contract regressions above hold,
and that unknown ids, path-shaped ids and Basic auth all behave.

Fixtures cover the hazards on purpose: a CJK title and author, a Latin book, a
title full of `&` and angle brackets, and a book with no author at all.

**Status: contract-verified, not device-confirmed.** The port proves the feed
satisfies the rules that firmware applies; it is not a photograph of an X3
browsing the catalog. `reference/readers.md` marks which verdicts have on-device
evidence — this one does not yet.

## Files

```
SKILL.md                     this file
config.example.json          copy to config.json (gitignored)
secrets/                     gitignored; the Basic-auth password
scripts/
  serve_opds.py              the server (stdlib http.server)
  library.py                 scan roots, read OPF metadata, group and search
  feeds.py                   Atom generation, with the client's rules baked in
  config.py                  config + credential resolution
  crosspoint_client.py       port of the device's client — the gate's oracle
  selftest.py                the gate
```
