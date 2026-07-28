#!/usr/bin/env python3
"""Build the scribe's writing context for one chapter.

This is the "guided writing" piece. Validation catches out-of-level words after
the fact; this front-loads the constraint so the scribe reaches for in-list
words while writing instead of being reworked afterwards.

It assembles, from plan.json + the lists, a single markdown brief the scribe
(an LLM, or you) fills in:
  - the chapter's beat (what happens) from the outline,
  - the running "introduced" set (reuse freely, do NOT re-gloss),
  - story-specific names / topic words available (with glosses),
  - the permitted vocabulary, grouped by band so the scribe can lean simple,
  - the hard constraints (level, gates, length) and output format.

Usage:
  gen_context.py BOOKDIR --chapter N [--out brief.md]
                 [--vocab-detail grouped|words|none] [--length 220] [--lists DIR]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vocab as vocab_mod  # noqa: E402

# Bands shown to the scribe, simplest first, so it can prefer low bands.
BAND_ORDER = ["HSK1", "HSK2", "HSK3", "HSK4", "SUP", "IDIOM"]
BAND_LABEL = {
    "HSK1": "HSK 1 (easiest — prefer these)",
    "HSK2": "HSK 2",
    "HSK3": "HSK 3",
    "HSK4": "HSK 4",
    "SUP": "Function/grammar words",
    "IDIOM": "Set expressions",
}


def find_chapter(plan: Dict, n: int) -> Dict:
    for ch in plan.get("outline", []):
        if ch.get("n") == n:
            return ch
    raise SystemExit(f"error: chapter {n} not found in plan.json outline")


def group_vocab(v: vocab_mod.Vocab) -> Dict[str, List[str]]:
    groups: Dict[str, List[str]] = {b: [] for b in BAND_ORDER}
    for word, e in v.entries.items():
        if e.source == "personal":
            continue  # shown separately as story topic words
        band = e.level if e.level in groups else "SUP"
        groups.setdefault(band, []).append(word)
    for b in groups:
        groups[b].sort()
    return groups


def render_brief(book_dir: Path, n: int, vocab_detail: str, length: int, v: vocab_mod.Vocab) -> str:
    plan = json.loads((book_dir / "plan.json").read_text(encoding="utf-8"))
    ch = find_chapter(plan, n)
    val = plan.get("validation", {})
    introduced = plan.get("introduced", {}).get("words", [])

    # Story topic words = this book's own vocab.tsv (names, places, props).
    # The user's personal.tsv is their standing vocabulary, not story material,
    # so it is deliberately not listed here.
    topic = [(w, e.pinyin, e.gloss) for w, e in v.entries.items() if e.source == "book"]
    topic.sort()

    out: List[str] = []
    out.append(f"# Scribe brief — chapter {n}: {ch.get('title','')}\n")
    out.append("You are the **scribe**. Write this one chapter of a Chinese graded reader. "
               "Stay strictly within the permitted vocabulary below. Validation runs right "
               "after you, so words outside the list cost a rework — choose the simpler word.\n")

    out.append("## What happens in this chapter")
    out.append(ch.get("summary", "(no summary in plan — ask the planner to fill it)") + "\n")
    prev = [c for c in plan.get("outline", []) if c.get("n", 0) < n]
    if prev:
        out.append("### Story so far (for continuity)")
        for c in prev:
            recap = c.get("recap") or c.get("summary", "")
            out.append(f"- Ch {c['n']} {c.get('title','')}: {recap}")
        out.append("")

    min_chars = val.get("min_chars", length)
    min_expr = val.get("min_expressions", 0)
    min_out = val.get("min_out_of_list", 0.0)

    out.append("## Hard constraints")
    out.append(f"- Target level: **{plan.get('target_level','HSK1-3')}**. When in doubt, pick the simpler word.")
    out.append(f"- Out-of-list budget: **between {min_out:.0%} and {val.get('threshold',0.05):.0%}** of tokens. "
               f"The lower bound is deliberate — a chapter with *zero* new words is too easy to learn "
               f"from. Reach a little beyond the list on purpose (a few words the story needs); each "
               f"gets glossed once. Compositional combinations of known characters are allowed "
               f"sparingly (≤ {val.get('max_stretch',0.15):.0%}).")
    out.append(f"- Length: at least **{min_chars} characters** of prose — this is checked and will fail "
               f"the chapter. Write a full, meaty episode, not a summary. Reach the length through "
               f"more scenes, dialogue, and concrete detail, never through fancier words.")
    if min_expr:
        out.append(f"- Expressions: use **at least {min_expr} different constructions** from the list "
                   f"below. This is checked. They are what make the prose sound like Chinese instead "
                   f"of translated English — build sentences around them rather than sprinkling them on.")
    out.append("- Use only the words listed below (plus the story names). Do not invent plot beyond the summary.\n")

    if introduced:
        out.append("## Already introduced (reuse freely — do NOT re-explain these)")
        out.append("、".join(introduced) + "\n")

    if topic:
        out.append("## Story names / topic words you may use (already glossed or will be glossed once)")
        for w, p, g in topic:
            out.append(f"- **{w}** {p} — {g}")
        out.append("")

    if v.expressions:
        pats = [(w, e) for w, (rx, e) in v.expressions.items() if rx is not None]
        phrases = [(w, e) for w, (rx, e) in v.expressions.items() if rx is None]
        out.append("## Expressions to build sentences with")
        out.append("Patterns — wrap your own words inside them:")
        for w, e in pats:
            out.append(f"- **{w}** {e.pinyin} — {e.gloss}")
        out.append("\nSet phrases — use as-is:")
        out.append("、".join(w for w, _ in phrases) + "\n")

    if vocab_detail != "none":
        out.append("## Permitted vocabulary")
        groups = group_vocab(v)
        for band in BAND_ORDER:
            words = groups.get(band, [])
            if not words:
                continue
            out.append(f"### {BAND_LABEL.get(band, band)}  ({len(words)} words)")
            if vocab_detail == "grouped":
                out.append("、".join(words) + "\n")
            else:  # "words" — same content; reserved for future compaction
                out.append(" ".join(words) + "\n")

    out.append("## Output format")
    out.append("Return the chapter as markdown:")
    out.append("```")
    out.append(f"# 第{_cn_num(n)}章 {ch.get('title','')}")
    out.append("")
    out.append("（段落……）")
    out.append("```")
    out.append("Write only the chapter. After it, on a separate final line, add a one-sentence "
               "recap prefixed `RECAP:` (English or Chinese) describing what happened — the "
               "state updater files this for the next chapter's continuity.")
    return "\n".join(out) + "\n"


def _cn_num(n: int) -> str:
    digits = "零一二三四五六七八九"
    if n < 10:
        return digits[n]
    if n < 20:
        return "十" + (digits[n - 10] if n > 10 else "")
    return str(n)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build the scribe writing brief for one chapter.")
    ap.add_argument("book_dir", type=Path)
    ap.add_argument("--chapter", type=int, required=True)
    ap.add_argument("--out", type=Path, default=None, help="write brief here (default: stdout)")
    ap.add_argument("--vocab-detail", choices=["grouped", "words", "none"], default="grouped")
    ap.add_argument("--length", type=int, default=450, help="minimum chapter length in characters "
                    "(chapters should be meaty episodes, not summaries)")
    ap.add_argument("--lists", type=Path, default=vocab_mod.LISTS_DIR)
    args = ap.parse_args(argv)

    plan_max_level = json.loads(
        (args.book_dir / "plan.json").read_text(encoding="utf-8")).get("max_level")
    v = vocab_mod.load_vocab(args.lists, max_level=plan_max_level, book_dir=args.book_dir)
    brief = render_brief(args.book_dir, args.chapter, args.vocab_detail, args.length, v)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(brief, encoding="utf-8")
        print(f"wrote brief -> {args.out} ({len(brief)} chars)")
    else:
        sys.stdout.write(brief)
    return 0


if __name__ == "__main__":
    sys.exit(main())
