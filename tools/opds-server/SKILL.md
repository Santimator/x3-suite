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
python3 tools/opds-server/scripts/serve_opds.py          # serve workspace/ on :6737
python3 tools/opds-server/scripts/library.py             # what would be served
python3 tools/opds-server/scripts/library.py --json      # ... as typed JSON
python3 tools/opds-server/scripts/selftest.py            # the gate; exit 0 = sound
```

Then on the device: **Settings → System → OPDS Servers → Add Server**, and
enter the URL the server prints at startup (`http://<lan-ip>:6737/opds`). The
reader stores up to 8 servers. Books land on the SD card as
`<author> - <title>.epub`.

Useful flags: `--root DIR` (repeatable) to serve somewhere else, `--port`,
`--host`, `--page-size`.

The default port is **6737** ("OPDS" on a phone keypad) rather than 8080 — the
most contested port on any machine, and one other ebook servers reach for
first. `--port 0` binds a free one and prints what it got, handy when you just
want it up for a minute.

## Configuration

`config.example.json` → copy to `config.json`, which is **gitignored**, as is
`secrets/`. Everything has a working default, so no config is needed to start.
The credential seam is the same one `ai-tools/graded-reader/headless/` uses —
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

## Running it as a service

[`opds-server.service`](opds-server.service) is a systemd system unit. It
carries two placeholders — the account and the clone path — and the shell fills
them in from the clone you are standing in, so nothing is typed by hand:

```bash
cd /path/to/x3-suite
sed -e "s|CHANGEME_REPO|$PWD|g" -e "s|CHANGEME_USER|$(id -un)|g" \
    tools/opds-server/opds-server.service \
    | sudo tee /etc/systemd/system/opds-server.service
sudo systemctl daemon-reload
sudo systemctl enable --now opds-server
sudo journalctl -u opds-server -f     # the startup banner, then requests
```

The installed unit is a **copy**, which is why it is written this way: a pull
that changes this file, or that moves the script it points at, needs the same
two lines run again. A unit editing itself in the checkout would fight every
pull instead.

It runs as your user (the books live in your home), and since the server only
ever reads, the unit confines it accordingly: the filesystem read-only, home
readable but not writable.

[`helper-info.txt`](helper-info.txt) sits next to the unit and explains each of
those commands, the day-to-day ones (`status`, `restart`, `stop`, reading the
log without following it), and what to check when the reader can't fetch.

Port, library roots and auth all stay in `config.json` — the unit doesn't
repeat them, so changing any of them is `systemctl restart opds-server` and
nothing more.

**If you pulled the move to `tools/` (2026-08)**, this unit moved from
`opds-server/` and your `config.json` and `secrets/` did not come with it — git
moves what it tracks, and those are gitignored by design. Move them across and
re-install the unit, which names the old path:

```bash
mv opds-server/config.json tools/opds-server/ 2>/dev/null
mv opds-server/secrets/* tools/opds-server/secrets/ 2>/dev/null
sed -e "s|CHANGEME_REPO|$PWD|g" -e "s|CHANGEME_USER|$(id -un)|g" \
    tools/opds-server/opds-server.service \
    | sudo tee /etc/systemd/system/opds-server.service
sudo systemctl daemon-reload && sudo systemctl restart opds-server
```

The unit is the part that bites: it names the script's path, so until it is
re-installed the service points at `opds-server/scripts/serve_opds.py`, which
no longer exists. `systemctl status opds-server` says so plainly, and the
catalog simply never answers.

Written for Debian: `/usr/bin/python3`, `multi-user.target`. On another distro
or init system, hand the file to your favourite AI and ask for the equivalent —
it's twenty declarative lines.

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

## When the device says it failed

The reader reports one thing — **"Failed to fetch feed"** — for every network
problem there is. The server's log is what tells them apart:

| Server log shows | What it is |
|---|---|
| `TLS handshake on a plain-HTTP port` | The URL stored on the device starts with `https://`. Re-enter it as `http://<ip>:6737/opds`, or with no scheme at all — the firmware prepends `http://` when there isn't one. The X3 cannot do HTTPS here at all (see the transport note below). |
| **nothing** | The request never arrived. Wrong IP, a different network or VLAN, or a firewall on this machine. The startup banner prints the address it believes it's on; try that URL from a phone on the same WiFi. |
| `401` | Auth is on and the device isn't sending usable credentials. It only sends them when *both* username and password are set on the server entry. |
| `200`, and the device says **"No entries"** | The feed arrived and parsed, but every entry was dropped — a missing title or an unresolvable href — or that section is genuinely empty. Run `selftest.py`; that is the exact failure it exists to catch. |

A book that lists but 404s on download means the library was rescanned and the
file moved or was rebuilt under a different name — ids are derived from the
path. Re-open the catalog to pick up the new one.

## What the device's client actually requires

The whole design is downstream of CrossPoint's OPDS client, read from source
(`lib/OpdsParser/`, `src/network/HttpDownloader.cpp`, `src/util/UrlUtils.cpp`,
`src/activities/browser/`, master @ 2026-07). `extras/readers.md` at the repo
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

**Status: device-confirmed (2026-07).** An X3 on WiFi browsed a catalog served
by this server and downloaded books from it. The client port remains the gate —
it is what catches a regression before a device ever sees it — but the
end-to-end claim is now evidence, not inference.

The one failure worth knowing from that first run: the URL saved on the device
had been typed with `https://`, so the reader opened TLS against a plain-HTTP
port and reported "Failed to fetch feed" while the server logged a ClientHello
as a bad request. Hence the diagnosis in the table above.

## Files

```
SKILL.md                     this file
config.example.json          copy to config.json (gitignored)
opds-server.service          systemd unit (Debian; edit two lines)
helper-info.txt              what each systemctl/journalctl command does
secrets/                     gitignored; the Basic-auth password
scripts/
  serve_opds.py              the server (stdlib http.server)
  library.py                 scan roots, read OPF metadata, group and search
  feeds.py                   Atom generation, with the client's rules baked in
  config.py                  config + credential resolution
  crosspoint_client.py       port of the device's client — the gate's oracle
  selftest.py                the gate
```
