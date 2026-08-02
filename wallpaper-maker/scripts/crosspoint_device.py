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


def upload(host: str, directory: str, file: Path,
           content_type: str = "application/octet-stream") -> None:
    """POST /upload?path=DIR with the file as multipart form data.

    The destination is a query parameter, not a form field, because the
    firmware's upload callback needs the path before the multipart body has
    finished arriving.

    The firmware refuses an upload onto an existing name (400 `File already
    exists`) rather than overwriting it, so replacing means `delete` first.
    """
    boundary = f"----x3suite{uuid.uuid4().hex}"
    head = (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{file.name}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n").encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    body = head + file.read_bytes() + tail

    code, resp = request(host, "/upload", method="POST", query={"path": directory},
                         body=body,
                         content_type=f"multipart/form-data; boundary={boundary}")
    if code == 200:
        return
    raise DeviceError(f"upload {file.name}: {code} {resp.decode(errors='replace')}")


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
