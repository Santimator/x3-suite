#!/usr/bin/env python3
"""A steward for the suite, operated from a phone.

    python3 tools/tgbot/scripts/bot.py

Everything this bot does, a script in this repo already did. It adds buttons, a
queue, and someone to tell you when a job finished — not a single new way of
making an EPUB or a wallpaper. That is the whole design: if the bot is deleted
tomorrow, nothing else in the repo notices.

**Two scopes, kept apart.**

*Manipulate* is always available and never touches the reader: build a
wallpaper, stage a PDF, put a book where the catalog will find it, rename and
delete inside `workspace/`. Work that is bound for the device is *queued*, not
sent.

*Push* happens only when you ask for it, and only while the X3 is sitting on
**Home -> File Transfer -> Join a Network**. If the reader cannot be found, the
queue is left exactly as it was — a push that half-happens in silence is the
one failure that would make the queue useless.

Books enter the delivery queue only when you explicitly ask for a card copy.
They are named exactly as the OPDS client would name them, so push and pull
converge on one file rather than producing duplicates.

**Who may talk to it.** One Telegram user id, checked before any handler runs,
on messages and on button presses alike — the buttons are where a bot that
only guarded messages would be wide open. Everything else is dropped without a
reply and logged here, so a mistyped id looks different from a dead bot.
"""

from __future__ import annotations

import argparse
import html
import queue as queuelib
import re
import sys
import threading
import time
import traceback
import unicodedata
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import suite
from config import ConfigError, inside, load, resolve_token, safe_join
from config import warnings as config_warnings
from state import Notes, Queue, Tokens
from telegram import GETFILE_LIMIT, Telegram, TelegramError

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
MATS = [("≈ waves", "waves"), ("▣ edges", "edges"),
        ("◌ blur", "blur"), ("— white", "none")]
NAV_PAGE = 7
ALPHABET_THRESHOLD = 7


def log(*parts) -> None:
    print(f"[{datetime.now():%H:%M:%S}]", *parts, flush=True)


def slugify(name: str) -> str:
    stem = Path(name).stem.lower()
    stem = re.sub(r"[^a-z0-9]+", "-", stem).strip("-")
    return stem or "untitled"


def human(n: float) -> str:
    if n < 1024:
        return f"{n:.0f} B"
    if n < 1024 ** 2:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 ** 2:.1f} MB"


def counted(n: int, singular: str, plural: str = "") -> str:
    return f"{n} {singular if n == 1 else (plural or singular + 's')}"


