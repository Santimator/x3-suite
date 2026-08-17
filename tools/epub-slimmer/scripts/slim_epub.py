#!/usr/bin/env python3
"""Strip an EPUB down to what the X3 can actually use.

A book downloaded from anywhere carries a great deal this reader will never
look at. The two big ones are settled facts rather than guesses, both recorded
in `extras/readers.md`:

  * **Embedded fonts are ignored.** The renderer only rasterizes pre-converted
    `.cpfont` bitmaps, so every byte of `@font-face` is dead weight. On a
    typical commercial EPUB that alone is most of the file.
  * **Colour is dropped and detail is wasted.** The panel is 528x792, four
    grey levels. A 2000px full-colour plate is decoded, converted and thrown
    away every time it is drawn.

So: drop the fonts, drop what the engine cannot run at all, and re-encode the
pictures to something the panel can show. Nothing else. This does not touch a
word of the text, the metadata, the spine or the nav — a slimmed book is the
same book, and `verify_epub.py` is run on the result before it is written.

**Deterministic**, in the way the font recipe is: the same EPUB in gives a
byte-identical EPUB out, so a slimmed copy can be cached and reused rather than
rebuilt. That holds for a given Pillow — image encoders are free to change
their output between versions, and this tool has no way to notice if they do.

Usage:
  slim_epub.py BOOK.epub --out slim.epub
  slim_epub.py BOOK.epub --out slim.epub --json      # report, for a caller
  slim_epub.py BOOK.epub --probe                     # what it would save
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "epub-builder" / "scripts"))

from verify_epub import verify_integrity  # noqa: E402

# The panel, and the width figures already used for content images elsewhere in
# the suite (epub-builder's prepare.py). Not invented here.
PANEL = (528, 792)
CONTENT_WIDTH = 480
JPEG_QUALITY = 72

FONT_TYPES = ("font/", "application/font", "application/vnd.ms-opentype",
              "application/x-font")
FONT_SUFFIXES = (".ttf", ".otf", ".woff", ".woff2", ".eot")
DEAD_TYPES = ("audio/", "video/", "text/javascript", "application/javascript",
              "application/x-javascript")
DEAD_SUFFIXES = (".js", ".mp3", ".m4a", ".mp4", ".webm", ".ogg", ".wav")
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png")

# `@font-face { ... }` including the braces, and any font-family declaration.
FONT_FACE_RE = re.compile(r"@font-face\s*\{[^}]*\}\s*", re.IGNORECASE)
FONT_FAMILY_RE = re.compile(r"[^;{}]*\bfont-family\s*:[^;}]*;?", re.IGNORECASE)

OPF_NS = "{http://www.idpf.org/2007/opf}"


def is_font(name: str, media_type: str) -> bool:
    return (media_type.startswith(FONT_TYPES)
            or name.lower().endswith(FONT_SUFFIXES))


def is_dead_weight(name: str, media_type: str) -> bool:
    """Something the engine cannot run, so shipping it is pure cost."""
    return (media_type.startswith(DEAD_TYPES)
            or name.lower().endswith(DEAD_SUFFIXES))


def find_opf(z: zipfile.ZipFile) -> str:
    container = ET.fromstring(z.read("META-INF/container.xml"))
    for rootfile in container.iter():
        if rootfile.tag.endswith("rootfile") and rootfile.get("full-path"):
            return rootfile.get("full-path")
    raise ValueError("no rootfile in META-INF/container.xml")


def cover_href(opf_root: ET.Element, opf_dir: str) -> str | None:
    """Which manifest item is the cover, by either of the two conventions.

    EPUB 3 marks it `properties="cover-image"`; EPUB 2 points at it with a
    `<meta name="cover" content="ID">`. Books in the wild use both, sometimes
    at once, so look for both and take whichever answers.
    """
    ids = {}
    for item in opf_root.iter(f"{OPF_NS}item"):
        ids[item.get("id")] = item.get("href", "")
        if "cover-image" in (item.get("properties") or ""):
            return join(opf_dir, item.get("href", ""))
    for meta in opf_root.iter(f"{OPF_NS}meta"):
        if meta.get("name") == "cover" and meta.get("content") in ids:
            return join(opf_dir, ids[meta.get("content")])
    return None


def join(directory: str, href: str) -> str:
    """Manifest hrefs are relative to the OPF's own folder."""
    if not directory:
        return href
    return f"{directory.rstrip('/')}/{href}"


def shrink_image(data: bytes, name: str, is_cover: bool) -> tuple[bytes, dict]:
    """Grayscale, fit to the panel, re-encode in the format it arrived in.

    The format is kept deliberately: the filename stays the same, so every
    reference to it in the XHTML and CSS keeps working without this tool having
    to rewrite markup. A book that renders differently afterwards would not be
    the same book.
    """
    from PIL import Image

    note = {"name": name, "before": len(data)}
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as exc:                      # not an image we can read
        note.update(after=len(data), skipped=f"unreadable: {type(exc).__name__}")
        return data, note

    fmt = (img.format or "").upper()
    if fmt not in ("JPEG", "PNG"):
        # GIF and SVG draw as an [Image] placeholder on this device either way;
        # converting them would mean renaming and rewriting every reference.
        note.update(after=len(data), skipped=f"{fmt or 'unknown'} left alone")
        return data, note

    box = PANEL if is_cover else (CONTENT_WIDTH, PANEL[1] * 4)
    if img.mode != "L":
        img = img.convert("L")
    if img.width > box[0] or img.height > box[1]:
        img.thumbnail(box, Image.LANCZOS)

    out = io.BytesIO()
    if fmt == "JPEG":
        # Baseline, always: progressive JPEGs fall back to a placeholder.
        img.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=False)
    else:
        img.save(out, "PNG", optimize=True)
    shrunk = out.getvalue()
    if len(shrunk) >= len(data):
        # Re-encoding made it bigger: keep what the book shipped.
        note.update(after=len(data), skipped="re-encode was no smaller")
        return data, note
    note.update(after=len(shrunk), width=img.width, height=img.height)
    return shrunk, note


