#!/usr/bin/env python3
"""Validate a chapter's vocabulary against the leveled list.

Pipeline: segment with jieba (configured from the list so boundaries match),
then run each Han-containing token through a cascade:

  (a) token in known-words list          -> KNOWN
  (b) token in chengyu/expression list   -> CHENGYU     (also "in list")
  (c) single character met in any known   -> KNOWN
      word
  (d) number grammar (第五名, 十二个) or   -> COMPOSED    (in-list: recognition,
      splits entirely into list words                    not a guess)
      (很快, 只能)
  (e) not in list, but every character    -> STRETCH     (kept, harvested,
      is individually known                              glossed once)
  (f) otherwise                           -> FLAGGED     (out of list)

Tier (d) exists because jieba merges frequent collocations into single tokens;
without it, ordinals and known-word compounds eat the stretch budget and the
rate stops measuring genuine reach (see SKILL.md).

The gate is measured per *segmented token*, not per character. Two rates:
  out_of_list_rate = FLAGGED / counted    (primary gate, default 5%)
  stretch_rate     = STRETCH / counted    (secondary gate, default 15%)
Tokens with no Han character (punctuation, spaces, digits, latin) are not
counted in the denominator and are never flagged. When the target is a book
directory, gates default to the book's plan.json validation params.

The glossary harvester rides along: STRETCH (and optionally FLAGGED) tokens
are collected with pinyin + gloss into a per-chapter new-word TSV.

Exit code: 0 = pass, 1 = fail (either gate exceeded). Use --json for the loop.

Usage:
  validate.py CHAPTER.md [--threshold 0.05] [--max-stretch 0.15]
              [--harvest-out PATH] [--json] [--lists DIR]
  validate.py BOOKDIR                # validate every chapter in book.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vocab as vocab_mod  # noqa: E402


def _has_han(token: str) -> bool:
    return any(vocab_mod._is_han(c) for c in token)


def _strip_headings(text: str) -> str:
    """Drop markdown headings and the RECAP line, so the length gate measures
    prose the reader actually reads — a chapter can't pad its way in with a
    long title."""
    import re
    text = re.sub(r"(?m)^#.*$", "", text)
    return re.sub(r"(?m)^RECAP:.*$", "", text)


def classify(token: str, v: vocab_mod.Vocab) -> str:
    """Return one of: known | chengyu | composed | stretch | flagged."""
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
    # tier (d): jieba-merged tokens that are pure recognition — number grammar
    # (ordinals, numeral+measure) or a concatenation of known list words.
    if vocab_mod.is_number_pattern(token) and v.all_chars_known(token):
        return "composed"
    if v.decomposes_known(token):
        return "composed"
    # tier (e): multi-char word, not in the list, but every character is known.
    # All-chars-known approximates "compositional"; the final semantic
    # judgement is left to the glossary editor (see SKILL.md).
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

    expressions = v.find_expressions(text)

    return {
        "counted_tokens": counted,
        "han_chars": sum(1 for c in _strip_headings(text) if vocab_mod._is_han(c)),
        "known": cats["known"],
        "chengyu": cats["chengyu"],
        "composed": cats["composed"],
        "stretch": cats["stretch"],
        "flagged": cats["flagged"],
        "expressions": expressions,
        "expression_count": len(expressions),
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


def gate_report(report: Dict, gates: "Gates") -> None:
    """Apply every gate to a validate_text report, in place.

    Note `min_out_of_list` is a *floor*, not a typo for the ceiling: text that
    stays 100% inside the list is too easy to teach from. A little unknown
    vocabulary in context is where the learning happens (i+1), so a chapter that
    never reaches beyond the list fails just as one that overshoots does. The
    same logic drives min_chars (a chapter must be a real episode) and
    min_expressions (it must sound like Chinese, not glossary soup)."""
    reasons = []
    if report["out_of_list_rate"] > gates.threshold:
        reasons.append("out_of_list_rate>threshold")
    if report["out_of_list_rate"] < gates.min_out_of_list:
        reasons.append("out_of_list_rate<min (too easy — reach a little further)")
    if report["stretch_rate"] > gates.max_stretch:
        reasons.append("stretch_rate>max_stretch")
    if report["han_chars"] < gates.min_chars:
        reasons.append(f"too short ({report['han_chars']}<{gates.min_chars} chars)")
    if report["expression_count"] < gates.min_expressions:
        reasons.append(f"too few expressions ({report['expression_count']}<{gates.min_expressions})")

    report["passed"] = not reasons
    report["threshold"] = gates.threshold
    report["min_out_of_list"] = gates.min_out_of_list
    report["max_stretch"] = gates.max_stretch
    report["min_chars"] = gates.min_chars
    report["min_expressions"] = gates.min_expressions
    report["fail_reasons"] = reasons


@dataclass
class Gates:
    """The validation envelope. Ceilings keep a chapter readable; floors keep it
    worth reading."""
    threshold: float = 0.05          # max out-of-list rate
    min_out_of_list: float = 0.0     # min out-of-list rate (the i+1 floor)
    max_stretch: float = 0.15
    min_chars: int = 0               # min Han chars of prose per chapter
    min_expressions: int = 0         # min distinct expressions.tsv constructions


# Defaults applied when neither the CLI nor plan.json says otherwise. The floors
# default to 0 so existing books keep passing; new books opt in via plan.json.
GATE_DEFAULTS = Gates()


def resolve_gates(args, book_dir: Path = None) -> Gates:
    """CLI flags win; else the book's plan.json validation params; else defaults."""
    val = {}
    if book_dir and (book_dir / "plan.json").exists():
        val = json.loads((book_dir / "plan.json").read_text(encoding="utf-8")).get("validation", {})

    def pick(cli_name, key, default):
        cli = getattr(args, cli_name, None)
        if cli is not None:
            return cli
        return val.get(key, default)

    return Gates(
        threshold=pick("threshold", "threshold", GATE_DEFAULTS.threshold),
        min_out_of_list=pick("min_out_of_list", "min_out_of_list", GATE_DEFAULTS.min_out_of_list),
        max_stretch=pick("max_stretch", "max_stretch", GATE_DEFAULTS.max_stretch),
        min_chars=pick("min_chars", "min_chars", GATE_DEFAULTS.min_chars),
        min_expressions=pick("min_expressions", "min_expressions", GATE_DEFAULTS.min_expressions),
    )


