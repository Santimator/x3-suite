#!/usr/bin/env python3
"""The gate: does slimming a book leave it the same book?

    .venv/bin/python tools/epub-slimmer/scripts/selftest.py

Builds a deliberately fat EPUB — embedded fonts, an oversized colour cover, a
colour plate, a progressive JPEG, a stylesheet full of `@font-face` — slims it,
and grades the four things that would actually hurt:

  1. **It is still a valid book.** Structure checked with the same verifier
     both AI tools use; text, spine and nav byte-identical to what went in.
  2. **The weight is actually gone**, and gone from the places claimed.
  3. **Deterministic.** Two runs, one file. That is what lets a slimmed copy be
     cached instead of rebuilt on every push.
  4. **Nothing is broken quietly.** A manifest entry never outlives its file,
     and a book that fails verification is not written at all.
"""

from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parents[3]
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO / "epub-builder" / "scripts"))

from PIL import Image  # noqa: E402

import slim_epub  # noqa: E402
from verify_epub import verify_integrity  # noqa: E402

failures = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}")
    if not ok:
        failures.append(label)
        if detail:
            print(f"        {detail}")


def colour_image(w: int, h: int, fmt: str, progressive: bool = False) -> bytes:
    """A picture that costs real bytes: noise, so it cannot be compressed away."""
    import random
    rng = random.Random(7)                       # seeded: the fixture is fixed
    img = Image.new("RGB", (w, h))
    img.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256))
                 for _ in range(w * h)])
    out = io.BytesIO()
    if fmt == "JPEG":
        img.save(out, "JPEG", quality=95, progressive=progressive)
    else:
        img.save(out, "PNG")
    return out.getvalue()


CONTAINER = b"""<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf"
    media-type="application/oebps-package+xml"/></rootfiles>
</container>"""

OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">urn:uuid:fat-book</dc:identifier>
    <dc:title>A Fat Book</dc:title><dc:creator>Someone</dc:creator>
    <dc:language>en</dc:language>
    <meta name="cover" content="cover-img"/>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>
    <item id="css" href="style.css" media-type="text/css"/>
    <item id="cover-img" href="images/cover.jpg" media-type="image/jpeg"/>
    <item id="plate" href="images/plate.png" media-type="image/png"/>
    <item id="prog" href="images/prog.jpg" media-type="image/jpeg"/>
    <item id="f1" href="fonts/Serif.ttf" media-type="font/ttf"/>
    <item id="f2" href="fonts/Sans.otf" media-type="application/vnd.ms-opentype"/>
    <item id="js" href="js/reader.js" media-type="text/javascript"/>
  </manifest>
  <spine><itemref idref="ch1"/></spine>
</package>"""

NAV = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>Contents</title></head><body><nav epub:type="toc">
<ol><li><a href="ch1.xhtml">One</a></li></ol></nav></body></html>"""

