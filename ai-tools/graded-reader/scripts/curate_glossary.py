#!/usr/bin/env python3
"""Apply the glossary editor's decisions to a proposed chapter glossary.

update_state.py proposes every gloss-worthy first appearance (over-inclusive on
purpose) into BOOK/build/chNN-glossary.tsv, pinyin already filled. The glossary
editor's judgement is then small: which rows to drop, which glosses to write.
Re-typing the whole TSV to express that is where pinyin gets mangled and rows
get lost, so the editor writes only its decisions and this script applies them:

  {
    "drop":   ["山上", "很多"],                  # transparent from their parts
    "gloss":  {"码头": "harbour, dock"},         # fill or replace a gloss
    "pinyin": {"长": "zhǎng"}                    # optional: fix a reading
  }

The script decides nothing. It refuses (exit 1, file untouched) when a decision
names a word that isn't in the proposed glossary — a typo would otherwise be
silently ignored — or when a kept row would still have no gloss.

Usage:
  curate_glossary.py BOOK --chapter N --decisions decisions.json
  curate_glossary.py BOOK --chapter N --decisions -      # JSON on stdin
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def curate(rows, decisions):
    """Return (kept_rows, errors). rows = [[word, pinyin, gloss], ...] sans header."""
    drop = set(decisions.get("drop", []))
    gloss = decisions.get("gloss", {})
    pinyin = decisions.get("pinyin", {})
    words = {r[0] for r in rows}
    errors = [f"decision names a word not in the glossary: {w}"
              for w in sorted((drop | set(gloss) | set(pinyin)) - words)]
    errors += [f"{w} is both dropped and glossed" for w in sorted(drop & set(gloss))]
    kept = []
    for word, py, gl in rows:
        if word in drop:
            continue
        py, gl = pinyin.get(word, py), gloss.get(word, gl)
        if not gl.strip():
            errors.append(f"kept row has no gloss: {word}")
        kept.append([word, py, gl])
    return kept, errors


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Apply glossary-editor decisions to chNN-glossary.tsv.")
    ap.add_argument("book_dir", type=Path)
    ap.add_argument("--chapter", type=int, required=True)
    ap.add_argument("--decisions", required=True, help="JSON file, or - for stdin")
    args = ap.parse_args(argv)

    path = args.book_dir / "build" / f"ch{args.chapter:02d}-glossary.tsv"
    if not path.exists():
        print(f"error: {path} not found — run update_state.py first", file=sys.stderr)
        return 2
    raw = sys.stdin.read() if args.decisions == "-" else Path(args.decisions).read_text(encoding="utf-8")
    decisions = json.loads(raw)

    lines = path.read_text(encoding="utf-8").splitlines()
    header, body = lines[0], [l for l in lines[1:] if l.strip()]
    rows = [(l.split("\t") + ["", ""])[:3] for l in body]
    kept, errors = curate(rows, decisions)
    if errors:
        for e in errors:
            print(f"error: {e}", file=sys.stderr)
        print(f"{path}: unchanged", file=sys.stderr)
        return 1
    path.write_text("\n".join([header] + ["\t".join(r) for r in kept]) + "\n", encoding="utf-8")
    print(f"{path}: kept {len(kept)} of {len(rows)} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
