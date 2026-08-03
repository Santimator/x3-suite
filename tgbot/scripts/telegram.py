#!/usr/bin/env python3
"""The Telegram Bot API, in as much of it as a single-user bot needs.

Stdlib only. Long polling is one HTTPS GET that blocks until something happens
or the timeout expires; sending a message is one POST. There is no third-party
library here for the same reason `services/graded-reader/headless/llm.py` has
none — the protocol is small enough that a dependency would cost more setup
than it saves, and this bot's whole promise is that it installs by being
cloned.

Two limits of the API shape everything above this file, so they are named here
rather than discovered later:

  20 MB   the ceiling on `getFile`. A bot *cannot* download anything bigger,
          whatever the sender's account can upload. Scanned PDFs live right at
          this line, so the caller checks `file_size` before asking.
  64 B    the ceiling on a callback_data string. Device filenames are far
          longer than that (`<author> - <title>.epub`, sometimes 200+ chars),
          which is why buttons carry short tokens and the bot keeps the real
          payload in memory. See `state.Tokens`.
"""

from __future__ import annotations

import json
import mimetypes
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

API = "https://api.telegram.org"
GETFILE_LIMIT = 20 * 1024 * 1024


class TelegramError(RuntimeError):
    pass


class Telegram:
    def __init__(self, token: str, timeout: float = 40):
        self._token = token
        self.timeout = timeout
        self.offset = 0

    # The token is a credential in a URL, so it must never reach a log line or
    # an exception message. Everything that formats an error uses the method
    # name, never the built URL.
    def _url(self, method: str) -> str:
        return f"{API}/bot{self._token}/{method}"

    def _post(self, method: str, payload: dict | None = None, *,
              files: dict | None = None, timeout: float | None = None) -> dict:
        payload = {k: v for k, v in (payload or {}).items() if v is not None}
        if files:
            body, content_type = _multipart(payload, files)
        else:
            body = json.dumps(payload).encode()
            content_type = "application/json"
        for attempt in range(3):
            req = urllib.request.Request(self._url(method), data=body, method="POST")
            req.add_header("Content-Type", content_type)
            try:
                with urllib.request.urlopen(req,
                                            timeout=timeout or self.timeout) as resp:
                    data = json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                detail, wait = "", 0
                try:
                    payload = json.loads(exc.read())
                    detail = payload.get("description", "")
                    wait = (payload.get("parameters") or {}).get("retry_after", 0)
                except Exception:
                    pass
                # 429 is not an error, it is a queue. Sending several photos in
                # a row — a preview of every wallpaper in a folder — is exactly
                # the burst that earns one, and the reply that follows would
                # otherwise be the message that vanishes.
                if exc.code == 429 and attempt < 2:
                    time.sleep(min(float(wait or 1) + 0.5, 30))
                    continue
                raise TelegramError(f"{method}: HTTP {exc.code} {detail}") from None
            except urllib.error.URLError as exc:
                if attempt < 2:
                    time.sleep(1 + attempt)
                    continue
                raise TelegramError(f"{method}: {exc.reason}") from None
            if not data.get("ok"):
                raise TelegramError(f"{method}: {data.get('description')}")
            return data.get("result")
        raise TelegramError(f"{method}: gave up after 3 attempts")

    # -- receiving ---------------------------------------------------------

    def get_updates(self, timeout: int = 30) -> list:
        """Long-poll. Returns updates and advances the offset past them.

        `allowed_updates` is deliberately narrow: this bot has no business
        being added to groups or channels, so it does not ask to hear about
        them. That is a convenience, not the access control — the whitelist in
        `bot.py` is what actually decides, because a setting on the API side is
        not something we can prove from here.
        """
        result = self._post("getUpdates", {
            "offset": self.offset,
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
        }, timeout=timeout + 10)
        if result:
            self.offset = max(u["update_id"] for u in result) + 1
        return result or []

    def get_file_path(self, file_id: str) -> str:
        return self._post("getFile", {"file_id": file_id})["file_path"]

    def download(self, file_id: str, dest: Path) -> Path:
        remote = self.get_file_path(file_id)
        url = f"{API}/file/bot{self._token}/{remote}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with urllib.request.urlopen(url, timeout=120) as resp, \
                    dest.open("wb") as out:
                while chunk := resp.read(64 * 1024):
                    out.write(chunk)
        except urllib.error.URLError as exc:
            raise TelegramError(f"download failed: {exc.reason}") from None
        return dest

    # -- sending -----------------------------------------------------------

    # HTML rather than Markdown throughout: filenames on this device are full
    # of underscores, asterisks and brackets, and Markdown would either mangle
    # them or fail the whole send. Callers escape with html.escape().
    def send_message(self, chat_id, text: str, keyboard=None, **kw) -> dict:
        return self._post("sendMessage", {
            "chat_id": chat_id, "text": text,
            "reply_markup": _markup(keyboard),
            "parse_mode": kw.get("parse_mode", "HTML"),
            "disable_web_page_preview": True,
        })

    def edit_message(self, chat_id, message_id, text: str, keyboard=None) -> dict:
        try:
            return self._post("editMessageText", {
                "chat_id": chat_id, "message_id": message_id, "text": text,
                "reply_markup": _markup(keyboard), "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })
        except TelegramError as exc:
            # Editing a message to exactly what it already says is an API
            # error and never a problem worth surfacing.
            if "not modified" in str(exc):
                return {}
            raise

    def edit_markup(self, chat_id, message_id, keyboard) -> dict:
        """Replace a message's buttons, leaving the message alone.

        What makes a tick-list feel live: the sheet stays put and only the
        keyboard under it changes. `editMessageText` cannot be used here at all
        — the sheet is a photo, and a photo has a caption, not text.
        """
        try:
            return self._post("editMessageReplyMarkup", {
                "chat_id": chat_id, "message_id": message_id,
                "reply_markup": _markup(keyboard) or json.dumps(
                    {"inline_keyboard": []}),
            })
        except TelegramError as exc:
            if "not modified" in str(exc):
                return {}
            raise

    def send_photo(self, chat_id, photo: Path, caption: str = "",
                   keyboard=None) -> dict:
        return self._post("sendPhoto",
                          {"chat_id": chat_id, "caption": caption,
                           "parse_mode": "HTML",
                           "reply_markup": _markup(keyboard)},
                          files={"photo": photo})

    def send_document(self, chat_id, doc: Path, caption: str = "") -> dict:
        return self._post("sendDocument",
                          {"chat_id": chat_id, "caption": caption},
                          files={"document": doc})

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        try:
            self._post("answerCallbackQuery",
                       {"callback_query_id": callback_id, "text": text or None})
        except TelegramError:
            pass        # an expired callback id is not worth a crash

    def set_commands(self, commands: list) -> None:
        self._post("setMyCommands", {"commands": commands})

    def get_me(self) -> dict:
        return self._post("getMe")


def _markup(keyboard) -> str | None:
    """Rows of (label, callback_data) tuples -> an inline keyboard."""
    if not keyboard:
        return None
    rows = [[{"text": label, "callback_data": data} for label, data in row]
            for row in keyboard]
    return json.dumps({"inline_keyboard": rows})


def _multipart(fields: dict, files: dict):
    boundary = f"----x3tgbot{uuid.uuid4().hex}"
    parts = []
    for key, value in fields.items():
        parts.append(f"--{boundary}\r\n"
                     f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                     f"{value}\r\n".encode())
    for key, path in files.items():
        path = Path(path)
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        parts.append(f"--{boundary}\r\n"
                     f'Content-Disposition: form-data; name="{key}"; '
                     f'filename="{path.name}"\r\n'
                     f"Content-Type: {ctype}\r\n\r\n".encode())
        parts.append(path.read_bytes())
        parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"
