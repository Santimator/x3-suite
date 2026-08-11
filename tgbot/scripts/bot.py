#!/usr/bin/env python3
"""A steward for the suite, operated from a phone.

    python3 tgbot/scripts/bot.py

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

Books are never queued. The X3 pulls those from the OPDS catalog itself, so a
book in a delivery queue could only ever produce a second copy on the SD card
under a different name.

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
        # A contact sheet stays on screen while you tick things off it, so the
        # bot has to remember what each sheet message is showing. Keyed by
        # message id, in memory: a restart makes the buttons stale, which is
        # answered rather than guessed at.
        self.sheets = {}
        self.selected = set()
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

    # -- menus -------------------------------------------------------------

    def menu(self, chat, text: str = "Menu") -> None:
        # Three collections on the server, then the ways in and out of it.
        # The queue is an outbox, so it is 📤 rather than a second 🖼.
        self.say(chat, text, [
            [("📚 Library", "m:lib"), ("🖼 Wallpapers", "m:wl")],
            [("🔤 Fonts", "m:fo"), ("📥 Inbox", "m:in")],
            [("📲 Device", "m:dev"), ("📤 Queue", "m:q")],
            [("⚙️ Status", "m:st")],
        ])

    def on_callback(self, cb: dict) -> None:
        data = cb.get("data") or ""
        chat = cb["message"]["chat"]["id"]
        self.tg.answer_callback(cb["id"])
        head, _, rest = data.partition(":")

        if head == "m":
            return {"lib": lambda: self.submit(chat, lambda: self.show_library(chat)),
                    "q": lambda: self.show_queue(chat),
                    "in": lambda: self.show_inbox(chat),
                    "dev": lambda: self.show_device(chat),
                    "fo": lambda: self.show_fonts(chat),
                    "wl": lambda: self.show_wallpapers(chat),
                    "st": lambda: self.submit(chat, lambda: self.show_status(chat)),
                    "main": lambda: self.menu(chat)}.get(rest, lambda: None)()

        if head == "fo":
            return self.on_font_callback(chat, rest)
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
            self.queue.add("book", book["path"],
                           label=book["title"][:40], meta=book)
            return self.say(
                chat,
                f"📕 queued for the card — {len(self.queue)} waiting.\n"
                f"It stays on the catalog either way.",
                [[("📲 Push now", "push:ask"), ("🏠 Menu", "m:main")]])

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
            return self.on_device_callback(chat, rest)
        if head == "lib":
            return self.on_library_callback(chat, rest)
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
        """A book needs no push — it needs to be somewhere the catalog scans."""
        dest_dir = self.workspace / "library"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = safe_join(dest_dir, path.name)
        if path.resolve() != dest.resolve():
            path.replace(dest)

        ok, detail = suite.verify_epub(dest)
        title = author = None
        try:
            for book in suite.library().get("books", []):
                if Path(book["path"]).resolve() == dest.resolve():
                    title, author = book["title"], book["author"]
                    break
        except suite.SuiteError:
            pass

        lines = ["📚 " + ("verified" if ok else "⚠️ verify_epub complained")]
        if title:
            # What the device will show, and what it will name the download.
            lines.append(f"<b>{html.escape(title)}</b>")
            lines.append(f"by {html.escape(author or 'unknown')}")
            lines.append(f"on the SD card it becomes: "
                         f"<code>{html.escape(f'{author} - {title}.epub')}</code>")
        if not ok:
            lines.append(f"<pre>{html.escape(detail[:500])}</pre>")
        lines.append("")
        lines.append("On the catalog now — no push needed."
                     if suite.opds_up(self.cfg["opds_url"])
                     else "⚠️ opds-server is not answering, so the reader "
                          "cannot fetch it yet.")

        # The catalog is the library; the device is a convenience. So the book
        # is filed first, always, and copying it onto the card is an extra you
        # ask for — never the only place it exists.
        token = self.tokens.put({"path": str(dest), "title": title or dest.stem,
                                 "author": author or ""})
        self.say(chat, "\n".join(lines),
                 [[("📤 Also send to device", f"bq:{token}")],
                  [("🏠 Menu", "m:main")]])

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
                     "(<code>services/pdf2epub/DESIGN.md</code>, open question 6), "
                     "so this one is waiting for a driver:")
        lines.append(f"<code>workspace/{slug}/</code> — point Claude Code at the "
                     f"repo and read <code>services/pdf2epub/SKILL.md</code>.")
        self.say(chat, "\n".join(lines), [[("🏠 Menu", "m:main")]])

    # -- views -------------------------------------------------------------

    def show_library(self, chat) -> None:
        try:
            lib = suite.library()
        except suite.SuiteError as exc:
            return self.say(chat, f"⚠️ {exc}")
        books = lib.get("books", [])
        if not books:
            return self.say(chat, "No books yet. Send me an EPUB.",
                            [[("🏠 Menu", "m:main")]])
        rows = []
        for book in books[:20]:
            # The whole record, not just the path: sending it to the card later
            # needs the author and title the catalog knows it by.
            token = self.tokens.put(book)
            label = f"{book['title'][:28]} · {(book['author'] or '?')[:14]}"
            rows.append([(label, f"lib:f:{token}")])
        rows.append([("🏠 Menu", "m:main")])
        self.say(chat, f"📚 {len(books)} book(s) the catalog would serve:", rows)

    def on_library_callback(self, chat, rest: str) -> None:
        action, _, token = rest.partition(":")
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
            return self.say(
                chat, f"<code>{html.escape(path.name)}</code>",
                [[("📤 Send to device", f"bq:{send}")],
                 [("✏️ Rename", f"lib:rn:{token}"), ("🗑 Delete", f"lib:rm:{token}")],
                 [("📚 Library", "m:lib")]])
        if action == "rn":
            self.pending = {"kind": "librename", "path": str(path)}
            return self.say(chat, "Send me the new filename.")
        if action == "rm":
            return self.say(chat, f"Delete <code>{html.escape(path.name)}</code> "
                                  f"from the server?",
                            [[("Yes, delete", f"lib:rm!:{token}"), ("No", "m:lib")]])
        if action == "rm!":
            if not inside(self.workspace, path):
                return self.say(chat, "⚠️ that is outside the workspace.")
            path.unlink(missing_ok=True)
            return self.say(chat, "🗑 gone.", [[("📚 Library", "m:lib")]])

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
            return self.say(chat, "No families in <code>reference/fonts/</code>.",
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
            def work():
                host, _ = self.device_host()
                data = suite.device.fonts(host)
                families = data.get("families", [])
                if not families:
                    return self.say(chat, "🔤 The reader has no SD fonts installed.",
                                    [[("🔤 Fonts", "m:fo")]])
                lines = ["🔤 <b>On the reader</b>"]
                for f in families:
                    sizes = f.get("sizes", [])
                    total = sum(x.get("size", 0) for x in f.get("files", []))
                    ui = "" if suite.UI_FALLBACK_SIZES.issubset(set(sizes)) \
                        else "  ⚠️ no 8/10/12 — blank chapter list"
                    lines.append(f"· <b>{html.escape(f.get('name', '?'))}</b> — "
                                 f"{', '.join(str(s) for s in sizes)} pt, "
                                 f"{human(total)}{ui}")
                lines.append("\nDelete one by browsing to it: "
                             "📲 Device → 📂 Browse → fonts")
                self.say(chat, "\n".join(lines), [[("🔤 Fonts", "m:fo")]])
            return self.submit(chat, work)

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
            minutes = ("\n\nSending this takes a few minutes — it is "
                       f"{human(family['bytes'])} over WiFi, and the reader "
                       "must stay on the File Transfer screen throughout."
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
                suite.device.upload_font(host, name, path)
                self.say(chat, f"  {n}/{len(files)} ✅ {html.escape(path.name)} "
                               f"({human(path.stat().st_size)})")
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
            # Nobody spends four minutes uploading a font they did not intend to
            # read with, so this is done rather than asked. It is two taps to
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

    # -- the device --------------------------------------------------------

    def show_device(self, chat) -> None:
        self.say(chat, "📲 The reader answers only while it is on "
                       "<b>Home → File Transfer → Join a Network</b>.",
                 [[("🔎 Find it", "dev:st:"), ("📂 Browse", "dev:ls0:")],
                  [("🖼 Wallpapers", "dev:wp:"), ("📤 Push queue", "push:ask")],
                  [("📍 Set its address", "dev:addr:")],
                  [("🏠 Menu", "m:main")]])

    def device_host(self):
        host, info = suite.device.find_device(None)
        return host, info

    def on_device_callback(self, chat, rest: str) -> None:
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
            return self.submit(chat, lambda: self.browse(chat, "/"))
        if action == "ls":
            path = self.tokens.get(token)
            if path is None:
                return self.stale(chat)
            return self.submit(chat, lambda: self.browse(chat, path))
        if action == "wp":
            def work():
                host, _ = self.device_host()
                self.browse(chat, suite.sleep_dir(host))
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
            rows = [[("✏️ Rename", f"dev:rn:{token}"),
                     ("🗑 Delete", f"dev:rm:{token}")],
                    [("⬇️ Pull to server", f"dev:get:{token}")],
                    [("📂 Back", f"dev:ls:{self.tokens.put(parent)}")]]
            if name.lower().endswith(".bmp"):
                rows.insert(0, [("👁 Preview", f"dev:see:{token}")])
            return self.say(
                chat, f"<code>{html.escape(name)}</code>\n{human(payload['size'])}",
                rows)

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
            return self.say(chat, f"Delete <code>{html.escape(name)}</code> "
                                  f"from the reader?",
                            [[("Yes, delete", f"dev:rm!:{token}"),
                              ("No", f"dev:f:{token}")]])
        if action == "rm!":
            def work():
                host, _ = self.device_host()
                ok = suite.device.delete(host, full)
                self.say(chat, "🗑 gone." if ok else "⚠️ the reader refused.")
                self.browse(chat, parent)
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

    def browse(self, chat, path: str) -> None:
        """List a folder on the SD card.

        Every name that comes back is carried in a token and sent straight back
        out untouched. The two Spanish books on the test device are stored with
        decomposed accents; normalizing one on the way through would produce a
        rename that fails against a file plainly sitting there.
        """
        host, _ = self.device_host()
        entries = suite.device.list_dir(host, path)
        rows = []
        for entry in sorted(entries, key=lambda e: (not e.get("isDirectory"),
                                                    e.get("name", "").lower())):
            name = entry.get("name", "")
            child = f"{path.rstrip('/')}/{name}"
            if entry.get("isDirectory"):
                rows.append([(f"📁 {name[:32]}", f"dev:ls:{self.tokens.put(child)}")])
            else:
                token = self.tokens.put({"parent": path, "name": name,
                                         "path": child,
                                         "size": entry.get("size", 0)})
                icon = "📕" if entry.get("isEpub") else "📄"
                rows.append([(f"{icon} {name[:32]}", f"dev:f:{token}")])
        bmps = sum(1 for e in entries if not e.get("isDirectory")
                   and e.get("name", "").lower().endswith(".bmp"))
        if bmps:
            rows.append([(f"👁 Preview all {bmps}", f"dev:wpall:{self.tokens.put(path)}")])
        if path != "/":
            up = path.rstrip("/").rpartition("/")[0] or "/"
            here = self.tokens.put(path)
            rows.append([("⬆️ Up", f"dev:ls:{self.tokens.put(up)}"),
                         ("✏️ Rename folder", f"dev:dirrn:{here}")])
            rows.append([("🗑 Delete this folder", f"dev:dirrm:{here}")])
        rows.append([("🏠 Menu", "m:main")])
        self.say(chat, f"📂 <code>{html.escape(path)}</code> — "
                       f"{len(entries)} item(s)", rows)

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
        # The reader is there and the work has started. Between here and the
        # report is the long quiet stretch — a 4 MB book over WiFi is a minute
        # on its own — so say what is about to happen and in what order.
        parts = []
        if walls:
            parts.append(f"{len(walls)} wallpaper(s)")
        if books:
            parts.append(f"{len(books)} book(s)")
        if fonts:
            parts.append(f"{len(fonts)} font family(ies)")
        self.say(chat, f"📲 Found it at <code>{html.escape(host)}</code>.\n"
                       f"Sending {' and '.join(parts)} — keep the reader on that "
                       f"screen.")

        lines = [f"📲 {host}"]
        done = []

        if walls:
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

        if books:
            lines.append("📕 SD root")
            for item in books:
                meta = item.get("meta") or {}
                name = item["label"]
                try:
                    # Named the way the OPDS client would name it, so pushing a
                    # book and later downloading it produce one file, not two.
                    name = suite.device_book_name(meta.get("author", ""),
                                                  meta.get("title", item["label"]),
                                                  host=host)
                    suite.upload_book(host, Path(item["path"]), name)
                    done.append(item)
                    lines.append(f"✅ {html.escape(name)}")
                except Exception as exc:
                    # Deliberately broad. A surprise in one item must not throw
                    # away the whole report — including the wallpapers that
                    # already landed, whose queue entries are only removed at
                    # the end of this method.
                    lines.append(f"❌ {html.escape(name)} — "
                                 f"{html.escape(str(exc)[:100])}")

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
                             f"reference/fonts/")
                continue
            try:
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

        self.queue.remove_many(i["id"] for i in done)
        self.notes.set("last_push", datetime.now().strftime("%Y-%m-%d %H:%M"))
        remaining = len(self.queue)
        lines.append(f"\n{remaining} still queued." if remaining
                     else "\nQueue empty. Leave the File Transfer screen and "
                          "let it sleep.")
        self.say(chat, "\n".join(lines), [[("🏠 Menu", "m:main")]])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="the X3 suite, operated from a phone")
    ap.add_argument("--config", type=Path, metavar="PATH",
                    help="configuration to read (default: tgbot/config.json). "
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
