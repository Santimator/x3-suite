#!/usr/bin/env python3
"""The reader's file-transfer web server, as a Python object.

Everything the X3 exposes while it sits on **Home -> File Transfer -> Join a
Network**: find it, list it, read it, write it, rename it, delete it, and read
or set its settings. Plain HTTP on port 80, no auth, CORS open. The endpoint
table and what is device-confirmed about each one lives in
`reference/readers.md`; this file is that table expressed as code.

Stdlib only, deliberately — the device speaks plain HTTP with query parameters
and multipart bodies, all of which urllib does, and a bot or a push script
should not need a venv to talk to a reader on the LAN.

Two consumers today: `push_wallpaper.py` (which is where all of this was first
written, and proved against a real device) and `tgbot/`. That is why it lives
here rather than in either of them — the transport is neither script's private
business. Add a third consumer and it deserves promoting somewhere neutral.

    from crosspoint_device import find_device, list_dir, rename, DeviceError

    host, info = find_device(None)          # remembered IP, then mDNS, then UDP
    for entry in list_dir(host, "/"):
        print(entry["name"], entry["size"])
    rename(host, "/Author - Title.epub", "shorter.epub")

**Names are opaque tokens.** Whatever `list_dir` hands back is what the SD card
holds, byte for byte, and it is what must go back out in a `path=`. Reader
libraries save books under names that came from someone else's catalog, and
those are full of decomposed accents (`é` as `e` + U+0301) that look identical
on screen to the composed form and compare unequal in `Storage.exists()`. Never
normalize, never retype, never round-trip a device name through anything that
might helpfully clean it up. Show it, carry it, send it back unchanged.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

# Where the last address that answered is kept, so the next run starts there.
# Gitignored: it is a fact about your LAN, not about this repo. X3_LAST_DEVICE
# moves it, which is how the self-tests keep their hands off yours.
LAST_DEVICE = Path(os.environ.get("X3_LAST_DEVICE")
                   or Path(__file__).resolve().parents[1] / "last-device.json")

# The firmware's own defaults: web server on 80, mDNS name crosspoint.local,
# and a UDP discovery responder on 8134 that answers the payload "hello".
DEFAULT_HOST = "crosspoint.local"
DISCOVERY_PORT = 8134
DISCOVERY_PAYLOAD = b"hello"

TIMEOUT = 20
PROBE_TIMEOUT = 3       # just asking "are you there?" — do not stall on a dead address
# A font family is ~24 MB in six files, the largest of them 6.8 MB, going into
# an ESP32 over WiFi. Minutes, not seconds.
FONT_TIMEOUT = 600


class DeviceError(Exception):
    pass


# --------------------------------------------------------------------------
# finding it


def discover(timeout: float = 2.0) -> list:
    """Broadcast the firmware's discovery ping and collect who answers.

    The reply is `crosspoint (on <hostname>);<websocket port>` — we only want
    the address it came from.
    """
    found = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(timeout)
    try:
        sock.sendto(DISCOVERY_PAYLOAD, ("255.255.255.255", DISCOVERY_PORT))
        while True:
            try:
                data, addr = sock.recvfrom(256)
            except socket.timeout:
                break
            if data.startswith(b"crosspoint") and addr[0] not in found:
                found.append(addr[0])
    except OSError:
        pass
    finally:
        sock.close()
    return found


def remember(host: str) -> None:
    """Write down the address that answered, for the next run to try first."""
    try:
        LAST_DEVICE.write_text(json.dumps(
            {"host": host,
             "confirmed": datetime.now().astimezone().isoformat(timespec="seconds")},
            indent=2) + "\n")
    except OSError:
        pass        # a read-only checkout is no reason to fail a push that worked


def recall() -> str | None:
    try:
        return json.loads(LAST_DEVICE.read_text()).get("host")
    except (OSError, ValueError, AttributeError):
        return None


def find_device(explicit: str | None = None):
    """Locate the reader, and return (host, its /api/status).

    An address given explicitly is used *as given* — if it does not answer that
    is an error, not a reason to go looking somewhere else and push to whatever
    turns up. Everything else is a guess, tried cheapest first:

      1. whatever answered last time (this LAN, this DHCP lease)
      2. crosspoint.local, if mDNS resolves here at all
      3. the firmware's UDP discovery ping, which needs no name service

    Guesses get a short timeout, so a stale address costs a moment rather than
    the better part of a minute.
    """
    if explicit:
        return explicit, status(explicit)

    tried = []
    for candidate in (recall(), DEFAULT_HOST):
        if not candidate or candidate in tried:
            continue
        tried.append(candidate)
        try:
            return candidate, status(candidate, timeout=PROBE_TIMEOUT)
        except DeviceError:
            pass

    for ip in discover():
        try:
            return ip, status(ip, timeout=PROBE_TIMEOUT)
        except DeviceError:
            pass

    raise DeviceError(
        "no reader found" + (f" (tried {', '.join(tried)})" if tried else "") + ".\n"
        "  On the device: Home -> File Transfer -> Join a Network.\n"
        "  Then pass the address it prints: --ip 192.168.x.x")


# --------------------------------------------------------------------------
# talking to it


def request(host: str, path: str, *, method: str = "GET", query: dict | None = None,
            body: bytes | None = None, content_type: str | None = None,
            timeout: float = TIMEOUT):
    """One HTTP call, returning (status_code, body_bytes).

    An HTTP error status is a *result*, not an exception — the firmware says
    plenty through 400 and 404 ("File already exists", "Item not found") that
    callers want to read rather than catch. Only a dead host raises.
    """
    url = f"http://{host}{path}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    req = urllib.request.Request(url, data=body, method=method)
    if content_type:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        raise DeviceError(f"cannot reach {host}: {exc.reason}") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise DeviceError(f"{host} stopped answering — is it still on the "
                          f"File Transfer screen?") from exc


def status(host: str, timeout: float = TIMEOUT) -> dict:
    code, body = request(host, "/api/status", timeout=timeout)
    if code != 200:
        raise DeviceError(f"/api/status returned {code}")
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise DeviceError(f"{host} answered, but not like a CrossPoint reader") from exc


def list_dir(host: str, path: str) -> list:
    """Contents of a folder, as /api/files reports them.

    Entries are dicts: name, size, isDirectory, isEpub.

    Note the firmware hides dot-prefixed *entries* from this listing unless the
    device's `showHiddenFiles` is on — so files inside /.sleep are visible but
    /.sleep itself is not listed at the root. A missing folder and an empty one
    both come back as [].
    """
    code, body = request(host, "/api/files", query={"path": path})
    if code != 200:
        return []
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return []


def mkdir(host: str, parent: str, name: str) -> bool:
    """True if the folder now exists (created, or already there)."""
    code, body = request(host, "/mkdir", method="POST",
                         query={"path": parent, "name": name})
    if code == 200:
        return True
    if b"already exists" in body:
        return True
    raise DeviceError(f"mkdir {parent}/{name}: {code} {body.decode(errors='replace')}")


def delete(host: str, path: str) -> bool:
    code, _ = request(host, "/delete", method="POST", query={"path": path})
    return code == 200


def delete_many(host: str, paths: list) -> tuple:
    """Delete several things in one call. Returns (ok, detail).

    `/delete` takes a `paths` JSON array as well as a single `path`, and
    reports the ones it refused in the body rather than failing the lot —
    handy, because a folder is only removed when it is already **empty**, so
    clearing one means the files first and the directory after.
    """
    code, body = request(host, "/delete", method="POST",
                         query={"paths": json.dumps(paths)})
    return code == 200, body.decode(errors="replace").strip()


def upload(host: str, directory: str, file: Path,
           content_type: str = "application/octet-stream",
           name: str | None = None, timeout: float | None = None) -> None:
    """POST /upload?path=DIR with the file as multipart form data.

    The destination is a query parameter, not a form field, because the
    firmware's upload callback needs the path before the multipart body has
    finished arriving. `name` overrides what it is called on the card, for
    callers that need a destination name the local file does not have — a book
    has to land under the same name the OPDS client would give it, or the
    reader ends up holding two copies of it.

    The firmware refuses an upload onto an existing name (400 `File already
    exists`) rather than overwriting it, so replacing means `delete` first.
    """
    boundary = f"----x3suite{uuid.uuid4().hex}"
    head = (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; '
            f'filename="{name or file.name}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n").encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    body = head + file.read_bytes() + tail

    code, resp = request(host, "/upload", method="POST", query={"path": directory},
                         body=body,
                         content_type=f"multipart/form-data; boundary={boundary}",
                         timeout=timeout or TIMEOUT)
    if code == 200:
        return
    raise DeviceError(f"upload {file.name}: {code} {resp.decode(errors='replace')}")


# --------------------------------------------------------------------------
# fonts, which have their own endpoints and their own rules


def fonts(host: str) -> dict:
    """`GET /api/fonts` — what families the reader has scanned.

    Returns {"families": [{"name", "sizes": [...], "files": [{"name","size"}]}],
    "maxFamilies": n}. The sizes list is the useful one: 1.5.0's CJK fallback
    for interface text looks for **exactly** 8, 10 and 12 pt, so a family
    without them leaves the chapter list blank however good it looks in a book.
    """
    code, body = request(host, "/api/fonts")
    if code != 200:
        raise DeviceError(f"/api/fonts returned {code}")
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise DeviceError("the font list came back unreadable") from exc


def upload_font(host: str, family: str, file: Path,
                timeout: float = FONT_TIMEOUT) -> None:
    """`POST /api/fonts/upload` — one `.cpfont`, into `/fonts/<family>/`.

    The family is a form field rather than a path: the firmware creates the
    directory itself and refuses a name it does not like, so we never build
    that path by hand.

    It checks the `CPFONT\\0\\0` magic on the first chunk, which catches the
    classic "save link as" HTML masquerading as a font. It cannot catch a
    *truncated* one — the magic is fine and the file is short — so verify byte
    counts against CHECKSUMS.tsv afterwards. A short font lists in the picker
    and silently reverts to built-in Noto when selected.

    These files are large (a single size can be 6.8 MB), so the timeout is
    minutes rather than the usual twenty seconds.
    """
    boundary = f"----x3suite{uuid.uuid4().hex}"
    parts = [
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="family"\r\n\r\n{family}\r\n'.encode(),
        (f"--{boundary}\r\n"
         f'Content-Disposition: form-data; name="file"; filename="{file.name}"\r\n'
         f"Content-Type: application/octet-stream\r\n\r\n").encode(),
        file.read_bytes(),
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    code, resp = request(host, "/api/fonts/upload", method="POST",
                         body=b"".join(parts),
                         content_type=f"multipart/form-data; boundary={boundary}",
                         timeout=timeout)
    if code != 200:
        raise DeviceError(f"{file.name}: {code} {resp.decode(errors='replace')}")


def delete_font_family(host: str, family: str) -> None:
    """`POST /api/fonts/delete` — the whole family, in one call.

    Worth using rather than deleting the files by hand: it goes through the
    firmware's own FontInstaller and marks the font registry dirty, so the
    device notices the change instead of listing a family that is no longer
    there.
    """
    code, body = request(host, "/api/fonts/delete", method="POST",
                         body=json.dumps({"family": family}).encode(),
                         content_type="application/json")
    if code != 200:
        raise DeviceError(f"delete {family}: {code} {body.decode(errors='replace')}")


def download(host: str, path: str, dest: Path) -> Path:
    """GET /download?path=... into a local file.

    Refused for anything whose *final segment* starts with a dot — the firmware
    guards its own system files that way. Note that only the last segment is
    checked, so a file inside /.sleep downloads fine; it is the dotted name
    itself that is protected.
    """
    code, body = request(host, "/download", query={"path": path})
    if code != 200:
        raise DeviceError(f"download {path}: {code} {body.decode(errors='replace')}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)
    return dest


def rename(host: str, path: str, new_name: str) -> None:
    """POST /rename — a real rename on the device, in one call.

    `new_name` is a *name*, not a path: no slashes, and it may not begin with a
    dot (the firmware rejects both). `path` must be the device's own spelling of
    the file — see the note about decomposed accents at the top of this file.

    Device-confirmed 2026-08 on a 1.5.0 RC, with parameters in the query string
    and spaces in both names. The firmware reads them from a form body too; we
    use the query form to match every other call here.
    """
    code, body = request(host, "/rename", method="POST",
                         query={"path": path, "name": new_name})
    if code != 200:
        raise DeviceError(f"rename {path}: {code} {body.decode(errors='replace')}")


def move(host: str, path: str, dest_dir: str) -> None:
    """POST /move — same file, different folder. Files only; not directories."""
    code, body = request(host, "/move", method="POST",
                         query={"path": path, "dest": dest_dir})
    if code != 200:
        raise DeviceError(f"move {path}: {code} {body.decode(errors='replace')}")


def dav_move(host: str, path: str, dest: str) -> None:
    """Rename or move via WebDAV, which is the only way to touch a *folder*.

    The HTTP API refuses directories outright — `/rename` and `/move` both end
    in "Only files can be renamed". WebDAV's MOVE has no such check: it opens
    the source and calls the filesystem's own rename, which on FAT works for a
    directory as well as a file.

    The catch is the other guard. WebDAV's `isProtectedPath` inspects **every
    segment** of the path, not just the last, so anything under a dot-prefixed
    folder — `/.sleep` and everything in it — is unreachable this way. That is
    the mirror image of the HTTP API, which only guards the final segment. Use
    this for folders; use `rename()` for files.

    Source-confirmed at 1.5.0; not yet driven against a device.
    """
    url = f"http://{host}{urllib.parse.quote(path)}"
    req = urllib.request.Request(url, method="MOVE")
    req.add_header("Destination", urllib.parse.quote(dest))
    req.add_header("Overwrite", "F")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            if resp.status in (201, 204):
                return
            raise DeviceError(f"move {path}: unexpected {resp.status}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace").strip()
        raise DeviceError(f"move {path}: {exc.code} {detail}") from None
    except urllib.error.URLError as exc:
        raise DeviceError(f"cannot reach {host}: {exc.reason}") from exc


def settings(host: str) -> dict:
    code, body = request(host, "/api/settings")
    if code != 200:
        raise DeviceError(f"/api/settings returned {code}")
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise DeviceError("settings came back unreadable") from exc


def set_settings(host: str, values: dict) -> bool:
    """POST /api/settings — partial JSON by key, e.g. {"sleepScreen": 2}.

    The keys are the firmware's own, from `src/SettingsList.h`.
    """
    code, _ = request(host, "/api/settings", method="POST",
                      body=json.dumps(values).encode(),
                      content_type="application/json")
    return code == 200
