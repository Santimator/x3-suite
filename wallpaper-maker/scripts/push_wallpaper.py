#!/usr/bin/env python3
"""Put built wallpapers on the reader over WiFi, into the folder it reads.

**Why this exists and OPDS does not do it.** The suite's other delivery path is
`opds-server/`, and it cannot carry a wallpaper. Not a limitation of ours: the
X3's OPDS client only follows an acquisition link whose type is exactly
`application/epub+zip`, and it saves whatever it downloads to the SD *root* as
`<author> - <title>.epub`. There is no content type it will accept and no
destination it can be pointed at. A catalog is a pull, and the device only ever
pulls books.

So delivery goes the other way — a push, into the file-transfer web server the
firmware already ships (`src/network/CrossPointWebServer.cpp`, port 80, no
auth, CORS open). That server is the same one the browser file manager talks
to; this is that conversation without the browser.

On the device: **Home -> File Transfer -> Join a Network**. It prints an
address and sits on that screen while we work; the server stops when you leave
it. Then:

    push_wallpaper.py                       # find the reader, push build/*.bmp
    push_wallpaper.py --ip 192.168.1.42     # if mDNS is unhelpful here
    push_wallpaper.py --list                # what is on the device now
    push_wallpaper.py --replace             # delete its wallpapers first

An address that answers is written to `last-device.json` (gitignored) and tried
first next time, so `--ip` is normally a one-off. mDNS is the thing most likely
to be missing — a local DNS filter or reverse proxy will happily answer for
`crosspoint.local` and never mention the reader — which is why a remembered
address is tried before the name, and the firmware's own UDP discovery ping
after both.

Where they land, and why it is decided for you:

  /.sleep/     The firmware's preferred pool — checked first, one file picked at
               random each time the device sleeps. Hidden, so it stays out of
               the file browser. This is the default target.
  /sleep/      The visible fallback, only read when /.sleep does not exist. If
               you already keep wallpapers here we push here instead, because
               creating /.sleep would silently shadow everything in it.

Two firmware behaviours worth knowing, both handled here: an upload onto an
existing name is *rejected*, not overwritten (so we delete first and retry),
and WebDAV — the other way in — refuses every path segment beginning with a
dot, which rules out /.sleep entirely. Hence the plain HTTP API.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from datetime import datetime
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DIR = REPO_ROOT / "workspace" / "wallpapers" / "build"

# Where the last address that answered is kept, so the next run starts there.
# Gitignored: it is a fact about your LAN, not about this repo. X3_LAST_DEVICE
# moves it, which is how the self-test keeps its hands off yours.
LAST_DEVICE = Path(os.environ.get("X3_LAST_DEVICE")
                   or Path(__file__).resolve().parents[1] / "last-device.json")

# The firmware's own defaults: web server on 80, mDNS name crosspoint.local,
# and a UDP discovery responder on 8134 that answers the payload "hello".
DEFAULT_HOST = "crosspoint.local"
DISCOVERY_PORT = 8134
DISCOVERY_PAYLOAD = b"hello"

PREFERRED_DIR = "/.sleep"
FALLBACK_DIR = "/sleep"

# SLEEP_SCREEN_MODE in src/CrossPointSettings.h; the web settings key is
# "sleepScreen" and takes the enum index.
SLEEP_MODE_CUSTOM = 2

TIMEOUT = 20
PROBE_TIMEOUT = 3       # just asking "are you there?" — do not stall on a dead address


class DeviceError(Exception):
    pass


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


def _request(host: str, path: str, *, method: str = "GET", query: dict | None = None,
             body: bytes | None = None, content_type: str | None = None,
             timeout: float = TIMEOUT):
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
    code, body = _request(host, "/api/status", timeout=timeout)
    if code != 200:
        raise DeviceError(f"/api/status returned {code}")
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise DeviceError(f"{host} answered, but not like a CrossPoint reader") from exc


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


def find_device(explicit: str | None):
    """Locate the reader, and return (host, its /api/status).

    An address given on the command line is used *as given* — if it does not
    answer that is an error, not a reason to go looking somewhere else and push
    to whatever turns up. Everything else is a guess, tried cheapest first:

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


def list_dir(host: str, path: str) -> list:
    """Contents of a folder, as /api/files reports them.

    Note the firmware hides dot-prefixed *entries* from this listing unless the
    device's `showHiddenFiles` is on — so files inside /.sleep are visible but
    /.sleep itself is not listed at the root. A missing folder and an empty one
    both come back as [].
    """
    code, body = _request(host, "/api/files", query={"path": path})
    if code != 200:
        return []
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return []


def mkdir(host: str, parent: str, name: str) -> bool:
    """True if the folder now exists (created, or already there)."""
    code, body = _request(host, "/mkdir", method="POST",
                          query={"path": parent, "name": name})
    if code == 200:
        return True
    if b"already exists" in body:
        return True
    raise DeviceError(f"mkdir {parent}/{name}: {code} {body.decode(errors='replace')}")


def delete(host: str, path: str) -> bool:
    code, _ = _request(host, "/delete", method="POST", query={"path": path})
    return code == 200