def strip_css(text: str) -> str:
    """Remove what can only point at fonts that are no longer there."""
    text = FONT_FACE_RE.sub("", text)
    return FONT_FAMILY_RE.sub("", text)


def rewrite_opf(opf_bytes: bytes, dropped_hrefs: set) -> bytes:
    """Take the dropped items out of the manifest.

    A manifest entry with no file behind it fails the same integrity check this
    tool runs at the end, so this is not tidiness — it is the difference
    between a valid book and a broken one.
    """
    ET.register_namespace("", "http://www.idpf.org/2007/opf")
    root = ET.fromstring(opf_bytes.decode("utf-8"))
    for parent in root.iter():
        for item in list(parent):
            if item.tag == f"{OPF_NS}item" and item.get("href") in dropped_hrefs:
                parent.remove(item)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def slim(src: Path, dest: Path | None) -> dict:
    """Slim `src` into `dest` (or measure only, when dest is None)."""
    report = {"source": str(src), "before": src.stat().st_size,
              "dropped": [], "images": [], "css_stripped": [], "kept": 0}

    with zipfile.ZipFile(src) as z:
        names = z.namelist()
        if "mimetype" not in names:
            raise ValueError("no mimetype entry — this is not an EPUB")
        opf_path = find_opf(z)
        opf_dir = opf_path.rpartition("/")[0]
        opf_root = ET.fromstring(z.read(opf_path).decode("utf-8"))
        cover = cover_href(opf_root, opf_dir)

        types = {}
        for item in opf_root.iter(f"{OPF_NS}item"):
            types[join(opf_dir, item.get("href", ""))] = item.get("media-type", "")

        entries, dropped_hrefs = [], set()
        for name in names:
            if name == "mimetype" or name.endswith("/"):
                continue
            data = z.read(name)
            media = types.get(name, "")
            if is_font(name, media) or is_dead_weight(name, media):
                report["dropped"].append({"name": name, "bytes": len(data),
                                          "why": "font" if is_font(name, media)
                                                 else "engine cannot use it"})
                # href as the manifest spells it, i.e. relative to the OPF
                dropped_hrefs.add(name[len(opf_dir) + 1:] if opf_dir else name)
                continue
            if name.lower().endswith(IMAGE_SUFFIXES):
                data, note = shrink_image(data, name, name == cover)
                report["images"].append(note)
            elif name.lower().endswith(".css"):
                text = data.decode("utf-8", "replace")
                stripped = strip_css(text)
                if stripped != text:
                    report["css_stripped"].append(
                        {"name": name, "saved": len(text) - len(stripped)})
                data = stripped.encode("utf-8")
            entries.append((name, data))

    entries = [(n, rewrite_opf(d, dropped_hrefs) if n == opf_path else d)
               for n, d in entries]
    report["kept"] = len(entries)

    payload = write_epub(entries)
    report["after"] = len(payload)
    report["saved"] = report["before"] - report["after"]
    report["ratio"] = round(report["after"] / report["before"], 4) if report["before"] else 1.0

    if dest is not None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payload)
        check = verify_integrity(dest)
        report["verified"] = check["pass"]
        report["errors"] = check["errors"]
        report["out"] = str(dest)
        if not check["pass"]:
            dest.unlink(missing_ok=True)          # never leave a broken book
    return report


def write_epub(entries: list) -> bytes:
    """One zip, written the same way every time.

    `mimetype` first and stored, everything else sorted and deflated, one fixed
    timestamp throughout — the same rules `build_epub.py` follows, for the same
    reason: two runs over one book must produce one file.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        first = zipfile.ZipInfo("mimetype", date_time=(2026, 1, 1, 0, 0, 0))
        z.writestr(first, "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        for name, data in sorted(entries, key=lambda e: e[0]):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            z.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return buf.getvalue()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Strip an EPUB to what the X3 uses.")
    ap.add_argument("epub", type=Path)
    ap.add_argument("--out", type=Path, help="where to write the slimmed book")
    ap.add_argument("--probe", action="store_true",
                    help="measure only, write nothing")
    ap.add_argument("--json", action="store_true", help="report as JSON")
    args = ap.parse_args(argv)

    if not args.probe and not args.out:
        print("error: --out or --probe", file=sys.stderr)
        return 2
    try:
        report = slim(args.epub, None if args.probe else args.out)
    except (ValueError, KeyError, zipfile.BadZipFile, ET.ParseError) as exc:
        print(f"error: {args.epub.name}: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        fonts = sum(d["bytes"] for d in report["dropped"] if d["why"] == "font")
        pics = sum(i["before"] - i["after"] for i in report["images"])
        print(f"{args.epub.name}: {report['before']:,} -> {report['after']:,} bytes "
              f"({report['ratio'] * 100:.1f}%)")
        print(f"  fonts dropped: {fonts:,} bytes in "
              f"{sum(1 for d in report['dropped'] if d['why'] == 'font')} file(s)")
        print(f"  images: {pics:,} bytes saved over {len(report['images'])}")
        if report.get("out"):
            print(f"  verified: {'yes' if report['verified'] else 'NO'} -> {report['out']}")
    if report.get("verified") is False:
        for e in report["errors"]:
            print(f"  - {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
