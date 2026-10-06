#!/usr/bin/env python3
"""pdf2epub: the gate after every model step — check one chunk's out.md.

Two checks, both deterministic, written to chunks/cNN/check.json together with
the sha256 of the out.md they judged (an edit makes the verdict stale):

1. Format. out.md may use only what the builder renders (epub-builder
   FORMAT.md: # and ## headings, paragraphs, *emphasis*, ```verse fences,
   footnotes) plus the harness's [[fig:ID | caption]] and [[continues]].
   Catches the silly composition mistakes: an unclosed fence, a stray HTML tag,
   a markdown table, a page number left in, a word still split "sustancial-
   mente" across a line break, a figure from another chunk's pages.

2. Fidelity — only where the chunk has a trustworthy text layer. The text
   layer (column-ordered, minus the text inside figures this chunk placed) is
   the baseline. Both sides are reduced to lowercase words, line-break hyphens
   joined. A word is *covered* when some word 5-gram containing it occurs on
   the other side (order between paragraphs does not matter; order inside
   one does):
     recall     share of the source's words covered by the chunk
     precision  share of the chunk's words covered by the source
   and, more telling, the longest *runs* of uncovered words: a dropped
   sentence is a long missing run, a paraphrase or invention a long invented
   run, while a fixed typo or a dropped running header is a short one.
   Figure captions may cover source words but never count as invented; they
   are checked on their own: every caption word must be printed on the
   chunk's pages (an invented description fails).
   Fails on a missing run >= MAX_MISSING_RUN words, an invented run >=
   MAX_INVENTED_RUN, recall < MIN_RECALL or precision < MIN_PRECISION, and
   prints the offending passages so the writer knows what to fix.

Without a text layer there is no baseline: the chunk passes on format alone
and assemble.py then requires a review.json (an AI reviewer comparing out.md
with the page images) on the same out.md.

Usage:
  check_chunk.py WORKDIR CID [CID ...]   check these chunks
  check_chunk.py WORKDIR                 check every written chunk, print the
                                         status of all; exit 0 only if all done
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from harness import (CONTINUES, PLACEHOLDER_RE, HarnessError, chunk_dir,
                     chunk_state, load_candidates, load_job, load_json,
                     markdown_to_text, ngram_set, placeholders, save_json,
                     sha256, tokens, uncovered_runs)

N = 5
MAX_MISSING_RUN = 12
MAX_INVENTED_RUN = 8
MIN_RECALL = 0.95
MIN_PRECISION = 0.97

HTML_TAG = re.compile(r"</?[A-Za-z][^>]*>")
ANNOTATION = re.compile(r"\{[^{}|\n]+\|[^{}\n]+\}")
TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
LIST_ITEM = re.compile(r"^\s*(?:[-+]|\d+[.)])\s+\S")
FOOTNOTE_REF = re.compile(r"\[\^([^\]]+)\](?!:)")
FOOTNOTE_DEF = re.compile(r"^\[\^([^\]]+)\]:", re.M)
SPLIT_WORD = re.compile(r"[^\W\d_]-\n[a-záéíóúñüàèìòùçâêîôûäëïöü]")


# --------------------------------------------------------------------------- #
# Format
# --------------------------------------------------------------------------- #
def paragraphs(md: str):
    """[(text, in_verse)] blocks; a verse fence's lines are kept as one block."""
    blocks, cur, in_verse = [], [], False
    for line in md.splitlines():
        s = line.strip()
        if s.startswith("```"):
            if cur:
                blocks.append(("\n".join(cur), in_verse))
                cur = []
            in_verse = not in_verse
            continue
        if not s and not in_verse:
            if cur:
                blocks.append(("\n".join(cur), False))
                cur = []
            continue
        cur.append(line)
    if cur:
        blocks.append(("\n".join(cur), in_verse))
    return blocks


