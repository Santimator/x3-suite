#!/usr/bin/env python3
"""Prepare annotated chapters for the builder. The last graded-reader step.

The EPUB builder is deliberately generic: it renders `{word|reading}` wherever
it finds it, and knows nothing about Chinese, pinyin or vocabulary levels.
Deciding *which* words carry a reading is a graded-reader judgement — it needs
the segmenter and the glossary — so it happens here, and the builder receives
material it can render without thinking.

For each chapter this script:
  1. segments the text with the same jieba configuration validate.py uses, so
     word boundaries match the vocabulary lists exactly;
  2. marks the FIRST occurrence of each glossary word as `{词|pīnyīn}`,
     preferring the curated glossary pinyin over a generated one (gloss-once:
     later occurrences stay bare, which is the whole point of a graded reader);
  3. writes the result to `build/annotated/chNN.md` and points book.json's
     chapter `source` at it.

`chapters/*.md` stays the human-readable source of truth — it is what the
scribe writes and validate.py grades. `build/annotated/` is generated output;
delete it and re-run at any time.

Usage:
  annotate.py BOOKDIR [--reading-style after|ruby|none] [--lists DIR]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vocab as vocab_mod  # noqa: E402
import validate as validate_mod  # noqa: E402

# Lines the annotator must not touch: headings, verse fences, image paragraphs
# and footnote definitions are structure, not prose.
SKIP_RE = re.compile(r"^\s*(#|```|!\[|\[\^)")


def compact(reading: str) -> str:
    """Join a word's syllables: `hóu zi` -> `hóuzi`, so the reading reads as one
    word inline. Capitalised readings are proper names (`Gāo Wén`), where the
    space carries meaning, so they keep it."""
    if any(c.isupper() for c in reading):
        return reading.strip()
    return reading.replace(" ", "")


def load_glossary(path: Path) -> Dict[str, str]:
    """word -> pinyin, from a chapter glossary TSV."""
    out: Dict[str, str] = {}
    if not path.exists():
        return out
    for i, raw in enumerate(path.read_text(encoding="utf-8").splitlines()):
        if not raw.strip() or raw.startswith("#"):
            continue
        parts = raw.split("\t")
        if i == 0 and parts[0].strip() == "word":
            continue
        if parts[0].strip():
            out[parts[0].strip()] = (parts[1].strip() if len(parts) > 1 else "")
    return out


def annotate_text(md: str, gloss: Dict[str, str], v: vocab_mod.Vocab) -> Tuple[str, List[str]]:
    """Mark the first occurrence of each glossary word. Returns (text, marked)."""
    marked: List[str] = []
    seen: set = set()
    out_lines: List[str] = []

    for line in md.splitlines():
        if SKIP_RE.match(line) or not line.strip():
            out_lines.append(line)
            continue
        pieces: List[str] = []
        for tok in vocab_mod.segment(line):
            if tok in gloss and tok not in seen:
                seen.add(tok)
                marked.append(tok)
                reading = compact(gloss[tok] or vocab_mod.pinyin_for(tok, v))
                pieces.append(f"{{{tok}|{reading}}}" if reading else tok)
            else:
                pieces.append(tok)
        out_lines.append("".join(pieces))
    return "\n".join(out_lines) + ("\n" if md.endswith("\n") else ""), marked


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("book_dir", type=Path)
    ap.add_argument("--reading-style", choices=("after", "ruby", "none"), default=None,
                    help="write this reading_style into book.json (default: leave as is)")
    ap.add_argument("--lists", type=Path, default=vocab_mod.LISTS_DIR)
    args = ap.parse_args(argv)

    book_dir: Path = args.book_dir
    book_path = book_dir / "book.json"
    book = json.loads(book_path.read_text(encoding="utf-8"))
    plan_path = book_dir / "plan.json"
    max_level = (json.loads(plan_path.read_text(encoding="utf-8")).get("max_level")
                 if plan_path.exists() else None)

    v = vocab_mod.load_vocab(args.lists, max_level=max_level, book_dir=book_dir)

    out_dir = book_dir / "build" / "annotated"
    out_dir.mkdir(parents=True, exist_ok=True)

    total = 0
    for idx, ch in enumerate(book.get("chapters", []), start=1):
        # Always annotate from the pristine source, never from a previous run.
        src_rel = ch.get("source_md") or ch["source"]
        src = book_dir / src_rel
        gloss = load_glossary(book_dir / ch["glossary"]) if ch.get("glossary") else {}
        text, marked = annotate_text(src.read_text(encoding="utf-8"), gloss, v)

        out_rel = f"build/annotated/ch{idx:02d}.md"
        (book_dir / out_rel).write_text(text, encoding="utf-8")
        # Remember the source so re-running is idempotent, and point the builder
        # at the annotated copy.
        ch["source_md"] = src_rel
        ch["source"] = out_rel
        total += len(marked)
        print(f"  ch{idx:02d}: {len(marked)} words annotated -> {out_rel}")

    if args.reading_style:
        book["reading_style"] = args.reading_style
    book.pop("pinyin_mode", None)  # superseded by reading_style
    book_path.write_text(json.dumps(book, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"annotated {len(book.get('chapters', []))} chapters, {total} readings; "
          f"book.json -> build/annotated/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
