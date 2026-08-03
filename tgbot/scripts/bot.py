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
                self.say(chat, f"⚠️ {exc}")
            finally:
                self._jobs.task_done()

    def submit(self, chat, fn) -> None:
        """Long work goes to the worker so polling never stops. In the
        self-test everything runs inline, which keeps the assertions honest."""
        if self.sync:
            try:
                fn()
            except Exception as exc:
                self.say(chat, f"⚠️ {exc}")
        else:
            self._jobs.put((fn, chat))

    def say(self, chat, text: str, keyboard=None):
        try:
            return self.tg.send_message(chat, text, keyboard)
        except TelegramError as exc:
            log("send failed:", exc)
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
        self.say(chat, text, [
            [("📚 Library", "m:lib"), ("🖼 Queue", "m:q")],
            [("📥 Inbox", "m:in"), ("📲 Device", "m:dev")],
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
                    "st": lambda: self.submit(chat, lambda: self.show_status(chat)),
                    "main": lambda: self.menu(chat)}.get(rest, lambda: None)()

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

    def send_preview(self, chat, png: Path, caption: str, keyboard) -> None:
        try:
            if png.exists():
                return self.tg.send_photo(chat, png, caption, keyboard)
        except TelegramError as exc:
            log("preview failed:", exc)
        self.say(chat, caption, keyboard)

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
            return self.say(chat, "🖼 Queue empty. Nothing is waiting for the reader.",
                            [[("🏠 Menu", "m:main")]])
        rows = [[(f"✗ {i['label'][:30]}", f"qdel:{i['id']}")] for i in items]
        rows.append([("📲 Push now", "push:ask"), ("Clear all", "qclr:")])
        rows.append([("🏠 Menu", "m:main")])
        self.say(chat, f"🖼 {len(items)} waiting to go to the reader:", rows)

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

    # -- the device --------------------------------------------------------

    def show_device(self, chat) -> None:
        self.say(chat, "📲 The reader answers only while it is on "
                       "<b>Home → File Transfer → Join a Network</b>.",
                 [[("🔎 Find it", "dev:st:"), ("📂 Browse", "dev:ls0:")],
                  [("🖼 Wallpapers", "dev:wp:"), ("📤 Push queue", "push:ask")],
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
                self.say(chat, f"👁 {len(bmps)} wallpaper(s) — one message each, "
                               f"so you can act on the one you recognise.")
                for entry in bmps[:10]:
                    self.preview_bmp(chat, host, path, entry.get("name", ""),
                                     entry.get("size", 0))
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
                self.say(chat, f"Send me the new name for\n"
                               f"<code>{html.escape(name)}</code>\n\n"
                               f"(name only — no folders, and it may not start "
                               f"with a dot)")
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
            rows.append([("⬆️ Up", f"dev:ls:{self.tokens.put(up)}"),
                         ("✏️ Rename folder", f"dev:dirrn:{self.tokens.put(path)}")])
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
            return self.say(
                chat,
                "📵 No reader found — the queue is untouched.\n\n"
                f"<pre>{html.escape(str(exc)[:400])}</pre>",
                [[("Try again", "push:ask")], [("🏠 Menu", "m:main")]])

        lines = [f"📲 {host}"]
        done = []

        walls = [i for i in live if i.get("kind") != "book"]
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

        books = [i for i in live if i.get("kind") == "book"]
        if books:
            lines.append("📕 SD root")
            for item in books:
                meta = item.get("meta") or {}
                # Named the way the OPDS client would name it, so pushing a
                # book and later downloading it produce one file, not two.
                name = suite.device_book_name(meta.get("author", ""),
                                              meta.get("title", item["label"]),
                                              host=host)
                try:
                    suite.upload_book(host, Path(item["path"]), name)
                    done.append(item)
                    lines.append(f"✅ {html.escape(name)}")
                except suite.DeviceError as exc:
                    lines.append(f"❌ {html.escape(name)} — "
                                 f"{html.escape(str(exc)[:80])}")

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