def check_format(md: str, allowed_figures: set) -> tuple:
    errors, warnings = [], []
    if not md.strip():
        return ["out.md is empty"], warnings

    fence_open = False
    for n, line in enumerate(md.splitlines(), 1):
        s = line.strip()
        if fence_open and not s:
            errors.append(f"line {n}: blank line inside a verse fence — close it and "
                          f"open a new one for the next stanza")
            continue
        if s.startswith("```"):
            if not fence_open and s != "```verse":
                errors.append(f"line {n}: only ```verse fences exist (got {s!r})")
            if fence_open and s != "```":
                errors.append(f"line {n}: a verse fence closes with a bare ``` (got {s!r})")
            fence_open = not fence_open
            continue
        if re.match(r"^#{3,}\s", s):
            errors.append(f"line {n}: only # and ## headings exist")
        if HTML_TAG.search(s):
            errors.append(f"line {n}: HTML is not markdown the builder knows: {s[:60]!r}")
        if ANNOTATION.search(s):
            errors.append(f"line {n}: {{word|reading}} is the graded-reader annotation, "
                          f"not for converted books: {s[:60]!r}")
        if TABLE_ROW.match(s):
            errors.append(f"line {n}: no markdown tables — place the table as a figure "
                          f"([[fig:ID | caption]])")
        if s.startswith("!["):
            errors.append(f"line {n}: images go in as [[fig:ID | caption]], not ![…](…)")
        if LIST_ITEM.match(line) and not fence_open:
            warnings.append(f"line {n}: lists render as plain paragraphs: {s[:50]!r}")
    if fence_open:
        errors.append("a ```verse fence is never closed")

    lines = [l.strip() for l in md.strip().splitlines()]
    for n, s in enumerate(lines, 1):
        if s == CONTINUES and n != len(lines):
            errors.append(f"{CONTINUES} may only be the last line")
        elif s.startswith("[[") and s != CONTINUES and not PLACEHOLDER_RE.match(s):
            errors.append(f"malformed harness line {s[:60]!r} — "
                          f"[[fig:ID | caption]] or {CONTINUES}")
    if lines and lines[-1] == CONTINUES and (len(lines) < 2 or not lines[-2]):
        errors.append(f"{CONTINUES} must follow the paragraph it continues, no blank line")

    for text, verse in paragraphs(md):
        s = text.strip()
        if "[[fig:" in s and not PLACEHOLDER_RE.match(s):
            errors.append(f"a figure placeholder must be a paragraph of its own: {s[:60]!r}")
        if re.fullmatch(r"\d{1,4}", s):
            errors.append(f"a bare number paragraph ({s}) — a page number left in?")
        if verse:
            continue
        if s.count("**") % 2 or s.replace("**", "").count("*") % 2:
            errors.append(f"unbalanced *italic* / **bold** in: {s[:60]!r}")
        if SPLIT_WORD.search(s):
            m = SPLIT_WORD.search(s)
            errors.append(f"a word still split across a line break: "
                          f"{s[max(0, m.start() - 15):m.end() + 10]!r} — join it")

    refs = set(FOOTNOTE_REF.findall(md))
    defs = set(FOOTNOTE_DEF.findall(md))
    for r in sorted(refs - defs):
        errors.append(f"footnote [^{r}] has no definition in this chunk")
    for d in sorted(defs - refs):
        errors.append(f"footnote definition [^{d}] is never referenced")

    for fid, _, _ in placeholders(md):
        if fid not in allowed_figures:
            errors.append(f"[[fig:{fid}]] is not a candidate on this chunk's pages "
                          f"(have: {', '.join(sorted(allowed_figures)) or 'none'})")
    return errors, warnings


# --------------------------------------------------------------------------- #
# Fidelity
# --------------------------------------------------------------------------- #
def inside(line: dict, bbox) -> bool:
    cx = (line["x0"] + line["x1"]) / 2
    cy = (line["top"] + line["bottom"]) / 2
    return bbox[0] <= cx <= bbox[2] and bbox[1] <= cy <= bbox[3]


def check_captions(page_text: str, used: list) -> list:
    """A caption must be printed text: every word of it on the chunk's pages.
    (A figure printed without a caption gets none.)"""
    printed = set(tokens(page_text))
    errors = []
    for fid, caption, _ in used:
        stray = [w for w in tokens(caption) if w not in printed]
        if stray:
            errors.append(f"caption of {fid} has words not printed on the page "
                          f"({' '.join(stray[:6])}) — copy the printed caption, or "
                          f"leave it empty: [[fig:{fid}]]")
    return errors


def baseline_text(workdir: Path, pages: list, figure_boxes: dict) -> str:
    """The chunk's pages from the text layer, in reading order, minus lines
    inside a figure this chunk placed (that text now lives in the image)."""
    wanted = set(pages)
    parts = []
    with (workdir / "extract" / "pages.jsonl").open(encoding="utf-8") as fh:
        for raw in fh:
            rec = json.loads(raw)
            if rec["page"] not in wanted:
                continue
            boxes = figure_boxes.get(rec["page"], [])
            parts.append("\n".join(ln["text"] for ln in rec["lines"]
                                   if not any(inside(ln, b) for b in boxes)))
    return "\n".join(parts)


def snippet(toks, run, limit=25) -> str:
    a, b = run
    words = toks[a:min(b, a + limit)]
    return " ".join(words) + (" …" if b - a > limit else "")


