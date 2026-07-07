#!/usr/bin/env python3
"""Emit the exact character set a book's EPUB renders — for building device fonts.

Why: e-ink readers like the Xteink X3 (ESP32-C3, ~400 KB RAM) can't hold a full
CJK font — 20k+ ideographs cause out-of-memory crashes — and their stock fonts
have no CJK glyphs at all (Han text renders as tofu boxes). But a graded reader
uses only a few hundred distinct characters, and the pipeline knows exactly
which. This script collects every codepoint the built EPUB would render
(hanzi, fullwidth punctuation, pinyin with tone marks, glossary text) and emits:

  - a summary count (how small your font can be),
  - CHARSET.txt        every distinct character, for pyftsubset --text-file=
  - INTERVALS.txt      merged codepoint ranges (0x4E00-0x4E01,0x4E09,...) for
                       CrossPoint's fontconvert_sdcard.py --intervals or the
                       custom-range field of the web font builder.

Usage:
  charset.py BOOKDIR [BOOKDIR...] [--out-dir DIR]
(multiple books are merged into one charset, so one font covers your library)
"""
from __future__ import annotations

import argparse
import json
import re
import string
import sys
from pathlib import Path
from typing import List, Set

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_epub  # noqa: E402
import vocab as vocab_mod  # noqa: E402

_TAG = re.compile(r"<[^>]+>")


def book_chars(book_dir: Path) -> Set[str]:
    """Every character the built EPUB renders, by assembling it in memory."""
    meta = json.loads((book_dir / "book.json").read_text(encoding="utf-8"))
    mode = meta.get("pinyin_mode", "ruby")
    chapters, meta = build_epub.assemble(book_dir, mode)
    chars: Set[str] = set(meta.get("title", "") + meta.get("author", ""))
    for ch in chapters:
        chars.update(_TAG.sub("", ch["body"]))
        chars.update(ch["title"])
    return chars


def merged_intervals(codepoints: List[int]) -> str:
    """Merge sorted codepoints into compact 0x..-0x.. range segments."""
    segs = []
    start = prev = codepoints[0]
    for cp in codepoints[1:]:
        if cp == prev + 1:
            prev = cp
            continue
        segs.append((start, prev))
        start = prev = cp
    segs.append((start, prev))
    return ",".join(f"0x{a:X}-0x{b:X}" if a != b else f"0x{a:X}" for a, b in segs)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Emit a book's exact character set for device font building.")
    ap.add_argument("books", nargs="+", type=Path, help="book directories (merged into one charset)")
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="write CHARSET.txt + INTERVALS.txt here (default: first book's build/)")
    ap.add_argument("--include-lists", action="store_true",
                    help="also cover every character of every vocab-list word, so the font "
                         "outlives the current books and covers anything the pipeline can write")
    args = ap.parse_args(argv)

    v = vocab_mod.load_vocab()  # configures jieba for assemble()

    chars: Set[str] = set(string.printable)  # ASCII always; UI chrome needs it
    for book in args.books:
        chars.update(book_chars(book))
    if args.include_lists:
        chars.update(v.known_chars)
        for e in v.entries.values():
            chars.update(e.pinyin)  # tone-marked pinyin used in ruby + glossaries
    chars = {c for c in chars if c.isprintable()}  # drop \n, \x0b, control chars

    han = sorted(c for c in chars if vocab_mod._is_han(c))
    rest = sorted(c for c in chars if not vocab_mod._is_han(c))
    codepoints = sorted(ord(c) for c in chars)

    out_dir = args.out_dir or (args.books[0] / "build")
    out_dir.mkdir(parents=True, exist_ok=True)
    charset_path = out_dir / "CHARSET.txt"
    intervals_path = out_dir / "INTERVALS.txt"
    charset_path.write_text("".join(han + rest) + "\n", encoding="utf-8")
    intervals_path.write_text(merged_intervals(codepoints) + "\n", encoding="utf-8")

    print(f"books: {', '.join(str(b) for b in args.books)}")
    print(f"distinct characters: {len(chars)}  (han: {len(han)}, other: {len(rest)})")
    print(f"charset   -> {charset_path}   (pyftsubset --text-file=...)")
    print(f"intervals -> {intervals_path}   (fontconvert --intervals / web builder custom range)")
    print("A font subset this small fits comfortably on an ESP32-class reader.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