CH1 = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>One</title>
<link rel="stylesheet" href="style.css"/></head>
<body><h1>One</h1><p>The text is the point, and it must survive.</p>
<img src="images/plate.png" alt="a plate"/></body></html>"""

CSS = """@font-face { font-family: "Serif"; src: url(fonts/Serif.ttf); }
@font-face { font-family: "Sans"; src: url(fonts/Sans.otf); }
body { font-family: "Serif", serif; line-height: 1.4; margin: 0 1em; }
h1 { font-size: 1.4em; }
"""


def build_fat_epub(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", CONTAINER)
        z.writestr("OEBPS/content.opf", OPF)
        z.writestr("OEBPS/nav.xhtml", NAV)
        z.writestr("OEBPS/ch1.xhtml", CH1)
        z.writestr("OEBPS/style.css", CSS)
        z.writestr("OEBPS/images/cover.jpg", colour_image(1400, 2100, "JPEG"))
        z.writestr("OEBPS/images/plate.png", colour_image(1200, 900, "PNG"))
        z.writestr("OEBPS/images/prog.jpg", colour_image(900, 600, "JPEG", progressive=True))
        z.writestr("OEBPS/fonts/Serif.ttf", b"\x00\x01\x00\x00" + b"F" * 400_000)
        z.writestr("OEBPS/fonts/Sans.otf", b"OTTO" + b"S" * 300_000)
        z.writestr("OEBPS/js/reader.js", b"window.alert('no');" * 500)


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        fat = tmp / "fat.epub"
        build_fat_epub(fat)

        print("\na fat book, slimmed:")
        check("the fixture verifies before we touch it",
              verify_integrity(fat)["pass"], str(verify_integrity(fat)["errors"]))

        slim = tmp / "slim.epub"
        report = slim_epub.slim(fat, slim)

        check("the result is a structurally sound EPUB", report["verified"],
              str(report.get("errors")))
        check("... and is much smaller", report["after"] < report["before"] / 2,
              f"{report['before']} -> {report['after']}")

        with zipfile.ZipFile(slim) as z:
            names = z.namelist()
            opf = z.read("OEBPS/content.opf").decode()
            css = z.read("OEBPS/style.css").decode()
            ch1 = z.read("OEBPS/ch1.xhtml").decode()
            nav = z.read("OEBPS/nav.xhtml").decode()
            cover = Image.open(io.BytesIO(z.read("OEBPS/images/cover.jpg")))
            plate = Image.open(io.BytesIO(z.read("OEBPS/images/plate.png")))
            prog = Image.open(io.BytesIO(z.read("OEBPS/images/prog.jpg")))

        print("\nwhat left, and what stayed:")
        check("both fonts are gone from the zip",
              not [n for n in names if "fonts/" in n], str(names))
        check("... and from the manifest, so nothing dangles",
              "Serif.ttf" not in opf and "Sans.otf" not in opf)
        check("the script is gone too", not [n for n in names if n.endswith(".js")])
        check("@font-face is gone from the CSS", "@font-face" not in css, css)
        check("... and so is font-family", "font-family" not in css, css)
        check("... but the rest of the CSS is untouched",
              "line-height: 1.4" in css and "margin: 0 1em" in css, css)
        check("the text is byte-identical", ch1 == CH1)
        check("the nav is byte-identical", nav == NAV)
        check("the title and author survive",
              "A Fat Book" in opf and "Someone" in opf)

        print("\nthe pictures:")
        check("the cover fits the panel", cover.size <= (528, 792), str(cover.size))
        check("... in grayscale", cover.mode == "L", cover.mode)
        check("a content image is capped at 480 wide", plate.width == 480,
              str(plate.size))
        check("... and is grayscale", plate.mode == "L", plate.mode)
        check("a progressive JPEG comes back baseline",
              not prog.info.get("progressive") and not prog.info.get("progression"),
              str(prog.info))
        check("every image got smaller",
              all(i["after"] <= i["before"] for i in report["images"]),
              str(report["images"]))

        print("\nrun it twice:")
        again = tmp / "again.epub"
        report2 = slim_epub.slim(fat, again)
        check("the same book slims to the same bytes",
              slim.read_bytes() == again.read_bytes())
        check("... and the report agrees", report["after"] == report2["after"])
        check("mimetype is first and stored",
              zipfile.ZipFile(slim).infolist()[0].filename == "mimetype"
              and zipfile.ZipFile(slim).infolist()[0].compress_type == zipfile.ZIP_STORED)

        print("\nrefusals:")
        probe = slim_epub.slim(fat, None)
        check("--probe writes nothing but measures the same",
              probe["after"] == report["after"] and "out" not in probe)

        not_a_book = tmp / "nope.epub"
        with zipfile.ZipFile(not_a_book, "w") as z:
            z.writestr("hello.txt", "not an epub")
        try:
            slim_epub.slim(not_a_book, tmp / "never.epub")
            check("a zip that is not an EPUB is refused", False, "it was accepted")
        except ValueError as exc:
            check("a zip that is not an EPUB is refused", "not an EPUB" in str(exc))
        check("... and nothing was written", not (tmp / "never.epub").exists())

        rc = subprocess.run([sys.executable, str(SCRIPTS / "slim_epub.py"),
                             str(fat), "--out", str(tmp / "cli.epub"), "--json"],
                            capture_output=True, text=True)
        check("the CLI exits 0 and prints JSON",
              rc.returncode == 0 and rc.stdout.strip().startswith("{"),
              rc.stderr[-200:])

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