def check_fidelity(source: str, chunk_text: str, captions: str = "") -> dict:
    """Captions are the writer's words for a figure: they may cover source
    text (a printed caption) but never count as invented."""
    src, out = tokens(source), tokens(chunk_text)
    if len(src) < 4 * N:
        return {"skipped": f"source has only {len(src)} words"}
    gs = ngram_set(src, N)
    go = ngram_set(out, N) | ngram_set(tokens(captions), N)
    missing = uncovered_runs(src, go, N)
    invented = uncovered_runs(out, gs, N)
    recall = 1 - sum(b - a for a, b in missing) / len(src)
    precision = (1 - sum(b - a for a, b in invented) / len(out)) if out else 0.0
    missing.sort(key=lambda r: r[0] - r[1])
    invented.sort(key=lambda r: r[0] - r[1])
    longest_missing = max((b - a for a, b in missing), default=0)
    longest_invented = max((b - a for a, b in invented), default=0)
    failures = []
    if longest_missing >= MAX_MISSING_RUN:
        failures.append(f"dropped text: a run of {longest_missing} source words is missing")
    if longest_invented >= MAX_INVENTED_RUN:
        failures.append(f"text not in the source: a run of {longest_invented} words "
                        f"(paraphrase? invention? text read from an image?)")
    if recall < MIN_RECALL:
        failures.append(f"recall {recall:.3f} < {MIN_RECALL}")
    if precision < MIN_PRECISION:
        failures.append(f"precision {precision:.3f} < {MIN_PRECISION}")
    return {
        "source_words": len(src),
        "chunk_words": len(out),
        "recall": round(recall, 4),
        "precision": round(precision, 4),
        "longest_missing_run": longest_missing,
        "longest_invented_run": longest_invented,
        "missing": [snippet(src, r) for r in missing[:8] if r[1] - r[0] >= 4],
        "invented": [snippet(out, r) for r in invented[:8] if r[1] - r[0] >= 4],
        "failures": failures,
    }


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
def check_chunk(workdir: Path, chunk: dict, cands: dict) -> dict:
    d = chunk_dir(workdir, chunk["id"])
    out_path = d / "out.md"
    if not out_path.exists():
        raise HarnessError(f"{chunk['id']}: no out.md to check")
    md = out_path.read_text(encoding="utf-8")
    inp = load_json(d / "input.json", {})
    allowed = set(inp.get("figure_candidates", []))

    errors, warnings = check_format(md, allowed)
    fidelity = None
    if chunk["text_layer"]:
        used = placeholders(md)
        boxes = {}
        for fid, _, _ in used:
            if fid in cands:
                boxes.setdefault(cands[fid]["page"], []).append(cands[fid]["bbox"])
        fidelity = check_fidelity(baseline_text(workdir, chunk["pages"], boxes),
                                  markdown_to_text(md), "\n".join(c for _, c, _ in used))
        errors += check_captions(baseline_text(workdir, chunk["pages"], {}), used)

    result = {
        "chunk": chunk["id"],
        "pages": chunk["pages"],
        "sha256": sha256(out_path),
        "format_errors": errors,
        "warnings": warnings,
        "fidelity": fidelity,
        "review_required": not chunk["text_layer"],
        "pass": not errors and not (fidelity or {}).get("failures"),
    }
    save_json(d / "check.json", result)
    return result


def describe(result: dict) -> str:
    head = f"{result['chunk']} (pages {result['pages'][0]}-{result['pages'][-1]}): " \
           f"{'PASS' if result['pass'] else 'FAIL'}"
    lines = [head]
    f = result["fidelity"]
    if f and "recall" in f:
        lines.append(f"  fidelity: recall {f['recall']}, precision {f['precision']}, "
                     f"longest missing run {f['longest_missing_run']}, "
                     f"longest invented run {f['longest_invented_run']}")
    elif f:
        lines.append(f"  fidelity: skipped ({f['skipped']})")
    else:
        lines.append("  fidelity: no text layer — needs review.json before assembly")
    for e in result["format_errors"]:
        lines.append(f"  format: {e}")
    if f:
        for e in f.get("failures", []):
            lines.append(f"  fidelity: {e}")
        if not result["pass"]:
            for s in f.get("missing", []):
                lines.append(f"    missing:  {s}")
            for s in f.get("invented", []):
                lines.append(f"    invented: {s}")
    for w in result["warnings"]:
        lines.append(f"  warning: {w}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workdir", type=Path)
    ap.add_argument("chunks", nargs="*", help="chunk ids (default: every written chunk)")
    args = ap.parse_args()
    try:
        job = load_job(args.workdir)
        cands = load_candidates(args.workdir)
        by_id = {c["id"]: c for c in job["chunks"]}
        unknown = [c for c in args.chunks if c not in by_id]
        if unknown:
            raise HarnessError(f"unknown chunk(s): {', '.join(unknown)}")
        targets = args.chunks or [c["id"] for c in job["chunks"]
                                  if (chunk_dir(args.workdir, c["id"]) / "out.md").exists()]
        ok = True
        for cid in targets:
            result = check_chunk(args.workdir, by_id[cid], cands)
            print(describe(result))
            ok &= result["pass"]
        if args.chunks:
            return 0 if ok else 1
        states = [(c["id"], chunk_state(args.workdir, c)["state"]) for c in job["chunks"]]
        print("status: " + "  ".join(f"{cid} {st}" for cid, st in states))
        return 0 if all(st == "done" for _, st in states) else 1
    except HarnessError as e:
        print(f"check_chunk: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
