#!/usr/bin/env python3
"""The bot's hands: every call it makes into the rest of the suite.

Two kinds of call, and the difference is deliberate.

**Pipelines are subprocesses.** Building a wallpaper, converting a PDF, writing
a graded reader — each is a script with a command line, typed output and a
non-zero exit on failure, and calling it any other way would make the bot a
second place where the pipeline's steps are written down. This is the pattern
`services/graded-reader/headless/run_book.py` already uses on the same scripts.
It also means a failure arrives as text we can forward to the chat verbatim
rather than as a traceback only a developer could love.

**The device is imported.** `crosspoint_device` is transport, not a pipeline:
there is no CLI for "rename this file on the reader" and inventing one so the
bot could subprocess it would be theatre. It is the same module
`push_wallpaper.py` uses, so there is exactly one description of the reader's
API in the repo.

Nothing here is imported *by* the suite. The dependency points one way, always:
someone who only ever builds EPUBs must never need a Telegram token.
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "wallpaper-maker" / "scripts"))

import crosspoint_device as device            # noqa: E402  (after the path fix)

DeviceError = device.DeviceError

# Stdlib-only scripts run on whatever interpreter we are; the ones with
# dependencies (Pillow, the services' packages) want the repo venv AGENTS.md
# describes. Falling back to our own interpreter keeps a missing venv a clear
# ImportError from the script itself rather than a puzzling "not found" here.
PY = sys.executable
VENV = REPO_ROOT / ".venv" / "bin" / "python"
PY_DEPS = str(VENV) if VENV.exists() else sys.executable

WALLPAPER_IN = REPO_ROOT / "workspace" / "wallpapers"
WALLPAPER_OUT = WALLPAPER_IN / "build"


class SuiteError(RuntimeError):
    pass


def run(cmd: list, timeout: int = 900) -> tuple:
    """(returncode, stdout, stderr) — never raises on a non-zero exit."""
    try:
        p = subprocess.run([str(c) for c in cmd], capture_output=True,
                           text=True, timeout=timeout, cwd=REPO_ROOT)
    except subprocess.TimeoutExpired:
        return 1, "", f"timed out after {timeout}s"
    except FileNotFoundError as exc:
        return 1, "", str(exc)
    return p.returncode, p.stdout, p.stderr


def _json_out(rc: int, out: str, err: str, what: str):
    if rc != 0 and not out.strip():
        raise SuiteError(err.strip() or out.strip() or f"{what} failed")
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        raise SuiteError(err.strip() or f"{what}: unreadable output") from None


# -- the library -----------------------------------------------------------


def library() -> dict:
    """What opds-server would serve, straight from its own scanner.

    Deliberately not a directory walk of our own: the catalog's idea of the
    library (OPF metadata, the exclude globs, the stable ids) is the one that
    matters, because it is what the device sees.
    """
    rc, out, err = run([PY, "opds-server/scripts/library.py", "--json"], timeout=120)
    return _json_out(rc, out, err, "library")


def opds_up(url: str, timeout: float = 3) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def verify_epub(path: Path) -> tuple:
    rc, out, err = run([PY, "epub-builder/scripts/verify_epub.py", str(path)],
                       timeout=120)
    return rc == 0, (out + err).strip()


# -- wallpapers ------------------------------------------------------------


def probe_image(src: Path) -> dict:
    """Does this image fill the panel, or does it need a mat?

    The threshold lives in make_wallpaper.py and is asked, never copied — see
    its `probe()`. A 500x750 photo is smaller than the panel in both dimensions
    and still fills.
    """
    rc, out, err = run([PY_DEPS, "wallpaper-maker/scripts/make_wallpaper.py",
                        str(src), "--probe"], timeout=180)
    reports = _json_out(rc, out, err, "probe")
    if not reports:
        raise SuiteError("that file did not read as an image")
    return reports[0]


def build_wallpaper(src: Path, mat: str = "waves") -> tuple:
    """Convert one image. Returns (bmp, preview_png)."""
    rc, out, err = run([PY_DEPS, "wallpaper-maker/scripts/make_wallpaper.py",
                        str(src), "--out", str(WALLPAPER_OUT),
                        "--mat", mat, "--preview"], timeout=300)
    if rc != 0:
        raise SuiteError((err or out).strip() or "the converter failed")
    # make_wallpaper sanitizes the stem; find what it actually wrote rather
    # than guessing at its naming rules.
    candidates = sorted(WALLPAPER_OUT.glob("*.bmp"), key=lambda p: p.stat().st_mtime)
    if not candidates:
        raise SuiteError("the converter reported success but wrote nothing")
    bmp = candidates[-1]
    return bmp, bmp.with_suffix(".png")


def push(files: list) -> dict:
    """Drain to the device. One JSON record per file, so the chat can say
    which ones landed."""
    cmd = [PY, "wallpaper-maker/scripts/push_wallpaper.py", "--json"]
    cmd += [str(f) for f in files]
    rc, out, err = run(cmd, timeout=600)
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"ok": False, "items": [],
                "error": (err or out).strip() or "the push script said nothing"}


# -- conversions -----------------------------------------------------------


def triage_pdf(pdf: Path, out: Path) -> dict:
    rc, o, err = run([PY_DEPS, "services/pdf2epub/scripts/triage.py", str(pdf),
                      "--out", str(out)], timeout=600)
    if rc != 0:
        raise SuiteError((err or o).strip() or "triage failed")
    try:
        return json.loads(out.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise SuiteError("triage wrote nothing readable") from None


def pdf2epub_runner() -> Path | None:
    """The headless entry point, if it exists yet.

    It does not, at the time of writing — `services/pdf2epub/DESIGN.md` open
    question 6 has the plan and the reasoning. The bot is built to call it and
    to say plainly when it cannot, rather than to pretend conversion is a thing
    it does.
    """
    p = REPO_ROOT / "services" / "pdf2epub" / "headless" / "run_conversion.py"
    return p if p.exists() else None


def graded_reader_ready() -> tuple:
    """(ready, why-not). The bot owns no AI configuration of its own — it only
    reports what the service says about itself."""
    cfg = REPO_ROOT / "services" / "graded-reader" / "headless" / "config.json"
    if not cfg.exists():
        return False, ("services/graded-reader/headless/config.json is missing "
                       "— copy config.example.json next to it and add a key")
    return True, ""


def run_book(book_dir: Path, chapter: int | None = None) -> tuple:
    cmd = [PY_DEPS, "services/graded-reader/headless/run_book.py", str(book_dir)]
    if chapter:
        cmd += ["--chapter", str(chapter)]
    rc, out, err = run(cmd, timeout=3600)
    return rc == 0, (out + err).strip()
