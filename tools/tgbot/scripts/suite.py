#!/usr/bin/env python3
"""The bot's hands: every call it makes into the rest of the suite.

Two kinds of call, and the difference is deliberate.

**Pipelines are subprocesses.** Building a wallpaper, converting a PDF, writing
a graded reader — each is a script with a command line, typed output and a
non-zero exit on failure, and calling it any other way would make the bot a
second place where the pipeline's steps are written down. This is the pattern
`ai-tools/graded-reader/headless/run_book.py` already uses on the same scripts.
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

import hashlib
import importlib.util
import json
import struct
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def _borrow(name: str, path: Path):
    """Import one module from another unit, by path, without touching sys.path.

    Adding a sibling `scripts/` directory to sys.path looks harmless and is
    not: every unit here has a `config.py`, so putting `tools/opds-server/scripts`
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
                 REPO_ROOT / "tools" / "wallpaper-maker" / "scripts" / "crosspoint_device.py")
opds_client = _borrow("crosspoint_client",
                      REPO_ROOT / "tools" / "opds-server" / "scripts" / "crosspoint_client.py")

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
    rc, out, err = run([PY, "tools/opds-server/scripts/library.py", "--json"], timeout=120)
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


INGEST_BOOK = REPO_ROOT / "tools" / "opds-server" / "scripts" / "ingest_book.py"


def ingest_book(path: Path, library_dir: Path, alias: str = "") -> dict:
    """Ask the catalog to inspect, name and file one EPUB.

    A `needs_alias` result is a normal pause, not an error: Telegram gathers
    the user's short name and repeats this exact call with `alias` set.
    """
    cmd = [PY, str(INGEST_BOOK), "--json", "ingest", str(path),
           "--library", str(library_dir)]
    if alias:
        cmd += ["--alias", alias]
    rc, out, err = run(cmd, timeout=180)
    return _json_out(rc, out, err, "book ingest")


def set_series_alias(library_dir: Path, series: str, alias: str,
                     dry_run: bool = False) -> dict:
    cmd = [PY, str(INGEST_BOOK), "--json", "set-alias", series, alias,
           "--library", str(library_dir)]
    if dry_run:
        cmd.append("--dry-run")
    rc, out, err = run(cmd, timeout=180)
    return _json_out(rc, out, err, "series alias")


# -- wallpapers ------------------------------------------------------------


def probe_image(src: Path) -> dict:
    """Does this image fill the panel, or does it need a mat?

    The threshold lives in make_wallpaper.py and is asked, never copied — see
    its `probe()`. A 500x750 photo is smaller than the panel in both dimensions
    and still fills.
    """
    rc, out, err = run([PY_DEPS, "tools/wallpaper-maker/scripts/make_wallpaper.py",
                        str(src), "--probe"], timeout=180)
    reports = _json_out(rc, out, err, "probe")
    if not reports:
        raise SuiteError("that file did not read as an image")
    return reports[0]


