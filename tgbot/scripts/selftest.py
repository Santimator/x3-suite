#!/usr/bin/env python3
"""The gate: does the bot refuse what it must refuse, and forget nothing?

    python3 tgbot/scripts/selftest.py

No token, no network, no reader. The bot is built against a fake Telegram that
records what would have been sent, and a temporary workspace, which is exactly
enough to grade the four things that would actually hurt:

  1. **The whitelist.** Every update type, including button presses. A bot that
     checked messages and not callbacks would look secure and hand a stranger
     the delete buttons.
  2. **The path jail.** Nothing the chat can name may escape `workspace/`.
  3. **The queue survives.** A crash between two writes must lose nothing, and
     a book is never queued behind your back — only when you ask for one.
  4. **A failed push changes nothing.** No device, no removals — because the
     whole point of queueing all week is that pushing cannot quietly eat it.

Checks 1 and 3 are why this file exists at all: they are invariants about
*refusing*, and the only way to see a refusal work is to try the thing.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import suite                                            # noqa: E402
from bot import Bot                                     # noqa: E402
from config import ConfigError, safe_join               # noqa: E402
from state import Queue, Tokens                         # noqa: E402

OWNER = 424242
STRANGER = 999999

failures = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}")
    if not ok:
        failures.append(label)
        if detail:
            print(f"        {detail}")


class FakeTelegram:
    """Records instead of sending. Nothing here talks to Telegram."""

    def __init__(self):
        self.sent = []
        self.answered = []

    def send_message(self, chat_id, text, keyboard=None, **kw):
        self.sent.append({"chat": chat_id, "text": text, "keyboard": keyboard})
        return {"message_id": len(self.sent)}

    def send_photo(self, chat_id, photo, caption="", keyboard=None):
        self.sent.append({"chat": chat_id, "text": caption, "photo": str(photo),
                          "keyboard": keyboard, "message_id": len(self.sent) + 1})
        return {"message_id": len(self.sent)}

    def send_document(self, chat_id, doc, caption=""):
        self.sent.append({"chat": chat_id, "text": caption, "doc": str(doc)})
        return {}

    def answer_callback(self, callback_id, text=""):
        self.answered.append(callback_id)

    def download(self, file_id, dest):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"pretend")
        return dest

    def edit_message(self, *a, **kw):
        return {}

    def edit_markup(self, chat_id, message_id, keyboard):
        # Record the swap the way Telegram would show it: same message, new
        # buttons.
        self.sent.append({"chat": chat_id, "text": "(markup)",
                          "keyboard": keyboard, "edited": message_id})
        return {}


_instances = 0


def make_bot(tmp: Path):
    """A bot with a workspace and a state dir of its own.

    Each call gets a fresh pair — an earlier case's queue leaking into a later
    one made a passing test out of a failing one exactly once, which is enough.
    """
    global _instances
    _instances += 1
    tmp = tmp / f"run{_instances}"
    cfg = {
        "telegram": {"user_id": OWNER, "poll_timeout": 1},
        "workspace": str(tmp / "workspace"),
        "workspace_path": tmp / "workspace",
        "state_dir": str(tmp / "state"),
        "state_path": tmp / "state",
        "opds_url": "http://127.0.0.1:9/opds",     # discard port: never answers
        "max_download_mb": 20,
        "_config_path": "(test)",
    }
    (tmp / "workspace").mkdir(parents=True, exist_ok=True)
    tg = FakeTelegram()
    return Bot(cfg, tg, sync=True), tg


def msg(text, user=OWNER, chat=None):
    return {"update_id": 1, "message": {"message_id": 1, "text": text,
                                        "chat": {"id": chat if chat is not None else user},
                                        "from": {"id": user}}}


def cb(data, user=OWNER, chat=None):
    return {"update_id": 2, "callback_query": {
        "id": "c1", "data": data, "from": {"id": user},
        "message": {"message_id": 1,
                    "chat": {"id": chat if chat is not None else user}}}}


# -- 1. the whitelist ------------------------------------------------------


def check_auth(tmp: Path) -> None:
    print("\nthe whitelist, before any handler runs:")
    bot, tg = make_bot(tmp)

    check("a stranger's message is dropped",
          bot.handle(msg("/menu", user=STRANGER)) is False and not tg.sent)

    tg.sent.clear()
    check("a stranger's button press is dropped",
          bot.handle(cb("m:main", user=STRANGER)) is False and not tg.sent,
          "callbacks are where the delete buttons live")

    tg.sent.clear()
    check("a stranger's edited message is dropped",
          bot.handle({"update_id": 3, "edited_message": {
              "message_id": 1, "text": "/menu", "chat": {"id": STRANGER},
              "from": {"id": STRANGER}}}) is False and not tg.sent)

    tg.sent.clear()
    # The owner's id arriving from a group chat is the case a sender-only check
    # would wave through.
    check("the owner's id in a foreign chat is dropped",
          bot.handle(msg("/menu", user=OWNER, chat=-100123)) is False and not tg.sent)

    tg.sent.clear()
    check("the owner is served",
          bot.handle(msg("/menu")) is True and len(tg.sent) == 1,
          str(tg.sent))

    tg.sent.clear()
    bot.handle(cb("m:q"))
    check("the owner's button press is served", len(tg.sent) == 1, str(tg.sent))


# -- 2. the path jail ------------------------------------------------------


def check_paths(tmp: Path) -> None:
    print("\nthe path jail:")
    root = tmp / "jail"
    (root / "inside").mkdir(parents=True, exist_ok=True)

    for hostile in ("../escape.txt", "../../etc/passwd", "/etc/passwd",
                    "sub/../../out.txt"):
        try:
            safe_join(root, hostile)
            check(f"refuses {hostile!r}", False, "it was allowed")
        except ConfigError:
            check(f"refuses {hostile!r}", True)

    ok = safe_join(root, "inside", "fine.bmp")
    check("allows an ordinary name", ok.name == "fine.bmp")

    # A symlink pointing out of the workspace resolves out of it, and must be
    # caught by resolution rather than by string comparison.
    link = root / "sneaky"
    try:
        link.symlink_to(tmp)
        try:
            safe_join(root, "sneaky", "x")
            check("refuses a symlink that leaves the root", False, "allowed")
        except ConfigError:
            check("refuses a symlink that leaves the root", True)
    except OSError:
        check("refuses a symlink that leaves the root", True, "(symlinks unavailable)")

    bot, tg = make_bot(tmp)
    outsider = tmp / "outsider.epub"
    outsider.write_bytes(b"not really an epub")
    token = bot.tokens.put(str(outsider))
    tg.sent.clear()
    bot.handle(cb(f"lib:rm!:{token}"))
    check("will not delete a file outside the workspace",
          outsider.exists() and "outside" in tg.sent[-1]["text"],
          tg.sent[-1]["text"] if tg.sent else "nothing said")


# -- 3. the queue ----------------------------------------------------------


def check_queue(tmp: Path) -> None:
    print("\nthe queue, across a restart:")
    path = tmp / "q" / "queue.json"
    q = Queue(path)
    q.add("wallpaper", "/tmp/one.bmp", "one")
    q.add("wallpaper", "/tmp/two.bmp", "two")

    fresh = Queue(path)          # a brand-new object, as after a restart
    check("survives being reopened", len(fresh) == 2, str(fresh.items()))
    check("keeps the order it was given",
          [i["label"] for i in fresh.items()] == ["one", "two"])

    ids = [i["id"] for i in fresh.items()]
    fresh.remove(ids[0])
    check("removing one leaves the other", len(Queue(path)) == 1)

    # The atomic write is the promise; a half-written file must not be
    # reachable, so nothing but the finished JSON ever has the real name.
    stray = list(path.parent.glob(".*tmp*"))
    check("leaves no temporary files behind", not stray, str(stray))

    raw = json.loads(path.read_text())
    check("stays readable JSON on disk", isinstance(raw, list))

    print("\nbooks never enter the delivery queue:")
    bot, tg = make_bot(tmp)
    book = bot.workspace / "incoming.epub"
    book.write_bytes(b"PK\x03\x04 not a real epub")
    bot.take_book(bot.user_id, book)
    check("an EPUB is filed, not queued", len(bot.queue) == 0, str(bot.queue.items()))
    check("... and lands where the catalog scans",
          (bot.workspace / "library" / "incoming.epub").exists())


# -- 4. a push that finds nothing -----------------------------------------


def no_reader():
    raise suite.DeviceError("no reader found")


def check_push_is_all_or_nothing(tmp: Path) -> None:
    print("\na push with no reader on the network:")
    bot, tg = make_bot(tmp)
    bmp = bot.workspace / "wall.bmp"
    bmp.write_bytes(b"BM fake")
    bot.queue.add("wallpaper", str(bmp), "wall.bmp")
    book = bot.workspace / "b.epub"
    book.write_bytes(b"PK\x03\x04")
    bot.queue.add("book", str(book), "A Book",
                  meta={"author": "Someone", "title": "A Book"})

    bot.device_host = no_reader
    tg.sent.clear()
    bot.handle(cb("push:go"))
    check("tapping Ready is acknowledged before anything slow happens",
          tg.sent and "Listening" in tg.sent[0]["text"],
          tg.sent[0]["text"] if tg.sent else "silence")
    check("nothing is removed — wallpapers or books",
          len(bot.queue) == 2, str(bot.queue.items()))
    check("and it says so out loud",
          any("No reader" in s["text"] for s in tg.sent), str(tg.sent[-1]))

    # The underlying error is push_wallpaper.py's, and it signs off with
    # "--ip 192.168.x.x" — sound at a terminal, unusable in a chat. The chat
    # gets a button instead, and must never quote the flag.
    everything = " ".join(s["text"] for s in tg.sent)
    check("no command-line advice leaks into the chat", "--ip" not in everything,
          everything[-200:])
    check("... and there is a way to give the address instead",
          any(b[1] == "dev:addr:" for s in tg.sent
              for row in (s["keyboard"] or []) for b in row), str(tg.sent[-1]))

    # Whatever the File Transfer screen shows, pasted from a phone.
    seen = {}
    orig_status, orig_remember = suite.device.status, suite.device.remember
    try:
        suite.device.status = lambda h, **k: seen.setdefault("host", h) and {} or {}
        suite.device.remember = lambda h: seen.__setitem__("kept", h)
        for typed in ("192.168.1.42", "http://192.168.1.42/", "  crosspoint.local  "):
            seen.clear()
            bot.handle(cb("dev:addr:"))
            bot.handle(msg(typed))
            check(f"{typed!r} is understood as an address",
                  seen.get("kept") in ("192.168.1.42", "crosspoint.local"),
                  str(seen))
    finally:
        suite.device.status, suite.device.remember = orig_status, orig_remember

    # One lands, one fails: exactly the one that landed leaves.
    print("\na push where one file lands and one does not:")
    bot, tg = make_bot(tmp)
    for name in ("wall.bmp", "other.bmp"):
        p = bot.workspace / name
        p.write_bytes(b"BM fake")
        bot.queue.add("wallpaper", str(p), name)

    original = suite.push
    try:
        bot.device_host = lambda: ("10.0.0.5", {})
        suite.push = lambda files, host=None: {
            "ok": False, "host": "10.0.0.5", "target": "/.sleep",
            "items": [{"name": "wall.bmp", "ok": True},
                      {"name": "other.bmp", "ok": False, "error": "File already exists"}]}
        bot.do_push(bot.user_id)
        left = [i["label"] for i in bot.queue.items()]
        check("only the file that landed leaves the queue", left == ["other.bmp"],
              str(left))
        check("the failure is reported per item",
              any("❌" in s["text"] and "✅" in s["text"] for s in tg.sent),
              tg.sent[-1]["text"] if tg.sent else "")
    finally:
        suite.push = original

    print("\na queued file that vanished from disk:")
    bot, tg = make_bot(tmp)
    bot.queue.add("wallpaper", str(bot.workspace / "never-existed.bmp"), "ghost")
    original = suite.push
    try:
        bot.device_host = lambda: ("10.0.0.5", {})
        suite.push = lambda files, host=None: {"ok": True, "host": "10.0.0.5",
                                               "items": []}
        bot.do_push(bot.user_id)
        check("is dropped and named, not pushed",
              len(bot.queue) == 0 and any("gone" in s["text"] for s in tg.sent),
              str(tg.sent))
    finally:
        suite.push = original


# -- 4b. books reach the card under the catalog's own name -----------------


def check_book_delivery(tmp: Path) -> None:
    """A book pushed and the same book pulled must be ONE file on the card."""
    print("\nbooks on the SD card:")
    import crosspoint_client as opds

    entry = opds.OpdsEntry()
    entry.author, entry.title = "Tirso de Molina", "Los alcaldes encontrados"
    check("the push name is the OPDS client's name",
          "/" + suite.device_book_name(entry.author, entry.title)
          == opds.sd_filename(entry),
          suite.device_book_name(entry.author, entry.title))

    # Against a device, the name depends on opdsFilenameFormat — and
    # /api/settings answers with a *list* of setting objects, not a map.
    # Reading it as a map raised "'list' object has no attribute 'get'" in the
    # middle of a push, which is exactly where it costs the most.
    orig_req = suite.device.request
    live_shape = [{"key": "sleepScreen", "type": "enum", "value": 2},
                  {"key": "opdsFilenameFormat", "type": "enum", "value": 1,
                   "options": ["Author - Title", "Title - Author", "Title"]}]
    try:
        suite.device.request = lambda host, path, **kw: (
            (200, json.dumps(live_shape).encode()) if path == "/api/settings"
            else orig_req(host, path, **kw))
        check("the device's own naming format is read from a list response",
              suite.device_book_name("Tirso de Molina", "Los alcaldes encontrados",
                                     host="h")
              == "Los alcaldes encontrados - Tirso de Molina.epub",
              suite.device_book_name("Tirso de Molina", "Los alcaldes encontrados",
                                     host="h"))
        check("... and an unreachable reader falls back to the default layout",
              suite.device_book_name("A", "B", host=None) == "A - B.epub")
    finally:
        suite.device.request = orig_req

    long_title = "A" * 200
    name = suite.device_book_name("Author", long_title)
    check("the 100-byte budget is respected, extension intact",
          len(name.encode()) <= 105 and name.endswith(".epub"), name)
    check("a title of only dots does not produce an empty name",
          suite.device_book_name("", "...") == "book.epub",
          suite.device_book_name("", "..."))

    bot, tg = make_bot(tmp)
    book = bot.workspace / "x.epub"
    book.write_bytes(b"PK\x03\x04")
    bot.take_book(bot.user_id, book)
    check("filing a book still does not queue it", len(bot.queue) == 0)

    offered = [b[1] for s in tg.sent for row in (s["keyboard"] or []) for b in row
               if b[1].startswith("bq:")]
    check("but the card is offered as an extra", bool(offered), str(tg.sent[-1]))

    tg.sent.clear()
    bot.handle(cb(offered[-1]))
    check("... and asking for it queues one", len(bot.queue) == 1,
          str(bot.queue.items()))
    check("the queue remembers author and title, not just a filename",
          "meta" in bot.queue.items()[0], str(bot.queue.items()[0]))

    sent = []
    original = suite.upload_book
    try:
        bot.device_host = lambda: ("10.0.0.5", {})
        suite.upload_book = lambda host, path, name: sent.append(name)
        bot.do_push(bot.user_id)
        check("it uploads under the device's own naming",
              sent and sent[0].endswith(".epub"), str(sent))
        check("and leaves the queue when it lands", len(bot.queue) == 0)
    finally:
        suite.upload_book = original

    check("the catalog copy is still there",
          (bot.workspace / "library" / "x.epub").exists())

    # A book already in the catalog must be sendable too — otherwise the card
    # is only reachable in the seconds after an upload.
    bot, tg = make_bot(tmp)
    token = bot.tokens.put({"path": str(book), "title": "Old Book",
                            "author": "Someone"})
    bot.handle(cb(f"lib:f:{token}"))
    check("a book already on the catalog can be sent as well",
          any(b[1].startswith("bq:") for row in (tg.sent[-1]["keyboard"] or [])
              for b in row), str(tg.sent[-1]["keyboard"]))

    # Buttons from before this change carried a bare path string.
    tg.sent.clear()
    bot.handle(cb(f"lib:f:{bot.tokens.put(str(book))}"))
    check("an older button carrying a bare path still opens",
          tg.sent and "x.epub" in tg.sent[-1]["text"], str(tg.sent))


# -- 5. buttons ------------------------------------------------------------


def check_tokens(tmp: Path) -> None:
    print("\nbuttons carry tokens, not names:")
    tokens = Tokens()
    # The real thing this protects: a device name far past Telegram's 64-byte
    # callback_data limit, with decomposed accents that must survive intact.
    nasty = ("/Historia-de-la-magia-resumen-de-sus-procedimientos-ritos-y-"
             "misterios-Eliphas-Lévi-versión-española.epub")
    key = tokens.put(nasty)
    check("a token fits in callback_data",
          len(f"dev:f:{key}".encode()) <= 64, f"{len(key)} bytes")
    check("the name comes back byte-identical",
          tokens.get(key) == nasty, repr(tokens.get(key)))
    check("an unknown token is not guessed at", tokens.get("t999") is None)

    bot, tg = make_bot(tmp)
    tg.sent.clear()
    bot.handle(cb("dev:f:t999"))
    check("a stale button is answered, not acted on",
          any("stale" in s["text"] or "restart" in s["text"] for s in tg.sent),
          str(tg.sent))


# -- 4d. the wallpaper collection ------------------------------------------


def check_wallpaper_collection(tmp: Path) -> None:
    print("\nwallpapers as a collection, not a one-way trip:")
    bot, tg = make_bot(tmp)
    build = bot.workspace / "wallpapers" / "build"
    build.mkdir(parents=True, exist_ok=True)

    original_out, original_preview = suite.WALLPAPER_OUT, suite.bmp_preview
    try:
        suite.WALLPAPER_OUT = build
        suite.bmp_preview = lambda bmp, png: (Path(png).write_bytes(b"x"),
                                              {"png": str(png)})[-1]
        # The sheet needs Pillow; this gate must not. Stub it and check the
        # thing that matters here — that the numbers, the caption and the
        # buttons all describe the same wallpapers.
        suite.contact_sheet = lambda files, dest, start=1: (
            Path(dest).parent.mkdir(parents=True, exist_ok=True),
            Path(dest).write_bytes(b"x"),
            {"png": str(dest), "count": len(files), "items": [
                {"n": start + i, "name": Path(f).name} for i, f in enumerate(files)]}
        )[-1]

        tg.sent.clear()
        bot.handle(cb("m:wl"))
        check("an empty collection says so rather than nothing",
              "No wallpapers" in tg.sent[-1]["text"], tg.sent[-1]["text"][:60])

        for name in ("dawn.bmp", "harbour.bmp"):
            (build / name).write_bytes(b"BM" + b"\0" * 100)

        tg.sent.clear()
        bot.handle(cb("m:wl"))
        caption = tg.sent[-1]["text"]
        check("a pushed wallpaper is still listed afterwards",
              "dawn.bmp" in caption, caption)
        check("the overview is one picture, not one per wallpaper",
              len(tg.sent) == 1 and "photo" in tg.sent[-1], str(len(tg.sent)))

        # The caption numbers the wallpapers; the buttons carry those numbers.
        order = [line.split(" ", 1) for line in caption.splitlines()[1:]]
        index = next(n for n, name in order if "dawn.bmp" in name)
        labels = [b[0] for row in (tg.sent[-1]["keyboard"] or []) for b in row]
        check("every listed wallpaper has a numbered button",
              all(n in labels for n, _ in order), f"{labels} vs {order}")
        one = [b[1] for row in (tg.sent[-1]["keyboard"] or []) for b in row
               if b[0] == index][0]
        tg.sent.clear()
        bot.handle(cb(one))
        check("tapping it renders a preview even with no PNG beside it",
              "photo" in tg.sent[-1], str(tg.sent[-1])[:120])

        send = [b[1] for row in (tg.sent[-1]["keyboard"] or []) for b in row
                if "Queue" in b[0]][0]
        bot.handle(cb(send))
        check("it can be queued from here", len(bot.queue) == 1,
              str(bot.queue.items()))
        tg.sent.clear()
        bot.handle(cb(send))
        check("and queueing it twice is refused, not doubled",
              len(bot.queue) == 1 and "Already" in tg.sent[-1]["text"],
              tg.sent[-1]["text"])

        tg.sent.clear()
        bot.handle(cb("m:wl"))
        check("the caption marks what is already queued",
              "📤" in tg.sent[-1]["text"], tg.sent[-1]["text"])

        # Picking several off the sheet, which is the whole point of a sheet:
        # tap tap tap, one confirmation, gone.
        tg.sent.clear()
        bot.handle(cb("m:wl"))
        mid = tg.sent[-1]["message_id"]

        def press(data):
            tg.sent.clear()
            bot.handle({"update_id": 1, "callback_query": {
                "id": "c", "data": data, "from": {"id": OWNER},
                "message": {"message_id": mid, "chat": {"id": OWNER}}}})
            return tg.sent[-1]

        labels = lambda m: [b[0] for r in (m["keyboard"] or []) for b in r]
        check("the sheet offers a way to pick several",
              any("Pick" in x for x in labels(tg.sent[-1])), str(labels(tg.sent[-1])))

        picking = press("wl:pick:")
        check("picking swaps the same numbers for tick boxes, in place",
              all(x.startswith("☐") for x in labels(picking)[:2])
              and picking.get("edited") == mid, str(labels(picking)))

        ticked = press("wl:t:1")
        check("a number ticks", "☑1" in labels(ticked), str(labels(ticked)))
        check("and the delete button counts what is picked",
              any("Delete 1" in x for x in labels(ticked)), str(labels(ticked)))
        check("tapping it again unticks", "☐1" in labels(press("wl:t:1")))

        press("wl:t:1")
        first_name = Path(bot.sheets[mid]["walls"][0]["path"]).name
        confirm = press("wl:del:")
        check("deleting several asks once, and names them",
              "Delete <b>1</b>" in confirm["text"] and first_name in confirm["text"],
              confirm["text"][:120])
        press(f"wl:del!:{mid}")
        check("... and then they are gone", not (build / first_name).exists(),
              str(sorted(p.name for p in build.iterdir())))
        check("the picks are forgotten afterwards", not bot.selected)

        (build / first_name).write_bytes(b"BM")        # put it back for later checks

        # A sheet from before a restart cannot be ticked against.
        bot.sheets.clear()
        stale_reply = press("wl:t:1")
        check("a sheet the bot no longer remembers says so",
              "stale" in stale_reply["text"] or "restart" in stale_reply["text"],
              stale_reply["text"][:80])

        # Renaming takes the preview along with it, or the pair comes apart.
        (build / "harbour.png").write_bytes(b"x")
        tok = bot.tokens.put({"path": str(build / "harbour.bmp"), "bytes": 2,
                              "name": "harbour.bmp", "png": None})
        bot.handle(cb(f"wl:rn:{tok}"))
        bot.handle(msg("sunrise"))
        check("renaming adds .bmp and moves the preview too",
              (build / "sunrise.bmp").exists() and (build / "sunrise.png").exists()
              and not (build / "harbour.bmp").exists(),
              str(sorted(p.name for p in build.iterdir())))

        # Deleting here removes the server copy only — the card keeps its own.
        rm = one.replace("wl:one:", "wl:rm!:")
        bot.handle(cb(rm))
        check("deleting removes the BMP and its preview",
              not (build / "dawn.bmp").exists()
              and not (build / "dawn.png").exists())

        outsider = tmp / "elsewhere.bmp"
        outsider.write_bytes(b"BM")
        token = bot.tokens.put({"path": str(outsider), "bytes": 2,
                                "name": "elsewhere.bmp", "png": None})
        tg.sent.clear()
        bot.handle(cb(f"wl:rm!:{token}"))
        check("and it will not reach outside the workspace",
              outsider.exists() and "outside" in tg.sent[-1]["text"],
              tg.sent[-1]["text"])
    finally:
        suite.WALLPAPER_OUT, suite.bmp_preview = original_out, original_preview


# -- 4c. fonts -------------------------------------------------------------


def check_fonts(tmp: Path) -> None:
    print("\nfonts:")
    families = suite.local_font_families()
    check("the repo's families are found", len(families) >= 2,
          str([f["name"] for f in families]))
    if families:
        by_name = {f["name"]: f for f in families}
        wz = by_name.get("WenZilla")
        check("a shipped family reports 8/10/12 as present",
              bool(wz) and wz["ui_ready"],
              str(wz["sizes"]) if wz else "WenZilla missing")
        check("... and its sizes come from the filenames",
              bool(wz) and wz["sizes"] == [8, 10, 12, 14, 16, 18],
              str(wz["sizes"]) if wz else "")

    sums = suite.checksums()
    check("CHECKSUMS.tsv parses", len(sums) >= 12, f"{len(sums)} rows")

    # The verify is the whole point of pushing a font from a phone, so grade it
    # against a device that reports a short file — the failure the reader
    # itself cannot tell you about.
    family = "WenZilla"
    expected = {rel.split("/", 1)[1]: size for rel, (size, _) in sums.items()
                if rel.startswith(f"{family}/")}
    good = {"families": [{"name": family, "sizes": [8, 10, 12, 14, 16, 18],
                          "files": [{"name": n, "size": s}
                                    for n, s in expected.items()]}]}
    short = json.loads(json.dumps(good))
    short["families"][0]["files"][0]["size"] -= 1

    original = suite.device.fonts
    try:
        suite.device.fonts = lambda host: good
        check("a complete family verifies clean",
              all(r["ok"] for r in suite.verify_font_family("h", family)))

        suite.device.fonts = lambda host: short
        report = suite.verify_font_family("h", family)
        bad = [r for r in report if not r["ok"]]
        check("one byte short is caught", len(bad) == 1, str(bad))
        check("... and it names expected vs actual",
              bad and bad[0]["expected"] == bad[0]["actual"] + 1, str(bad))

        suite.device.fonts = lambda host: {"families": []}
        check("a family the device never received is all failures",
              all(not r["ok"] for r in suite.verify_font_family("h", family)))
    finally:
        suite.device.fonts = original

    # Selecting the family it just sent. The index is looked up by label, never
    # computed from a built-in count, so a firmware that adds a built-in font
    # cannot silently shift the selection onto the wrong family.
    posted = {}
    entry = {"key": "fontFamily", "type": "enum", "value": 0,
             "options": ["Noto Serif", "Noto Sans", "WenKaiFull", "WenZilla"]}
    orig_req = suite.device.request

    def fake_request(host, path, *, method="GET", query=None, body=None, **kw):
        if path == "/api/settings" and method == "GET":
            return 200, json.dumps([entry]).encode()
        if path == "/api/settings" and method == "POST":
            posted.update(json.loads(body))
            entry["value"] = posted["fontFamily"]      # the reader read back
            return 200, b"{}"
        return orig_req(host, path, method=method, query=query, body=body, **kw)

    try:
        suite.device.request = fake_request
        ok, detail = suite.device.select_font_family("h", "WenZilla")
        check("selecting a family posts the label's own index",
              ok and posted.get("fontFamily") == 3, f"{ok} {detail} {posted}")

        entry["options"] = ["Noto Serif", "Noto Sans"]
        entry["value"] = 0
        ok, detail = suite.device.select_font_family("h", "WenZilla")
        check("a family the reader has not scanned is declined, not guessed",
              not ok and "does not list" in detail, detail)
    finally:
        suite.device.request = orig_req

    # Deleting a font family goes through the firmware's own endpoint; a plain
    # folder does not. Recognising which is which is the whole of that branch.
    bot, _ = make_bot(tmp)
    check("/fonts/WenZilla is a family", bot.font_family_of("/fonts/WenZilla") == "WenZilla")
    check("/fonts itself is not", bot.font_family_of("/fonts") is None)
    check("/fonts/X/Y is not", bot.font_family_of("/fonts/X/Y") is None)
    check("/sleep is not", bot.font_family_of("/sleep") is None)

    # What a family covers is read out of the .cpfont, never guessed from the
    # name — it decides whether "no 8/10/12 pt" is a warning or a non-event.
    families = {f["name"]: f for f in suite.local_font_families()}
    if "WenZilla" in families:
        check("a CJK family is recognised from its own interval table",
              families["WenZilla"]["cjk"],
              str(len(suite.cpfont_intervals(families["WenZilla"]["files"][0]))))
    if "NaskhFull" in families:
        check("... and a non-CJK one is not", not families["NaskhFull"]["cjk"])
    check("a file that is not a font reads as no intervals at all",
          suite.cpfont_intervals(FONTS := (tmp / "notafont")) == []
          and not FONTS.exists())


# -- 4d. a font family through the queue -----------------------------------


def check_font_queue(tmp: Path) -> None:
    """Fonts queue like everything else bound for the device.

    A family is one queue entry, not six: half a family on the card is a font
    that lists in the picker and reverts to Noto, so "landed" has to mean all
    of it or none. And the entry names the family rather than freezing its
    file list, so a rebuild between queueing and pushing sends the new bytes.
    """
    print("\na font family through the queue:")
    bot, tg = make_bot(tmp)

    fonts_dir = tmp / "fontrepo"
    (fonts_dir / "Fake").mkdir(parents=True)
    for size in (12, 14):
        (fonts_dir / "Fake" / f"Fake_{size}.cpfont").write_bytes(b"CPFONT\x00\x00")

    original_dir = suite.FONTS_DIR
    orig_upload = suite.device.upload_font
    orig_verify = suite.verify_font_family
    orig_select = suite.device.select_font_family
    orig_push = suite.push
    try:
        suite.FONTS_DIR = fonts_dir
        family = suite.local_font_families()[0]
        token = bot.tokens.put(family)

        bot.handle(cb(f"fo:q:{token}"))
        items = bot.queue.items()
        check("a family is one queue entry, not one per file", len(items) == 1,
              str(items))
        check("... filed as a font, under the family's name",
              items[0]["kind"] == "font" and items[0]["label"] == "Fake",
              str(items[0]))
        check("... and it survives a restart",
              [i["label"] for i in Queue(bot.queue.path).items()] == ["Fake"])

        tg.sent.clear()
        bot.handle(cb(f"fo:q:{token}"))
        check("queueing the same family twice is refused, not doubled",
              len(bot.queue) == 1 and "Already" in tg.sent[-1]["text"],
              tg.sent[-1]["text"])

        # A third file appears after queueing: the push must send what the
        # family is now, not what it was when the button was tapped.
        (fonts_dir / "Fake" / "Fake_16.cpfont").write_bytes(b"CPFONT\x00\x00")

        uploaded, selected, pushed = [], [], []
        suite.device.upload_font = lambda host, name, path: \
            uploaded.append((name, Path(path).name))
        suite.verify_font_family = lambda host, name: \
            [{"name": n, "expected": 8, "actual": 8, "ok": True}
             for _, n in uploaded]
        suite.device.select_font_family = lambda host, name: \
            (selected.append(name), (True, ""))[1]
        suite.push = lambda files, host=None: (pushed.extend(files),
                                               {"ok": True, "items": []})[1]
        bot.device_host = lambda: ("10.0.0.5", {})

        bot.do_push(bot.user_id)
        check("the push sends every file in the family as it stands now",
              [n for _, n in uploaded] == ["Fake_12.cpfont", "Fake_14.cpfont",
                                           "Fake_16.cpfont"], str(uploaded))
        check("... through the font endpoint, never as a wallpaper",
              not pushed, str(pushed))
        check("... verifies and selects it, exactly like sending it by hand",
              selected == ["Fake"], str(selected))
        check("... and only then does it leave the queue", len(bot.queue) == 0,
              str(bot.queue.items()))

        # Half a family is the failure this whole feature exists to catch.
        print("\na font family the device only half received:")
        bot, tg = make_bot(tmp)
        bot.device_host = lambda: ("10.0.0.5", {})
        family = suite.local_font_families()[0]
        bot.handle(cb(f"fo:q:{bot.tokens.put(family)}"))
        suite.verify_font_family = lambda host, name: [
            {"name": "Fake_12.cpfont", "expected": 8, "actual": 8, "ok": True},
            {"name": "Fake_14.cpfont", "expected": 8, "actual": 3, "ok": False}]
        selected.clear()
        bot.do_push(bot.user_id)
        check("a short file keeps the family in the queue",
              len(bot.queue) == 1, str(bot.queue.items()))
        check("... is named in the report",
              any("Fake_14.cpfont" in s["text"] for s in tg.sent),
              tg.sent[-1]["text"] if tg.sent else "")
        check("... and the family is not selected on the reader",
              not selected, str(selected))

        # A family deleted from the repo between queueing and pushing.
        print("\na queued family that is no longer in the repo:")
        bot, tg = make_bot(tmp)
        bot.device_host = lambda: ("10.0.0.5", {})
        bot.queue.add("font", str(fonts_dir / "Gone"), label="Gone",
                      meta={"family": "Gone"})
        bot.do_push(bot.user_id)
        check("is reported, not pushed as a wallpaper",
              any("Gone" in s["text"] for s in tg.sent), str(tg.sent[-1]))
    finally:
        suite.FONTS_DIR = original_dir
        suite.device.upload_font = orig_upload
        suite.verify_font_family = orig_verify
        suite.device.select_font_family = orig_select
        suite.push = orig_push


# -- 4d2. files and folders on the card ------------------------------------


def check_device_files(tmp: Path) -> None:
    """Making a folder, and putting things in it.

    The half of Browse that had no buttons for a long time: you could rename
    and delete one file at a time, and that was all. "These three books go in
    that folder" is the gesture worth having, so it is a listing that can be
    ticked, a destination you walk to, and a move that reports per file.
    """
    print("\nfiles and folders on the card:")
    fs = {"/": [{"name": "Books", "isDirectory": True},
                {"name": "a.epub", "isDirectory": False, "size": 10, "isEpub": True},
                {"name": "b.epub", "isDirectory": False, "size": 20, "isEpub": True}],
          "/Books": []}
    made, moved, deleted = [], [], []

    orig = (suite.device.list_dir, suite.device.move, suite.device.mkdir,
            suite.device.delete_many)
    try:
        suite.device.list_dir = lambda h, p: fs.get(p, [])
        suite.device.move = lambda h, path, dest: moved.append((path, dest))
        suite.device.mkdir = lambda h, parent, name: made.append((parent, name))
        suite.device.delete_many = lambda h, paths: (deleted.extend(paths), (True, ""))[1]

        bot, tg = make_bot(tmp)
        bot.device_host = lambda: ("10.0.0.5", {})
        bot.handle(cb("dev:ls0:"))
        listing = tg.sent[-1]
        flat = [b for row in listing["keyboard"] for b in row]
        check("a folder in the listing carries its own delete",
              any(t == "🗑" and d.startswith("dev:dirrm") for t, d in flat),
              str(flat))
        check("... and the listing offers a new folder and a multi-pick",
              any(d.startswith("dev:mkdir") for _, d in flat)
              and any(d.startswith("dev:pick") for _, d in flat), str(flat))

        # New folder, and a name that is really a path.
        bot.handle(cb(next(d for _, d in flat if d.startswith("dev:mkdir"))))
        tg.sent.clear()
        bot.handle(msg("Sci/Fi"))
        check("a folder name with a slash is refused",
              not made and "no slashes" in tg.sent[-1]["text"], tg.sent[-1]["text"])
        bot.handle(cb(next(d for _, d in flat if d.startswith("dev:mkdir"))))
        bot.handle(msg("Poetry"))
        check("a plain name creates the folder where you are",
              made == [("/", "Poetry")], str(made))

        # Tick two files and move them into /Books.
        tg.sent.clear()
        bot.handle(cb(next(d for _, d in flat if d.startswith("dev:pick"))))
        picking = [b for row in tg.sent[-1]["keyboard"] for b in row]
        check("picking turns every file into a tick box, folders aside",
              sum(1 for t, _ in picking if t.startswith("☐")) == 2, str(picking))
        for _ in range(2):          # tick whichever is still empty, twice
            buttons = [b for row in (tg.sent[-1]["keyboard"] or []) for b in row]
            bot.handle(cb(next(d for t, d in buttons if t.startswith("☐"))))
        ticked = [b for row in tg.sent[-1]["keyboard"] for b in row]
        check("... and the count follows what is ticked",
              any("Move 2" in t for t, _ in ticked), str(ticked))

        tg.sent.clear()
        bot.handle(cb("dev:mvpick:"))
        check("the destination is walked to, not typed",
              any(d.startswith("dev:mvto:") for row in tg.sent[-1]["keyboard"]
                  for _, d in row), str(tg.sent[-1]["keyboard"]))
        check("... and moving books warns about the reading position",
              "loses its place" in tg.sent[-1]["text"], tg.sent[-1]["text"])
        into = next(d for row in tg.sent[-1]["keyboard"] for _, d in row
                    if d.startswith("dev:mvto:"))
        tg.sent.clear()
        bot.handle(cb(into))            # walk into /Books, then move there
        go = next(d for row in tg.sent[-1]["keyboard"] for _, d in row
                  if d.startswith("dev:mvgo:"))
        tg.sent.clear()
        bot.handle(cb(go))
        check("moving sends one call per file, to the folder you stopped on",
              sorted(moved) == [("/a.epub", "/Books"), ("/b.epub", "/Books")],
              str(moved))
        check("... and nothing stays ticked afterwards", not bot.picked,
              str(bot.picked))

        # A file already in the destination is not moved onto itself.
        moved.clear()
        bot.moving = {"paths": ["/Books/old.epub"], "back": "/Books"}
        bot.do_move(bot.user_id, "/Books")
        check("a file already in the destination is left alone", not moved,
              str(moved))

        # Deleting several asks first, then deletes exactly those.
        bot, tg = make_bot(tmp)
        bot.device_host = lambda: ("10.0.0.5", {})
        bot.handle(cb("dev:ls0:"))
        flat = [b for row in tg.sent[-1]["keyboard"] for b in row]
        bot.handle(cb(next(d for _, d in flat if d.startswith("dev:pick"))))
        bot.handle(cb("dev:pall:"))
        tg.sent.clear()
        bot.handle(cb("dev:rmpick:"))
        check("deleting several asks first, naming them",
              not deleted and "a.epub" in tg.sent[-1]["text"], tg.sent[-1]["text"])
        bot.handle(cb(next(d for row in tg.sent[-1]["keyboard"] for _, d in row
                           if d.startswith("dev:rmpick!"))))
        check("... and then deletes exactly what was ticked",
              sorted(deleted) == ["/a.epub", "/b.epub"], str(deleted))

        # A listing from before a restart cannot be ticked against.
        bot.listings.clear()
        tg.sent.clear()
        bot.handle(cb("dev:pall:"))
        check("a listing the bot no longer remembers is answered, not guessed",
              "before a restart" in tg.sent[-1]["text"], tg.sent[-1]["text"])
    finally:
        (suite.device.list_dir, suite.device.move, suite.device.mkdir,
         suite.device.delete_many) = orig


# -- 4e. the families already on the reader --------------------------------


def check_device_fonts(tmp: Path) -> None:
    """Select, rename and delete a family that is already on the card.

    All three are name-based on the device and none of them is symmetrical
    with the others: selection is a settings enum resolved by label, deletion
    is the firmware's own endpoint behind a stricter name rule than anything
    that creates a folder, and a rename is a *folder* rename that only WebDAV
    will do, only outside `/.fonts`, and which leaves the reader's own font
    list stale until something makes it scan again.
    """
    print("\nfont families on the reader:")

    def device(state):
        """A fake reader whose fonts folder is whatever `state` says."""
        def fonts(host):
            return {"families": [
                {"name": n, "sizes": [12, 14],
                 "files": [{"name": f"{n}_12.cpfont", "size": state["sizes"].get(n, 10)}]}
                for n in state["families"]]}

        def settings(host):
            return {"fontFamily": {"key": "fontFamily", "type": "enum",
                                   "value": state["value"],
                                   "options": state["options"]}}

        def list_dir(host, path):
            if path == "/":
                return [{"name": d, "isDirectory": True} for d in state["root"]]
            if path == "/fonts":
                return [{"name": d, "isDirectory": True} for d in state["visible"]]
            return []

        def delete_family(host, name):
            state["deleted"].append(name)

        suite.device.fonts = fonts
        suite.device.settings = settings
        suite.device.list_dir = list_dir
        suite.device.delete_font_family = delete_family
        suite.device.dav_move = lambda h, src, dst: state["moved"].append((src, dst))
        suite.verify_font_family = lambda h, n: \
            [{"name": "x", "expected": 1, "actual": 1, "ok": True}]

        def select(host, name):
            if name not in state["options"]:
                return False, f"the reader does not list {name} yet"
            state["value"] = state["options"].index(name)
            return True, name
        suite.device.select_font_family = select

    def buttons(sent):
        return [b[1] for row in (sent["keyboard"] or []) for b in row]

    def open_family(bot, tg, listing, label):
        for row in listing["keyboard"]:
            for text, data in row:
                if label in text:
                    tg.sent.clear()
                    bot.handle(cb(data))
                    return tg.sent[-1]
        raise AssertionError(f"no button for {label}")

    orig = (suite.device.fonts, suite.device.settings, suite.device.list_dir,
            suite.device.dav_move, suite.device.delete_font_family,
            suite.device.select_font_family, suite.verify_font_family)
    try:
        # A reader with a visible /fonts, one family in it, and one the
        # registry remembers from a scan that is no longer true.
        state = {"families": ["WenZilla", "Ghost"], "visible": ["WenZilla"],
                 "root": ["fonts", "Books"], "sizes": {"Ghost": 0},
                 "value": 3, "moved": [], "deleted": [],
                 "options": ["Noto Serif", "Noto Sans", "WenKaiFull", "WenZilla"]}
        device(state)
        bot, tg = make_bot(tmp)
        bot.device_host = lambda: ("10.0.0.5", {})

        tg.sent.clear()
        bot.handle(cb("dev:fo:"))
        listing = tg.sent[-1]
        check("the reader's families are listed", "WenZilla" in listing["text"])
        check("... with the selected one marked", "✅" in listing["text"])
        check("a family whose files all report 0 B is called stale, not empty",
              "stale" in str(listing["keyboard"]) and "last scanned" in listing["text"],
              listing["text"])
        check("... and a re-scan is offered",
              "dev:foscan:" in str(listing["keyboard"]), str(listing["keyboard"]))

        card = open_family(bot, tg, listing, "WenZilla")
        check("a family with a folder in /fonts can be renamed",
              any(b.startswith("dfo:rn") for b in buttons(card)), str(buttons(card)))

        ghost = open_family(bot, tg, listing, "Ghost")
        check("a family with no folder is not offered a rename",
              not any(b.startswith("dfo:rn") for b in buttons(ghost)), str(buttons(ghost)))
        check("... and is not blamed on /.fonts, which we cannot know",
              "/.fonts" not in ghost["text"], ghost["text"])
        check("... it says the list is stale and offers the re-scan",
              "not caught up" in ghost["text"]
              and "dev:foscan:" in str(buttons(ghost)), ghost["text"])

        # Deleting the stale one: the firmware would answer OK and remove
        # nothing, so the bot must not report a deletion.
        tg.sent.clear()
        bot.handle(cb(next(b for b in buttons(ghost) if b.startswith("dfo:rm"))))
        bot.handle(cb(next(b for b in buttons(tg.sent[-1]) if b.startswith("dfo:rm!"))))
        check("deleting a stale family removes nothing and says so",
              "Ghost" not in [d for d in state["deleted"]
                              if not d.startswith("cprescan")]
              and any("Nothing to delete" in s["text"] for s in tg.sent),
              str(state["deleted"]))
        check("... and makes the reader re-scan instead",
              any(d.startswith("cprescan") for d in state["deleted"]),
              str(state["deleted"]))

        # The re-scan itself: a delete aimed at a name that cannot exist.
        state["deleted"].clear()
        bot.handle(cb("dev:foscan:"))
        check("the re-scan probe is a valid family name that cannot match one",
              len(state["deleted"]) == 1
              and state["deleted"][0].startswith("cprescan")
              and suite.family_name_problem(state["deleted"][0]) is None,
              str(state["deleted"]))

        # Names: the firmware's rule, not a looser one of ours.
        for bad in ("Noto Naskh", "Naskh.v2", "Ärabe", "a/b", "_old"):
            check(f"{bad!r} is refused as a family name",
                  suite.family_name_problem(bad) is not None)
        check("a plain name is accepted",
              suite.family_name_problem("Naskh-Full_2") is None)

        card = open_family(bot, tg, listing, "WenZilla")
        rename = next(b for b in buttons(card) if b.startswith("dfo:rn"))
        bot.handle(cb(rename))
        tg.sent.clear()
        bot.handle(msg("Noto Naskh"))
        check("a name the reader could not delete is refused before the move",
              not state["moved"] and "only accepts" in tg.sent[-1]["text"],
              tg.sent[-1]["text"])

        state["deleted"].clear()
        bot.handle(cb(rename))
        tg.sent.clear()
        state["options"] = state["options"] + ["Zilla"]
        bot.handle(msg("Zilla"))
        check("a good name renames the folder, and only the folder",
              state["moved"] == [("/fonts/WenZilla", "/fonts/Zilla")],
              str(state["moved"]))
        check("... then makes the reader re-scan, so its list is not left stale",
              any(d.startswith("cprescan") for d in state["deleted"]),
              str(state["deleted"]))
        check("... and re-selects it, since it was the family being read with",
              state["value"] == state["options"].index("Zilla")
              and "reads with" in tg.sent[-1]["text"], tg.sent[-1]["text"])

        # A family whose folder holds characters the delete endpoint rejects.
        state.update(families=["Noto Naskh"], visible=["Noto Naskh"],
                     sizes={}, value=0)
        tg.sent.clear()
        bot.handle(cb("dev:fo:"))
        card = open_family(bot, tg, tg.sent[-1], "Noto Naskh")
        tg.sent.clear()
        bot.handle(cb(next(b for b in buttons(card) if b.startswith("dfo:rm"))))
        check("a family the firmware cannot name is not offered a doomed delete",
              "refuse to delete" in tg.sent[-1]["text"]
              and not any(b.startswith("dfo:rm!") for b in buttons(tg.sent[-1])),
              tg.sent[-1]["text"])
        check("... and rename is offered as the way out",
              any(b.startswith("dfo:rn") for b in buttons(tg.sent[-1])),
              str(buttons(tg.sent[-1])))

        # A reader with no visible /fonts at all: everything is in /.fonts.
        state.update(families=["WenZilla"], visible=[], root=["Books"],
                     sizes={}, value=0)
        tg.sent.clear()
        bot.handle(cb("dev:fo:"))
        card = open_family(bot, tg, tg.sent[-1], "WenZilla")
        check("with no /fonts on the card, the hidden root is named as the reason",
              "/.fonts" in card["text"]
              and not any(b.startswith("dfo:rn") for b in buttons(card)),
              card["text"])
    finally:
        (suite.device.fonts, suite.device.settings, suite.device.list_dir,
         suite.device.dav_move, suite.device.delete_font_family,
         suite.device.select_font_family, suite.verify_font_family) = orig


# -- 5a. a message must never simply vanish --------------------------------


def check_never_silent(tmp: Path) -> None:
    """The worst failure this bot has is saying nothing.

    A delete confirmation that never arrives is indistinguishable from a delete
    button that ignored you — which is exactly how it was reported. Formatting
    rejections and rate limits both used to end that way.
    """
    print("\nnothing said is worse than something ugly:")
    from telegram import TelegramError

    bot, tg = make_bot(tmp)
    real_send = tg.send_message

    calls = []

    def picky(chat_id, text, keyboard=None, **kw):
        calls.append(kw.get("parse_mode", "HTML"))
        if kw.get("parse_mode", "HTML") == "HTML":
            raise TelegramError("sendMessage: HTTP 400 can't parse entities")
        return real_send(chat_id, text, keyboard, **kw)

    tg.send_message = picky
    bot.say(bot.user_id, "Delete <code>x.bmp</code> from the reader?")
    check("a formatting rejection costs the formatting, not the message",
          tg.sent and "x.bmp" in tg.sent[-1]["text"], str(tg.sent))
    check("... and the retry drops the markup", "<code>" not in tg.sent[-1]["text"],
          tg.sent[-1]["text"] if tg.sent else "")

    tg.send_message = lambda *a, **k: (_ for _ in ()).throw(
        TelegramError("sendMessage: HTTP 429 Too Many Requests"))
    check("a send that cannot be made returns None rather than raising",
          bot.say(bot.user_id, "anything") is None)

    # And a button the router does not know must say so, not do nothing.
    bot, tg = make_bot(tmp)
    token = bot.tokens.put({"parent": "/sleep", "name": "x.bmp",
                            "path": "/sleep/x.bmp", "size": 1})
    tg.sent.clear()
    bot.handle(cb(f"dev:nonsense:{token}"))
    check("an unknown device button reports itself",
          tg.sent and "did nothing" in tg.sent[-1]["text"], str(tg.sent))

    # The delete path from a preview, which is where this was noticed.
    tg.sent.clear()
    bot.handle(cb(f"dev:rm:{token}"))
    check("delete asks for confirmation",
          tg.sent and "Delete" in tg.sent[-1]["text"], str(tg.sent))
    yes = [b[1] for row in (tg.sent[-1]["keyboard"] or []) for b in row
           if "Yes" in b[0]]
    check("... and the confirm button carries the same file", bool(yes), str(tg.sent[-1]))


# -- 5b. the real entry point actually starts ------------------------------


def check_entry_point() -> None:
    """Start bot.py the way systemd does, in a subprocess.

    Importing the modules from *this* file is not the same thing, and the
    difference has bitten once already: `suite` used to put a sibling unit's
    `scripts/` on sys.path, where its `config.py` shadowed the bot's own. Which
    one won depended on import order — and this gate's order was the lucky one,
    so it passed green while `python3 tgbot/scripts/bot.py` died on an
    ImportError before reading a line of config.

    So the check is not "can I import it" but "does it run". A missing config
    is the expected, healthy outcome: it means every module loaded and the bot
    got as far as looking for its user id.
    """
    print("\nthe entry point, started the way systemd starts it:")
    import subprocess

    bot_py = Path(__file__).resolve().parent / "bot.py"
    for label, args in (("--help", ["--help"]),
                        ("with a config that isn't there",
                         ["--config", "/nonexistent/tgbot.json"])):
        p = subprocess.run([sys.executable, str(bot_py), *args],
                           capture_output=True, text=True, timeout=60)
        output = p.stdout + p.stderr
        check(f"{label}: no import error",
              "ImportError" not in output and "Traceback" not in output,
              output.strip()[-300:])
    check("... and it complains about the user id, having loaded everything",
          "user id" in subprocess.run(
              [sys.executable, str(bot_py), "--config", "/nonexistent/tgbot.json"],
              capture_output=True, text=True, timeout=60).stderr)


# -- 6. it does not reach into the rest of the repo ------------------------


def check_optional() -> None:
    print("\noptional by construction:")
    hits = []
    for path in Path(suite.REPO_ROOT).rglob("*.py"):
        if "tgbot" in path.parts or ".venv" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "import tgbot" in text or "from tgbot" in text:
            hits.append(str(path))
    check("nothing outside tgbot/ imports it", not hits, str(hits))


# -- 6b. the device file menu ---------------------------------------------


def check_device_menu(tmp: Path) -> None:
    """Menu construction only — no call here reaches the network."""
    print("\nthe device file menu:")
    bot, tg = make_bot(tmp)

    def menu_for(name):
        token = bot.tokens.put({"parent": "/sleep", "name": name,
                                "path": f"/sleep/{name}", "size": 1234})
        tg.sent.clear()
        bot.handle(cb(f"dev:f:{token}"))
        return [b[0] for row in (tg.sent[-1]["keyboard"] or []) for b in row]

    check("a wallpaper offers a preview", "👁 Preview" in menu_for("x.bmp"))
    check("... case-insensitively, as the firmware scans",
          "👁 Preview" in menu_for("SHOT.BMP"))
    check("a book does not", "👁 Preview" not in menu_for("book.epub"))

    # Renaming a book on the device costs your reading position — the firmware
    # keys it by path and the web handlers never repoint it. Say so first.
    bot.device_host = lambda: ("10.0.0.5", {})
    for name, expected in (("book.epub", True), ("shot.bmp", False)):
        token = bot.tokens.put({"parent": "/", "name": name,
                                "path": f"/{name}", "size": 1})
        tg.sent.clear()
        bot.handle(cb(f"dev:rn:{token}"))
        said = "forget your place" in tg.sent[-1]["text"]
        check(f"renaming {name} warns about reading position: {expected}",
              said is expected, tg.sent[-1]["text"][:120])
    bot.pending = None      # asking for a name leaves one waiting; this is a test

    # A folder can only be renamed through WebDAV, and WebDAV refuses every
    # path containing a dot-prefixed segment — so /.sleep must be turned down
    # before anything tries, not after a confusing 403.
    tg.sent.clear()
    bot.handle(cb(f"dev:dirrn:{bot.tokens.put('/.sleep')}"))
    check("renaming /.sleep is refused with a reason",
          "can't be renamed" in tg.sent[-1]["text"], tg.sent[-1]["text"][:80])
    check("... and nothing was queued behind it", bot.pending is None)


# -- 7. secrets that live outside the checkout -----------------------------


def check_secrets_outside(tmp: Path) -> None:
    """The point of secrets_dir is that a coding agent pointed at this repo
    finds nothing. So the tests are about what is *not* in the repo."""
    print("\nkeeping the token and the id out of the checkout:")
    import config as conf

    home = tmp / "elsewhere"          # stands in for somewhere outside the repo
    home.mkdir(parents=True, exist_ok=True)
    (home / "telegram.token").write_text("111:AAsecret\n")
    (home / "telegram.user_id").write_text("777\n")

    repo_cfg = tmp / "cfgdir" / "config.json"
    repo_cfg.parent.mkdir(parents=True, exist_ok=True)
    repo_cfg.write_text(json.dumps({"secrets_dir": str(home)}))

    cfg = conf.load(repo_cfg)
    check("the id is read from outside the repo",
          cfg["telegram"]["user_id"] == 777, str(cfg["telegram"]["user_id"]))
    check("the token is read from outside the repo",
          conf.resolve_token(cfg) == "111:AAsecret")
    written = repo_cfg.read_text()
    check("neither value appears in the config that stays in the repo",
          "111:AAsecret" not in written and "777" not in written, written)

    # The outside file is authoritative: a stale id left in the config must not
    # quietly win, or moving the secret out would change nothing.
    repo_cfg.write_text(json.dumps({"secrets_dir": str(home),
                                    "telegram": {"user_id": 111}}))
    check("the outside file beats a leftover id in the config",
          conf.load(repo_cfg)["telegram"]["user_id"] == 777)

    # A config moved wholesale out of the repo must resolve its own relative
    # paths, not paths relative to this source file.
    away = tmp / "away"
    (away / "secrets").mkdir(parents=True, exist_ok=True)
    (away / "secrets" / "telegram.token").write_text("222:BBtoken")
    (away / "config.json").write_text(json.dumps({"telegram": {"user_id": 5}}))
    cfg = conf.load(away / "config.json")
    check("a relocated config resolves paths against itself",
          conf.resolve_token(cfg) == "222:BBtoken")

    # And the whole point, stated as a check: secrets inside the repo are
    # called out rather than silently accepted.
    inside_cfg = tmp / "inside.json"
    inside_cfg.write_text(json.dumps({"secrets_dir": str(conf.REPO_ROOT / "tgbot"),
                                      "telegram": {"user_id": 9}}))
    grumbles = conf.warnings(conf.load(inside_cfg))
    check("a secrets_dir inside the repo is called out",
          any("inside the repo" in g for g in grumbles), str(grumbles))

    (home / "telegram.user_id").write_text("not-a-number")
    try:
        conf.load(repo_cfg)
        check("a non-numeric id is refused, not truncated", False, "accepted")
    except ConfigError as exc:
        check("a non-numeric id is refused, not truncated",
              "not a number" in str(exc))


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        check_auth(tmp)
        check_paths(tmp)
        check_queue(tmp)
        check_push_is_all_or_nothing(tmp)
        check_book_delivery(tmp)
        check_wallpaper_collection(tmp)
        check_fonts(tmp)
        check_font_queue(tmp)
        check_device_files(tmp)
        check_device_fonts(tmp)
        check_tokens(tmp)
        check_device_menu(tmp)
        check_never_silent(tmp)
        check_entry_point()
        check_secrets_outside(tmp)
        check_optional()

    print()
    if failures:
        print(f"{len(failures)} check(s) failed:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
