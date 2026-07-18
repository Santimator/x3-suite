#!/usr/bin/env python3
"""Pipeline self-test: run after changing scripts or lists.

Checks, in order:
  1. the classification cascade on canonical examples of every tier,
  2. every chapter of every workspace book still passes its gates,
  3. an EPUB builds, is well-formed XML, and its glossary links resolve.

Exit 0 = all good. No test framework needed:  python scripts/selftest.py
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import xml.dom.minidom
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# the EPUB builder is suite-shared infrastructure, not a graded-reader script
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "epub-builder" / "scripts"))
import build_epub  # noqa: E402
import validate as validate_mod  # noqa: E402
import vocab as vocab_mod  # noqa: E402

REPO = Path(__file__).resolve().parents[4]
WORKSPACE = REPO / "workspace"

# One canonical token per cascade tier (see validate.py docstring).
CLASSIFY_CASES = [
    ("聪明", "known"),        # (a) list word
    ("马马虎虎", "chengyu"),  # (b) idiom list
    ("山", "known"),          # (c) single char met in known words (爬山)
    ("第十一名", "composed"),  # (d) ordinal number grammar
    ("十二个", "composed"),    # (d) numeral + measure
    ("很快", "composed"),      # (d) concatenation of list words 很+快
    ("山上", "stretch"),       # (e) chars known, not a taught combination
    ("尾巴", "flagged"),       # (f) contains an untaught character
]


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'ok' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail and not ok else ""))
    return ok


def main() -> int:
    all_ok = True
    v = vocab_mod.load_vocab()

    print("1. classification cascade")
    for token, want in CLASSIFY_CASES:
        got = validate_mod.classify(token, v)
        all_ok &= check(f"{token} -> {want}", got == want, f"got {got}")

    print("2. workspace books pass their gates")
    # plan.json is graded-reader's own workspace convention; other suite
    # tasks (pdf2epub) also drop a book.json under workspace/<slug>/, so
    # its presence alone doesn't mean "graded-reader book".
    books = sorted(p.parent for p in WORKSPACE.glob("*/book.json") if (p.parent / "plan.json").exists())
    if not books:
        all_ok &= check("found workspace books", False, "none found")
    for book in books:
        meta = json.loads((book / "book.json").read_text(encoding="utf-8"))
        plan = json.loads((book / "plan.json").read_text(encoding="utf-8"))
        gates = plan.get("validation", {})
        thr, ms = gates.get("threshold", 0.05), gates.get("max_stretch", 0.15)
        for ch in meta["chapters"]:
            text = (book / ch["source"]).read_text(encoding="utf-8")
            r = validate_mod.validate_text(text, v)
            ok = r["out_of_list_rate"] <= thr and r["stretch_rate"] <= ms
            all_ok &= check(
                f"{book.name}/{ch['source']}", ok,
                f"out {r['out_of_list_rate']:.1%}, stretch {r['stretch_rate']:.1%}",
            )

    print("3. epub build + glossary links")
    for book in books:
        with tempfile.NamedTemporaryFile(suffix=".epub", delete=True) as tmp:
            out = Path(tmp.name)
            # build in the mode the book ships with (gloss-pinyin on the X3),
            # so the test covers the markup we actually deliver.
            meta_mode = json.loads((book / "book.json").read_text(encoding="utf-8")).get("pinyin_mode", "plain")
            chapters, meta = build_epub.assemble(book, meta_mode)
            build_epub.write_epub(out, meta["title"], meta.get("author", ""),
                                  meta.get("language", "zh"), chapters)
            with zipfile.ZipFile(out) as z:
                names = z.namelist()
                all_ok &= check(f"{book.name}: mimetype stored first", names[0] == "mimetype")
                malformed = []
                for n in names:
                    if n.endswith((".xhtml", ".opf")):
                        try:
                            xml.dom.minidom.parseString(z.read(n))
                        except Exception:
                            malformed.append(n)
                all_ok &= check(f"{book.name}: xhtml well-formed", not malformed, str(malformed))
                dead = []
                for n in names:
                    if not n.endswith(".xhtml") or "nav" in n:
                        continue
                    html = z.read(n).decode("utf-8")
                    ids = set(re.findall(r'id="([^"]+)"', html))
                    for href in re.findall(r'href="#([^"]+)"', html):
                        if href not in ids:
                            dead.append(f"{n}#{href}")
                all_ok &= check(f"{book.name}: glossary links resolve", not dead, str(dead))

    print("PASS" if all_ok else "FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
