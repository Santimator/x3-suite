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

import importlib.util
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _borrow(name: str, path: Path):
    """Import one module from another unit, by path, without touching sys.path.

    Adding a sibling `scripts/` directory to sys.path looks harmless and is
    not: every unit here has a `config.py`, so putting `opds-server/scripts`
    on the path makes `import config` ambiguous, and which one wins depends on
    the order modules happened to be imported in. That failure is invisible
    from a test that imports things in a different order than the real entry
    point does — this bot shipped with exactly that bug, passing its own gate
    while refusing to start.

    Loading by explicit path gives the module the name we ask for and puts
    nothing on the search path, so no other unit's files can be reached by
    accident. Both modules below are stdlib-only, so nothing follows them in.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


device = _borrow("crosspoint_device",
                 REPO_ROOT / "wallpaper-maker" / "scripts" / "crosspoint_device.py")
opds_client = _borrow("crosspoint_client",
                      REPO_ROOT / "opds-server" / "scripts" / "crosspoint_client.py")

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


def bmp_preview(bmp: Path, png: Path) -> dict:
    """Render a wallpaper as the *panel* would draw it.

    Through `crosspoint_bmp`, the port of the firmware's own reader, not
    through a general image library — a wallpaper on the SD card is worth
    looking at precisely when it does not look how you expect, and the useful
    question is what the device does with it. The report says whether the
    preview is exact or whether the firmware would re-dither that file.
    """
    rc, out, err = run([PY_DEPS, "wallpaper-maker/scripts/crosspoint_bmp.py",
                        str(bmp), "--png", str(png)], timeout=180)
    return _json_out(rc, out, err, "preview")


# -- fonts -----------------------------------------------------------------

FONTS_DIR = REPO_ROOT / "reference" / "fonts"
UI_FALLBACK_SIZES = {8, 10, 12}


def checksums() -> dict:
    """`reference/fonts/CHECKSUMS.tsv` as {relative path: (bytes, md5)}."""
    out = {}
    path = FONTS_DIR / "CHECKSUMS.tsv"
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("#") or not line.strip():
                continue
            rel, size, md5 = (line.split("\t") + ["", ""])[:3]
            out[rel] = (int(size), md5)
    except (OSError, ValueError):
        pass
    return out


def local_font_families() -> list:
    """The families in `reference/fonts/`, with what each one ships.

    `sizes` is worth showing before anyone pushes 24 MB over WiFi: 1.5.0's
    interface fallback looks for exactly 8, 10 and 12 pt, so a family without
    them renders books beautifully and leaves the chapter list blank.
    """
    families = []
    if not FONTS_DIR.is_dir():
        return families
    for d in sorted(p for p in FONTS_DIR.iterdir() if p.is_dir()):
        files = sorted(d.glob("*.cpfont"))
        if not files:
            continue
        sizes = sorted({int(m) for f in files
                        for m in [f.stem.rpartition("_")[2]] if m.isdigit()})
        families.append({
            "name": d.name,
            "path": str(d),
            "files": [str(f) for f in files],
            "sizes": sizes,
            "bytes": sum(f.stat().st_size for f in files),
            "ui_ready": UI_FALLBACK_SIZES.issubset(set(sizes)),
        })
    return families


def verify_font_family(host: str, family: str) -> list:
    """Compare what the device holds against CHECKSUMS.tsv, byte for byte.

    The check that matters. The reader lists fonts by *filename only* — it
    never opens them — so a truncated copy shows up in the picker and only
    fails when selected, at which point the family silently reverts to
    built-in Noto. The upload validates the CPFONT magic, which catches a
    downloaded HTML page; it cannot catch a short write. Sizes can.

    Returns one record per expected file: name, expected, actual, ok.
    """
    expected = {rel.split("/", 1)[1]: size
                for rel, (size, _) in checksums().items()
                if rel.startswith(f"{family}/")}
    on_device = {}
    for entry in device.fonts(host).get("families", []):
        if entry.get("name") == family:
            on_device = {f["name"]: f.get("size", 0) for f in entry.get("files", [])}
            break

    if not expected:      # a family of your own, with nothing to compare to
        return [{"name": name, "expected": None, "actual": size, "ok": size > 0}
                for name, size in sorted(on_device.items())]
    return [{"name": name, "expected": size, "actual": on_device.get(name),
             "ok": on_device.get(name) == size}
            for name, size in sorted(expected.items())]


def sleep_dir(host: str) -> str:
    """Which folder the device is actually reading wallpapers from.

    The firmware checks /.sleep first and only falls back to /sleep when it
    does not exist — and /.sleep never appears in a listing, because
    /api/files hides dot-prefixed entries. So the bot cannot find it by
    browsing; it has to ask the same question push_wallpaper.py asks, and
    without creating anything.
    """
    if device.list_dir(host, "/.sleep"):
        return "/.sleep"
    if device.list_dir(host, "/sleep"):
        return "/sleep"
    return "/.sleep"


def device_book_name(author: str, title: str, host: str | None = None) -> str:
    """The name this book would land under if the reader downloaded it.

    Pushing a book and pulling the same book must produce **one** file on the
    SD card, not two — so the upload borrows the OPDS client's own naming,
    ported in `crosspoint_client.opds_book_filename`. Since 1.5.0 the layout is
    a device setting (`opdsFilenameFormat`), so when the reader is in front of
    us we ask it rather than assuming the default.
    """
    fmt = opds_client.FILENAME_AUTHOR_TITLE
    if host:
        try:
            fmt = int(device.setting_value(host, "opdsFilenameFormat", fmt))
        except (DeviceError, TypeError, ValueError):
            pass            # the default is also the pre-1.5.0 behaviour
    return opds_client.opds_book_filename(author or "", title or "", fmt)


def upload_book(host: str, path: Path, name: str) -> None:
    """Put a book on the SD root, where the reader looks for books.

    The firmware refuses an upload onto an existing name rather than
    overwriting, so a replacement deletes first — the same dance
    push_wallpaper.py does. Deleting here is intended: the name is derived from
    author and title, so a collision *is* this book.
    """
    if any(e.get("name") == name for e in device.list_dir(host, "/")):
        device.delete(host, f"/{name}")
    device.upload(host, "/", path, content_type="application/epub+zip", name=name)


def push(files: list, host: str | None = None) -> dict:
    """Drain wallpapers to the device. One JSON record per file, so the chat
    can say which ones landed."""
    cmd = [PY, "wallpaper-maker/scripts/push_wallpaper.py", "--json"]
    if host:
        cmd += ["--ip", host]
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