class Bot:
    """The handlers. Deliberately constructible with a fake Telegram and a
    temporary workspace, which is how `selftest.py` exercises the whitelist,
    the path jail and the queue without a token or a device."""

    def __init__(self, cfg: dict, tg, *, sync: bool = False):
        self.cfg = cfg
        self.tg = tg
        self.sync = sync
        self.user_id = int(cfg["telegram"]["user_id"])
        self.workspace = Path(cfg["workspace_path"])
        self.state_dir = Path(cfg["state_path"])
        self.queue = Queue(self.state_dir / "queue.json")
        self.notes = Notes(self.state_dir / "notes.json")
        self.tokens = Tokens()
        self.pending = None           # what the next plain text message means
        # Uploads that need human series aliases are grouped by the exact
        # embedded series name.  Only ``active_alias_group`` owns the next
        # plain-text reply; later series wait silently behind it instead of
        # each upload overwriting ``pending``.
        self.alias_groups = {}
        self.active_alias_group = None
        # A contact sheet stays on screen while you tick things off it, so the
        # bot has to remember what each sheet message is showing. Keyed by
        # message id, in memory: a restart makes the buttons stale, which is
        # answered rather than guessed at.
        self.sheets = {}
        self.selected = set()
        # The same trick for a folder listing on the device: what a message is
        # showing, and which of its files are ticked. Separate from the
        # wallpaper set above — one is paths on this server, the other paths on
        # the card, and mixing them would delete the wrong thing.
        self.listings = {}
        self.picked = set()
        self.moving = None            # files waiting for a destination folder
        self.max_bytes = int(cfg.get("max_download_mb", 20)) * 1024 * 1024
        self._jobs = queuelib.Queue()
        self._worker = None

    # -- plumbing ----------------------------------------------------------

    def start_worker(self) -> None:
        if self.sync or self._worker:
            return
        self._worker = threading.Thread(target=self._run_jobs, daemon=True)
        self._worker.start()

    def _run_jobs(self) -> None:
        while True:
            fn, chat = self._jobs.get()
            try:
                fn()
            except Exception as exc:                      # never kill the worker
                log("job failed:", traceback.format_exc().strip().splitlines()[-1])
                self._blame(chat, exc)
            finally:
                self._jobs.task_done()

    def submit(self, chat, fn) -> None:
        """Long work goes to the worker so polling never stops. In the
        self-test everything runs inline, which keeps the assertions honest."""
        if self.sync:
            try:
                fn()
            except Exception as exc:
                self._blame(chat, exc)
        else:
            self._jobs.put((fn, chat))

    def _blame(self, chat, exc: Exception) -> None:
        """Every unhandled failure ends here, and one of them is not a bug.

        A reader that is not on its File Transfer screen is the single most
        common thing to go wrong, and it reaches this point from browsing,
        previewing, renaming and pushing alike. Answering all of them with the
        same offer to name the address costs one branch and saves repeating it
        at a dozen call sites.
        """
        if isinstance(exc, suite.DeviceError):
            return self.no_reader(chat, str(exc))
        self.say(chat, f"⚠️ {exc}")

    def say(self, chat, text: str, keyboard=None):
        """Say it, and if that fails say it plainly rather than not at all.

        A swallowed send is the worst failure this bot has, because it is
        indistinguishable from a button that did nothing: the delete
        confirmation that never arrives looks like a delete that ignored you.
        So a formatting rejection costs the formatting, not the message, and
        anything worse is logged loudly enough to find in the journal.
        """
        try:
            return self.tg.send_message(chat, text, keyboard)
        except TelegramError as exc:
            log("send failed:", exc, "— retrying without formatting")
        try:
            plain = html.unescape(re.sub(r"<[^>]+>", "", text))
            return self.tg.send_message(chat, plain, keyboard, parse_mode=None)
        except TelegramError as exc:
            log("SEND LOST:", exc, "|", text[:120].replace("\n", " "))
            return None

    def panel(self, chat, message_id, text: str, keyboard=None):
        """Render a text navigator in place when it came from a button.

        Telegram inline keyboards are a small UI, not a stream of status
        messages.  Library, metadata and device navigation therefore edit the
        message whose button was pressed.  A command or a free-standing result
        has no message id and starts a new panel; an old/media message that
        Telegram refuses to edit falls back to the same safe send path.
        """
        if message_id:
            try:
                self.tg.edit_message(chat, message_id, text, keyboard)
                return {"message_id": message_id}
            except TelegramError as exc:
                log("panel edit failed:", exc, "— sending a fresh panel")
        return self.say(chat, text, keyboard)

    @staticmethod
    def initial_bucket(label: str) -> str:
        """A compact A-Z navigator, with accents folded and everything else #."""
        folded = unicodedata.normalize("NFKD", label or "")
        first = next((char.upper() for char in folded
                      if not unicodedata.combining(char) and char.isalnum()), "#")
        return first if "A" <= first <= "Z" else "#"

    def alphabet_rows(self, labels: list[str], selected: str,
                      callback) -> list:
        """Only show buckets which contain something; six fit comfortably."""
        buckets = sorted({self.initial_bucket(label) for label in labels},
                         key=lambda value: (value == "#", value))
        rows = []
        for start in range(0, len(buckets), 6):
            rows.append([
                (("✓ " if bucket == selected else "") + bucket,
                 callback("" if bucket == selected else bucket))
                for bucket in buckets[start:start + 6]
            ])
        return rows

    # -- the gate ----------------------------------------------------------

    def handle(self, update: dict) -> bool:
        """Every update enters here, and the whitelist is the first thing that
        runs. Returns True if it was ours and was dispatched."""
        message = update.get("message") or update.get("edited_message")
        callback = update.get("callback_query")
        source = callback or message
        if not source:
            return False

        sender = (source.get("from") or {}).get("id")
        chat = ((callback or {}).get("message", {}).get("chat")
                or (message or {}).get("chat") or {}).get("id")

        # One person, and only in their own private chat with the bot. A group
        # the bot was added to would carry the right sender id and the wrong
        # chat, so both are checked.
        if sender != self.user_id or (chat is not None and chat != self.user_id):
            log(f"ignored update from user={sender} chat={chat}")
            return False

        try:
            if callback:
                self.on_callback(callback)
            else:
                self.on_message(message)
        except Exception as exc:
            log("handler failed:", traceback.format_exc())
            self.say(chat, f"⚠️ {exc}")
        return True

    # -- messages ----------------------------------------------------------

    def on_message(self, msg: dict) -> None:
        chat = msg["chat"]["id"]
        text = (msg.get("text") or "").strip()

        if msg.get("photo"):
            biggest = sorted(msg["photo"], key=lambda p: p.get("file_size", 0))[-1]
            return self.ingest(chat, biggest["file_id"],
                               f"photo-{int(time.time())}.jpg",
                               biggest.get("file_size", 0))
        if msg.get("document"):
            doc = msg["document"]
            return self.ingest(chat, doc["file_id"],
                               doc.get("file_name") or "file",
                               doc.get("file_size", 0))

        if text.startswith("/"):
            return self.on_command(chat, text)
        if self.pending:
            return self.on_pending_text(chat, text)
        self.menu(chat, "Send me a picture, a PDF or an EPUB — or pick one:")

    def on_command(self, chat, text: str) -> None:
        cmd = text.split()[0].lstrip("/").split("@")[0]
        if cmd in ("start", "menu", "help"):
            self.menu(chat, "Let's read!")
        elif cmd == "status":
            self.submit(chat, lambda: self.show_status(chat))
        elif cmd == "queue":
            self.show_queue(chat)
        elif cmd == "library":
            self.submit(chat, lambda: self.show_library(chat))
        elif cmd == "wallpapers":
            self.submit(chat, lambda: self.show_wallpapers(chat))
        elif cmd == "device":
            self.show_device(chat)
        elif cmd == "inbox":
            self.show_inbox(chat)
        elif cmd == "push":
            self.ask_push(chat)
        elif cmd == "cancel":
            if self.pending and self.pending.get("kind") == "seriesaliasgroup":
                return self.cancel_series_ingest(chat)
            self.pending = None
            self.menu(chat, "Dropped it.")
        else:
            self.menu(chat, "I don't know that one.")

    def on_pending_text(self, chat, text: str) -> None:
        """A plain message means whatever the last button asked for. One user
        means one pending question at a time, which is the whole reason this is
        a single slot rather than a state machine."""
        job, self.pending = self.pending, None
        if not text:
            return self.say(chat, "That was empty — nothing done.")

        if job["kind"] == "devrename":
            def work():
                suite.device.rename(job["host"], job["path"], text)
                self.say(chat, f"✅ renamed to <code>{html.escape(text)}</code>")
                self.browse(chat, job["parent"])
            return self.submit(chat, work)

        if job["kind"] == "devaddr":
            # Whatever the device screen shows, pasted from a phone: an IP, a
            # name, sometimes with http:// in front of it and a slash after.
            host = text.strip().split("//")[-1].split("/")[0].split()[0]
            host = host.strip("<>[] ")

            def work():
                info = suite.device.status(host)      # raises if it is not there
                suite.device.remember(host)
                self.say(chat,
                         f"📲 Found it at <code>{html.escape(host)}</code> — "
                         f"{info.get('model', 'reader')}, firmware "
                         f"{info.get('version', '?')}.\n\n"
                         f"Remembered, so I will try this one first from now on.",
                         [[("📤 Push queue", "push:ask"), ("📂 Browse", "dev:ls0:")],
                          [("🏠 Menu", "m:main")]])
            return self.submit(chat, work)

        if job["kind"] == "devmkdir":
            name = text.strip()
            if "/" in name or "\\" in name:
                return self.say(chat, "A folder name, not a path — no slashes.")

            def work():
                suite.device.mkdir(job["host"], job["path"], name)
                note = ("\n<i>(a dot-prefixed folder is hidden from the "
                        "reader's listings — including this one)</i>"
                        if name.startswith(".") else "")
                self.say(chat, f"📁 <code>{html.escape(name)}</code> created."
                               f"{note}")
                self.browse(chat, job["path"])
            return self.submit(chat, work)

        if job["kind"] == "devdirrename":
            # Folders go through WebDAV, which is the only route that will
            # touch one — and which refuses any path with a dotted segment.
            parent = job["path"].rstrip("/").rpartition("/")[0] or ""
            dest = f"{parent}/{text}"

            def work():
                suite.device.dav_move(job["host"], job["path"], dest)
                self.say(chat, f"✅ folder renamed to <code>{html.escape(dest)}</code>")
                self.browse(chat, dest)
            return self.submit(chat, work)

        if job["kind"] == "devfontrename":
            # A family *is* its folder — the registry reads the name off the
            # directory entry and never compares it to the filenames inside —
            # so renaming one is a single folder rename, and the .cpfont files
            # keep the old prefix without the reader minding.
            new = text.strip()
            why = suite.family_name_problem(new)
            if why:
                return self.say(chat, f"Not renamed — {why}.",
                                [[("🔤 On the reader", "dev:fo:")]])

            old_path = f"{job['root']}/{job['family']}"
            new_path = f"{job['root']}/{new}"

            def work():
                suite.device.dav_move(job["host"], old_path, new_path)
                lines = [f"✅ <b>{html.escape(job['family'])}</b> is now "
                         f"<b>{html.escape(new)}</b> on the card."]
                # A WebDAV rename marks nothing dirty, so without this the
                # reader would keep listing the old name with dead paths until
                # its next boot — and refuse to be selected under the new one.
                try:
                    suite.rescan_device_fonts(job["host"])
                    lines.append("🔄 and the reader has re-scanned, so its own "
                                 "list is right without a power-cycle.")
                except suite.DeviceError as exc:
                    lines.append(f"⚠️ could not make it re-scan "
                                 f"({html.escape(str(exc)[:80])}) — its font "
                                 f"list will stay stale until you power-cycle.")
                if job["selected"]:
                    ok, detail = suite.device.select_font_family(job["host"], new)
                    lines.append(
                        "✅ and it is still the family it reads with."
                        if ok else
                        f"⚠️ it was the selected family and could not be "
                        f"re-selected ({html.escape(str(detail)[:80])}). Pick it "
                        f"again here or under Settings → Reader → Font Family, "
                        f"or the reader starts on a built-in font.")
                self.say(chat, "\n".join(lines),
                         [[("🔤 On the reader", "dev:fo:")], [("🏠 Menu", "m:main")]])
            return self.submit(chat, work)

        if job["kind"] == "wlrename":
            src = Path(job["path"])
            if not inside(self.workspace, src):
                return self.say(chat, "⚠️ that file is outside the workspace.")
            name = Path(text).name
            if not name.lower().endswith(".bmp"):
                name += ".bmp"
            dest = safe_join(src.parent, name)
            if dest.exists():
                return self.say(chat, "⚠️ something is already called that.")
            src.rename(dest)
            # The preview beside it carries the same stem, so it follows.
            png = src.with_suffix(".png")
            if png.exists():
                png.rename(dest.with_suffix(".png"))
            return self.say(chat, f"✅ renamed to <code>{html.escape(dest.name)}</code>",
                            [[("🖼 Wallpapers", "m:wl")]])

        if job["kind"] == "librename":
            src = Path(job["path"])
            if not inside(self.workspace, src):
                return self.say(chat, "⚠️ that file is outside the workspace.")
            dest = safe_join(src.parent, Path(text).name)
            if dest.exists():
                return self.say(chat, "⚠️ something is already called that.")
            src.rename(dest)
            return self.say(chat, f"✅ renamed to <code>{html.escape(dest.name)}</code>")

        if job["kind"] == "seriesaliasgroup":
            return self.submit(
                chat, lambda: self.finish_series_ingest(chat, job["key"], text))

        if job["kind"] == "seriesaliaschange":
            return self.submit(
                chat, lambda: self.change_series_alias(
                    chat, job["series"], text,
                    message_id=job.get("message_id"),
                    library_state=job.get("library_state")))

        if job["kind"] == "seriesnamechange":
            return self.submit(
                chat, lambda: self.preview_series_name(
                    chat, job["series"], text,
                    message_id=job.get("message_id"),
                    library_state=job.get("library_state")))

        if job["kind"] == "bookmetadata":
            if text == "-" and job["field"] not in {"author", "series", "series_index"}:
                return self.say(chat, "That field is required and cannot be cleared.")
            value = "" if text == "-" else text
            return self.submit(
                chat, lambda: self.preview_book_metadata(
                    chat, Path(job["path"]), {job["field"]: value},
                    message_id=job.get("message_id"),
                    back_book=job.get("back_book")))

        if job["kind"] == "bookmetaalias":
            return self.submit(
                chat, lambda: self.preview_book_metadata(
                    chat, Path(job["path"]), job["updates"], alias=text,
                    alias_prompt=(job["series"], job["suggestion"]),
                    message_id=job.get("message_id"),
                    back_book=job.get("back_book")))

        if job["kind"] == "bookseriesname":
            return self.submit(
                chat, lambda: self.accept_book_series_name(
                    chat, Path(job["path"]), text, job.get("back_book") or {},
                    message_id=job.get("message_id")))

        if job["kind"] == "bookseriesalias":
            return self.submit(
                chat, lambda: self.accept_book_series_alias(
                    chat, Path(job["path"]), job["series"], text,
                    job.get("back_book") or {}, message_id=job.get("message_id")))

        if job["kind"] == "bookseriesposition":
            position = "" if text == "-" else text
            retry = {"path": job["path"], "series": job["series"],
                     "alias": job.get("alias", ""),
                     "back_book": job.get("back_book") or {}}
            return self.submit(
                chat, lambda: self.preview_book_metadata(
                    chat, Path(job["path"]),
                    {"series": job["series"], "series_index": position},
                    alias=job.get("alias", ""),
                    message_id=job.get("message_id"),
                    back_book=job.get("back_book"), retry_position=retry))

    # -- menus -------------------------------------------------------------

    def menu(self, chat, text: str = "Menu", message_id=None) -> None:
        # Three collections on the server, then the ways in and out of it.
        # The queue is an outbox, so it is 📤 rather than a second 🖼.
        self.panel(chat, message_id, text, [
            [("📚 Library", "m:lib"), ("🖼 Wallpapers", "m:wl")],
            [("🔤 Fonts", "m:fo"), ("📥 Inbox", "m:in")],
            [("📲 Device", "m:dev"), ("📤 Queue", "m:q")],
            [("⚙️ Status", "m:st")],
        ])

    def on_callback(self, cb: dict) -> None:
        data = cb.get("data") or ""
        chat = cb["message"]["chat"]["id"]
        message_id = (cb.get("message") or {}).get("message_id")
        self.tg.answer_callback(cb["id"])
        head, _, rest = data.partition(":")

        if head == "m":
            return {"lib": lambda: self.submit(
                        chat, lambda: self.show_library(chat, message_id=message_id)),
                    "q": lambda: self.show_queue(chat),
                    "in": lambda: self.show_inbox(chat),
                    "dev": lambda: self.show_device(chat, message_id=message_id),
                    "fo": lambda: self.show_fonts(chat),
                    "wl": lambda: self.show_wallpapers(chat),
                    "st": lambda: self.submit(chat, lambda: self.show_status(chat)),
                    "main": lambda: self.menu(chat, message_id=message_id)}.get(
                        rest, lambda: None)()

        if head == "fo":
            return self.on_font_callback(chat, rest)
        if head == "dfo":                      # a family already on the reader
            return self.on_device_font_callback(chat, rest)
        if head == "wl":
            return self.on_wallpaper_callback(
                chat, rest, (cb.get("message") or {}).get("message_id"))

        if head == "wp":                       # wp:<token>:<mat>
            token, _, mat = rest.partition(":")
            src = self.tokens.get(token)
            if not src:
                return self.stale(chat)
            return self.submit(chat, lambda: self.make_wallpaper(chat, Path(src), mat))
        if head == "wpq":                      # queue an already-built BMP
            bmp = self.tokens.get(rest)
            if not bmp:
                return self.stale(chat)
            self.queue.add("wallpaper", bmp)
            return self.say(chat, f"🖼 queued — {len(self.queue)} waiting.",
                            [[("📲 Push now", "push:ask"), ("🏠 Menu", "m:main")]])
        if head == "wpx":
            return self.say(chat, "Dropped it.", [[("🏠 Menu", "m:main")]])

        if head == "bq":                       # queue a book for the SD card
            book = self.tokens.get(rest)
            if not book:
                return self.stale(chat)

            def work():
                result = self.queue_book(book)
                if not result["queued"]:
                    return self.say(
                        chat, "Already queued — there will still be only one card copy.",
                        [[("📤 Queue", "m:q"), ("🏠 Menu", "m:main")]])
                self.say(
                    chat,
                    f"📕 queued for the card — {len(self.queue)} waiting."
                    f"{result['note']}\nIt stays on the catalog either way.",
                    [[("📲 Push now", "push:ask"), ("🏠 Menu", "m:main")]])
            return self.submit(chat, work)

        if head == "balias":                  # accept a deterministic suggestion
            if rest == "cancel":
                return self.cancel_series_ingest(chat)
            job = self.tokens.get(rest)
            if not job:
                return self.stale(chat)
            if job.get("key") != self.active_alias_group:
                return self.stale(chat)
            self.pending = None
            return self.submit(
                chat, lambda: self.finish_series_ingest(
                    chat, job["key"], job["alias"]))

        if head == "qdel":
            self.queue.remove(rest)
            return self.show_queue(chat)
        if head == "qclr":
            return self.say(chat, "Clear the whole queue?",
                            [[("Yes, clear it", "qclr!"), ("No", "m:q")]])
        if head == "qclr!":
            n = self.queue.clear()
            return self.say(chat, f"Cleared {n}.", [[("🏠 Menu", "m:main")]])

        if head == "push":
            if rest == "ask":
                return self.ask_push(chat)
            if rest == "go":
                if not len(self.queue):
                    return self.say(chat, "Nothing queued.",
                                    [[("🏠 Menu", "m:main")]])
                # Said here rather than inside do_push, and before anything
                # slow: finding the reader takes seconds, and the worker may be
                # busy with something else entirely. A tap that produces
                # silence reads as a tap that missed.
                self.say(chat, "📡 Listening for the reader…")
                return self.submit(chat, lambda: self.do_push(chat))

        if head == "dev":
            return self.on_device_callback(
                chat, rest, message_id)
        if head == "lib":
            return self.on_library_callback(chat, rest, message_id)
        if head == "ser":
            return self.on_series_callback(chat, rest, message_id)
        if head == "srn":
            return self.on_series_rename_callback(chat, rest, message_id)
        if head == "bm":
            return self.on_book_metadata_callback(chat, rest, message_id)
        if head == "in":
            path = self.tokens.get(rest)
            if not path:
                return self.stale(chat)
            return self.submit(chat, lambda: self.route_local(chat, Path(path)))

    def no_reader(self, chat, detail: str = "", retry: str | None = None) -> None:
        """The one failure that happens to everybody, answered usefully.

        The underlying error is `push_wallpaper.py`'s, and it ends by
        suggesting `--ip 192.168.x.x` — sound advice at a terminal and useless
        in a chat window. So the bot says it its own way, and offers the thing
        the flag would have done.
        """
        rows = []
        if retry:
            rows.append([("🔁 Try again", retry)])
        rows.append([("📍 Enter its address", "dev:addr:")])
        rows.append([("🏠 Menu", "m:main")])
        self.say(chat,
                 "📵 <b>No reader found.</b>\n\n"
                 "Tried the address that worked last time, "
                 "<code>crosspoint.local</code>, and a discovery ping on this "
                 "network.\n\n"
                 "On the X3: <b>Home → File Transfer → Join a Network</b>. It "
                 "prints an address on that screen — send it to me and I will "
                 "remember it."
                 # Only the first line of the underlying error. It names the
                 # addresses actually tried, which is worth having; the rest is
                 # the command-line advice this button replaces.
                 + (f"\n\n<i>{html.escape(detail.splitlines()[0][:200])}</i>"
                    if detail.strip() else ""),
                 rows)

    def stale(self, chat) -> None:
        self.say(chat, "That menu is from before a restart — open it again.",
                 [[("🏠 Menu", "m:main")]])

    # -- ingest ------------------------------------------------------------

    def ingest(self, chat, file_id: str, filename: str, size: int) -> None:
        """Something arrived in the chat. Check it will fit, fetch it, route it.

        The size check is not politeness: `getFile` refuses anything over 20 MB
        outright, whatever the sender's account managed to upload. Scanned PDFs
        sit right on that line, so this is the common case and not the edge.
        """
        if size and size > min(self.max_bytes, GETFILE_LIMIT):
            return self.say(
                chat,
                f"📛 {html.escape(filename)} is {human(size)} — Telegram won't let "
                f"a bot download more than {human(GETFILE_LIMIT)}.\n\n"
                f"Put it in <code>workspace/inbox/</code> on the server and tap "
                f"📥 Inbox.",
                [[("📥 Inbox", "m:in")]])

        suffix = Path(filename).suffix.lower()
        dest_dir = (suite.WALLPAPER_IN if suffix in IMAGE_SUFFIXES
                    else self.workspace / "inbox")
        dest = safe_join(dest_dir, Path(filename).name)
        dest.parent.mkdir(parents=True, exist_ok=True)

        def work():
            self.tg.download(file_id, dest)
            log("received", dest)
            self.route_local(chat, dest)
        self.submit(chat, work)

    def route_local(self, chat, path: Path) -> None:
        """Decide what a file on disk is for. Used both for chat arrivals and
        for whatever you dropped in the inbox by hand."""
        if not path.exists():
            return self.say(chat, "That file is gone.")
        suffix = path.suffix.lower()
        if suffix in IMAGE_SUFFIXES:
            return self.offer_wallpaper(chat, path)
        if suffix == ".epub":
            return self.take_book(chat, path)
        if suffix == ".pdf":
            return self.take_pdf(chat, path)
        self.say(chat, f"I don't know what to do with <code>{html.escape(path.name)}</code>.")

    # -- wallpapers --------------------------------------------------------

    def offer_wallpaper(self, chat, src: Path) -> None:
        """Ask about the mat only when there is a choice to make.

        An image that fills the panel has no mat, so it gets built and offered
        straight away. One that does not is previewed on plain white — you see
        exactly what is yours and what is filling — and the four fillings are
        the buttons. Picking one builds and queues in the same tap; asking
        twice for one decision is what makes a phone tiring.
        """
        report = suite.probe_image(src)
        token = self.tokens.put(str(src))
        if report["fills"]:
            return self.make_wallpaper(chat, src, "waves", offer_queue=True)

        _, png = suite.build_wallpaper(src, "none")
        caption = (f"{report['width']}×{report['height']} — too small to fill "
                   f"528×792. How should the rest be filled?")
        keyboard = [[(label, f"wp:{token}:{style}") for label, style in MATS[:2]],
                    [(label, f"wp:{token}:{style}") for label, style in MATS[2:]],
                    [("✗ discard", "wpx:")]]
        self.send_preview(chat, png, caption, keyboard)

    def make_wallpaper(self, chat, src: Path, mat: str,
                       offer_queue: bool = False) -> None:
        bmp, png = suite.build_wallpaper(src, mat)
        if offer_queue:
            token = self.tokens.put(str(bmp))
            return self.send_preview(
                chat, png, f"{bmp.name} — fills the panel, no mat needed.",
                [[("✓ Queue it", f"wpq:{token}"), ("✗ discard", "wpx:")]])
        # A mat was chosen deliberately, so queue it without asking again.
        self.queue.add("wallpaper", str(bmp))
        self.send_preview(chat, png,
                          f"✅ {bmp.name} — queued ({len(self.queue)} waiting).",
                          [[("📲 Push now", "push:ask"), ("🏠 Menu", "m:main")]])

    def send_preview(self, chat, png: Path, caption: str, keyboard):
        try:
            if png.exists():
                return self.tg.send_photo(chat, png, caption, keyboard)
        except TelegramError as exc:
            log("preview failed:", exc)
        return self.say(chat, caption, keyboard)

    # -- books and PDFs ----------------------------------------------------

    def take_book(self, chat, path: Path) -> None:
        """Hand one upload to the catalog's deterministic ingester."""
        if not inside(self.workspace, path):
            return self.say(chat, "⚠️ that book is outside the workspace.")
        dest_dir = self.workspace / "library"
        dest_dir.mkdir(parents=True, exist_ok=True)
        report = suite.ingest_book(path, dest_dir)
        if report.get("status") == "needs_alias":
            return self.stage_series_ingest(chat, path, report)
        self.report_book_ingest(chat, report)

    def stage_series_ingest(self, chat, path: Path, report: dict) -> None:
        """Collect unresolved volumes and expose one textual question at a time.

        This is Telegram's presentation of ingest_book's ``needs_alias``
        result.  Alias suggestions, validation and filing remain entirely in
        the catalog script; compare its ``tidy_interactive`` terminal UI.
        """
        series = report.get("series", "")
        key = series.casefold()
        group = self.alias_groups.setdefault(key, {
            "series": series,
            "suggestion": report.get("suggested_alias") or "SERIES",
            "books": [],
            "message_id": None,
        })
        if not any(item["path"] == str(path) for item in group["books"]):
            group["books"].append({
                "path": str(path),
                "title": report.get("base_title") or report.get("title") or path.stem,
                "series_index": report.get("series_index", ""),
            })
        if self.active_alias_group is None:
            self.active_alias_group = key
            return self.ask_series_alias(chat, key)
        if self.active_alias_group == key:
            return self.ask_series_alias(chat, key, refresh=True)
        if len(group["books"]) == 1:
            self.say(
                chat,
                f"📚 Staged <b>{html.escape(series)}</b>. I’ll ask for its short "
                "name after the current series; no reply is expected yet.")

    def _series_alias_question(self, group: dict, error: str = "") -> tuple:
        books = sorted(group["books"], key=lambda item: (
            item.get("series_index") == "", item.get("series_index", ""),
            item.get("title", "").casefold()))
        positions = ", ".join(item["series_index"] for item in books
                              if item.get("series_index"))
        suggestion = group["suggestion"]
        token = self.tokens.put({"key": group["series"].casefold(),
                                 "alias": suggestion})
        lead = (f"⚠️ {html.escape(error)}\n\n" if error else "")
        text = (
            lead + f"📚 <b>{html.escape(group['series'])}</b> — "
            f"{len(books)} staged book(s)"
            + (f" · volumes {html.escape(positions)}" if positions else "")
            + "\n\nSend one short name (maximum 6 characters). It applies to "
              "every staged volume in this series. The suggestion is mechanical, "
              "so you remain the one deciding it.")
        keyboard = [[(f"Use {suggestion}", f"balias:{token}")],
                    [("✗ Leave this series in the inbox", "balias:cancel")]]
        return text, keyboard

    def ask_series_alias(self, chat, key: str, *, refresh: bool = False,
                         error: str = "") -> None:
        group = self.alias_groups.get(key)
        if not group:
            return
        self.active_alias_group = key
        self.pending = {"kind": "seriesaliasgroup", "key": key}
        text, keyboard = self._series_alias_question(group, error)
        message_id = group.get("message_id")
        if refresh and message_id:
            try:
                self.tg.edit_message(chat, message_id, text, keyboard)
                return
            except TelegramError:
                pass
        sent = self.say(chat, text, keyboard)
        if sent and sent.get("message_id"):
            group["message_id"] = sent["message_id"]

    def _next_series_alias(self, chat) -> None:
        self.active_alias_group = next(iter(self.alias_groups), None)
        if self.active_alias_group is None:
            self.pending = None
            return
        self.ask_series_alias(chat, self.active_alias_group)

    def cancel_series_ingest(self, chat) -> None:
        key = self.active_alias_group
        group = self.alias_groups.pop(key, None) if key else None
        self.pending = None
        self.active_alias_group = None
        if group:
            self.say(chat, f"Left {len(group['books'])} "
                          f"<b>{html.escape(group['series'])}</b> upload(s) untouched.")
        self._next_series_alias(chat)

    def finish_series_ingest(self, chat, key: str, alias: str) -> None:
        group = self.alias_groups.get(key)
        if not group or self.active_alias_group != key:
            return self.stale(chat)
        reports = []
        for number, item in enumerate(list(group["books"])):
            report = suite.ingest_book(
                Path(item["path"]), self.workspace / "library",
                alias if number == 0 else "")
            if (number == 0 and report.get("status") == "conflict"
                    and report.get("series") and not report.get("series_alias")
                    and not report.get("destination")):
                return self.ask_series_alias(
                    chat, key, error=report.get("error", "That short name did not work."))
            reports.append(report)

        self.alias_groups.pop(key, None)
        self.pending = None
        self.active_alias_group = None
        filed = [report for report in reports
                 if report.get("status") in ("filed", "already_present")]
        failed = [report for report in reports
                  if report.get("status") not in ("filed", "already_present")]
        already = sum(report.get("status") == "already_present" for report in filed)
        lines = [f"📚 <b>{html.escape(group['series'])}</b> "
                 f"[{html.escape(alias)}]",
                 f"filed: {len(filed) - already}",
                 f"already present: {already}"]
        for report in filed[:10]:
            destination = Path(report.get("destination") or report.get("source", "book"))
            lines.append("· <code>" + html.escape(destination.name) + "</code>")
        if len(filed) > 10:
            lines.append(f"· …and {len(filed) - 10} more")
        if failed:
            lines.append("Left untouched:")
            lines.extend("· " + html.escape(
                (report.get("error") or Path(report.get("source", "book")).name)[:120])
                         for report in failed)
        self.say(chat, "\n".join(lines),
                 [[("📚 Library", "m:lib"), ("🏠 Menu", "m:main")]])
        self._next_series_alias(chat)

    def report_book_ingest(self, chat, report: dict) -> None:
        status = report.get("status")
        if status in ("invalid", "conflict", "error"):
            return self.say(
                chat,
                "⚠️ Not added. The uploaded file was left untouched.\n\n"
                f"{html.escape(report.get('error') or 'catalog ingest failed')}",
                [[("📥 Inbox", "m:in"), ("🏠 Menu", "m:main")]])

        destination = Path(report.get("destination") or report.get("source", ""))
        if status == "already_present":
            intro = ("📚 Already in the catalog; corrected its catalog name. "
                     "The duplicate upload was left untouched."
                     if report.get("renamed_from") else
                     "📚 Already in the catalog; the duplicate upload was left untouched.")
        elif status == "filed":
            intro = "📚 Filed from embedded metadata."
        else:
            return self.say(chat, f"⚠️ Unexpected ingest result: {html.escape(str(status))}")

        title = report.get("title") or destination.stem
        author = report.get("author") or ""
        lines = [intro, f"<b>{html.escape(title)}</b>",
                 f"by {html.escape(author or 'unknown')}"]
        if report.get("series"):
            series_line = (f"series: {html.escape(report['series'])} "
                           f"[{html.escape(report.get('series_alias') or '?')}]")
            if report.get("series_index"):
                series_line += f" · volume {html.escape(report['series_index'])}"
            lines.append(series_line)
        lines.append(f"catalog file: <code>{html.escape(destination.name)}</code>")
        if not report.get("verify_ok", False):
            lines.append("\n<i>Readable EPUB, but the suite's stricter builder "
                         "check noted differences typical of third-party books.</i>")
        if not suite.opds_up(self.cfg["opds_url"]):
            lines.append("\n⚠️ Filed, but opds-server is not answering yet.")
        if self.active_alias_group in self.alias_groups:
            waiting = self.alias_groups[self.active_alias_group]["series"]
            lines.append("\n↩️ Your next plain-text reply still sets the short "
                         f"name for <b>{html.escape(waiting)}</b>.")

        book = {key: report.get(key, "") for key in (
            "title", "base_title", "author", "language", "series",
            "series_index", "series_alias")}
        book["path"] = str(destination)
        token = self.tokens.put(book)
        self.say(chat, "\n".join(lines),
                 [[("📤 Also send to device", f"bq:{token}")],
                  [("🏠 Menu", "m:main")]])

    def queue_book(self, book: dict) -> dict:
        """Warm the same slim cache for one book, individual or whole series."""
        path = Path(book["path"])
        resolved = path.resolve()
        for item in self.queue.items():
            if item.get("kind") == "book" and Path(item["path"]).resolve() == resolved:
                return {"queued": False, "note": "", "saved": 0}
        meta = dict(book)
        slimmed = suite.slim_book(path, self.state_dir / "cache" / "slim")
        note = ""
        if slimmed["used"]:
            meta["slim"] = str(slimmed["path"])
            note = (f"\nSlimmed for the reader: {human(slimmed['before'])} → "
                    f"{human(slimmed['after'])}. The catalog keeps the original.")
        self.queue.add("book", str(path),
                       label=(book.get("title") or path.stem)[:40], meta=meta)
        return {"queued": True, "note": note, "saved": slimmed.get("saved", 0)}

    def take_pdf(self, chat, path: Path) -> None:
        """Stage the conversion and say honestly what happens next.

        The bot does not convert PDFs. It makes the workspace a converter can
        run in, reports what triage found, and names the driver that would do
        the work — which today is you, with Claude Code and the skill.
        """
        slug = slugify(path.name)
        job_dir = safe_join(self.workspace, slug)
        job_dir.mkdir(parents=True, exist_ok=True)
        dest = job_dir / "source.pdf"
        if path.resolve() != dest.resolve():
            path.replace(dest)

        (job_dir / "build").mkdir(exist_ok=True)
        try:
            report = suite.triage_pdf(dest, job_dir / "build" / "triage.json")
        except suite.SuiteError as exc:
            return self.say(chat, f"📄 staged at <code>workspace/{slug}/</code>, "
                                  f"but triage failed:\n<pre>{html.escape(str(exc)[:400])}</pre>")

        route = report.get("route", "?")
        lines = [f"📄 <b>{html.escape(slug)}</b> staged",
                 f"route: <b>{html.escape(str(route))}</b>",
                 f"pages: {report.get('pages', '?')}", ""]
        runner = suite.pdf2epub_runner()
        if runner:
            lines.append("Running the headless converter…")
            self.say(chat, "\n".join(lines))
            rc, out, err = suite.run([suite.PY_DEPS, str(runner), str(job_dir)],
                                     timeout=3600)
            tail = (err or out).strip()[-600:]
            return self.say(
                chat,
                ("✅ converted — it is on the catalog." if rc == 0
                 else "⚠️ the converter stopped and saved its progress.")
                + (f"\n<pre>{html.escape(tail)}</pre>" if tail else ""),
                [[("🏠 Menu", "m:main")]])
        lines.append("pdf2epub has no headless runner yet "
                     "(<code>ai-tools/pdf2epub/DESIGN.md</code>, open question 6), "
                     "so this one is waiting for a driver:")
        lines.append(f"<code>workspace/{slug}/</code> — point Claude Code at the "
                     f"repo and read <code>ai-tools/pdf2epub/SKILL.md</code>.")
        self.say(chat, "\n".join(lines), [[("🏠 Menu", "m:main")]])

    # -- views -------------------------------------------------------------

    def show_library(self, chat, page: int = 0, *, message_id=None,
                     state: dict | None = None) -> None:
        try:
            lib = suite.library()
        except suite.SuiteError as exc:
            return self.panel(chat, message_id, f"⚠️ {exc}")
        books = lib.get("books", [])
        if not books:
            return self.panel(chat, message_id, "No books yet. Send me an EPUB.",
                              [[("🏠 Menu", "m:main")]])

        state = dict(state or {"kind": "all", "initial": "", "page": page})
        kind = state.get("kind") if state.get("kind") in {
            "all", "series", "standalone"} else "all"
        initial = state.get("initial", "")

        groups = lib.get("series", [])
        grouped_ids = {book_id for group in groups
                       for book_id in group.get("book_ids", [])}
        standalone = [book for book in books if book.get("id") not in grouped_ids]
        entries = []
        for group in groups:
            label = f"📚 {group['name']} · {group['count']}"
            entries.append((group["name"].casefold(), 0, label,
                            "series", group["key"], group["name"]))
        for book in standalone:
            title = book.get("base_title") or book.get("title") or Path(book["path"]).stem
            author = book.get("author") or "?"
            label = f"📖 {title[:32]} · {author[:14]}"
            # The whole record, not just the path: sending it to the card later
            # needs the author and title the catalog knows it by.
            entries.append((title.casefold(), 1, label, "book", book, title))
        entries.sort(key=lambda entry: (entry[0], entry[1]))

        entry_kind = {"series": "series", "standalone": "book"}.get(kind)
        typed = [entry for entry in entries
                 if kind == "all" or entry[3] == entry_kind]
        buckets = {self.initial_bucket(entry[5]) for entry in typed}
        if initial not in buckets:
            initial = ""
        filtered = [entry for entry in typed
                    if not initial or self.initial_bucket(entry[5]) == initial]
        last_page = max(0, (len(filtered) - 1) // NAV_PAGE)
        page = max(0, min(int(state.get("page", 0)), last_page))
        state = {"kind": kind, "initial": initial, "page": page}

        def view(**changes):
            target = {**state, **changes}
            return f"lib:v:{self.tokens.put(target)}"

        rows = [[
            (("✓ " if kind == value else "") + label, view(kind=value, page=0))
            for value, label in (("all", "All"), ("series", "Series"),
                                 ("standalone", "Standalone"))
        ]]
        if len(typed) > ALPHABET_THRESHOLD:
            rows.extend(self.alphabet_rows(
                [entry[5] for entry in typed], initial,
                lambda bucket: view(initial=bucket, page=0)))

        start = page * NAV_PAGE
        for _, _, label, entry_kind, payload, _ in filtered[start:start + NAV_PAGE]:
            context = dict(state)
            if entry_kind == "series":
                payload = {"key": payload, "page": 0,
                           "library_state": context}
            else:
                payload = {**payload, "_library_state": context}
            token = self.tokens.put(payload)
            callback = (f"ser:f:{token}" if entry_kind == "series"
                        else f"lib:f:{token}")
            rows.append([(label[:52], callback)])
        navigation = []
        if page:
            navigation.append(("‹", view(page=page - 1)))
        if last_page:
            navigation.append((f"{page + 1}/{last_page + 1}", "lib:nop:"))
        if page < last_page:
            navigation.append(("›", view(page=page + 1)))
        if navigation:
            rows.append(navigation)
        rows.append([("🏠 Menu", "m:main")])
        summary = f"📚 <b>Library</b>\n{counted(len(books), 'book')} total"
        if groups:
            summary += " · " + counted(len(groups), "series", "series")
            if standalone:
                summary += " · " + counted(len(standalone), "standalone book")
        shown = {"all": "All", "series": "Series",
                 "standalone": "Standalone"}[kind]
        summary += f"\nShowing: {shown}" + (f" · {initial}" if initial else "")
        if last_page:
            summary += f"\nPage {page + 1} of {last_page + 1}"
        self.panel(chat, message_id, summary, rows)

    def series_group(self, key: str) -> tuple:
        lib = suite.library()
        group = next((g for g in lib.get("series", []) if g.get("key") == key), None)
        if not group:
            return None, []
        by_id = {book["id"]: book for book in lib.get("books", [])}
        return group, [by_id[book_id] for book_id in group.get("book_ids", [])
                       if book_id in by_id]

    def on_series_callback(self, chat, rest: str, message_id=None) -> None:
        if self.pending and self.pending.get("kind") in {
                "seriesaliaschange", "seriesnamechange"}:
            self.pending = None
        if rest == "list":
            # Compatibility for buttons sent before the unified library view.
            return self.submit(
                chat, lambda: self.show_library(chat, message_id=message_id))
        action, _, token = rest.partition(":")
        payload = self.tokens.get(token)
        if not payload:
            return self.stale(chat)
        key = payload.get("key") if isinstance(payload, dict) else payload
        page = payload.get("page", 0) if isinstance(payload, dict) else 0
        library_state = (payload.get("library_state", {})
                         if isinstance(payload, dict) else {})
        again_payload = {"key": key, "page": page,
                         "library_state": library_state}
        try:
            group, books = self.series_group(key)
        except suite.SuiteError as exc:
            return self.panel(chat, message_id, f"⚠️ {exc}")
        if not group:
            return self.stale(chat)

        if action == "m":
            # Compatibility for the short-lived separate metadata picker.
            action = "f"

        if action == "f":
            last_page = max(0, (len(books) - 1) // NAV_PAGE)
            page = max(0, min(page, last_page))
            start = page * NAV_PAGE
            lines = [f"📚 <b>{html.escape(group['name'])}</b>",
                     f"short name: <code>{html.escape(group.get('alias') or 'not set')}</code>",
                     counted(len(books), "book")]
            if last_page:
                lines.append(f"Page {page + 1} of {last_page + 1}")
            rows = []
            for book in books[start:start + NAV_PAGE]:
                position = (f"{book.get('series_index')} · "
                            if book.get("series_index") else "")
                title = (book.get("base_title") or book.get("title")
                         or Path(book["path"]).stem)
                contextual = {**book, "_series_key": key, "_series_page": page,
                              "_library_state": library_state}
                rows.append([(("📖 " + position + title)[:52],
                              f"lib:f:{self.tokens.put(contextual)}")])
            navigation = []
            if page:
                previous = self.tokens.put({"key": key, "page": page - 1,
                                            "library_state": library_state})
                navigation.append(("‹ Previous", f"ser:f:{previous}"))
            if page < last_page:
                following = self.tokens.put({"key": key, "page": page + 1,
                                             "library_state": library_state})
                navigation.append(("Next ›", f"ser:f:{following}"))
            if navigation:
                rows.append(navigation)
            again = self.tokens.put(again_payload)
            library_back = (f"lib:v:{self.tokens.put(library_state)}"
                            if library_state else "m:lib")
            rows.extend([
                [(f"📤 Add all {len(books)} to X3", f"ser:q:{again}")],
                [("✏️ Change series name", f"ser:n:{again}")],
                [("✏️ Change short name", f"ser:a:{again}")],
                [("📚 Library", library_back)],
            ])
            return self.panel(chat, message_id, "\n".join(lines), rows)

        if action == "q":
            self.panel(chat, message_id,
                       f"Preparing {counted(len(books), 'book')} for the queue…")
            return self.submit(
                chat, lambda: self.queue_series(
                    chat, group, books, message_id=message_id,
                    library_state=library_state))

        if action in {"rm", "rm!"}:
            # Compatibility for series cards already sitting in chat. Device
            # deletion now belongs exclusively to the connected-device view.
            again = self.tokens.put({"key": key, "page": page,
                                     "library_state": library_state})
            return self.panel(
                chat, message_id,
                "Removing books belongs in <b>Device</b>, where the X3 is "
                "connected and its actual contents are visible.",
                [[("📲 Device", "m:dev"),
                  ("◀ Series", f"ser:f:{again}")]])

        if action == "n":
            if self.pending:
                return self.panel(
                    chat, message_id,
                    "Finish the current question first, or send /cancel.")
            self.pending = {
                "kind": "seriesnamechange", "series": group["name"],
                "message_id": message_id, "library_state": library_state,
            }
            return self.panel(
                chat, message_id,
                f"Send the new full name for <b>{html.escape(group['name'])}</b>.\n\n"
                "Every volume will be previewed together before anything is written.",
                [[("✗ Cancel", f"ser:f:{self.tokens.put({**again_payload})}")]])

        if action == "a":
            if self.pending:
                return self.panel(
                    chat, message_id,
                    "Finish the current question first, or send /cancel.")
            self.pending = {"kind": "seriesaliaschange", "series": group["name"],
                            "message_id": message_id,
                            "library_state": library_state}
            return self.panel(
                chat,
                message_id,
                f"Send the new short name for <b>{html.escape(group['name'])}</b> "
                "(maximum 6 characters). This renames all of its catalog files "
                "together; their EPUB bytes do not change.",
                [[("✗ Cancel", f"ser:f:{self.tokens.put(again_payload)}")]])

    def queue_series(self, chat, group: dict, books: list, *, message_id=None,
                     library_state: dict | None = None) -> None:
        queued = skipped = 0
        saved = 0
        failed = []
        for book in books:
            try:
                result = self.queue_book(book)
            except Exception as exc:
                failed.append(f"{book.get('base_title') or book.get('title')}: {exc}")
                continue
            if result["queued"]:
                queued += 1
                saved += result["saved"]
            else:
                skipped += 1
        lines = [f"📕 <b>{html.escape(group['name'])}</b>",
                 f"queued: {queued}", f"already queued: {skipped}",
                 "Catalog originals were not changed."]
        if saved:
            lines.insert(3, f"reader-only slimming saved {human(saved)}")
        if failed:
            lines.append("Could not prepare:\n" + "\n".join(
                f"· {html.escape(detail[:100])}" for detail in failed))
        library_back = (f"lib:v:{self.tokens.put(library_state)}"
                        if library_state else "m:lib")
        self.panel(chat, message_id, "\n".join(lines),
                   [[("📲 Push now", "push:ask"), ("📤 Queue", "m:q")],
                    [("📚 Library", library_back)]])

    def change_series_alias(self, chat, series: str, alias: str, *,
                            message_id=None,
                            library_state: dict | None = None) -> None:
        report = suite.set_series_alias(self.workspace / "library", series, alias)
        if report.get("status") not in ("alias_set", "dry_run"):
            return self.panel(
                chat, message_id, "⚠️ Short name not changed: "
                f"{html.escape(report.get('error', 'unknown error'))}",
                [[("📚 Library", "m:lib")]])
        try:
            lib = suite.library()
        except suite.SuiteError:
            lib = {"books": []}
        fresh = {str(Path(book["path"])): book for book in lib.get("books", [])}
        replacements = {}
        for rename in report.get("renames", []):
            book = fresh.get(str(Path(rename["to"])))
            if book:
                replacements[str(Path(rename["from"]))] = book
        followed = self.queue.remap_books(replacements)
        group = next((item for item in lib.get("series", [])
                      if item.get("name", "").casefold() == series.casefold()), None)
        rows = [[("📚 Library", f"lib:v:{self.tokens.put(library_state)}"
                  if library_state else "m:lib")]]
        if group:
            series_token = self.tokens.put({
                'key': group['key'], 'page': 0,
                'library_state': library_state or {},
            })
            rows.insert(0, [("◀ Series", f"ser:f:{series_token}")])
        self.panel(
            chat, message_id,
            f"✅ <b>{html.escape(series)}</b> now uses "
            f"<code>{html.escape(report['series_alias'])}</code>. "
            f"Renamed {report.get('changed', 0)} catalog file(s)"
            + (f" and updated {followed} queued path(s)." if followed else "."),
            rows)

    def preview_series_name(self, chat, series: str, new_name: str, *,
                            merge: bool = False, message_id=None,
                            library_state: dict | None = None) -> None:
        report = suite.rename_series(
            self.workspace / "library", series, new_name,
            merge=merge, dry_run=True)
        if report.get("status") == "needs_merge":
            token = self.tokens.put({
                "series": series, "new_name": report.get("target_series", new_name),
                "library_state": library_state or {},
            })
            return self.panel(
                chat, message_id,
                f"<b>{html.escape(report.get('target_series', new_name))}</b> already "
                f"exists with {counted(report.get('target_count', 0), 'book')}.\n\n"
                f"Merge the {counted(report.get('count', 0), 'book')} from "
                f"<b>{html.escape(series)}</b> into it?",
                [[("Merge series", f"srn:merge:{token}"),
                  ("No", "m:lib")]])
        if report.get("status") in {"invalid", "conflict", "error"}:
            try:
                group, _ = self.series_group(next(
                    item["key"] for item in suite.library().get("series", [])
                    if item.get("name", "").casefold() == series.casefold()))
            except (StopIteration, suite.SuiteError):
                group = None
            rows = [[("📚 Library", "m:lib")]]
            if group:
                series_token = self.tokens.put({
                    'key': group['key'], 'page': 0,
                    'library_state': library_state or {},
                })
                rows.insert(0, [("◀ Series", f"ser:f:{series_token}")])
            return self.panel(
                chat, message_id,
                "⚠️ Series name not changed: "
                + html.escape(report.get("error", "The rename is not valid.")), rows)
        if report.get("status") == "no_change":
            return self.panel(
                chat, message_id, "That is already the series name.",
                [[("📚 Library", "m:lib")]])

        lines = ["📝 <b>Confirm series change</b>",
                 f"<code>{html.escape(report.get('series_before', series))}</code> → "
                 f"<code>{html.escape(report.get('series', new_name))}</code>",
                 counted(report.get("count", 0), "book")]
        if report.get("merge"):
            lines.append("The destination series’ short name will be used.")
        for item in report.get("books", [])[:NAV_PAGE]:
            position = (item.get("series_index") + " · "
                        if item.get("series_index") else "")
            lines.append("· " + html.escape(position + item.get("title", "")))
        if report.get("count", 0) > NAV_PAGE:
            lines.append(f"… and {report['count'] - NAV_PAGE} more")
        token = self.tokens.put({
            "series": series, "new_name": report.get("series", new_name),
            "merge": bool(report.get("merge")),
            "expected_sha256s": report.get("expected_sha256s", {}),
            "library_state": library_state or {},
        })
        self.panel(
            chat, message_id, "\n".join(lines),
            [[("✓ Merge" if report.get("merge") else "✓ Rename",
              f"srn:apply:{token}"),
              ("No", "m:lib")]])

    def on_series_rename_callback(self, chat, rest: str, message_id=None) -> None:
        action, _, token = rest.partition(":")
        payload = self.tokens.get(token)
        if not isinstance(payload, dict):
            return self.stale(chat)
        if action == "merge":
            return self.submit(
                chat, lambda: self.preview_series_name(
                    chat, payload["series"], payload["new_name"], merge=True,
                    message_id=message_id,
                    library_state=payload.get("library_state")))
        if action != "apply":
            return self.stale(chat)
        self.panel(chat, message_id, "Updating every volume…")
        return self.submit(
            chat, lambda: self.apply_series_name(
                chat, payload, message_id=message_id))

    def apply_series_name(self, chat, payload: dict, *, message_id=None) -> None:
        report = suite.rename_series(
            self.workspace / "library", payload["series"], payload["new_name"],
            merge=payload.get("merge", False),
            expected_sha256s=payload.get("expected_sha256s", {}))
        if report.get("status") not in {"renamed", "no_change"}:
            return self.panel(
                chat, message_id,
                "⚠️ Series name not changed: "
                + html.escape(report.get("error", "The catalog changed.")),
                [[("📚 Library", "m:lib")]])
        try:
            lib = suite.library()
        except suite.SuiteError:
            lib = {"books": [], "series": []}
        fresh = {str(Path(book["path"])): book for book in lib.get("books", [])}
        replacements = {}
        for item in report.get("books", []):
            book = fresh.get(str(Path(item["to"])))
            if book:
                replacements[str(Path(item["from"]))] = book
        followed = self.queue.remap_books(replacements, invalidate_slim=True)
        group = next((item for item in lib.get("series", [])
                      if item.get("name", "").casefold()
                      == report.get("series", "").casefold()), None)
        rows = []
        if group:
            series_token = self.tokens.put({
                'key': group['key'], 'page': 0,
                'library_state': payload.get('library_state', {}),
            })
            rows.append([("📚 Open series", f"ser:f:{series_token}")])
        rows.append([("📚 Library", f"lib:v:{self.tokens.put(payload.get('library_state', {}))}"
                     if payload.get("library_state") else "m:lib")])
        verb = "Merged" if report.get("merge") else "Renamed"
        note = f"\nUpdated {followed} queued book(s)." if followed else ""
        self.panel(
            chat, message_id,
            f"✅ {verb} {counted(report.get('count', 0), 'book')} under "
            f"<b>{html.escape(report.get('series', payload['new_name']))}</b>."
            + note, rows)

    def on_library_callback(self, chat, rest: str, message_id=None) -> None:
        action, _, token = rest.partition(":")
        if action == "nop":
            return
        if action == "v":
            state = self.tokens.get(token)
            if not isinstance(state, dict):
                return self.stale(chat)
            return self.submit(
                chat, lambda: self.show_library(
                    chat, message_id=message_id, state=state))
        if action == "p":
            try:
                page = int(token)
            except ValueError:
                return self.stale(chat)
            return self.submit(
                chat, lambda: self.show_library(
                    chat, page, message_id=message_id))
        book = self.tokens.get(token)
        if not book:
            return self.stale(chat)
        # Older buttons in the scrollback carried a bare path; newer ones carry
        # the catalog record.
        book = book if isinstance(book, dict) else {"path": book}
        path = Path(book["path"])
        if action == "f":
            send = self.tokens.put({"path": str(path),
                                    "title": book.get("title") or path.stem,
                                    "author": book.get("author") or ""})
            library_state = book.get("_library_state") or {}
            library_back = (f"lib:v:{self.tokens.put(library_state)}"
                            if library_state else "m:lib")
            back = [("📚 Library", library_back)]
            if book.get("_series_key"):
                series_token = self.tokens.put({
                    "key": book["_series_key"], "page": book.get("_series_page", 0),
                    "library_state": library_state})
                back = [("◀ Series", f"ser:f:{series_token}"),
                        ("📚 Library", library_back)]
            return self.panel(
                chat, message_id, f"<code>{html.escape(path.name)}</code>",
                [[("📤 Send to device", f"bq:{send}")],
                 [("📝 Metadata", f"lib:meta:{token}")],
                 [("✏️ Rename file", f"lib:rn:{token}"),
                  ("🗑 Delete", f"lib:rm:{token}")],
                 back])
        if action == "meta":
            return self.submit(
                chat, lambda: self.show_book_metadata(
                    chat, path, book, message_id=message_id))
        if action == "rn":
            if self.pending:
                return self.panel(
                    chat, message_id,
                    "Finish the current question first, or send /cancel.")
            self.pending = {"kind": "librename", "path": str(path)}
            return self.panel(chat, message_id, "Send me the new filename.")
        if action == "rm":
            return self.panel(
                chat, message_id,
                f"Delete <code>{html.escape(path.name)}</code> from the server?",
                [[("Yes, delete", f"lib:rm!:{token}"),
                  ("No", f"lib:f:{token}")]])
        if action == "rm!":
            if not inside(self.workspace, path):
                return self.panel(
                    chat, message_id, "⚠️ that is outside the workspace.")
            path.unlink(missing_ok=True)
            library_state = book.get("_library_state") or {}
            library_back = (f"lib:v:{self.tokens.put(library_state)}"
                            if library_state else "m:lib")
            return self.panel(chat, message_id, "🗑 gone.",
                              [[("📚 Library", library_back)]])

    # -- catalog metadata -------------------------------------------------

    META_LABELS = {
        "title": "Title", "author": "Author", "series": "Series",
        "series_index": "Series position", "language": "Language",
    }
    META_OPTIONAL = {"author", "series", "series_index"}

    def show_book_metadata(self, chat, path: Path, back_book: dict = None,
                           *, message_id=None) -> None:
        """Telegram view over the OPDS ingester's five-field editor.

        Keep the conversational wording aligned with
        ``ingest_book.edit_interactive``. This method must only display the
        typed report and collect an answer; it must never duplicate metadata
        validation or EPUB-writing rules from the catalog scripts.
        """
        catalog = self.workspace / "library"
        report = suite.book_metadata(path, catalog)
        if report.get("status") == "invalid":
            return self.panel(
                chat, message_id, "⚠️ " + html.escape(
                    report.get("error", "That book cannot be inspected.")),
                [[("📚 Library", "m:lib")]])
        values = report.get("metadata", {})
        back_book = back_book or {
            "path": report["source"], "title": report.get("title", ""),
            "author": values.get("author", ""),
        }
        lines = ["📝 <b>Embedded metadata</b>",
                 f"file: <code>{html.escape(Path(report['source']).name)}</code>",
                 f"EPUB package: {html.escape(report.get('package_version', '?'))}", ""]
        for field in ("title", "author", "series", "series_index", "language"):
            lines.append(f"{self.META_LABELS[field]}: "
                         f"<code>{html.escape(values.get(field) or 'empty')}</code>")
        if values.get("series"):
            lines.append("Short name: <code>" + html.escape(
                report.get("series_alias") or "not set") + "</code>")
        for warning in report.get("warnings", []):
            lines.append("⚠️ " + html.escape(warning))

        rows = []
        if report.get("editable", True):
            fields = ["title", "author", "language"]
            for start in range(0, len(fields), 2):
                row = []
                for field in fields[start:start + 2]:
                    token = self.tokens.put({"path": report["source"], "field": field,
                                             "current": values.get(field, ""),
                                             "back_book": back_book})
                    row.append((f"✏️ {self.META_LABELS[field]}", f"bm:e:{token}"))
                rows.append(row)
            series_action = "↔ Change series" if values.get("series") \
                else "➕ Add to series"
            picker = self.tokens.put({"path": report["source"],
                                      "back_book": back_book})
            rows.append([(series_action, f"bm:s:{picker}")])
            if values.get("series"):
                position = self.tokens.put({
                    "path": report["source"], "field": "series_index",
                    "current": values.get("series_index", ""),
                    "back_book": back_book,
                })
                rows.append([("✏️ Series position", f"bm:e:{position}")])
            if report.get("status") == "needs_alias":
                token = self.tokens.put({"path": report["source"], "updates": {},
                                         "back_book": back_book})
                rows.append([("🏷 Set short series name", f"bm:a:{token}")])
        back = self.tokens.put(back_book)
        rows.append([("◀ Book", f"lib:f:{back}"), ("📚 Library", "m:lib")])
        self.panel(chat, message_id, "\n".join(lines), rows)

    def show_series_picker(self, chat, path: Path, back_book: dict = None,
                           *, message_id=None, state: dict | None = None) -> None:
        """Choose existing series metadata without making the user retype it."""
        report = suite.book_metadata(path, self.workspace / "library")
        if report.get("status") == "invalid":
            return self.panel(
                chat, message_id,
                "⚠️ " + html.escape(report.get("error", "Book not readable.")))
        current = report.get("metadata", {}).get("series", "")
        try:
            groups = suite.library().get("series", [])
        except suite.SuiteError as exc:
            return self.panel(chat, message_id, f"⚠️ {exc}")
        groups = sorted(groups, key=lambda group: group.get("name", "").casefold())
        state = dict(state or {"initial": "", "page": 0})
        initial = state.get("initial", "")
        buckets = {self.initial_bucket(group.get("name", "")) for group in groups}
        if initial not in buckets:
            initial = ""
        filtered = [group for group in groups
                    if not initial
                    or self.initial_bucket(group.get("name", "")) == initial]
        last_page = max(0, (len(filtered) - 1) // NAV_PAGE)
        page = max(0, min(int(state.get("page", 0)), last_page))
        state = {"initial": initial, "page": page}
        base = {"path": str(path), "back_book": back_book or {"path": str(path)}}

        def view(**changes):
            return f"bm:sv:{self.tokens.put({**base, 'state': {**state, **changes}})}"

        rows = []
        if len(groups) > ALPHABET_THRESHOLD:
            rows.extend(self.alphabet_rows(
                [group.get("name", "") for group in groups], initial,
                lambda bucket: view(initial=bucket, page=0)))
        start = page * NAV_PAGE
        for group in filtered[start:start + NAV_PAGE]:
            payload = {**base, "series": group.get("name", ""),
                       "alias": group.get("alias", "")}
            mark = "✓ " if group.get("name", "").casefold() == current.casefold() else ""
            rows.append([((mark + group.get("name", ""))[:52],
                          f"bm:sp:{self.tokens.put(payload)}")])
        navigation = []
        if page:
            navigation.append(("‹", view(page=page - 1)))
        if last_page:
            navigation.append((f"{page + 1}/{last_page + 1}", "bm:nop:"))
        if page < last_page:
            navigation.append(("›", view(page=page + 1)))
        if navigation:
            rows.append(navigation)
        rows.append([("＋ New series", f"bm:sn:{self.tokens.put(base)}")])
        if current:
            rows.append([("✕ Remove from series", f"bm:sr:{self.tokens.put(base)}")])
        rows.append([("◀ Metadata", f"bm:show:{self.tokens.put(base)}")])
        shown = f" · {initial}" if initial else ""
        page_line = f"\nPage {page + 1} of {last_page + 1}" if last_page else ""
        self.panel(
            chat, message_id,
            f"📚 <b>Choose a series</b>{shown}{page_line}\n"
            + (f"Current: <code>{html.escape(current)}</code>" if current
               else "This book is currently standalone."),
            rows)

    def accept_book_series_name(self, chat, path: Path, series: str,
                                back_book: dict, *, message_id=None) -> None:
        series = " ".join((series or "").split())
        if not series:
            self.pending = {"kind": "bookseriesname", "path": str(path),
                            "back_book": back_book, "message_id": message_id}
            return self.panel(chat, message_id,
                              "That name was empty. Send the full series name.")
        try:
            groups = suite.library().get("series", [])
        except suite.SuiteError as exc:
            return self.panel(chat, message_id, f"⚠️ {exc}")
        existing = next(
            (group for group in groups
             if group.get("name", "").casefold() == series.casefold()), None)
        if existing:
            canonical = existing.get("name", series)
            if existing.get("alias"):
                return self.ask_series_position(
                    chat, path, canonical, existing["alias"], back_book,
                    message_id=message_id)
            series = canonical
        self.ask_series_alias_for_book(
            chat, path, series, back_book, message_id=message_id)

    def ask_series_alias_for_book(self, chat, path: Path, series: str,
                                  back_book: dict, *, message_id=None) -> None:
        preview = suite.edit_book_metadata(
            path, self.workspace / "library",
            {"series": series, "series_index": ""}, dry_run=True)
        if preview.get("status") == "needs_alias":
            suggestion = preview.get("suggested_alias") or "SERIES"
            self.pending = {
                "kind": "bookseriesalias", "path": str(path), "series": series,
                "back_book": back_book, "message_id": message_id,
                "suggestion": suggestion,
            }
            accept = self.tokens.put({
                "path": str(path), "series": series, "alias": suggestion,
                "back_book": back_book,
            })
            return self.panel(
                chat, message_id,
                f"Send the short name for <b>{html.escape(series)}</b> "
                "(maximum 6 characters), or accept the mechanical suggestion.",
                [[(f"Use {suggestion}", f"bm:sa:{accept}")],
                 [("✗ Cancel", f"bm:s:{self.tokens.put({'path': str(path), 'back_book': back_book})}")]])
        if preview.get("status") in {"invalid", "conflict", "error"}:
            return self.panel(
                chat, message_id,
                "⚠️ " + html.escape(preview.get("error", "That series is not valid.")),
                [[("◀ Series", f"bm:s:{self.tokens.put({'path': str(path), 'back_book': back_book})}")]])
        alias = preview.get("series_alias", "")
        self.ask_series_position(
            chat, path, series, alias, back_book, message_id=message_id)

    def accept_book_series_alias(self, chat, path: Path, series: str, alias: str,
                                 back_book: dict, *, message_id=None) -> None:
        preview = suite.edit_book_metadata(
            path, self.workspace / "library",
            {"series": series, "series_index": ""}, alias=alias, dry_run=True)
        if preview.get("status") in {"invalid", "conflict", "error", "needs_alias"}:
            suggestion = preview.get("suggested_alias") or alias or "SERIES"
            self.pending = {
                "kind": "bookseriesalias", "path": str(path), "series": series,
                "back_book": back_book, "message_id": message_id,
                "suggestion": suggestion,
            }
            return self.panel(
                chat, message_id,
                "⚠️ " + html.escape(preview.get("error", "That short name did not work."))
                + "\nSend another short name.",
                [[(f"Use {suggestion}", f"bm:sa:{self.tokens.put({'path': str(path), 'series': series, 'alias': suggestion, 'back_book': back_book})}")],
                 [("✗ Cancel", f"bm:s:{self.tokens.put({'path': str(path), 'back_book': back_book})}")]])
        self.pending = None
        self.ask_series_position(
            chat, path, series, preview.get("series_alias") or alias, back_book,
            message_id=message_id)

    def ask_series_position(self, chat, path: Path, series: str, alias: str,
                            back_book: dict, *, message_id=None) -> None:
        payload = {"path": str(path), "series": series, "alias": alias,
                   "back_book": back_book}
        self.pending = {"kind": "bookseriesposition", **payload,
                        "message_id": message_id}
        self.panel(
            chat, message_id,
            f"What is this book’s position in <b>{html.escape(series)}</b>?\n\n"
            "Send a number such as <code>1</code> or <code>1.5</code>.",
            [[("No position", f"bm:px:{self.tokens.put(payload)}")],
             [("✗ Cancel", f"bm:s:{self.tokens.put({'path': str(path), 'back_book': back_book})}")]])

    def on_book_metadata_callback(self, chat, rest: str, message_id=None) -> None:
        action, _, token = rest.partition(":")
        if action == "nop":
            return
        payload = self.tokens.get(token)
        if not payload:
            return self.stale(chat)
        path = Path(payload["path"])
        back_book = payload.get("back_book") or {"path": str(path)}
        if (action in {"e", "c", "a", "pa", "apply", "s", "sv", "sp",
                       "sn", "sr", "sa", "px"} and self.pending
                and not self.pending.get("kind", "").startswith("book")):
            return self.panel(
                chat, message_id,
                "Finish the current question first, or send /cancel.")
        if action == "show":
            if self.pending and self.pending.get("kind", "").startswith("book"):
                self.pending = None
            return self.submit(
                chat, lambda: self.show_book_metadata(
                    chat, path, back_book, message_id=message_id))
        if action == "s":
            if self.pending and self.pending.get("kind", "").startswith("book"):
                self.pending = None
            return self.submit(
                chat, lambda: self.show_series_picker(
                    chat, path, back_book, message_id=message_id))
        if action == "sv":
            return self.submit(
                chat, lambda: self.show_series_picker(
                    chat, path, back_book, message_id=message_id,
                    state=payload.get("state", {})))
        if action == "sp":
            series = payload.get("series", "")
            if not series:
                return self.stale(chat)
            if payload.get("alias"):
                return self.ask_series_position(
                    chat, path, series, payload["alias"], back_book,
                    message_id=message_id)
            return self.submit(
                chat, lambda: self.ask_series_alias_for_book(
                    chat, path, series, back_book, message_id=message_id))
        if action == "sn":
            self.pending = {
                "kind": "bookseriesname", "path": str(path),
                "back_book": back_book, "message_id": message_id,
            }
            return self.panel(
                chat, message_id, "Send the full name of the new series.",
                [[("✗ Cancel", f"bm:s:{self.tokens.put({'path': str(path), 'back_book': back_book})}")]])
        if action == "sr":
            self.pending = None
            return self.submit(
                chat, lambda: self.preview_book_metadata(
                    chat, path, {"series": "", "series_index": ""},
                    message_id=message_id, back_book=back_book))
        if action == "sa":
            self.pending = None
            return self.submit(
                chat, lambda: self.accept_book_series_alias(
                    chat, path, payload.get("series", ""),
                    payload.get("alias", ""), back_book,
                    message_id=message_id))
        if action == "px":
            self.pending = None
            return self.submit(
                chat, lambda: self.preview_book_metadata(
                    chat, path,
                    {"series": payload.get("series", ""), "series_index": ""},
                    alias=payload.get("alias", ""), message_id=message_id,
                    back_book=back_book, retry_position=payload))
        if action == "e":
            if self.pending:
                return self.panel(
                    chat, message_id,
                    "Finish the current question first, or send /cancel.")
            field = payload["field"]
            if field == "series":       # buttons from before the picker existed
                return self.submit(
                    chat, lambda: self.show_series_picker(
                        chat, path, back_book, message_id=message_id))
            self.pending = {"kind": "bookmetadata", "path": str(path),
                            "field": field, "message_id": message_id,
                            "back_book": back_book}
            current = payload.get("current") or "empty"
            rows = []
            if field in self.META_OPTIONAL and payload.get("current"):
                clear = self.tokens.put({"path": str(path), "field": field,
                                         "back_book": back_book})
                rows.append([("Clear this field", f"bm:c:{clear}")])
            back = self.tokens.put({"path": str(path), "back_book": back_book})
            rows.append([("✗ Cancel", f"bm:show:{back}")])
            return self.panel(
                chat, message_id,
                f"Send the new <b>{html.escape(self.META_LABELS[field])}</b>.\n"
                f"Current value: <code>{html.escape(current)}</code>\n\n"
                "I will show the exact metadata and filename change before writing it.",
                rows)
        if action == "c":
            self.pending = None
            return self.submit(
                chat, lambda: self.preview_book_metadata(
                    chat, path, {payload["field"]: ""}, message_id=message_id,
                    back_book=back_book))
        if action == "a":
            return self.submit(
                chat, lambda: self.preview_book_metadata(
                    chat, path, payload.get("updates", {}), message_id=message_id,
                    back_book=back_book))
        if action == "pa":
            self.pending = None
            return self.submit(
                chat, lambda: self.preview_book_metadata(
                    chat, path, payload.get("updates", {}), payload.get("alias", ""),
                    message_id=message_id, back_book=back_book))
        if action == "apply":
            self.pending = None
            return self.submit(
                chat, lambda: self.apply_book_metadata(
                    chat, path, payload.get("updates", {}), payload.get("alias", ""),
                    payload.get("sha256", ""), message_id=message_id,
                    back_book=back_book))

    def preview_book_metadata(self, chat, path: Path, updates: dict,
                              alias: str = "", alias_prompt=None, *,
                              message_id=None, back_book: dict = None,
                              retry_position: dict = None) -> None:
        report = suite.edit_book_metadata(
            path, self.workspace / "library", updates, alias=alias, dry_run=True)
        back_book = back_book or {"path": str(path)}
        if report.get("status") == "needs_alias":
            suggestion = report.get("suggested_alias") or "SERIES"
            self.pending = {"kind": "bookmetaalias", "path": str(path),
                            "updates": updates, "series": report.get("series", ""),
                            "suggestion": suggestion, "message_id": message_id,
                            "back_book": back_book}
            token = self.tokens.put({"path": str(path), "updates": updates,
                                     "alias": suggestion,
                                     "back_book": back_book})
            return self.panel(
                chat, message_id,
                f"<b>{html.escape(report.get('series', 'This series'))}</b> has "
                "no short catalog name yet. Send one (maximum 6 characters), "
                "or accept the mechanical suggestion.",
                [[(f"Use {suggestion}", f"bm:pa:{token}")],
                 [("✗ Cancel", f"bm:show:{self.tokens.put({'path': str(path), 'back_book': back_book})}")]])
        if report.get("status") in {"invalid", "conflict", "error"}:
            if retry_position:
                self.pending = {"kind": "bookseriesposition", **retry_position,
                                "message_id": message_id}
                return self.panel(
                    chat, message_id,
                    "⚠️ " + html.escape(
                        report.get("error", "That position is not valid."))
                    + "\nSend a number such as 1 or 1.5.",
                    [[("No position", f"bm:px:{self.tokens.put(retry_position)}")],
                     [("✗ Cancel", f"bm:s:{self.tokens.put({'path': str(path), 'back_book': back_book})}")]])
            if alias_prompt:
                series, suggestion = alias_prompt
                self.pending = {"kind": "bookmetaalias", "path": str(path),
                                "updates": updates, "series": series,
                                "suggestion": suggestion, "message_id": message_id,
                                "back_book": back_book}
                token = self.tokens.put({"path": str(path), "updates": updates,
                                         "alias": suggestion,
                                         "back_book": back_book})
                return self.panel(
                    chat, message_id,
                    "⚠️ " + html.escape(
                        report.get("error", "That short name did not work."))
                    + "\nSend another short name.",
                    [[(f"Use {suggestion}", f"bm:pa:{token}"),
                      ("✗ Cancel", f"bm:show:{self.tokens.put({'path': str(path), 'back_book': back_book})}")]])
            return self.panel(
                chat, message_id, "⚠️ Nothing changed. "
                + html.escape(report.get("error", "The edit is not valid.")),
                [[("📝 Metadata", f"bm:show:{self.tokens.put({'path': str(path), 'back_book': back_book})}")]])

        lines = ["📝 <b>Confirm metadata edit</b>"]
        for field in report.get("changed_fields", []):
            before = report["metadata_before"].get(field) or "empty"
            after = report["metadata_after"].get(field) or "empty"
            lines.append(f"{self.META_LABELS[field]}: "
                         f"<code>{html.escape(before)}</code> → "
                         f"<code>{html.escape(after)}</code>")
        if report.get("renames"):
            lines.append("File: <code>" + html.escape(path.name) + "</code> → "
                         "<code>" + html.escape(Path(report["destination"]).name)
                         + "</code>")
        if report.get("alias_changed"):
            lines.append("Short series name: <code>" +
                         html.escape(report.get("series_alias", "")) + "</code>")
        if not report.get("changed_fields") and not report.get("renames") \
                and not report.get("alias_changed"):
            return self.panel(
                chat, message_id,
                "That already has the requested value — nothing to write.",
                [[("📝 Metadata", f"bm:show:{self.tokens.put({'path': str(path), 'back_book': back_book})}")]])
        token = self.tokens.put({"path": str(path), "updates": updates, "alias": alias,
                                 "sha256": report.get("sha256", ""),
                                 "back_book": back_book})
        back = self.tokens.put({"path": str(path), "back_book": back_book})
        self.panel(chat, message_id, "\n".join(lines),
                   [[("✓ Write it", f"bm:apply:{token}"),
                     ("No", f"bm:show:{back}")]])

    def apply_book_metadata(self, chat, path: Path, updates: dict,
                            alias: str = "", expected_sha256: str = "", *,
                            message_id=None, back_book: dict = None) -> None:
        report = suite.edit_book_metadata(
            path, self.workspace / "library", updates, alias=alias,
            expected_sha256=expected_sha256)
        if report.get("status") not in {"updated", "no_change"}:
            return self.panel(
                chat, message_id, "⚠️ Nothing changed. "
                + html.escape(report.get("error", "The catalog changed since the preview.")),
                [[("📚 Library", "m:lib")]])
        destination = Path(report.get("destination") or path)
        values = report.get("metadata_after", {})
        fresh_book = {
            "path": str(destination),
            "title": report.get("title") or values.get("title") or destination.stem,
            "base_title": report.get("base_title") or values.get("title", ""),
            "author": values.get("author", ""),
            "language": values.get("language", ""),
            "series": values.get("series", ""),
            "series_index": values.get("series_index", ""),
            "series_alias": report.get("series_alias", ""),
        }
        followed = self.queue.remap_books(
            {str(path): fresh_book}, invalidate_slim=True)
        fields = ", ".join(self.META_LABELS.get(field, field)
                           for field in report.get("changed_fields", [])) or "filename"
        token = self.tokens.put({"path": str(destination),
                                 "back_book": {"path": str(destination)}})
        queued_note = ""
        if followed:
            noun = "copy" if followed == 1 else "copies"
            queued_note = f"\nUpdated {followed} queued {noun}."
        self.panel(
            chat, message_id,
            f"✅ Updated {html.escape(fields)}.\n"
            f"catalog file: <code>{html.escape(destination.name)}</code>"
            + queued_note,
            [[("📝 Metadata", f"bm:show:{token}"), ("📚 Library", "m:lib")]])

    def show_queue(self, chat) -> None:
        items = self.queue.items()
        if not items:
            return self.say(chat, "📤 Queue empty. Nothing is waiting for the reader.",
                            [[("🏠 Menu", "m:main")]])
        icons = {"book": "📕", "font": "🔤"}
        rows = [[(f"✗ {icons.get(i.get('kind'), '🖼')} {i['label'][:28]}",
                  f"qdel:{i['id']}")] for i in items]
        rows.append([("📲 Push now", "push:ask"), ("Clear all", "qclr:")])
        rows.append([("🏠 Menu", "m:main")])
        self.say(chat, f"📤 {len(items)} waiting to go to the reader:", rows)

    def show_inbox(self, chat) -> None:
        inbox = self.workspace / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        files = sorted(p for p in inbox.iterdir() if p.is_file())
        if not files:
            return self.say(
                chat,
                "📥 Inbox empty.\n\nDrop anything too big for Telegram into "
                "<code>workspace/inbox/</code> on the server and it shows up here.",
                [[("🏠 Menu", "m:main")]])
        rows = [[(f"{p.name[:30]} ({human(p.stat().st_size)})",
                  f"in:{self.tokens.put(str(p))}")] for p in files[:20]]
        rows.append([("🏠 Menu", "m:main")])
        self.say(chat, "📥 Tap one to process it:", rows)

    def show_status(self, chat) -> None:
        opds = suite.opds_up(self.cfg["opds_url"])
        ready, why = suite.graded_reader_ready()
        try:
            count = suite.library().get("count", "?")
        except suite.SuiteError:
            count = "?"
        last = self.notes.get("last_push")
        lines = [
            "⚙️ <b>Status</b>",
            f"catalog: {'up' if opds else 'not answering'} ({self.cfg['opds_url']})",
            f"books served: {count}",
            f"queue: {len(self.queue)} waiting",
            f"graded-reader AI: {'configured' if ready else 'not configured'}",
            f"pdf2epub runner: {'present' if suite.pdf2epub_runner() else 'not built yet'}",
            f"last push: {last or 'never'}",
        ]
        if not ready:
            lines.append(f"\n{html.escape(why)}")
        self.say(chat, "\n".join(lines), [[("🏠 Menu", "m:main")]])

    # -- the wallpaper collection ------------------------------------------

    PAGE = 24                      # 8 x 3 cells, one comfortable phone-sized sheet

    def sheet_rows(self, items: list, prefix: str, start: int = 1) -> list:
        """Numbered buttons that line up with the numbers drawn on the sheet.

        Five to a row: enough to reach across a phone without the digits
        becoming a puzzle, and it divides 24 into neat rows of five.
        """
        rows, row = [], []
        for i, payload in enumerate(items):
            row.append((str(start + i), f"{prefix}{self.tokens.put(payload)}"))
            if len(row) == 5:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        return rows

    def show_wallpapers(self, chat, page: int = 0) -> None:
        """Everything built here, as one picture you can point at.

        The counterpart of 📚 Library and 🔤 Fonts: a collection that lives on
        the server, browsable and re-sendable. Pushing one never removed the
        file, so this is also the answer to "the card got wiped, put them all
        back" — which is exactly when you want to see thirty at once rather
        than scroll thirty messages.
        """
        walls = suite.local_wallpapers()
        if not walls:
            return self.say(
                chat,
                "🖼 No wallpapers built yet.\n\nSend me a picture and it "
                "becomes one — they collect here afterwards.",
                [[("📲 On the device", "wl:dev:")], [("🏠 Menu", "m:main")]])

        pages = (len(walls) + self.PAGE - 1) // self.PAGE
        page = max(0, min(page, pages - 1))
        shown = walls[page * self.PAGE:(page + 1) * self.PAGE]
        first = page * self.PAGE + 1

        sheet = self.state_dir / "cache" / f"wallpapers-{page}.png"
        report = suite.contact_sheet([w["path"] for w in shown], sheet, start=first)

        queued = {i["path"] for i in self.queue.items()}
        lines = [f"🖼 <b>{len(walls)} wallpaper(s)</b>"
                 + (f" — page {page + 1} of {pages}" if pages > 1 else "")]
        for n, w in enumerate(shown, first):
            mark = " 📤" if w["path"] in queued else ""
            lines.append(f"{n} {html.escape(w['name'][:34])}{mark}")

        sent = self.send_preview(chat, Path(report["png"]),
                                 "\n".join(lines)[:1000],
                                 self.sheet_keyboard(shown, page, pages, first))
        if sent and sent.get("message_id"):
            self.sheets[sent["message_id"]] = {"walls": shown, "page": page,
                                               "pages": pages, "start": first}

    def sheet_keyboard(self, shown: list, page: int, pages: int, first: int,
                       picking: bool = False) -> list:
        """The buttons under a sheet, in either of its two moods.

        Browsing: a number opens that wallpaper. Picking: a number ticks it,
        and the same numbers stay in the same places — the point of ticking off
        a contact sheet is that your eye stays on the picture while your thumb
        works down the row.
        """
        rows, row = [], []
        for i, w in enumerate(shown):
            n = first + i
            if picking:
                ticked = w["path"] in self.selected
                row.append((f"{'☑' if ticked else '☐'}{n}", f"wl:t:{n}"))
            else:
                row.append((str(n), f"wl:one:{self.tokens.put(w)}"))
            if len(row) == 5:
                rows.append(row)
                row = []
        if row:
            rows.append(row)

        if picking:
            here = {w["path"] for w in shown}
            rows.append([(f"🗑 Delete {len(self.selected)}", "wl:del:")]
                        if self.selected else [("Tap the numbers to pick", "wl:nop:")])
            rows.append([("All", "wl:all:"), ("None", "wl:none:"),
                         ("✖ Done", "wl:browse:")]
                        if here else [("✖ Done", "wl:browse:")])
            return rows

        nav = []
        if page:
            nav.append(("◀ Back", f"wl:page:{page - 1}"))
        if page + 1 < pages:
            nav.append(("More ▶", f"wl:page:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([("☑ Pick several", "wl:pick:"),
                     ("📲 On the device", "wl:dev:")])
        rows.append([("🏠 Menu", "m:main")])
        return rows

    def redraw_sheet(self, chat, msg_id, picking: bool) -> bool:
        """Swap the keyboard under a sheet that is already on screen."""
        sheet = self.sheets.get(msg_id)
        if not sheet:
            return False
        self.tg.edit_markup(chat, msg_id,
                            self.sheet_keyboard(sheet["walls"], sheet["page"],
                                                sheet["pages"], sheet["start"],
                                                picking))
        return True

    def on_wallpaper_callback(self, chat, rest: str, msg_id=None) -> None:
        action, _, token = rest.partition(":")

        # -- picking several off the sheet ---------------------------------
        if action == "nop":
            return
        if action in ("pick", "browse", "all", "none", "t"):
            sheet = self.sheets.get(msg_id)
            if not sheet:
                return self.stale(chat)
            here = [w["path"] for w in sheet["walls"]]
            if action == "pick":
                self.selected.clear()
            elif action == "browse":
                self.selected.clear()
            elif action == "all":
                self.selected.update(here)
            elif action == "none":
                self.selected.difference_update(here)
            else:                                   # a single number toggled
                try:
                    path = sheet["walls"][int(token) - sheet["start"]]["path"]
                except (ValueError, IndexError):
                    return self.stale(chat)
                self.selected.symmetric_difference_update({path})
            return self.redraw_sheet(chat, msg_id, action != "browse")

        if action == "del":
            if not self.selected:
                return self.say(chat, "Nothing picked.")
            names = sorted(Path(p).name for p in self.selected)
            listed = "\n".join(f"· {html.escape(n)}" for n in names[:15])
            more = f"\n… and {len(names) - 15} more" if len(names) > 15 else ""
            return self.say(
                chat,
                f"Delete <b>{len(names)}</b> wallpaper(s) from the server?\n"
                f"{listed}{more}\n\n"
                f"Any already on the reader stay there.",
                [[("Yes, delete them", f"wl:del!:{msg_id or 0}"),
                  ("No", "wl:browse:")]])

        if action == "del!":
            gone, kept = 0, []
            for p in sorted(self.selected):
                path = Path(p)
                if not inside(self.workspace, path):
                    kept.append(path.name)
                    continue
                path.unlink(missing_ok=True)
                path.with_suffix(".png").unlink(missing_ok=True)
                gone += 1
            self.selected.clear()
            self.sheets.pop(int(token) if token.isdigit() else None, None)
            note = (f"\n⚠️ left alone, outside the workspace: "
                    f"{html.escape(', '.join(kept))}" if kept else "")
            self.say(chat, f"🗑 {gone} deleted.{note}")
            return self.submit(chat, lambda: self.show_wallpapers(chat))

        if action == "page":
            return self.submit(
                chat, lambda: self.show_wallpapers(chat, int(token or 0)))

        if action == "rn":
            wall = self.tokens.get(token)
            if not wall:
                return self.stale(chat)
            self.pending = {"kind": "wlrename", "path": wall["path"]}
            return self.say(chat, f"Send me the new name for\n"
                                  f"<code>{html.escape(Path(wall['path']).name)}</code>"
                                  f"\n\n(the <code>.bmp</code> is added if you "
                                  f"leave it off)")

        if action == "dev":
            def work():
                host, _ = self.device_host()
                self.browse(chat, suite.sleep_dir(host))
            return self.submit(chat, work)

        wall = self.tokens.get(token)
        if not wall:
            return self.stale(chat)
        path = Path(wall["path"])

        if action == "one":
            def work():
                # A wallpaper built before --preview existed, or by the CLI, has
                # no PNG beside it. Render one the way the panel would draw it
                # rather than showing nothing.
                png = Path(wall["png"]) if wall.get("png") else \
                    path.with_suffix(".png")
                if not png.exists():
                    suite.bmp_preview(path, png)
                queued = any(i["path"] == str(path) for i in self.queue.items())
                self.send_preview(
                    chat, png,
                    f"<b>{html.escape(path.name)}</b>\n{human(wall['bytes'])}"
                    + ("\n📤 already queued" if queued else ""),
                    [[("📤 Queue for the device", f"wl:q:{token}")],
                     [("✏️ Rename", f"wl:rn:{token}"),
                      ("🗑 Delete", f"wl:rm:{token}")],
                     [("🖼 Wallpapers", "m:wl")]])
            return self.submit(chat, work)

        if action == "q":
            if any(i["path"] == str(path) for i in self.queue.items()):
                return self.say(chat, "Already in the queue.",
                                [[("📲 Push now", "push:ask"),
                                  ("🖼 Wallpapers", "m:wl")]])
            self.queue.add("wallpaper", str(path))
            return self.say(chat, f"📤 queued — {len(self.queue)} waiting.",
                            [[("📲 Push now", "push:ask"),
                              ("🖼 Wallpapers", "m:wl")]])

        if action == "rm":
            return self.say(chat, f"Delete <code>{html.escape(path.name)}</code> "
                                  f"from the server? It stays on the reader if "
                                  f"you already sent it.",
                            [[("Yes, delete", f"wl:rm!:{token}"),
                              ("No", f"wl:one:{token}")]])
        if action == "rm!":
            if not inside(self.workspace, path):
                return self.say(chat, "⚠️ that is outside the workspace.")
            path.unlink(missing_ok=True)
            path.with_suffix(".png").unlink(missing_ok=True)
            return self.say(chat, "🗑 gone.", [[("🖼 Wallpapers", "m:wl")]])

        log("unhandled wallpaper callback:", action, token)
        self.say(chat, "That button did nothing — please tell Claude.")

    # -- fonts -------------------------------------------------------------

    def show_fonts(self, chat) -> None:
        families = suite.local_font_families()
        if not families:
            return self.say(chat, "No families in <code>extras/fonts/</code>.",
                            [[("🏠 Menu", "m:main")]])
        queued = {i["path"] for i in self.queue.items() if i.get("kind") == "font"}
        rows = [[(f"{f['name']} · {human(f['bytes'])}"
                  + (" 📤" if f["path"] in queued else ""),
                  f"fo:one:{self.tokens.put(f)}")] for f in families]
        rows.append([("📲 On the device", "fo:dev:")])
        rows.append([("🏠 Menu", "m:main")])
        self.say(chat, "🔤 Families in this repo:", rows)

    def on_font_callback(self, chat, rest: str) -> None:
        action, _, token = rest.partition(":")

        if action == "dev":
            return self.submit(chat, lambda: self.show_device_fonts(chat))

        family = self.tokens.get(token)
        if not family:
            return self.stale(chat)

        if action == "one":
            if family["ui_ready"]:
                warn = ""
            elif family["cjk"]:
                warn = ("\n\n⚠️ No 8/10/12 pt — books will render, but the "
                        "chapter list and library will draw <b>blank</b> for "
                        "CJK titles.")
            else:
                # The interface fallback probes 一/あ/ア/가 and skips a family
                # that has none of them, so this one would never be asked for
                # those sizes. Saying nothing would leave the sizes line
                # looking short; saying "missing" would be wrong.
                warn = ("\n\nNo 8/10/12 pt, and none needed — those are for the "
                        "CJK interface fallback, which this family does not "
                        "trigger. The built-in menu fonts draw the rest.")
            queued = any(i["path"] == family["path"] and i.get("kind") == "font"
                         for i in self.queue.items())
            minutes = ("\n\nKeep the reader on the File Transfer screen "
                       f"until it finishes — it is {human(family['bytes'])}."
                       if family["bytes"] > 4_000_000 else "")
            send_row = [("📤 Send now", f"fo:send:{token}")]
            if not queued:
                send_row.insert(0, ("✓ Queue it", f"fo:q:{token}"))
            return self.say(
                chat,
                f"🔤 <b>{html.escape(family['name'])}</b>\n"
                f"{', '.join(str(s) for s in family['sizes'])} pt · "
                f"{len(family['files'])} files · {human(family['bytes'])}"
                f"{warn}{minutes}"
                + ("\n\n📤 Already in the queue." if queued else ""),
                [send_row, [("📤 Queue", "m:q"), ("🔤 Fonts", "m:fo")]])

        if action == "q":
            # Same promise as a wallpaper: queued now, sent when you are next
            # in front of the reader. Twice is a slip, not an instruction.
            if any(i["path"] == family["path"] and i.get("kind") == "font"
                   for i in self.queue.items()):
                return self.say(chat, "Already in the queue.",
                                [[("📤 Queue", "m:q"), ("🔤 Fonts", "m:fo")]])
            self.queue.add("font", family["path"], label=family["name"],
                           meta={"family": family["name"]})
            return self.say(
                chat,
                f"🔤 queued — {len(self.queue)} waiting.\n"
                f"{family['name']} goes across on the next push, and is "
                f"verified and selected there.",
                [[("📲 Push now", "push:ask"), ("🏠 Menu", "m:main")]])

        if action == "send":
            return self.submit(chat, lambda: self.send_font(chat, family))

    def send_font(self, chat, family: dict) -> None:
        """The immediate path: the reader is in front of you, send it now."""
        host, _ = self.device_host()
        _, lines = self.push_font_family(chat, host, family)
        self.say(chat, "\n".join(lines), [[("🔤 Fonts", "m:fo")],
                                          [("🏠 Menu", "m:main")]])

    def push_font_family(self, chat, host, family: dict):
        """Push a family file by file, then check the device really has it.

        The verify is the point. The reader lists fonts by filename and never
        opens them, so a short write shows up in the picker and only fails when
        selected — at which point the family quietly reverts to built-in Noto
        and looks like a bad font. Comparing byte counts against CHECKSUMS.tsv
        turns that into a line in a chat message.

        Shared by the two ways a font travels — sent now, or drained from the
        queue later — so "sending a font" means one thing, verify and select
        included, whichever button you pressed. Returns (landed, report lines);
        the queue reads the flag, the chat gets the lines.
        """
        files = [Path(f) for f in family["files"]]
        name = family["name"]
        self.say(chat, f"📤 {name} → {host}\n{len(files)} files, "
                       f"{human(family['bytes'])}. Starting…")

        failed = []
        for n, path in enumerate(files, 1):
            try:
                # Timed, because this repo carried a "minutes over WiFi" claim
                # for months that nobody had measured and that turned out to be
                # wrong. One number per file settles it for good.
                started = time.monotonic()
                suite.device.upload_font(host, name, path)
                took = time.monotonic() - started
                self.say(chat, f"  {n}/{len(files)} ✅ {html.escape(path.name)} "
                               f"({human(path.stat().st_size)}, {took:.1f}s)")
            except suite.DeviceError as exc:
                failed.append(path.name)
                self.say(chat, f"  {n}/{len(files)} ❌ {html.escape(path.name)} — "
                               f"{html.escape(str(exc)[:120])}")

        report = suite.verify_font_family(host, name)
        bad = [r for r in report if not r["ok"]]
        lines = ["", "<b>Verified against CHECKSUMS.tsv</b>"]
        if not report:
            lines.append("⚠️ the reader lists no files for this family at all.")
        elif bad:
            for r in bad:
                lines.append(f"❌ {html.escape(r['name'])} — expected "
                             f"{r['expected']}, device has {r['actual']}")
            lines.append("\nA short file lists in the picker and reverts to Noto "
                         "when selected. Send it again.")
        else:
            lines.append(f"✅ all {len(report)} files, byte for byte.")

        if not bad and not failed:
            # Nobody uploads a font they did not intend to read with, so this is
            # done rather than asked. It is two taps to
            # change on the device, and the message says what happened.
            ok, detail = suite.device.select_font_family(host, name)
            if ok:
                lines += ["", f"✅ <b>{html.escape(name)} is now the reading font.</b>",
                          "It takes effect when you <b>power-cycle the reader</b> — "
                          "fonts are scanned at boot, so the choice is saved now "
                          "and becomes real on the next start.",
                          "To pick a different one: Settings → Reader → Font Family."]
            else:
                lines += ["", "<b>Now power-cycle the reader</b>, then choose it "
                          "under Settings → Reader → Font Family.",
                          f"<i>(couldn't select it from here: "
                          f"{html.escape(str(detail)[:120])})</i>"]
        return (not bad and not failed), lines

    # -- fonts on the device -----------------------------------------------

    def show_device_fonts(self, chat) -> None:
        """The reader's own font registry: what it scanned, and what it reads with.

        🔤 Fonts lists what this repo can *send*; this lists what the device
        *has*. The difference matters once a card has been used for a while —
        families sent months ago, families copied by hand, a family you no
        longer want — and until now the only way to touch one from here was to
        browse to `/fonts/<Family>` and use the folder's delete button.
        """
        host, _ = self.device_host()
        families = suite.device.fonts(host).get("families", [])
        current = self.device_font_selection(host)
        if not families:
            return self.say(
                chat,
                "🔤 The reader has no SD font families.\n"
                f"It is reading with <b>{html.escape(current or 'a built-in font')}</b>.",
                [[("🔤 Send one", "m:fo")], [("📲 Device", "m:dev")]])

        local = {f["name"]: f for f in suite.local_font_families()}
        lines, rows, stale = ["🔤 <b>On the reader</b>"], [], False
        for f in families:
            name = f.get("name", "?")
            sizes = f.get("sizes", [])
            total = sum(x.get("size", 0) for x in f.get("files", []))
            # Every file reporting 0 B means the registry is holding paths that
            # no longer open — handleFontList writes 0 when the file will not
            # open. The family was renamed or removed since the last scan.
            if f.get("files") and not total:
                stale = True
                lines.append(f"⚠️ <b>{html.escape(name)}</b> — the reader still "
                             f"lists it, but its files no longer open. It was "
                             f"renamed or removed since it last scanned.")
                rows.append([(f"⚠️ {name} · stale",
                              f"dfo:one:{self.tokens.put({'name': name, 'sizes': sizes, 'bytes': 0, 'selected': name == current})}")])
                continue
            # A family we ship is one we can read the coverage of; for anything
            # else, keep the old blunt warning rather than guess it away.
            known = local.get(name)
            ui = ""
            if not suite.UI_FALLBACK_SIZES.issubset(set(sizes)):
                ui = ("" if known and not known["cjk"]
                      else "  ⚠️ no 8/10/12 — blank chapter list for CJK")
            here = "" if name in local else "  (not in this repo)"
            lines.append(f"{'✅' if name == current else '·'} "
                         f"<b>{html.escape(name)}</b> — "
                         f"{', '.join(str(s) for s in sizes)} pt, "
                         f"{human(total)}{ui}{here}")
            token = self.tokens.put({"name": name, "sizes": sizes,
                                     "bytes": total, "selected": name == current})
            rows.append([(f"{'✅ ' if name == current else ''}{name} · "
                          f"{human(total)}", f"dfo:one:{token}")])
        if current and current not in {f.get("name") for f in families}:
            lines.append(f"\nReading with <b>{html.escape(current)}</b>, "
                         f"which is built in.")
        if stale:
            lines.append("\nThe reader only re-reads its fonts folder at boot. "
                         "<b>🔄 Re-scan</b> makes it do so now.")
            rows.append([("🔄 Re-scan", "dev:foscan:")])
        rows.append([("🔤 Send one", "m:fo"), ("📲 Device", "m:dev")])
        self.say(chat, "\n".join(lines), rows)

    @staticmethod
    def device_font_selection(host) -> str | None:
        """The family the reader is set to, by name.

        `fontFamily` is an enum whose value is an index into its own `options`
        list — built-ins first, then the scanned SD families — so the name has
        to be looked up rather than computed. Returns None if the firmware
        exposes no such setting.
        """
        entry = suite.device.settings(host).get("fontFamily") or {}
        options, value = entry.get("options") or [], entry.get("value")
        if isinstance(value, int) and 0 <= value < len(options):
            return options[value]
        return None

    def on_device_font_callback(self, chat, rest: str) -> None:
        action, _, token = rest.partition(":")
        family = self.tokens.get(token)
        if not family:
            return self.stale(chat)
        name = family["name"]

        if action == "one":
            def work():
                host, _ = self.device_host()
                lines = [f"🔤 <b>{html.escape(name)}</b> — on the reader",
                         f"{', '.join(str(s) for s in family['sizes'])} pt · "
                         f"{human(family['bytes'])}"]
                if family["selected"]:
                    lines.append("✅ this is the family it reads with")
                if any(f["name"] == name for f in suite.local_font_families()):
                    report = suite.verify_font_family(host, name)
                    bad = [r for r in report if not r["ok"]]
                    lines.append(f"❌ {len(bad)} of {len(report)} files do not "
                                 f"match CHECKSUMS.tsv — a short one reverts to "
                                 f"Noto when selected"
                                 if bad else
                                 "✅ every file matches CHECKSUMS.tsv")
                # Renaming is a folder rename, and only the visible root can be
                # reached; asked now so the button is offered only when it
                # works, and so the *reason* it does not is the true one.
                root, where = suite.device_font_location(host, name)
                rows = []
                if where == "hidden-root":
                    lines.append("\n<i>Cannot be renamed from here: this reader "
                                 "keeps its fonts in the hidden "
                                 "<code>/.fonts</code>, which WebDAV refuses to "
                                 "touch.</i>")
                elif where == "missing":
                    lines.append(
                        "\n⚠️ <b>The reader lists this family, but there is no "
                        "<code>/fonts/" + html.escape(name) + "</code> on the "
                        "card.</b> Its font list is from the last boot and has "
                        "not caught up — a rename does exactly this. Re-scan "
                        "and the list will tell the truth; deleting it now "
                        "reports success and removes nothing.")
                    rows.append([("🔄 Re-scan the reader", "dev:foscan:")])
                if not family["selected"] and where != "missing":
                    rows.append([("✅ Read with this", f"dfo:sel:{token}")])
                rows.append([("✏️ Rename", f"dfo:rn:{token}"),
                             ("🗑 Delete", f"dfo:rm:{token}")]
                            if root else [("🗑 Delete", f"dfo:rm:{token}")])
                rows.append([("🔤 On the reader", "dev:fo:"), ("🏠 Menu", "m:main")])
                self.say(chat, "\n".join(lines), rows)
            return self.submit(chat, work)

        if action == "sel":
            def work():
                host, _ = self.device_host()
                ok, detail = suite.device.select_font_family(host, name)
                if ok:
                    return self.say(
                        chat,
                        f"✅ <b>{html.escape(name)}</b> is now the reading font.\n"
                        f"It takes effect when you <b>power-cycle the reader</b> — "
                        f"the choice is stored by name and the next boot's scan "
                        f"makes it real.",
                        [[("🔤 On the reader", "dev:fo:")], [("🏠 Menu", "m:main")]])
                self.say(chat, f"Could not select it: {html.escape(str(detail))}",
                         [[("🔤 On the reader", "dev:fo:")]])
            return self.submit(chat, work)

        if action == "rn":
            def work():
                host, _ = self.device_host()
                root, where = suite.device_font_location(host, name)
                if not root:
                    return self.say(chat, "There is no <code>/fonts/"
                                          f"{html.escape(name)}</code> on the card "
                                          "to rename — see the family's card for "
                                          "which of the two reasons that is.",
                                    [[("🔤 On the reader", "dev:fo:")]])
                self.pending = {"kind": "devfontrename", "host": host,
                                "root": root, "family": name,
                                "selected": family["selected"]}
                warn = ("\n\n⚠️ This is the family the reader is using. The "
                        "selection is stored by <i>name</i>, so renaming it "
                        "drops that selection — I will try to set it again "
                        "under the new name and tell you if it has to wait for "
                        "the power-cycle."
                        if family["selected"] else "")
                self.say(chat,
                         f"Send the new name for <b>{html.escape(name)}</b>.\n"
                         f"The folder name <i>is</i> the family name — the files "
                         f"inside keep their own names and the reader will not "
                         f"care.\n\n<b>Letters, digits, <code>-</code> and "
                         f"<code>_</code> only</b>, and not starting with "
                         f"<code>_</code>. That is the firmware's rule, not "
                         f"mine: a folder whose name it cannot parse is one it "
                         f"will later refuse to delete.{warn}")
            return self.submit(chat, work)

        if action == "rm":
            # The delete endpoint validates the family *name* before it looks
            # for the folder, with a stricter rule than anything that created
            # it. A folder named by hand or by a rename outside those characters
            # can never be deleted this way — say so instead of offering a
            # button that always 500s.
            unusable = suite.family_name_problem(name)
            if unusable:
                return self.say(
                    chat,
                    f"The reader will refuse to delete <b>{html.escape(name)}</b>: "
                    f"{unusable}.\n\nRename it to something with only letters, "
                    f"digits, <code>-</code> and <code>_</code> first — then the "
                    f"delete goes through.",
                    [[("✏️ Rename", f"dfo:rn:{token}")],
                     [("🔤 On the reader", "dev:fo:")]])
            return self.say(
                chat,
                f"Delete <b>{html.escape(name)}</b> from the reader — "
                f"{human(family['bytes'])}, all of it?"
                + ("\n\n⚠️ It is the family the reader is using; it will fall "
                   "back to a built-in font." if family["selected"] else ""),
                [[("Yes, delete", f"dfo:rm!:{token}"), ("No", "dev:fo:")]])

        if action == "rm!":
            def work():
                host, _ = self.device_host()
                root, where = suite.device_font_location(host, name)
                if where == "missing":
                    # deleteFamily returns OK when the folder is in neither
                    # root — "already gone" — so this would report success and
                    # remove nothing, and the stale list would still show it.
                    suite.rescan_device_fonts(host)
                    self.say(chat,
                             f"Nothing to delete: there is no folder for "
                             f"<b>{html.escape(name)}</b> on the card. The "
                             f"reader was listing it from its last scan, and "
                             f"has now re-scanned.")
                    return self.show_device_fonts(chat)
                suite.device.delete_font_family(host, name)
                suite.rescan_device_fonts(host)
                self.say(chat, f"🗑 <b>{html.escape(name)}</b> is gone from the "
                               f"reader.", [[("🔤 On the reader", "dev:fo:")],
                                            [("🏠 Menu", "m:main")]])
            return self.submit(chat, work)

        log("unhandled device font callback:", action, token)
        self.say(chat, "That button did nothing — please tell Claude.")

    # -- the device --------------------------------------------------------

    def show_device(self, chat, *, message_id=None) -> None:
        self.panel(
            chat, message_id,
            "📲 The reader answers only while it is on "
            "<b>Home → File Transfer → Join a Network</b>.",
            [[("🔎 Find it", "dev:st:"), ("📂 Browse", "dev:ls0:")],
             [("🖼 Wallpapers", "dev:wp:"), ("🔤 Fonts", "dev:fo:")],
             [("📤 Push queue", "push:ask")],
             [("📍 Set its address", "dev:addr:")],
             [("🏠 Menu", "m:main")]])

    def device_host(self):
        host, info = suite.device.find_device(None)
        return host, info

    def on_device_callback(self, chat, rest: str, msg_id=None) -> None:
        action, _, token = rest.partition(":")

        if action == "st":
            def work():
                host, info = self.device_host()
                self.say(chat, f"📲 {host}\n"
                               f"{info.get('model', 'reader')} · firmware "
                               f"{info.get('version', '?')}\n"
                               f"free heap: {info.get('freeHeap', '?')}",
                         [[("📂 Browse", "dev:ls0:")], [("🏠 Menu", "m:main")]])
            return self.submit(chat, work)

        if action == "addr":
            self.pending = {"kind": "devaddr"}
            return self.say(
                chat,
                "📍 Send me the address the X3 is showing on its <b>File "
                "Transfer</b> screen.\n\n"
                "An IP like <code>192.168.1.42</code> is what you want; a name "
                "works too. I will check it answers before keeping it.")

        if action == "ls0":
            return self.submit(
                chat, lambda: self.browse(chat, "/", message_id=msg_id))
        if action == "ls":
            path = self.tokens.get(token)
            if path is None:
                return self.stale(chat)
            return self.submit(
                chat, lambda: self.browse(chat, path, message_id=msg_id))
        if action == "sf":
            payload = self.tokens.get(token)
            if not isinstance(payload, dict):
                return self.stale(chat)
            return self.submit(
                chat, lambda: self.show_device_series(
                    chat, payload["path"], payload["key"], payload.get("page", 0),
                    message_id=msg_id))
        if action == "srm":
            payload = self.tokens.get(token)
            if not isinstance(payload, dict):
                return self.stale(chat)
            return self.submit(
                chat, lambda: self.confirm_device_series_removal(
                    chat, payload, message_id=msg_id))
        if action == "srm!":
            payload = self.tokens.get(token)
            if not isinstance(payload, dict):
                return self.stale(chat)
            self.panel(chat, msg_id, "Removing the series from the X3…")
            return self.submit(
                chat, lambda: self.remove_device_series(
                    chat, payload, message_id=msg_id))
        if action == "wp":
            def work():
                host, _ = self.device_host()
                self.browse(chat, suite.sleep_dir(host), message_id=msg_id)
            return self.submit(chat, work)
        if action == "fo":
            return self.submit(chat, lambda: self.show_device_fonts(chat))
        if action == "foscan":
            def work():
                host, _ = self.device_host()
                suite.rescan_device_fonts(host)
                self.say(chat, "🔄 The reader re-read its fonts folders.")
                self.show_device_fonts(chat)
            return self.submit(chat, work)

        # -- picking several out of a listing ------------------------------
        if action == "nop":
            return
        if action in ("pick", "pall", "pnone", "pdone", "t"):
            listing = self.listings.get(msg_id)
            if not listing:
                return self.stale(chat)
            path, entries = listing["path"], listing["entries"]
            here = {f"{path.rstrip('/')}/{e.get('name', '')}"
                    for e in entries if not e.get("isDirectory")}
            if action in ("pick", "pdone"):
                self.picked -= here
            elif action == "pall":
                self.picked |= here
            elif action == "pnone":
                self.picked -= here
            else:                                   # one name toggled
                payload = self.tokens.get(token)
                if not payload:
                    return self.stale(chat)
                self.picked ^= {payload["path"]}
            self.tg.edit_markup(chat, msg_id,
                                self.browse_keyboard(path, entries,
                                                     action != "pdone",
                                                     series_groups=listing.get(
                                                         "series_groups", []),
                                                     grouped_names=listing.get(
                                                         "grouped_names", set())))
            return

        if action == "rmpick":
            if not self.picked:
                return self.say(chat, "Nothing picked.")
            names = sorted(Path(p).name for p in self.picked)
            listed = "\n".join(f"· {html.escape(n)}" for n in names[:15])
            more = f"\n… and {len(names) - 15} more" if len(names) > 15 else ""
            return self.say(chat,
                            f"Delete <b>{len(names)}</b> file(s) from the "
                            f"reader?\n{listed}{more}",
                            [[("Yes, delete them", f"dev:rmpick!:{msg_id or 0}"),
                              ("No", "dev:pdone:")]])

        if action == "rmpick!":
            def work():
                host, _ = self.device_host()
                targets = sorted(self.picked)
                ok, detail = suite.device.delete_many(host, targets)
                self.picked.clear()
                self.say(chat, f"🗑 {len(targets)} deleted." if ok
                         else f"⚠️ {html.escape(detail[:200])}")
                listing = self.listings.get(int(token) if token.isdigit() else None)
                self.browse(chat, listing["path"] if listing else "/")
            return self.submit(chat, work)

        if action == "mvpick":
            if not self.picked:
                return self.say(chat, "Nothing picked.")
            listing = self.listings.get(msg_id) or {}
            self.moving = {"paths": sorted(self.picked),
                           "back": listing.get("path", "/")}
            self.picked.clear()
            return self.submit(chat, lambda: self.show_move_picker(chat, "/"))

        if action == "mv":                          # move one file
            payload = self.tokens.get(token)
            if not payload:
                return self.stale(chat)
            self.moving = {"paths": [payload["path"]], "back": payload["parent"]}
            return self.submit(chat, lambda: self.show_move_picker(chat, "/"))

        if action == "mvto":
            dest = self.tokens.get(token)
            if dest is None:
                return self.stale(chat)
            return self.submit(chat, lambda: self.show_move_picker(chat, dest))

        if action == "mvgo":
            dest = self.tokens.get(token)
            if dest is None:
                return self.stale(chat)
            return self.submit(chat, lambda: self.do_move(chat, dest))

        if action == "mkdir":
            path = self.tokens.get(token)
            if path is None:
                return self.stale(chat)

            def work():
                host, _ = self.device_host()
                self.pending = {"kind": "devmkdir", "host": host, "path": path}
                self.say(chat, f"Send me a name for the new folder inside\n"
                               f"<code>{html.escape(path)}</code>\n\n"
                               f"(a name, not a path — no slashes)")
            return self.submit(chat, work)

        if action == "dirrn":                      # rename the folder we are in
            path = self.tokens.get(token)
            if path is None:
                return self.stale(chat)
            if "/." in path or path.startswith("/."):
                return self.say(
                    chat,
                    "That folder can't be renamed. WebDAV — the only way in for "
                    "a folder — refuses every path with a dot-prefixed segment, "
                    "and the plain API refuses directories outright.",
                    [[("📂 Back", f"dev:ls:{self.tokens.put(path)}")]])

            def work():
                host, _ = self.device_host()
                self.pending = {"kind": "devdirrename", "host": host, "path": path}
                self.say(chat, f"Send me the new name for the folder\n"
                               f"<code>{html.escape(path)}</code>\n\n"
                               f"(name only — it stays where it is)")
            return self.submit(chat, work)

        if action in ("dirrm", "dirrm!"):
            path = self.tokens.get(token)
            if path is None:
                return self.stale(chat)
            family = self.font_family_of(path)
            what = (f"the font family <b>{html.escape(family)}</b>" if family
                    else f"<code>{html.escape(path)}</code> and everything in it")
            if action == "dirrm":
                return self.say(chat, f"Delete {what} from the reader?",
                                [[("Yes, delete", f"dev:dirrm!:{token}"),
                                  ("No", f"dev:ls:{self.tokens.put(path)}")]])

            def work():
                host, _ = self.device_host()
                if family:
                    # The firmware's own route: it goes through FontInstaller
                    # and marks the registry dirty, so the reader stops listing
                    # a family that is no longer there.
                    suite.device.delete_font_family(host, family)
                    self.say(chat, f"🗑 {html.escape(family)} removed.")
                    return self.browse(chat, path.rstrip("/").rpartition("/")[0] or "/")
                # Everything else: /delete only removes an *empty* directory,
                # so the contents go first and the folder after.
                entries = suite.device.list_dir(host, path)
                if any(e.get("isDirectory") for e in entries):
                    return self.say(chat, "⚠️ there are folders inside this one — "
                                          "empty those first.")
                if entries:
                    suite.device.delete_many(
                        host, [f"{path.rstrip('/')}/{e['name']}" for e in entries])
                ok, detail = suite.device.delete_many(host, [path])
                self.say(chat, f"🗑 {html.escape(path)} removed." if ok
                         else f"⚠️ {html.escape(detail[:200])}")
                self.browse(chat, path.rstrip("/").rpartition("/")[0] or "/")
            return self.submit(chat, work)

        if action == "wpall":                      # preview every BMP here
            path = self.tokens.get(token)
            if path is None:
                return self.stale(chat)

            def work():
                host, _ = self.device_host()
                bmps = [e for e in suite.device.list_dir(host, path)
                        if not e.get("isDirectory")
                        and e.get("name", "").lower().endswith(".bmp")]
                if not bmps:
                    return self.say(chat, "No wallpapers here.")
                shown = bmps[:self.PAGE]
                self.say(chat, f"👁 fetching {len(shown)} wallpaper(s) from the "
                               f"reader…")
                # Every one has to come across the wire regardless; what the
                # sheet saves is the upload back, which used to be one message
                # per wallpaper and a rate-limit at the end of it.
                cache = self.state_dir / "cache" / "device"
                local, payloads = [], []
                for entry in shown:
                    name = entry.get("name", "")
                    full = f"{path.rstrip('/')}/{name}"
                    dest = safe_join(cache, Path(name).name)
                    suite.device.download(host, full, dest)
                    local.append(dest)
                    payloads.append({"parent": path, "name": name, "path": full,
                                     "size": entry.get("size", 0)})
                sheet = self.state_dir / "cache" / "device-sheet.png"
                report = suite.contact_sheet(local, sheet)
                lines = [f"🖼 <b>{len(shown)} on the reader</b> — "
                         f"<code>{html.escape(path)}</code>"]
                lines += [f"{n} {html.escape(e.get('name', '')[:34])}"
                          for n, e in enumerate(shown, 1)]
                rows = self.sheet_rows(payloads, "dev:f:")
                rows.append([("📂 Back", f"dev:ls:{self.tokens.put(path)}")])
                self.send_preview(chat, Path(report["png"]),
                                  "\n".join(lines)[:1000], rows)
            return self.submit(chat, work)

        payload = self.tokens.get(token)
        if not payload:
            return self.stale(chat)
        parent, name = payload["parent"], payload["name"]
        full = payload["path"]

        if action == "f":
            back_callback = f"dev:ls:{self.tokens.put(parent)}"
            if payload.get("_device_series_key"):
                series_token = self.tokens.put({
                    'path': parent, 'key': payload['_device_series_key'],
                    'page': payload.get('_device_series_page', 0),
                })
                back_callback = f"dev:sf:{series_token}"
            rows = [[("✏️ Rename", f"dev:rn:{token}"),
                     ("📦 Move to…", f"dev:mv:{token}")],
                    [("🗑 Delete", f"dev:rm:{token}"),
                     ("⬇️ Pull to server", f"dev:get:{token}")],
                    [("📂 Back", back_callback)]]
            if name.lower().endswith(".bmp"):
                rows.insert(0, [("👁 Preview", f"dev:see:{token}")])
            return self.panel(
                chat, msg_id,
                f"<code>{html.escape(name)}</code>\n{human(payload['size'])}", rows)

        if action == "see":
            def work():
                host, _ = self.device_host()
                self.preview_bmp(chat, host, parent, name, payload["size"])
            return self.submit(chat, work)

        if action == "rn":
            def work():
                host, _ = self.device_host()
                self.pending = {"kind": "devrename", "host": host,
                                "path": full, "parent": parent}
                warn = ("\n\n⚠️ If you are part-way through this book, the "
                        "reader will forget your place: it keys reading state "
                        "by path and does not follow a rename made from here."
                        if name.lower().endswith((".epub", ".txt", ".xtc"))
                        else "")
                self.say(chat, f"Send me the new name for\n"
                               f"<code>{html.escape(name)}</code>\n\n"
                               f"(name only — no folders, and it may not start "
                               f"with a dot){warn}")
            return self.submit(chat, work)

        if action == "rm":
            return self.panel(
                chat, msg_id,
                f"Delete <code>{html.escape(name)}</code> from the reader?",
                [[("Yes, delete", f"dev:rm!:{token}"),
                  ("No", f"dev:f:{token}")]])
        if action == "rm!":
            def work():
                host, _ = self.device_host()
                ok = suite.device.delete(host, full)
                if not ok:
                    return self.panel(chat, msg_id, "⚠️ the reader refused.")
                if payload.get("_device_series_key"):
                    return self.show_device_series(
                        chat, parent, payload["_device_series_key"],
                        payload.get("_device_series_page", 0), message_id=msg_id)
                self.browse(chat, parent, message_id=msg_id)
            return self.submit(chat, work)

        if action not in ("get",):
            log("unhandled device callback:", action, token)
            return self.say(chat, f"That button did nothing — I don't know "
                                  f"<code>{html.escape(action)}</code>. "
                                  f"Please tell Claude.")

        if action == "get":
            def work():
                host, _ = self.device_host()
                dest = safe_join(self.workspace, "from-device", name)
                suite.device.download(host, full, dest)
                self.say(chat, f"⬇️ saved to <code>workspace/from-device/"
                               f"{html.escape(name)}</code>")
            return self.submit(chat, work)

    def preview_bmp(self, chat, host: str, parent: str, name: str,
                    size: int = 0) -> None:
        """Pull a wallpaper off the device and show what the panel would draw.

        The whole reason this exists: wallpapers end up on the SD card with
        names that say nothing, and there is no way to tell three of them apart
        without looking. Rendering goes through `crosspoint_bmp`, the port of
        the firmware's own reader, so the picture in the chat is the picture on
        the panel — including the black field around an under-size one, and
        saying so when the file is one the reader dithers itself, where the
        preview can only approximate what lands.
        """
        full = f"{parent.rstrip('/')}/{name}"
        cache = self.state_dir / "cache"
        local = safe_join(cache, Path(name).name)
        suite.device.download(host, full, local)
        report = suite.bmp_preview(local, local.with_suffix(".png"))

        bits = [f"<b>{html.escape(name)}</b>",
                f"{report['width']}×{report['height']} · {report['bpp']}-bpp · "
                f"{human(size or local.stat().st_size)}"]
        if not report.get("drawn_by_sleep_scan"):
            bits.append("⚠️ the sleep-screen scan skips this name")
        if not report.get("exact"):
            # Not a warning any more: shipping continuous tone and letting the
            # reader's Atkinson quantise it is what wallpaper-maker now does by
            # default, because it looks better. The preview just cannot show the
            # dither the device will apply, so say that plainly.
            bits.append("preview is approximate — the reader does its own "
                        "dithering on this one")
        elif report.get("scaled_down"):
            bits.append("scaled down to fit the panel")
        elif report.get("x") or report.get("y"):
            bits.append("smaller than the panel — shown in its black field")

        token = self.tokens.put({"parent": parent, "name": name, "path": full,
                                 "size": size})
        self.send_preview(chat, Path(report["png"]), "\n".join(bits),
                          [[("✏️ Rename", f"dev:rn:{token}"),
                            ("🗑 Delete", f"dev:rm:{token}")],
                           [("📂 Back", f"dev:ls:{self.tokens.put(parent)}")]])

    @staticmethod
    def font_family_of(path: str) -> str | None:
        """`/fonts/WenZilla` -> `WenZilla`, anything else -> None.

        Fonts get deleted through their own endpoint rather than as a folder of
        files, so the browse view has to recognise one when it is standing in
        it. `/fonts` itself is not a family and must not be removable this way.
        """
        parts = [p for p in path.strip("/").split("/") if p]
        return parts[1] if len(parts) == 2 and parts[0].lower() == "fonts" else None

    def device_series_groups(self, host: str, path: str,
                             entries: list) -> tuple[list, set]:
        """Group only exact catalog/device filename matches.

        `/api/files` exposes names, sizes and an EPUB flag, but no embedded
        metadata.  Guessing an alias-looking prefix would eventually put an
        unrelated book under a destructive series action.  The safe virtual
        folders are therefore the intersection of the live listing and names
        the catalog knows the current X3 filename setting would produce.
        """
        epub_entries = {entry.get("name", ""): entry for entry in entries
                        if not entry.get("isDirectory")
                        and (entry.get("isEpub")
                             or entry.get("name", "").lower().endswith(".epub"))}
        if not epub_entries:
            return [], set()
        try:
            lib = suite.library()
        except suite.SuiteError:
            return [], set()
        by_id = {book.get("id"): book for book in lib.get("books", [])}
        ordered = []
        for group in lib.get("series", []):
            for order, book_id in enumerate(group.get("book_ids", [])):
                book = by_id.get(book_id)
                if book:
                    ordered.append((group, order, book))
        if not ordered:
            return [], set()
        candidates = suite.device_book_name_candidates(
            [item[2] for item in ordered], host)
        owners = {}
        for (group, order, book), names in zip(ordered, candidates):
            identity = (group.get("key"), order, book.get("id"))
            for name in names:
                owners.setdefault(name, set()).add(identity)

        grouped = {}
        used = set()
        for name, entry in epub_entries.items():
            matches = owners.get(name, set())
            # More than one catalog volume mapping to the same SD name is
            # genuinely ambiguous (not least in title-only mode), so it stays
            # an ordinary file instead of acquiring a bulk-delete button.
            if len(matches) != 1:
                continue
            key, order, book_id = next(iter(matches))
            book = by_id.get(book_id)
            group = next((value for value in lib.get("series", [])
                          if value.get("key") == key), None)
            if not book or not group:
                continue
            bucket = grouped.setdefault(key, {"key": key, "name": group.get("name", ""),
                                               "alias": group.get("alias", ""),
                                               "files": []})
            bucket["files"].append({"entry": entry, "book": book, "order": order})
            used.add(name)
        result = []
        for group in grouped.values():
            group["files"].sort(key=lambda item: (item["order"],
                                                   item["entry"].get("name", "")))
            result.append(group)
        result.sort(key=lambda group: group["name"].casefold())
        return result, used

    def device_series_group(self, host: str, path: str, key: str) -> tuple[dict | None, list]:
        entries = sorted(suite.device.list_dir(host, path),
                         key=lambda entry: entry.get("name", "").lower())
        groups, _ = self.device_series_groups(host, path, entries)
        group = next((value for value in groups if value.get("key") == key), None)
        return group, group.get("files", []) if group else []

    def show_device_series(self, chat, path: str, key: str, page: int = 0,
                           *, message_id=None) -> None:
        host, _ = self.device_host()
        group, files = self.device_series_group(host, path, key)
        if not group:
            return self.panel(
                chat, message_id,
                "That series is no longer in this device listing.",
                [[("📂 Back", f"dev:ls:{self.tokens.put(path)}")]])
        last_page = max(0, (len(files) - 1) // NAV_PAGE)
        page = max(0, min(page, last_page))
        rows = []
        for item in files[page * NAV_PAGE:(page + 1) * NAV_PAGE]:
            entry, book = item["entry"], item["book"]
            name = entry.get("name", "")
            full = f"{path.rstrip('/')}/{name}"
            payload = {"parent": path, "name": name, "path": full,
                       "size": entry.get("size", 0),
                       "_device_series_key": key, "_device_series_page": page}
            position = (book.get("series_index") + " · "
                        if book.get("series_index") else "")
            title = book.get("base_title") or book.get("title") or name
            rows.append([(("📕 " + position + title)[:52],
                          f"dev:f:{self.tokens.put(payload)}")])
        navigation = []
        if page:
            navigation.append(("‹", f"dev:sf:{self.tokens.put({'path': path, 'key': key, 'page': page - 1})}"))
        if last_page:
            navigation.append((f"{page + 1}/{last_page + 1}", "dev:nop:"))
        if page < last_page:
            navigation.append(("›", f"dev:sf:{self.tokens.put({'path': path, 'key': key, 'page': page + 1})}"))
        if navigation:
            rows.append(navigation)
        current = self.tokens.put({"path": path, "key": key, "page": page})
        rows.append([(f"🗑 Remove all {len(files)} from X3", f"dev:srm:{current}")])
        rows.append([("📂 Back", f"dev:ls:{self.tokens.put(path)}")])
        lines = [f"📚 <b>{html.escape(group['name'])}</b>",
                 f"on the X3: {counted(len(files), 'book file')}"]
        if last_page:
            lines.append(f"Page {page + 1} of {last_page + 1}")
        self.panel(chat, message_id, "\n".join(lines), rows)

    def confirm_device_series_removal(self, chat, payload: dict, *,
                                      message_id=None) -> None:
        host, _ = self.device_host()
        group, files = self.device_series_group(
            host, payload["path"], payload["key"])
        if not group or not files:
            return self.panel(chat, message_id, "Nothing from that series is here now.",
                              [[("📂 Browse", f"dev:ls:{self.tokens.put(payload['path'])}")]])
        token = self.tokens.put(payload)
        self.panel(
            chat, message_id,
            f"Remove all {counted(len(files), 'book')} in "
            f"<b>{html.escape(group['name'])}</b> from the X3?",
            [[("Yes, remove all", f"dev:srm!:{token}"),
              ("No", f"dev:sf:{token}")]])

    def remove_device_series(self, chat, payload: dict, *, message_id=None) -> None:
        host, _ = self.device_host()
        group, files = self.device_series_group(
            host, payload["path"], payload["key"])
        if not group or not files:
            return self.panel(chat, message_id, "Nothing from that series is here now.",
                              [[("📂 Browse", f"dev:ls:{self.tokens.put(payload['path'])}")]])
        targets = [f"{payload['path'].rstrip('/')}/{item['entry']['name']}"
                   for item in files]
        ok, detail = suite.device.delete_many(host, targets)
        if not ok:
            return self.panel(
                chat, message_id,
                "⚠️ The X3 refused the removal: " + html.escape(detail[:200]),
                [[("◀ Series", f"dev:sf:{self.tokens.put(payload)}")]])
        self.panel(
            chat, message_id,
            f"🗑 Removed {counted(len(targets), 'book')} in "
            f"<b>{html.escape(group['name'])}</b> from the X3.",
            [[("📂 Browse", f"dev:ls:{self.tokens.put(payload['path'])}")]])

    def browse(self, chat, path: str, *, message_id=None) -> None:
        """List a folder on the SD card.

        Every name that comes back is carried in a token and sent straight back
        out untouched. The two Spanish books on the test device are stored with
        decomposed accents; normalizing one on the way through would produce a
        rename that fails against a file plainly sitting there.
        """
        host, _ = self.device_host()
        entries = sorted(suite.device.list_dir(host, path),
                         key=lambda e: (not e.get("isDirectory"),
                                        e.get("name", "").lower()))
        series_groups, grouped_names = self.device_series_groups(host, path, entries)
        sent = self.panel(chat, message_id,
                          f"📂 <code>{html.escape(path)}</code> — "
                          f"{len(entries)} item(s)",
                          self.browse_keyboard(
                              path, entries, series_groups=series_groups,
                              grouped_names=grouped_names))
        # Remembered by message id, like a contact sheet, so ☑ Pick several can
        # swap this listing's buttons in place instead of sending it again.
        panel_id = message_id or ((sent or {}).get("message_id"))
        if panel_id:
            self.listings[panel_id] = {
                "path": path, "entries": entries,
                "series_groups": series_groups, "grouped_names": grouped_names,
            }

    def show_move_picker(self, chat, path: str) -> None:
        """Walk the card looking for somewhere to put what you picked.

        A separate view rather than a list of every folder on the device: the
        card is a tree, and the only honest way to offer a destination three
        levels down is to let you walk to it. **📥 Move here** is on every
        level, so the folder you are looking at is always a valid answer.
        """
        if not self.moving:
            return self.stale(chat)
        host, _ = self.device_host()
        files = self.moving["paths"]
        rows = []
        for entry in sorted(suite.device.list_dir(host, path),
                            key=lambda e: e.get("name", "").lower()):
            if not entry.get("isDirectory"):
                continue
            child = f"{path.rstrip('/')}/{entry['name']}"
            rows.append([(f"📁 {entry['name'][:32]}",
                          f"dev:mvto:{self.tokens.put(child)}")])
        here = self.tokens.put(path)
        rows.append([(f"📥 Move {len(files)} here", f"dev:mvgo:{here}")])
        if path != "/":
            up = path.rstrip("/").rpartition("/")[0] or "/"
            rows.append([("⬆️ Up", f"dev:mvto:{self.tokens.put(up)}")])
        rows.append([("✖ Cancel", f"dev:ls:{self.tokens.put(self.moving['back'])}")])
        listed = "\n".join(f"· {html.escape(Path(p).name)}" for p in files[:10])
        more = f"\n… and {len(files) - 10} more" if len(files) > 10 else ""
        # The reading-position warning belongs here as much as on a rename: the
        # handlers behave identically, and a move is the one people expect to
        # be harmless.
        books = [p for p in files
                 if p.lower().endswith((".epub", ".txt", ".xtc"))]
        warn = ("\n\n⚠️ The reader keys reading state by path and does not "
                "follow a move made from here, so a book you are part-way "
                "through loses its place." if books else "")
        self.say(chat, f"📦 Moving:\n{listed}{more}\n\n"
                       f"Destination — now in <code>{html.escape(path)}</code>"
                       f"{warn}", rows)

    def do_move(self, chat, dest: str) -> None:
        if not self.moving:
            return self.stale(chat)
        host, _ = self.device_host()
        files, back = self.moving["paths"], self.moving["back"]
        self.moving = None
        moved, failed = [], []
        for path in files:
            if path.rstrip("/").rpartition("/")[0] == dest.rstrip("/"):
                continue               # already where it is being sent
            try:
                suite.device.move(host, path, dest)
                moved.append(Path(path).name)
            except suite.DeviceError as exc:
                failed.append((Path(path).name, str(exc)))
        lines = [f"📦 {len(moved)} file(s) → <code>{html.escape(dest)}</code>"]
        lines += [f"❌ {html.escape(n)} — {html.escape(e[:80])}" for n, e in failed]
        if not moved and not failed:
            lines = ["Nothing to do — they are already there."]
        self.say(chat, "\n".join(lines))
        self.browse(chat, dest if moved else back)

    def browse_keyboard(self, path: str, entries: list,
                        picking: bool = False, *, series_groups: list = None,
                        grouped_names: set = None) -> list:
        """The buttons under a folder listing, in either of its two moods.

        Browsing: a folder navigates and carries its own delete, a file opens.
        Picking: every *file* becomes a tick box in the same place, and the
        actions apply to the lot — which is the only way "these three books go
        in that folder" is a single gesture rather than nine.
        """
        rows = []
        series_groups = series_groups or []
        grouped_names = grouped_names or set()
        for entry in entries:
            name = entry.get("name", "")
            child = f"{path.rstrip('/')}/{name}"
            if entry.get("isDirectory"):
                if picking:
                    continue           # folders are not the thing being picked
                rows.append([(f"📁 {name[:28]}", f"dev:ls:{self.tokens.put(child)}"),
                             ("🗑", f"dev:dirrm:{self.tokens.put(child)}")])
                continue
            if not picking and name in grouped_names:
                continue
            token = self.tokens.put({"parent": path, "name": name, "path": child,
                                     "size": entry.get("size", 0)})
            if picking:
                mark = "☑" if child in self.picked else "☐"
                rows.append([(f"{mark} {name[:30]}", f"dev:t:{token}")])
            else:
                icon = "📕" if entry.get("isEpub") else "📄"
                rows.append([(f"{icon} {name[:32]}", f"dev:f:{token}")])

        if not picking:
            folder_rows = [row for row in rows
                           if row and row[0][0].startswith("📁")]
            file_rows = [row for row in rows if row not in folder_rows]
            series_rows = [[
                ((f"📚 {group['name']} · {len(group['files'])}")[:52],
                 f"dev:sf:{self.tokens.put({'path': path, 'key': group['key'], 'page': 0})}")
            ] for group in series_groups]
            rows = folder_rows + series_rows + file_rows

        files = [e for e in entries if not e.get("isDirectory")]
        if picking:
            here = {f"{path.rstrip('/')}/{e.get('name', '')}" for e in files}
            chosen = len(self.picked & here)
            rows.append([(f"📦 Move {chosen}", "dev:mvpick:"),
                         (f"🗑 Delete {chosen}", "dev:rmpick:")]
                        if chosen else [("Tap the names to pick", "dev:nop:")])
            rows.append([("All", "dev:pall:"), ("None", "dev:pnone:"),
                         ("✖ Done", "dev:pdone:")])
            return rows

        bmps = sum(1 for e in files
                   if e.get("name", "").lower().endswith(".bmp"))
        if bmps:
            rows.append([(f"👁 Preview all {bmps}", f"dev:wpall:{self.tokens.put(path)}")])
        here = self.tokens.put(path)
        actions = [("➕ New folder", f"dev:mkdir:{here}")]
        if files:
            actions.append(("☑ Pick several", f"dev:pick:{here}"))
        rows.append(actions)
        if path != "/":
            up = path.rstrip("/").rpartition("/")[0] or "/"
            rows.append([("⬆️ Up", f"dev:ls:{self.tokens.put(up)}"),
                         ("✏️ Rename folder", f"dev:dirrn:{here}")])
            rows.append([("🗑 Delete this folder", f"dev:dirrm:{here}")])
        rows.append([("🏠 Menu", "m:main")])
        return rows

    # -- push --------------------------------------------------------------

    def ask_push(self, chat) -> None:
        n = len(self.queue)
        if not n:
            return self.say(chat, "Nothing queued.", [[("🏠 Menu", "m:main")]])
        self.say(chat,
                 f"📤 {n} item(s) ready.\n\nOn the X3: <b>Home → File Transfer → "
                 f"Join a Network</b>, then tap Ready.",
                 [[("✅ Ready", "push:go")], [("🏠 Menu", "m:main")]])

    def do_push(self, chat) -> None:
        """Drain the queue, and leave it alone if the reader is not there.

        Only items the push script reported as landed are removed. A device
        that never answered removes nothing at all — which is the promise that
        makes it safe to queue things all week and push once.
        """
        items = self.queue.items()
        if not items:
            return self.say(chat, "Nothing queued.")

        missing = [i for i in items if not Path(i["path"]).exists()]
        live = [i for i in items if Path(i["path"]).exists()]
        if missing:
            self.queue.remove_many(i["id"] for i in missing)
            self.say(chat, "⚠️ dropped from the queue (the file is gone):\n"
                     + "\n".join(f"· {html.escape(i['label'])}" for i in missing))
        if not live:
            return self.say(chat, "Nothing left to push.", [[("🏠 Menu", "m:main")]])

        # Find the reader once, and hand the address to both halves. Two
        # discovery passes could disagree, and "no reader" has to mean the same
        # thing for wallpapers and books or the all-or-nothing promise leaks.
        try:
            host, _ = self.device_host()
        except suite.DeviceError as exc:
            self.say(chat, "The queue is untouched — nothing was sent.")
            return self.no_reader(chat, str(exc), retry="push:ask")

        books = [i for i in live if i.get("kind") == "book"]
        fonts = [i for i in live if i.get("kind") == "font"]
        # Anything else is a wallpaper. Kept as the default rather than an
        # explicit "wallpaper" test because queues written before fonts and
        # books existed carry entries with no kind at all.
        walls = [i for i in live if i.get("kind") not in ("book", "font")]
        lines = [f"📲 {host}"]
        progress_message_id = None

        def show_progress(current="", keyboard=None):
            """Build the existing final report in place as files land."""
            nonlocal progress_message_id
            text = "\n".join(lines + ([current] if current else []))
            try:
                sent = self.panel(chat, progress_message_id, text, keyboard)
                if sent and sent.get("message_id"):
                    progress_message_id = sent["message_id"]
            except Exception as exc:
                # A status edit must never change whether a transfer succeeds.
                log("push progress failed:", str(exc))

        done = []

        if walls:
            try:
                total = human(sum(Path(i["path"]).stat().st_size for i in walls))
            except OSError:
                total = ""
            what = (html.escape(Path(walls[0]["path"]).name)
                    if len(walls) == 1 else f"{len(walls)} wallpapers")
            show_progress(f"📤 {what}" + (f" ({total})" if total else ""))
            report = suite.push([i["path"] for i in walls], host=host)
            landed = {r["name"] for r in report.get("items", []) if r.get("ok")}
            done += [i for i in walls if Path(i["path"]).name in landed]
            if report.get("target"):
                lines.append(f"🖼 <code>{html.escape(report['target'])}</code>")
            for r in report.get("items", []):
                mark = "✅" if r.get("ok") else "❌"
                lines.append(f"{mark} {html.escape(r['name'])}"
                             + ("" if r.get("ok")
                                else f" — {html.escape(str(r.get('error'))[:80])}"))
            if report.get("sleep_mode_set"):
                lines.append("Sleep screen set to Custom.")
            show_progress()

        if books:
            lines.append("📕 SD root")
            slim_cache = self.state_dir / "cache" / "slim"
            for item in books:
                meta = item.get("meta") or {}
                name = item["label"]
                try:
                    # Named the way the OPDS client would name it, so pushing a
                    # book and later downloading it produce one file, not two.
                    name = suite.device_book_name(meta.get("author", ""),
                                                  meta.get("title", item["label"]),
                                                  host=host)
                    # Slimming belongs to the push, not to the queue: queueing
                    # only warms the cache. Whatever route a book took to get
                    # here — queued days ago, cache since cleared, or never
                    # queued at all — it is slimmed before it goes across.
                    slim = Path(meta["slim"]) if meta.get("slim") else None
                    if not (slim and slim.exists()):
                        made = suite.slim_book(Path(item["path"]), slim_cache)
                        slim = made["path"] if made["used"] else None
                    source = slim or Path(item["path"])
                    try:
                        amount = human(source.stat().st_size)
                    except OSError:
                        amount = ""
                    show_progress(f"📤 {html.escape(name)}"
                                  + (f" ({amount})" if amount else ""))
                    suite.upload_book(host, source, name)
                    done.append(item)
                    lines.append(f"✅ {html.escape(name)}"
                                 + (f" ({human(source.stat().st_size)}, slimmed)"
                                    if slim else ""))
                    # It has landed, so the copy is rubbish. The cache is for
                    # the gap between queueing and pushing and nothing else.
                    if slim:
                        suite.drop_slim(slim, slim_cache)
                except Exception as exc:
                    # Deliberately broad. A surprise in one item must not throw
                    # away the whole report — including the wallpapers that
                    # already landed, whose queue entries are only removed at
                    # the end of this method.
                    lines.append(f"❌ {html.escape(name)} — "
                                 f"{html.escape(str(exc)[:100])}")
                show_progress()

        # Fonts last: a family is the slow item, and the quick things should
        # already be on the card by the time it starts. What a family *is* is
        # read from disk now rather than trusted from the queue entry — rebuild
        # it between queueing and pushing and the new bytes are what travel.
        for item in fonts:
            name = (item.get("meta") or {}).get("family") or item["label"]
            family = next((f for f in suite.local_font_families()
                           if f["name"] == name), None)
            if not family:
                lines.append(f"❌ {html.escape(name)} — no longer in "
                             f"extras/fonts/")
                show_progress()
                continue
            try:
                show_progress(f"📤 {html.escape(name)} "
                              f"({human(family['bytes'])})")
                landed, report = self.push_font_family(chat, host, family)
                lines += report
                if landed:
                    done.append(item)
            except Exception as exc:
                # As broad as the book loop above, and for the same reason: one
                # surprising family must not throw away a report that other
                # items are still counting on.
                lines.append(f"❌ {html.escape(name)} — "
                             f"{html.escape(str(exc)[:100])}")
            show_progress()

        self.queue.remove_many(i["id"] for i in done)
        # Whatever is left in the cache now belongs to no queued book — pushed,
        # or dropped from the queue without ever being sent. Either way it is
        # not a store, so it does not keep them.
        suite.prune_slim_cache(
            self.state_dir / "cache" / "slim",
            {(i.get("meta") or {}).get("slim") for i in self.queue.items()
             if (i.get("meta") or {}).get("slim")})
        self.notes.set("last_push", datetime.now().strftime("%Y-%m-%d %H:%M"))
        remaining = len(self.queue)
        lines.append(f"\n{remaining} still queued." if remaining
                     else "\nQueue empty. Leave the File Transfer screen and "
                          "let it sleep.")
        show_progress(keyboard=[[("🏠 Menu", "m:main")]])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="the X3 suite, operated from a phone")
    ap.add_argument("--config", type=Path, metavar="PATH",
                    help="configuration to read (default: tools/tgbot/config.json). "
                         "Paths inside it resolve against its own directory, so "
                         "the whole configuration — token included — can live "
                         "outside this repo")
    args = ap.parse_args(argv)

    try:
        cfg = load(args.config)
        token = resolve_token(cfg)
    except ConfigError as exc:
        print(f"tgbot: {exc}", file=sys.stderr)
        return 1

    tg = Telegram(token)
    bot = Bot(cfg, tg)
    bot.start_worker()

    try:
        me = tg.get_me()
        log(f"@{me.get('username')} is up; serving user {bot.user_id}")
        tg.set_commands([
            {"command": "menu", "description": "the buttons"},
            {"command": "status", "description": "is anything wrong"},
            {"command": "queue", "description": "what is waiting for the reader"},
            {"command": "push", "description": "send the queue to the X3"},
            {"command": "library", "description": "books the catalog serves"},
            {"command": "wallpapers", "description": "sleep screens built here"},
            {"command": "device", "description": "browse the reader"},
            {"command": "inbox", "description": "files dropped on the server"},
        ])
        bot.menu(bot.user_id, "Let's read!")
    except TelegramError as exc:
        print(f"tgbot: cannot reach Telegram: {exc}", file=sys.stderr)
        return 1

    log(f"workspace {cfg['workspace_path']}, state {cfg['state_path']}, "
        f"config {cfg['_config_path']}"
        + (f", secrets {cfg['secrets_path']}" if cfg.get("secrets_path") else ""))
    for grumble in config_warnings(cfg):
        log("warning:", grumble)
    while True:
        try:
            for update in tg.get_updates(cfg["telegram"]["poll_timeout"]):
                bot.handle(update)
        except TelegramError as exc:
            log("poll failed:", exc, "— retrying in 5s")
            time.sleep(5)
        except KeyboardInterrupt:
            log("stopping")
            return 0


if __name__ == "__main__":
    sys.exit(main())