def upload(host: str, directory: str, file: Path) -> None:
    """POST /upload?path=DIR with the file as multipart form data.

    The destination is a query parameter, not a form field, because the
    firmware's upload callback needs the path before the multipart body has
    finished arriving.
    """
    boundary = f"----x3suite{uuid.uuid4().hex}"
    head = (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{file.name}"\r\n'
            f"Content-Type: image/bmp\r\n\r\n").encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    body = head + file.read_bytes() + tail

    code, resp = _request(host, "/upload", method="POST", query={"path": directory},
                          body=body,
                          content_type=f"multipart/form-data; boundary={boundary}")
    if code == 200:
        return
    raise DeviceError(f"upload {file.name}: {code} {resp.decode(errors='replace')}")


def choose_dir(host: str, create: bool = True) -> str:
    """Pick /.sleep or /sleep — whichever the device is actually reading.

    The firmware checks /.sleep first and only falls back to /sleep when it does
    not exist. So if there are already wallpapers in /sleep, creating /.sleep
    would make them all disappear without a word. Respect what is there.
    """
    existing = [f for f in list_dir(host, FALLBACK_DIR)
                if not f.get("isDirectory") and f.get("name", "").lower().endswith(".bmp")]
    if existing:
        return FALLBACK_DIR
    if create:
        mkdir(host, "/", PREFERRED_DIR.lstrip("/"))
    return PREFERRED_DIR


def set_custom_mode(host: str) -> bool:
    body = json.dumps({"sleepScreen": SLEEP_MODE_CUSTOM}).encode()
    code, _ = _request(host, "/api/settings", method="POST", body=body,
                       content_type="application/json")
    return code == 200


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", type=Path,
                    help=f"BMPs or a folder (default: {DEFAULT_DIR.relative_to(REPO_ROOT)}/)")
    ap.add_argument("--ip", "--host", dest="host", metavar="ADDR",
                    help="the reader's address. Remembered on success, and "
                         "tried first next time. Without it: the remembered "
                         f"address, then {DEFAULT_HOST}, then UDP discovery")
    ap.add_argument("--dir", help="target folder on the SD card "
                                  f"(default: {PREFERRED_DIR}, or {FALLBACK_DIR} if in use)")
    ap.add_argument("--list", action="store_true", help="show what is there and stop")
    ap.add_argument("--replace", action="store_true",
                    help="delete the wallpapers already on the device first")
    ap.add_argument("--no-set-mode", action="store_true",
                    help="do not switch the sleep screen to Custom")
    args = ap.parse_args()

    try:
        host, info = find_device(args.host)
        remember(host)
        print(f"reader at {host}: {info.get('model', 'unknown model')}, "
              f"firmware {info.get('version', '?')}")

        # --list must not change the device: creating /.sleep to look inside it
        # would shadow whatever is in /sleep.
        target = args.dir or choose_dir(host, create=not args.list)
        if args.dir and not args.list:
            parent, _, name = args.dir.rstrip("/").rpartition("/")
            mkdir(host, parent or "/", name)
        present = [f for f in list_dir(host, target)
                   if not f.get("isDirectory") and f.get("name", "").lower().endswith(".bmp")]

        if args.list:
            print(f"\n{target}/ — {len(present)} wallpaper(s)")
            for f in present:
                print(f"  {f['name']}  ({f.get('size', 0) // 1024} KB)")
            if target == PREFERRED_DIR:
                print("\n(this folder is hidden; the device's file browser will "
                      "not show it unless Show Hidden Files is on)")
            return 0

        inputs = args.files or [DEFAULT_DIR]
        sources = []
        for item in inputs:
            if item.is_dir():
                sources += sorted(p for p in item.iterdir()
                                  if p.suffix.lower() == ".bmp")
            elif item.is_file():
                sources.append(item)
        if not sources:
            where = ", ".join(str(i) for i in inputs)
            print(f"push_wallpaper: no .bmp files in {where} — "
                  "run make_wallpaper.py first", file=sys.stderr)
            return 1

        if args.replace and present:
            for f in present:
                delete(host, f"{target}/{f['name']}")
            print(f"cleared {len(present)} wallpaper(s) from {target}/")
            present = []

        on_device = {f["name"] for f in present}
        print(f"\npushing {len(sources)} file(s) to {target}/")
        for src in sources:
            # The firmware refuses an upload onto an existing name rather than
            # overwriting it, so replacing means deleting first.
            if src.name in on_device:
                delete(host, f"{target}/{src.name}")
            upload(host, target, src)
            print(f"  {src.name}  ({src.stat().st_size // 1024} KB)")

        if not args.no_set_mode:
            if set_custom_mode(host):
                print("\nsleep screen set to Custom")
            else:
                print("\ncould not set the sleep screen mode — do it on the device: "
                      "Settings -> Display -> Sleep Screen -> Custom")
        else:
            print("\nSleep screen must be set to Custom for these to show: "
                  "Settings -> Display -> Sleep Screen")

        print("Leave the File Transfer screen and let it sleep.")

    except DeviceError as exc:
        print(f"push_wallpaper: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