def resolve_book_dir(chapter: Path) -> Path:
    """The book directory for a chapter path (BOOK/chapters/chNN.md -> BOOK),
    or the path itself if it is a directory."""
    if chapter.is_dir():
        return chapter
    if chapter.parent.name == "chapters":
        return chapter.parent.parent
    return chapter.parent


def resolve_max_level(args, book_dir: Path):
    """CLI --max-level wins; else the book's plan.json `max_level` (opt-in; a
    book without it stays uncapped)."""
    if getattr(args, "max_level", None):
        return args.max_level
    if book_dir and (book_dir / "plan.json").exists():
        return json.loads((book_dir / "plan.json").read_text(encoding="utf-8")).get("max_level")
    return None


def validate_book(book_dir: Path, args, v: vocab_mod.Vocab) -> int:
    """Validate every chapter listed in BOOKDIR/book.json. Exit 0 iff all pass."""
    book = json.loads((book_dir / "book.json").read_text(encoding="utf-8"))
    gates = resolve_gates(args, book_dir)
    reports, all_passed = [], True
    for ch in book.get("chapters", []):
        # Grade the human-written source, never the annotated copy the
        # builder consumes (`source` points there once annotate.py has run).
        path = book_dir / (ch.get("source_md") or ch["source"])
        report = validate_text(path.read_text(encoding="utf-8"), v)
        gate_report(report, gates)
        report["chapter"] = ch.get("source_md") or ch["source"]
        reports.append(report)
        all_passed &= report["passed"]
    if args.json:
        public = [{k: v_ for k, v_ in r.items() if not k.startswith("_")} for r in reports]
        print(json.dumps(public, ensure_ascii=False, indent=2))
    else:
        print(f"{book_dir}  (gates: out-of-list {gates.min_out_of_list:.0%}-{gates.threshold:.0%}, "
              f"stretch ≤ {gates.max_stretch:.0%}, ≥{gates.min_chars} chars, "
              f"≥{gates.min_expressions} expressions)")
        for r in reports:
            flagged = ("  flagged: " + " ".join(r["flagged_tokens"][:8])) if r["flagged_tokens"] else ""
            why = ("  <- " + "; ".join(r["fail_reasons"])) if r["fail_reasons"] else ""
            print(f"  {r['chapter']}: {r['han_chars']:4d} chars  "
                  f"out {r['out_of_list_rate']:.1%}  stretch {r['stretch_rate']:.1%}  "
                  f"expr {r['expression_count']:2d}  "
                  f"{'PASS' if r['passed'] else 'FAIL'}{why}{flagged}")
        print(f"RESULT: {'ALL PASS' if all_passed else 'FAIL'} ({len(reports)} chapters)")
    return 0 if all_passed else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Validate chapter vocabulary against the leveled list.")
    ap.add_argument("chapter", type=Path, help="chapter file, or a book directory (validates all chapters)")
    ap.add_argument("--threshold", type=float, default=None,
                    help="max out-of-list (flagged) rate (default: plan.json or 0.05)")
    ap.add_argument("--max-stretch", type=float, default=None,
                    help="max stretch rate (default: plan.json or 0.15)")
    ap.add_argument("--min-out-of-list", type=float, default=None, dest="min_out_of_list",
                    help="min out-of-list rate — the i+1 floor; below it a chapter is "
                         "too easy (default: plan.json or 0)")
    ap.add_argument("--min-chars", type=int, default=None, dest="min_chars",
                    help="min Han characters of prose per chapter (default: plan.json or 0)")
    ap.add_argument("--min-expressions", type=int, default=None, dest="min_expressions",
                    help="min distinct expressions.tsv constructions (default: plan.json or 0)")
    ap.add_argument("--max-level", default=None,
                    help="cap the known HSK list at this band, e.g. HSK3 "
                         "(default: plan.json `max_level`, else uncapped)")
    ap.add_argument("--harvest-out", type=Path, default=None, help="write per-chapter new-word TSV here")
    ap.add_argument("--harvest-flagged", action="store_true", help="include flagged words in harvest (no gloss yet)")
    ap.add_argument("--lists", type=Path, default=vocab_mod.LISTS_DIR, help="lists directory")
    ap.add_argument("--json", action="store_true", help="emit JSON report on stdout")
    args = ap.parse_args(argv)

    if not args.chapter.exists():
        print(f"error: chapter not found: {args.chapter}", file=sys.stderr)
        return 2

    book_dir = resolve_book_dir(args.chapter)
    v = vocab_mod.load_vocab(args.lists, max_level=resolve_max_level(args, book_dir),
                             book_dir=book_dir)

    if args.chapter.is_dir():
        return validate_book(args.chapter, args, v)

    gates = resolve_gates(args, resolve_book_dir(args.chapter))
    report = validate_text(args.chapter.read_text(encoding="utf-8"), v)
    gate_report(report, gates)

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
        print(f"chapter: {args.chapter}  ({report['han_chars']} chars of prose)")
        print(f"counted tokens: {report['counted_tokens']}")
        for cat in ("known", "chengyu", "composed"):
            print(f"  {cat + ':':<9} {report[cat]}")
        print(f"  stretch:  {report['stretch']}  ({report['stretch_rate']:.1%})")
        print(f"  flagged:  {report['flagged']}  ({report['out_of_list_rate']:.1%})")
        print(f"  expressions: {report['expression_count']}  "
              + ("、".join(report["expressions"][:10]) if report["expressions"] else "(none)"))
        print(f"gate: out-of-list {report['out_of_list_rate']:.1%} in "
              f"[{gates.min_out_of_list:.0%}, {gates.threshold:.0%}] | "
              f"stretch {report['stretch_rate']:.1%} ≤ {gates.max_stretch:.0%} | "
              f"chars {report['han_chars']} ≥ {gates.min_chars} | "
              f"expr {report['expression_count']} ≥ {gates.min_expressions}")
        print(f"RESULT: {'PASS' if report['passed'] else 'FAIL'}  {report['fail_reasons']}")
        if report["flagged_tokens"]:
            print("flagged tokens (worst first): " + " ".join(report["flagged_tokens"][:40]))
        if report["stretch_tokens"]:
            print("stretch tokens: " + " ".join(report["stretch_tokens"][:40]))
        if args.harvest_out:
            print(f"harvest -> {args.harvest_out} ({report['harvest_count']} words)")

    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
