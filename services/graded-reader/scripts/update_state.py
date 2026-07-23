#!/usr/bin/env python3
"""Deterministic state update after a chapter is accepted. No LLM.

Runs once a chapter passes validation. It does the bookkeeping the user
described -- "a script updates the needed info (used words, what happened)":

  1. Segment the accepted chapter and find this chapter's gloss-worthy first
     appearances: story/topic words (personal layer) + compositional stretch
     words, minus anything already in plan.json's `introduced` set.
  2. Write the per-chapter glossary (gloss-once) to build/chNN-glossary.tsv,
     filling glosses from the lists where known.
  3. Add those newly-glossed words to plan.json `introduced.words`.
  4. Mark the outline entry accepted and file its recap (taken from --recap, or
     a trailing `RECAP:` line the scribe appended, which is then stripped from
     the saved chapter so it never reaches the EPUB).
  5. Ensure book.json has a chapter entry (source + glossary) for chapter N.

Glossing policy: a word is glossed on FIRST appearance only. Common in-list
function/HSK words are never glossed (the reader knows them), so they don't
enter `introduced`; only gloss-worthy words do, which keeps the ledger meaningful.

Usage:
  update_state.py BOOKDIR --chapter N [--recap "..."] [--status accepted]
                  [--keep-recap] [--dry-run] [--lists DIR]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vocab as vocab_mod  # noqa: E402
import validate as validate_mod  # noqa: E402


def chapter_paths(book_dir: Path, n: int, book: Dict) -> Tuple[Path, str, str]:
    """Return (chapter_md_path, source_rel, glossary_rel), honoring book.json."""
    source_rel = f"chapters/ch{n:02d}.md"
    glossary_rel = f"build/ch{n:02d}-glossary.tsv"
    chapters = book.get("chapters", [])
    if 1 <= n <= len(chapters):  # positional convention: chapters[n-1] is chapter n
        entry = chapters[n - 1]
        source_rel = entry.get("source", source_rel)
        glossary_rel = entry.get("glossary", glossary_rel)
    return book_dir / source_rel, source_rel, glossary_rel


def extract_recap(text: str) -> Tuple[str, Optional[str]]:
    """Pull a trailing 'RECAP:' line. Return (text_without_recap, recap_or_None)."""
    lines = text.rstrip().splitlines()
    for i in range(len(lines) - 1, -1, -1):
        s = lines[i].strip()
        if not s:
            continue
        if s.upper().startswith("RECAP:"):
            recap = s[len("RECAP:"):].strip()
            del lines[i]
            return "\n".join(lines).rstrip() + "\n", recap
        break  # only consider the genuine last content line
    return text, None


def gloss_worthy(text: str, v: vocab_mod.Vocab) -> List[str]:
    """First-appearance-ordered unique tokens that deserve a gloss this chapter."""
    seen = set()
    ordered: List[str] = []
    for tok in vocab_mod.segment(text):
        if tok in seen or not any(vocab_mod._is_han(c) for c in tok):
            continue
        e = v.get(tok)
        cat = validate_mod.classify(tok, v)
        worthy = (e is not None and e.source == "personal") or cat == "stretch"
        if worthy:
            seen.add(tok)
            ordered.append(tok)
    return ordered


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Update plan/glossary state after a chapter is accepted.")
    ap.add_argument("book_dir", type=Path)
    ap.add_argument("--chapter", type=int, required=True)
    ap.add_argument("--recap", default=None, help="continuity recap (else read a trailing RECAP: line)")
    ap.add_argument("--status", default="accepted", help="outline status to set (default: accepted)")
    ap.add_argument("--keep-recap", action="store_true", help="do not strip the RECAP: line from the chapter")
    ap.add_argument("--dry-run", action="store_true", help="print what would change, write nothing")
    ap.add_argument("--lists", type=Path, default=vocab_mod.LISTS_DIR)
    args = ap.parse_args(argv)

    book_dir: Path = args.book_dir
    n = args.chapter
    v = vocab_mod.load_vocab(args.lists)

    plan_path = book_dir / "plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    book_path = book_dir / "book.json"
    book = json.loads(book_path.read_text(encoding="utf-8")) if book_path.exists() else {"chapters": []}

    ch_path, source_rel, glossary_rel = chapter_paths(book_dir, n, book)
    if not ch_path.exists():
        print(f"error: chapter file not found: {ch_path}", file=sys.stderr)
        return 2

    raw = ch_path.read_text(encoding="utf-8")
    body, recap = extract_recap(raw)
    recap = args.recap or recap

    introduced = plan.setdefault("introduced", {}).setdefault("words", [])
    introduced_set = set(introduced)

    candidates = gloss_worthy(body, v)
    new_words = [w for w in candidates if w not in introduced_set]

    gloss_rows: List[Tuple[str, str, str]] = []
    for w in new_words:
        e = v.get(w)
        pinyin = vocab_mod.pinyin_for(w, v)
        gloss = e.gloss if e else ""  # compositional stretch may have no gloss yet
        gloss_rows.append((w, pinyin, gloss))

    # --- report ---
    print(f"chapter {n}: {ch_path.name}")
    print(f"  gloss-worthy first appearances: {len(new_words)}")
    print("  new glossary words: " + ("、".join(new_words) if new_words else "(none)"))
    missing = [w for w, _, g in gloss_rows if not g]
    if missing:
        print("  REVIEW (compositional stretch, no list gloss): " + "、".join(missing))
        print("         -> fill a gloss, or delete the row if it's transparent from its parts.")
    if recap:
        print(f"  recap: {recap}")
    if args.dry_run:
        print("  [dry-run] no files written")
        return 0

    # --- write chapter glossary ---
    gloss_path = book_dir / glossary_rel
    validate_mod.write_harvest(gloss_path, gloss_rows)  # same word/pinyin/gloss schema
    print(f"  glossary -> {glossary_rel} ({len(gloss_rows)} words)")

    # --- update introduced set ---
    introduced.extend(new_words)

    # --- update outline entry status + recap ---
    for ch in plan.get("outline", []):
        if ch.get("n") == n:
            ch["status"] = args.status
            if recap:
                ch["recap"] = recap
            break

    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"  plan.json: introduced +{len(new_words)} (total {len(introduced)}), outline[{n}].status={args.status}")

    # --- strip RECAP from the saved chapter ---
    if recap and not args.keep_recap and body != raw:
        ch_path.write_text(body, encoding="utf-8")
        print(f"  stripped RECAP line from {source_rel}")

    # --- ensure book.json has this chapter wired ---
    chapters = book.setdefault("chapters", [])
    if not any(c.get("source") == source_rel for c in chapters):
        chapters.append({"source": source_rel, "glossary": glossary_rel})
        book_path.write_text(json.dumps(book, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"  book.json: added chapter entry {source_rel}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