def build_wallpaper(src: Path, mat: str = "waves") -> tuple:
    """Convert one image. Returns (bmp, preview_png)."""
    rc, out, err = run([PY_DEPS, "tools/wallpaper-maker/scripts/make_wallpaper.py",
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


def local_wallpapers() -> list:
    """Every wallpaper this server has built, newest first.

    `workspace/wallpapers/build/` was always the collection — the converter
    writes there and nothing ever cleans it — but until now the only way to see
    it was over ssh. Pushing a wallpaper leaves the file behind by design, so
    the folder accumulates exactly the set worth re-sending after a card wipe.
    """
    out = []
    if not WALLPAPER_OUT.is_dir():
        return out
    for bmp in sorted(WALLPAPER_OUT.glob("*.bmp"),
                      key=lambda p: p.stat().st_mtime, reverse=True):
        png = bmp.with_suffix(".png")
        out.append({"name": bmp.name, "path": str(bmp),
                    "bytes": bmp.stat().st_size, "mtime": bmp.stat().st_mtime,
                    "png": str(png) if png.exists() else None})
    return out


def contact_sheet(files: list, dest: Path, start: int = 1) -> dict:
    """One numbered picture of many wallpapers.

    Replaces a message per wallpaper with a single upload, which is both
    cheaper and the end of the burst that used to earn a rate-limit. The
    numbers drawn on the cells are what the buttons underneath refer to.
    """
    cmd = [PY_DEPS, "tools/wallpaper-maker/scripts/contact_sheet.py", str(dest),
           "--start", str(start)] + [str(f) for f in files]
    rc, out, err = run(cmd, timeout=300)
    return _json_out(rc, out, err, "contact sheet")


def bmp_preview(bmp: Path, png: Path) -> dict:
    """Render a wallpaper as the *panel* would draw it.

    Through `crosspoint_bmp`, the port of the firmware's own reader, not
    through a general image library — a wallpaper on the SD card is worth
    looking at precisely when it does not look how you expect, and the useful
    question is what the device does with it. The report says whether the
    preview is exact or whether the firmware would re-dither that file.
    """
    rc, out, err = run([PY_DEPS, "tools/wallpaper-maker/scripts/crosspoint_bmp.py",
                        str(bmp), "--png", str(png)], timeout=180)
    return _json_out(rc, out, err, "preview")


# -- fonts -----------------------------------------------------------------

FONTS_DIR = REPO_ROOT / "extras" / "fonts"
UI_FALLBACK_SIZES = {8, 10, 12}

# 一 あ ア 가 — Han, Hiragana, Katakana, Hangul. Not a selection of our own:
# it is `kCjkProbes` from the firmware's SdCardFontSystem.cpp, the exact test
# 1.5.0 applies before it will use an SD family for interface text.
CJK_PROBES = (0x4E00, 0x3042, 0x30A2, 0xAC00)


def cpfont_intervals(path) -> list:
    """The codepoint ranges one `.cpfont` carries, read out of the file.

    Header (32 bytes) → first style's TOC entry (32) → the interval table it
    points at, three uint32s per entry. Asking the file is the only honest way
    to know what a family covers: the filename says the point size and nothing
    else, and a family's script is not something to infer from its name.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(64)
            if len(head) < 64 or head[:8] != b"CPFONT\x00\x00":
                return []
            if head[12] < 1:                       # styleCount
                return []
            # Style TOC entry "<B3xIIBhhHHBBBI4x" at 32: intervalCount is its
            # second field (+4), dataOffset its last (+24).
            n_int = struct.unpack_from("<I", head, 36)[0]
            data_offset = struct.unpack_from("<I", head, 56)[0]
            if not 0 < n_int <= 4096:              # a corrupt count, not a font
                return []
            fh.seek(data_offset)
            table = fh.read(12 * n_int)
    except OSError:
        return []
    if len(table) < 12 * n_int:
        return []
    return [struct.unpack_from("<III", table, 12 * i)[:2] for i in range(n_int)]


def font_covers_cjk(files) -> bool:
    """Would 1.5.0 use this family to draw CJK in the interface?

    Only families that pass this get asked for their 8/10/12 pt files, so it
    is also the answer to "does this family *need* them". Unreadable files say
    yes: the three CJK families this repo ships are the norm, and a warning
    that fires when we cannot tell is the safe way round.
    """
    for path in files:
        intervals = cpfont_intervals(path)
        if intervals:
            return any(start <= cp <= end
                       for cp in CJK_PROBES for start, end in intervals)
    return True


def checksums() -> dict:
    """`extras/fonts/CHECKSUMS.tsv` as {relative path: (bytes, md5)}."""
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
    """The families in `extras/fonts/`, with what each one ships.

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
            # Whether the missing sizes would matter. The interface fallback
            # only ever asks a family that draws CJK, so an Arabic or Latin
            # family shipping 12-18 is complete, not short of three files.
            "cjk": font_covers_cjk(str(f) for f in files),
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


def family_name_problem(name: str) -> str | None:
    """Why the reader would reject or ignore this family name — or None.

    Two different rules, from two different places in the firmware, and a name
    has to satisfy both:

    - `FontInstaller::isValidFamilyName` allows **only** `[A-Za-z0-9_-]`. It
      guards the upload *and the delete*, so a family whose folder holds a
      space, a dot or an accent can be created by hand (or by a WebDAV rename,
      which applies no such rule) and then **cannot be deleted through the
      firmware's own endpoint** — it answers 500 for the name itself.
    - `SdCardFontRegistry::scanRoot` skips any directory whose name starts with
      `.` or `_`, so such a family is simply invisible to the reader, with no
      error anywhere.
    """
    if not name:
        return "an empty name"
    if name.startswith((".", "_")):
        return ("the reader's scan skips folders starting with <code>.</code> "
                "or <code>_</code> — the family would vanish from the picker")
    bad = sorted({c for c in name if not (c.isascii() and (c.isalnum() or c in "-_"))})
    if bad:
        shown = " ".join("space" if c == " " else f"<code>{c}</code>" for c in bad)
        return (f"the reader only accepts letters, digits, <code>-</code> and "
                f"<code>_</code> in a family name (not {shown}) — a folder it "
                f"cannot name is one it will refuse to delete")
    if len(name.encode("utf-8")) > 100:
        return "that is longer than the reader's name buffer"
    return None


def device_font_location(host: str, family: str) -> tuple:
    """Where the family's folder actually is: (root, state).

    `GET /api/fonts` reports names, sizes and files, **never a path**, and the
    firmware scans two roots — `/.fonts` first, then `/fonts`. Renaming needs
    the visible one (WebDAV refuses dot-prefixed segments), so the folder has
    to be found rather than assumed. Three answers, and they are not the same:

    - `("/fonts", "visible")` — the folder is there and can be renamed.
    - `(None, "hidden-root")` — there is no `/fonts` at all, so every family is
      in `/.fonts` and none of them can be renamed by any route here.
    - `(None, "missing")` — `/fonts` exists but has no folder by that name.
      Either the family lives in `/.fonts` alongside a visible root, or the
      card changed since the reader last scanned its fonts — which is what a
      rename through this bot does. Do not report that as a hidden family: it
      is the state the registry is lying about.
    """
    roots = {e.get("name") for e in device.list_dir(host, "/")
             if e.get("isDirectory")}
    if "fonts" not in roots:
        return None, "hidden-root"
    for entry in device.list_dir(host, "/fonts"):
        if entry.get("isDirectory") and entry.get("name") == family:
            return "/fonts", "visible"
    return None, "missing"


def rescan_device_fonts(host: str) -> None:
    """Make the reader re-read its fonts folders, without a power-cycle.

    The registry is built at boot and refreshed **only** when something marks
    it dirty — `GET /api/fonts` calls `refreshIfDirty()`, not `discover()` —
    and the only two things that mark it are the font upload and delete
    endpoints. A rename through WebDAV marks nothing, which is why a renamed
    family keeps its old name in the list, reports 0 B for files whose paths no
    longer open, and cannot be selected under its new name.

    The way out is the delete endpoint's own no-op branch:
    `FontInstaller::deleteFamily` walks both roots, and when the family is in
    neither it removes nothing and returns OK — at which point the handler
    marks the registry dirty. So a delete aimed at a name that cannot exist is
    a remote re-scan. The probe name is random and passes the firmware's
    `[A-Za-z0-9_-]` rule, so it can match nothing and delete nothing.
    """
    device.delete_font_family(host, "cprescan" + uuid.uuid4().hex[:8])
    device.fonts(host)          # the GET is what acts on the dirty flag


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


def device_book_name_candidates(books: list, host: str) -> list:
    """Exact SD-root names per book, one settings read.

    The base-title variant is included for a volume downloaded before the
    catalog gained its series alias. Nothing fuzzy is ever deleted.
    """
    fmt = opds_client.FILENAME_AUTHOR_TITLE
    try:
        fmt = int(device.setting_value(host, "opdsFilenameFormat", fmt))
    except (DeviceError, TypeError, ValueError):
        pass
    candidates = []
    for book in books:
        titles = [book.get("title", "")]
        if book.get("base_title") and book["base_title"] not in titles:
            titles.append(book["base_title"])
        candidates.append(list(dict.fromkeys(
            opds_client.opds_book_filename(book.get("author", ""), title, fmt)
            for title in titles)))
    return candidates


SLIM = REPO_ROOT / "tools" / "epub-slimmer" / "scripts" / "slim_epub.py"
# Below this, substituting a slimmed copy buys nothing worth the second file:
# books built by this suite already carry no fonts and a panel-sized cover.
SLIM_WORTH_IT = 0.05


def slim_book(src: Path, cache_dir: Path) -> dict:
    """Slim a book for the device, keeping the original untouched.

    The server holds the book you were given; the reader gets the one it can
    actually use. Slimming is deterministic, so the result is cached by content
    hash and a book queued twice is only ever built once.

    Returns {"path", "before", "after", "saved", "used"} — `used` is False when
    the saving was too small to be worth a second file, in which case `path` is
    the original and pushing behaves exactly as it did before.
    """
    before = src.stat().st_size
    digest = hashlib.sha1(src.read_bytes()).hexdigest()[:16]
    dest = cache_dir / f"{digest}.epub"
    plain = {"path": src, "before": before, "after": before, "saved": 0,
             "used": False}
    if not dest.exists():
        cache_dir.mkdir(parents=True, exist_ok=True)
        rc, out, err = run([PY_DEPS, str(SLIM), str(src), "--out", str(dest),
                            "--json"], timeout=300)
        if rc != 0 or not dest.exists():
            # A book that will not slim is still a book: push what we have.
            return plain
    after = dest.stat().st_size
    if before and (before - after) / before < SLIM_WORTH_IT:
        return plain
    return {"path": dest, "before": before, "after": after,
            "saved": before - after, "used": True}


def drop_slim(path: Path, cache_dir: Path) -> bool:
    """Delete a slimmed copy, and refuse to delete anything else.

    The slim cache is scratch space that exists between queueing a book and
    pushing it, so a pushed book's copy is rubbish the moment it lands. The
    guard is the whole of this function's job: `slim_book` hands back the
    *original* when slimming was not worth it, and unlinking that would delete
    a book out of the user's library.
    """
    path, cache_dir = Path(path).resolve(), Path(cache_dir).resolve()
    if cache_dir not in path.parents:
        return False
    path.unlink(missing_ok=True)
    return True


def prune_slim_cache(cache_dir: Path, keep) -> int:
    """Leave only the copies the queue still refers to.

    Anything else is a book that was pushed, or one dropped from the queue
    without ever being sent. Either way the cache is not a store: it holds work
    in progress and nothing more.
    """
    cache_dir = Path(cache_dir)
    if not cache_dir.is_dir():
        return 0
    wanted = {str(Path(k).resolve()) for k in keep}
    gone = 0
    for f in cache_dir.glob("*.epub"):
        if str(f.resolve()) not in wanted:
            f.unlink(missing_ok=True)
            gone += 1
    return gone


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
    cmd = [PY, "tools/wallpaper-maker/scripts/push_wallpaper.py", "--json"]
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
    rc, o, err = run([PY_DEPS, "ai-tools/pdf2epub/scripts/triage.py", str(pdf),
                      "--out", str(out)], timeout=600)
    if rc != 0:
        raise SuiteError((err or o).strip() or "triage failed")
    try:
        return json.loads(out.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise SuiteError("triage wrote nothing readable") from None


def pdf2epub_runner() -> Path | None:
    """The headless entry point, if it exists yet.

    It does not, at the time of writing — `ai-tools/pdf2epub/DESIGN.md` open
    question 6 has the plan and the reasoning. The bot is built to call it and
    to say plainly when it cannot, rather than to pretend conversion is a thing
    it does.
    """
    p = REPO_ROOT / "ai-tools" / "pdf2epub" / "headless" / "run_conversion.py"
    return p if p.exists() else None


def graded_reader_ready() -> tuple:
    """(ready, why-not). The bot owns no AI configuration of its own — it only
    reports what the service says about itself."""
    cfg = REPO_ROOT / "ai-tools" / "graded-reader" / "headless" / "config.json"
    if not cfg.exists():
        return False, ("ai-tools/graded-reader/headless/config.json is missing "
                       "— copy config.example.json next to it and add a key")
    return True, ""


def run_book(book_dir: Path, chapter: int | None = None) -> tuple:
    cmd = [PY_DEPS, "ai-tools/graded-reader/headless/run_book.py", str(book_dir)]
    if chapter:
        cmd += ["--chapter", str(chapter)]
    rc, out, err = run(cmd, timeout=3600)
    return rc == 0, (out + err).strip()
