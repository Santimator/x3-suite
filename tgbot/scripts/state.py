#!/usr/bin/env python3
"""What the bot must not forget: the delivery queue, and a note or two.

The queue is the seam between the two scopes. Building a wallpaper happens
whenever you like; putting it on the reader happens only when you say so and
only while the device is on its File Transfer screen. In between, the work sits
here — on disk, because "the bot restarted" and "the Pi lost power" must not be
the same thing as "your wallpaper never existed".

Written as JSON rather than SQLite: the whole point of this file is that you
can `cat` it when something looks wrong, and a queue of a dozen wallpapers is
not a database problem. Every write goes to a temporary file in the same
directory and is then `os.replace`d over the real one, which is atomic on POSIX
— so a crash mid-write leaves the previous queue intact rather than half a
file. That is the property the whole "never drain partially" promise rests on.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path


def _write_atomic(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)          # atomic: readers see old or new, never half


class Queue:
    """Things waiting to be pushed to the device, in the order they arrived.

    Only device-bound work belongs here. Books never do — the X3 pulls those
    from the OPDS catalog by itself, so a book in a delivery queue would be a
    category error waiting to become a duplicate on the SD card.
    """

    def __init__(self, path: Path):
        self.path = Path(path)

    def _read(self) -> list:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return data if isinstance(data, list) else []

    def items(self) -> list:
        return self._read()

    def __len__(self) -> int:
        return len(self._read())

    def add(self, kind: str, path: str, label: str = "") -> dict:
        item = {"id": uuid.uuid4().hex[:8], "kind": kind, "path": str(path),
                "label": label or Path(path).name, "added": time.time()}
        items = self._read()
        items.append(item)
        _write_atomic(self.path, items)
        return item

    def remove(self, item_id: str) -> bool:
        items = self._read()
        kept = [i for i in items if i["id"] != item_id]
        if len(kept) == len(items):
            return False
        _write_atomic(self.path, kept)
        return True

    def clear(self) -> int:
        n = len(self._read())
        _write_atomic(self.path, [])
        return n

    def remove_many(self, ids) -> None:
        ids = set(ids)
        _write_atomic(self.path, [i for i in self._read() if i["id"] not in ids])


class Notes:
    """A small key-value scratchpad — last push, last device, that sort of
    thing. Same atomic write, same reason."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def all(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, key, default=None):
        return self.all().get(key, default)

    def set(self, key, value) -> None:
        data = self.all()
        data[key] = value
        _write_atomic(self.path, data)


class Tokens:
    """Short handles for things too long to put in a button.

    Telegram caps `callback_data` at 64 bytes. A device filename alone can be
    three times that — and the one thing we must never do is reconstruct a
    device name from something shorter, because the SD card holds names with
    decomposed accents (`é` as `e` + U+0301) that compare unequal to the
    composed form the moment anything helpfully normalizes them. So the button
    carries a token, and the real string is kept here, verbatim.

    In memory on purpose: a restart should invalidate the buttons in your
    scrollback, since the folder they describe may have changed underneath
    them. An unknown token is answered with "that menu is stale", not guessed
    at.
    """

    def __init__(self, limit: int = 2000):
        self._values = {}
        self._order = []
        self._limit = limit
        self._n = 0

    def put(self, value) -> str:
        self._n += 1
        key = f"t{self._n:x}"
        self._values[key] = value
        self._order.append(key)
        while len(self._order) > self._limit:
            self._values.pop(self._order.pop(0), None)
        return key

    def get(self, key):
        return self._values.get(key)
