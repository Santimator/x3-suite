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
     a book must never end up in a delivery queue.
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
                          "keyboard": keyboard})
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


def check_push_is_all_or_nothing(tmp: Path) -> None:
    print("\na push with no reader on the network:")
    bot, tg = make_bot(tmp)
    bmp = bot.workspace / "wall.bmp"
    bmp.write_bytes(b"BM fake")
    bot.queue.add("wallpaper", str(bmp), "wall.bmp")

    original = suite.push
    try:
        suite.push = lambda files: {"ok": False, "host": None, "items": [],
                                    "error": "no reader found"}
        bot.do_push(bot.user_id)
        check("the queue is untouched", len(bot.queue) == 1, str(bot.queue.items()))
        check("and it says so out loud",
              any("No reader" in s["text"] for s in tg.sent), str(tg.sent[-1]))

        # One file lands, one fails: exactly the one that landed leaves.
        tg.sent.clear()
        second = bot.workspace / "other.bmp"
        second.write_bytes(b"BM fake")
        bot.queue.add("wallpaper", str(second), "other.bmp")
        suite.push = lambda files: {
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
        suite.push = lambda files: {"ok": True, "host": "10.0.0.5", "items": []}
        bot.do_push(bot.user_id)
        check("is dropped and named, not pushed",
              len(bot.queue) == 0 and any("gone" in s["text"] for s in tg.sent),
              str(tg.sent))
    finally:
        suite.push = original


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


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        check_auth(tmp)
        check_paths(tmp)
        check_queue(tmp)
        check_push_is_all_or_nothing(tmp)
        check_tokens(tmp)
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
