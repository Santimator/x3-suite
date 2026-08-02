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
to; this is that conversation without the browser. The conversation itself —
discovery, listing, upload, delete — lives in `crosspoint_device.py` next door,
because `tgbot/` holds the same conversation and neither script owns it.

On the device: **Home -> File Transfer -> Join a Network**. It prints an
address and sits on that screen while we work; the server stops when you leave
it. Then:

    push_wallpaper.py                       # find the reader, push build/*.bmp
    push_wallpaper.py --ip 192.168.1.42     # if mDNS is unhelpful here
    push_wallpaper.py --list                # what is on the device now
    push_wallpaper.py --replace             # delete its wallpapers first
    push_wallpaper.py --json                # ... for something else to read

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
import sys
from pathlib import Path

from crosspoint_device import (DeviceError, delete, find_device, list_dir, mkdir,
                               remember, set_settings, upload)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DIR = REPO_ROOT / "workspace" / "wallpapers" / "build"

PREFERRED_DIR = "/.sleep"
FALLBACK_DIR = "/sleep"

# SLEEP_SCREEN_MODE in src/CrossPointSettings.h; the web settings key is
# "sleepScreen" and takes the enum index.
SLEEP_MODE_CUSTOM = 2


def bmps_in(host: str, path: str) -> list:
    return [f for f in list_dir(host, path)
            if not f.get("isDirectory") and f.get("name", "").lower().endswith(".bmp")]


def choose_dir(host: str, create: bool = True) -> str:
    """Pick /.sleep or /sleep — whichever the device is actually reading.

    The firmware checks /.sleep first and only falls back to /sleep when it does
    not exist. So if there are already wallpapers in /sleep, creating /.sleep
    would make them all disappear without a word. Respect what is there.
    """
    if bmps_in(host, FALLBACK_DIR):
        return FALLBACK_DIR
    if create:
        mkdir(host, "/", PREFERRED_DIR.lstrip("/"))
    return PREFERRED_DIR


def set_custom_mode(host: str) -> bool:
    return set_settings(host, {"sleepScreen": SLEEP_MODE_CUSTOM})


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", type=Path,
                    help=f"BMPs or a folder (default: {DEFAULT_DIR.relative_to(REPO_ROOT)}/)")
    ap.add_argument("--ip", "--host", dest="host", metavar="ADDR",
                    help="the reader's address. Remembered on success, and "
                         "tried first next time. Without it: the remembered "
                         "address, then crosspoint.local, then UDP discovery")
    ap.add_argument("--dir", help="target folder on the SD card "
                                  f"(default: {PREFERRED_DIR}, or {FALLBACK_DIR} if in use)")
    ap.add_argument("--list", action="store_true", help="show what is there and stop")
    ap.add_argument("--replace", action="store_true",
                    help="delete the wallpapers already on the device first")
    ap.add_argument("--no-set-mode", action="store_true",
                    help="do not switch the sleep screen to Custom")
    ap.add_argument("--json", action="store_true",
                    help="report as JSON on stdout instead of prose — one "
                         "record per file, so a caller can say which ones "
                         "landed. Nothing else is printed")
    args = ap.parse_args()

    report = {"ok": False, "host": None, "target": None, "items": [], "error": None}

    def say(*a, **kw):
        if not args.json:
            print(*a, **kw)

    try:
        host, info = find_device(args.host)
        remember(host)
        report["host"] = host
        report["firmware"] = info.get("version")
        say(f"reader at {host}: {info.get('model', 'unknown model')}, "
            f"firmware {info.get('version', '?')}")

        # --list must not change the device: creating /.sleep to look inside it
        # would shadow whatever is in /sleep.
        target = args.dir or choose_dir(host, create=not args.list)
        if args.dir and not args.list:
            parent, _, name = args.dir.rstrip("/").rpartition("/")
            mkdir(host, parent or "/", name)
        report["target"] = target
        present = bmps_in(host, target)

        if args.list:
            report["ok"] = True
            report["items"] = [{"name": f["name"], "bytes": f.get("size", 0)}
                               for f in present]
            say(f"\n{target}/ — {len(present)} wallpaper(s)")
            for f in present:
                say(f"  {f['name']}  ({f.get('size', 0) // 1024} KB)")
            if target == PREFERRED_DIR:
                say("\n(this folder is hidden; the device's file browser will "
                    "not show it unless Show Hidden Files is on)")
            if args.json:
                json.dump(report, sys.stdout, ensure_ascii=False)
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
            report["error"] = f"no .bmp files in {where}"
            if args.json:
                json.dump(report, sys.stdout, ensure_ascii=False)
            else:
                print(f"push_wallpaper: {report['error']} — "
                      "run make_wallpaper.py first", file=sys.stderr)
            return 1

        if args.replace and present:
            for f in present:
                delete(host, f"{target}/{f['name']}")
            say(f"cleared {len(present)} wallpaper(s) from {target}/")
            present = []

        on_device = {f["name"] for f in present}
        say(f"\npushing {len(sources)} file(s) to {target}/")
        failures = 0
        for src in sources:
            # The firmware refuses an upload onto an existing name rather than
            # overwriting it, so replacing means deleting first.
            item = {"name": src.name, "bytes": src.stat().st_size, "ok": False}
            try:
                if src.name in on_device:
                    delete(host, f"{target}/{src.name}")
                upload(host, target, src, content_type="image/bmp")
                item["ok"] = True
                say(f"  {src.name}  ({src.stat().st_size // 1024} KB)")
            except DeviceError as exc:
                # One bad file is not a reason to abandon the rest; the caller
                # is told exactly which ones landed.
                item["error"] = str(exc)
                failures += 1
                say(f"  {src.name}  FAILED — {exc}", file=sys.stderr)
            report["items"].append(item)

        if not args.no_set_mode:
            report["sleep_mode_set"] = set_custom_mode(host)
            if report["sleep_mode_set"]:
                say("\nsleep screen set to Custom")
            else:
                say("\ncould not set the sleep screen mode — do it on the device: "
                    "Settings -> Display -> Sleep Screen -> Custom")
        else:
            say("\nSleep screen must be set to Custom for these to show: "
                "Settings -> Display -> Sleep Screen")

        say("Leave the File Transfer screen and let it sleep.")
        report["ok"] = failures == 0
        if args.json:
            json.dump(report, sys.stdout, ensure_ascii=False)
        return 0 if failures == 0 else 1

    except DeviceError as exc:
        report["error"] = str(exc)
        if args.json:
            json.dump(report, sys.stdout, ensure_ascii=False)
        else:
            print(f"push_wallpaper: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
