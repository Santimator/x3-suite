#!/usr/bin/env python3
"""Validate a chapter's vocabulary against the leveled list.

Pipeline: segment with jieba (configured from the list so boundaries match),
then run each Han-containing token through a cascade:

  (a) token in known-words list        -> KNOWN
  (b) token in chengyu/expression list -> CHENGYU      (also "in list")
  (c) not in list, but every character  -> STRETCH      (kept, harvested,
      is individually known                              glossed once)
  (d) otherwise                         -> FLAGGED      (out of list)

The gate is measured per *segmented token*, not per character. Two rates:
  out_of_list_rate = FLAGGED / counted    (primary gate, default 5%)
  stretch_rate     = STRETCH / counted    (secondary gate, default 15%)
Tokens with no Han character (punctuation, spaces, digits, latin) are not
counted in the denominator and are never flagged.

The glossary harvester rides along: STRETCH (and optionally FLAGGED) tokens
are collected with pinyin + gloss into a per-chapter new-word TSV. This is the
tier-(c) collection the brief asks for — it falls out of the validation pass
rather than being a separate scan.

Exit code: 0 = pass, 1 = fail (either gate exceeded). Use --json for the loop.

Usage:
  validate.py CHAPTER.md [--threshold 0.05] [--max-stretch 0.15]
              [--harvest-out PATH] [--json] [--lists DIR]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vocab as vocab_mod  # noqa: E402


def _has_han(token: str) -> bool:
    return any(vocab_mod._is_han(c) for c in token)


def classify(token: str, v: vocab_mod.Vocab) -> str:
    """Return one of: known | chengyu | stretch | flagged."""
    if v.is_chengyu(token):
        return "chengyu"
    if v.is_known(token):
        return "known"
    han = [c for c in token if vocab_mod._is_han(c)]
    # A single character the learner has met (appears in some known word) is
    # known, not a failure -- the official word list just doesn't list it
    # standalone.
    if len(han) == 1 and v.char_known(han[0]):
        return "known"
    # tier (c): multi-char word, not in the list, but every character is known.
    # All-chars-known approximates "compositional"; the final semantic
    # judgement is left to the author/QA gate (see SKILL.md).
    if len(han) >= 2 and v.all_chars_known(token):
        return "stretch"
    return "flagged"


def validate_text(text: str, v: vocab_mod.Vocab) -> Dict:
    tokens = vocab_mod.segment(text)
    counted = 0
    cats = Counter()
    stretch: Counter = Counter()
    flagged: Counter = Counter()

    for tok in tokens:
        if not _has_han(tok):
            continue
        counted += 1
        cat = classify(tok, v)
        cats[cat] += 1
        if cat == "stretch":
            stretch[tok] += 1
        elif cat == "flagged":
            flagged[tok] += 1

    out_of_list_rate = (cats["flagged"] / counted) if counted else 0.0
    stretch_rate = (cats["stretch"] / counted) if counted else 0.0

    return {
        "counted_tokens": counted,
        "known": cats["known"],
        "chengyu": cats["chengyu"],
        "stretch": cats["stretch"],
        "flagged": cats["flagged"],
        "out_of_list_rate": round(out_of_list_rate, 4),
        "stretch_rate": round(stretch_rate, 4),
        # most-frequent first so the loop sees the worst offenders up top
        "flagged_tokens": [w for w, _ in flagged.most_common()],
        "stretch_tokens": [w for w, _ in stretch.most_common()],
        "_flagged_counts": dict(flagged),
        "_stretch_counts": dict(stretch),
    }


def build_harvest_rows(report: Dict, v: vocab_mod.Vocab, include_flagged: bool) -> List[Tuple[str, str, str]]:
    """Rows for the per-chapter new-word glossary: (word, pinyin, gloss)."""
    rows = []
    words = list(report["stretch_tokens"])
    if include_flagged:
        words += list(report["flagged_tokens"])
    for w in words:
        e = v.get(w)
        pinyin = vocab_mod.pinyin_for(w, v)
        gloss = e.gloss if e else ""  # flagged words won't have a gloss yet
        rows.append((w, pinyin, gloss))
    return rows


def write_harvest(path: Path, rows: List[Tuple[str, str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write("word\tpinyin\tgloss\n")
        for w, p, g in rows:
            f.write(f"{w}\t{p}\t{g}\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Validate chapter vocabulary against the leveled list.")
    ap.add_argument("chapter", type=Path, help="chapter file (text/markdown)")
    ap.add_argument("--threshold", type=float, default=0.05, help="max out-of-list (flagged) rate (default 0.05)")
    ap.add_argument("--max-stretch", type=float, default=0.15, help="max stretch rate (default 0.15)")
    ap.add_argument("--harvest-out", type=Path, default=None, help="write per-chapter new-word TSV here")
    ap.add_argument("--harvest-flagged", action="store_true", help="include flagged words in harvest (no gloss yet)")
    ap.add_argument("--lists", type=Path, default=vocab_mod.LISTS_DIR, help="lists directory")
    ap.add_argument("--json", action="store_true", help="emit JSON report on stdout")
    args = ap.parse_args(argv)

    if not args.chapter.exists():
        print(f"error: chapter not found: {args.chapter}", file=sys.stderr)
        return 2

    v = vocab_mod.load_vocab(args.lists)
    text = args.chapter.read_text(encoding="utf-8")
    report = validate_text(text, v)

    fail_flagged = report["out_of_list_rate"] > args.threshold
    fail_stretch = report["stretch_rate"] > args.max_stretch
    passed = not (fail_flagged or fail_stretch)
    report["passed"] = passed
    report["threshold"] = args.threshold
    report["max_stretch"] = args.max_stretch
    report["fail_reasons"] = (
        (["out_of_list_rate>threshold"] if fail_flagged else [])
        + (["stretch_rate>max_stretch"] if fail_stretch else [])
    )

    if args.harvest_out:
        rows = build_harvest_rows(report, v, include_flagged=args.harvest_flagged)
        write_harvest(args.harvest_out, rows)
        report["harvest_out"] = str(args.harvest_out)
        report["harvest_count"] = len(rows)

    if args.json:
        # drop private fields from machine output
        public = {k: val for k, val in report.items() if not k.startswith("_")}
        print(json.dumps(public, ensure_ascii=False, indent=2))
    else:
        print(f"chapter: {args.chapter}")
        print(f"counted tokens: {report['counted_tokens']}")
        print(f"  known:   {report['known']}")
        print(f"  chengyu: {report['chengyu']}")
        print(f"  stretch: {report['stretch']}  ({report['stretch_rate']:.1%})")
        print(f"  flagged: {report['flagged']}  ({report['out_of_list_rate']:.1%})")
        print(f"gate: out-of-list {report['out_of_list_rate']:.1%} vs {args.threshold:.0%} | "
              f"stretch {report['stretch_rate']:.1%} vs {args.max_stretch:.0%}")
        print(f"RESULT: {'PASS' if passed else 'FAIL'}  {report['fail_reasons']}")
        if report["flagged_tokens"]:
            print("flagged tokens (worst first): " + " ".join(report["flagged_tokens"][:40]))
        if report["stretch_tokens"]:
            print("stretch tokens: " + " ".join(report["stretch_tokens"][:40]))
        if args.harvest_out:
            print(f"harvest -> {args.harvest_out} ({report['harvest_count']} words)")

    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
