#!/usr/bin/env python3
"""Stage 2 of pdf2epub: apply a policy.json's mechanical decisions to raw
extraction (pages.jsonl), producing restored.md -- clean paragraph text
ready for the agent to draft chapters from.

Everything here is a *mechanical transform the agent already decided*:
which lines are furniture, how each page range reflows, what punctuation
to normalize, whether to dehyphenate. This script never guesses; every
switch comes from policy.json. The one thing it does decide on its own is
whether the result is trustworthy -- the fidelity gate (char_ratio +
ngram_containment) -- and it exits 1 when that gate fails, which is the
signal for the agent to look at restore-report.json and adjust the policy.

Usage:
  restore.py EXTRACTDIR --policy workspace/<slug>/policy.json --out RESTOREDIR
"""
import argparse
import json
import re
import statistics
import sys
from pathlib import Path

TERMINAL_PUNCT = set(".!?…:”»)")
WHITESPACE = re.compile(r"\s+")


class RestoreError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def load_pages(extract_dir: Path) -> dict:
    pages = {}
    for raw in (extract_dir / "pages.jsonl").read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        rec = json.loads(raw)
        pages[rec["page"]] = rec
    return pages


def parse_page_spec(spec: str):
    a, _, b = spec.partition("-")
    lo = int(a)
    hi = int(b) if b else lo
    return lo, hi


def assign_pages_to_ranges(page_ranges, all_page_nums):
    """Map page_num -> its page_ranges entry. Every extracted page must be
    covered by exactly one range -- ambiguity is a policy bug, not a guess
    restore.py should make."""
    assignment = {}
    for r in page_ranges:
        lo, hi = parse_page_spec(r["pages"])
        for p in range(lo, hi + 1):
            if p in assignment:
                raise RestoreError(
                    f"page {p} covered by multiple page_ranges "
                    f"({assignment[p]['pages']!r} and {r['pages']!r})"
                )
            assignment[p] = r
    missing = sorted(set(all_page_nums) - set(assignment))
    if missing:
        raise RestoreError(f"pages not covered by any page_range: {missing}")
    return assignment


# --------------------------------------------------------------------------- #
# Furniture
# --------------------------------------------------------------------------- #
def compile_furniture(patterns):
    return [(p, re.compile(p)) for p in patterns]


def furniture_match(text, compiled):
    for raw, rx in compiled:
        if rx.search(text):
            return raw
    return None


# --------------------------------------------------------------------------- #
# Dehyphenation
# --------------------------------------------------------------------------- #
def dehyphenate_lines(lines, exceptions):
    """Merge a line ending '-' into a following line starting lowercase,
    dropping the hyphen unless the joined word is a listed exception (then
    keep it, still merge the two physical lines into one)."""
    exceptions_lower = {e.lower() for e in exceptions}
    out = []
    resolved = 0
    i = 0
    n = len(lines)
    while i < n:
        cur = dict(lines[i])
        j = i + 1
        while cur["text"].endswith("-") and j < n and cur["_page"] == lines[j]["_page"] and lines[j]["text"][:1].islower():
            nxt = lines[j]
            last_token = cur["text"].rstrip().split(" ")[-1]
            if len(last_token) < 2:
                break
            prefix = last_token[:-1]
            first_token = nxt["text"].split(" ", 1)[0]
            joined_word = prefix + first_token
            if joined_word.lower() in exceptions_lower:
                merged_text = cur["text"] + nxt["text"]
            else:
                merged_text = cur["text"][:-1] + nxt["text"]
                resolved += 1
            cur = {**cur, "text": merged_text, "bottom": nxt["bottom"]}
            j += 1
        out.append(cur)
        i = j
    return out, resolved


# --------------------------------------------------------------------------- #
# Reflow
# --------------------------------------------------------------------------- #
def reflow_sentence(lines):
    paragraphs = []
    buf = []
    joins = 0
    for ln in lines:
        if buf:
            joins += 1
        buf.append(ln["text"])
        text = ln["text"].rstrip()
        if text and text[-1] in TERMINAL_PUNCT:
            paragraphs.append(" ".join(buf))
            buf = []
    if buf:
        paragraphs.append(" ".join(buf))
    return paragraphs, joins


def reflow_prose(lines):
    if not lines:
        return [], 0
    gaps = [
        lines[i]["top"] - lines[i - 1]["bottom"]
        for i in range(1, len(lines))
        if lines[i]["_page"] == lines[i - 1]["_page"]
    ]
    gaps = [g for g in gaps if g > 0]
    median_gap = statistics.median(gaps) if gaps else 0.0
    try:
        baseline_x0 = statistics.mode(round(l["x0"]) for l in lines)
    except statistics.StatisticsError:
        baseline_x0 = round(lines[0]["x0"])

    paragraphs = []
    buf = [lines[0]["text"]]
    joins = 0
    for i in range(1, len(lines)):
        prev, cur = lines[i - 1], lines[i]
        new_para = False
        if cur["_page"] == prev["_page"] and median_gap > 0:
            if (cur["top"] - prev["bottom"]) > 1.6 * median_gap:
                new_para = True
        if abs(round(cur["x0"]) - baseline_x0) > 10:
            new_para = True
        if new_para:
            paragraphs.append(" ".join(buf))
            buf = [cur["text"]]
        else:
            buf.append(cur["text"])
            joins += 1
    if buf:
        paragraphs.append(" ".join(buf))
    return paragraphs, joins


def verse_block(lines):
    return "\n".join(ln["text"] for ln in lines)


# --------------------------------------------------------------------------- #
# Chunk processing
# --------------------------------------------------------------------------- #
def process_chunk(range_entry, page_nums, pages, global_reflow, dehyphenate_on,
                   exceptions, furniture_compiled, furniture_counts):
    treat = range_entry["treat"]
    reflow_mode = range_entry.get("reflow", global_reflow)

    flat_lines = []
    for p in page_nums:
        for ln in pages[p]["lines"]:
            hit = furniture_match(ln["text"], furniture_compiled)
            if hit:
                furniture_counts[hit] = furniture_counts.get(hit, 0) + 1
                continue
            entry = dict(ln)
            entry["_page"] = p
            flat_lines.append(entry)

    if treat == "skip":
        # Furniture within a skipped page is still tallied above, but none
        # of its lines make it into the restored text.
        return [], 0, 0, 0

    lines_out = len(flat_lines)

    if treat == "front_matter":
        # Verbatim: one paragraph per surviving physical line, untouched.
        return [("para", ln["text"]) for ln in flat_lines], 0, 0, lines_out

    if treat != "body":
        raise RestoreError(f"unknown treat {treat!r}")

    hyphens_resolved = 0
    lines_for_reflow = flat_lines
    if dehyphenate_on:
        lines_for_reflow, hyphens_resolved = dehyphenate_lines(flat_lines, exceptions)

    if reflow_mode == "sentence":
        paragraphs, joins = reflow_sentence(lines_for_reflow)
        units = [("para", p) for p in paragraphs]
    elif reflow_mode == "prose":
        paragraphs, joins = reflow_prose(lines_for_reflow)
        units = [("para", p) for p in paragraphs]
    elif reflow_mode == "verse":
        joins = 0
        units = [("verse", verse_block(lines_for_reflow))] if lines_for_reflow else []
    else:
        raise RestoreError(f"unknown reflow {reflow_mode!r}")

    return units, joins, hyphens_resolved, lines_out


# --------------------------------------------------------------------------- #
# Fidelity gate
# --------------------------------------------------------------------------- #
def apply_normalize(text, table, counts=None):
    for old, new in table.items():
        if counts is not None:
            counts[old] = counts.get(old, 0) + text.count(old)
        text = text.replace(old, new)
    return text


def word_ngrams(text, n=5):
    words = text.split()
    if len(words) < n:
        return set()
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def nonwhitespace_chars(text):
    return len(WHITESPACE.sub("", text))


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
def render_markdown(units):
    parts = []
    for kind, text in units:
        if kind == "verse":
            parts.append("```verse\n" + text + "\n```")
        else:
            parts.append(text)
    return "\n\n".join(parts) + "\n"


def restore(extract_dir: Path, policy: dict) -> dict:
    pages = load_pages(extract_dir)
    all_page_nums = sorted(pages)
    assignment = assign_pages_to_ranges(policy["page_ranges"], all_page_nums)

    furniture_compiled = compile_furniture(policy.get("furniture", []))
    furniture_counts = {}

    # Raw "in" text for the fidelity gate: every extracted line, across every
    # page regardless of treat, minus furniture -- a deliberate skip still
    # has to justify itself against this baseline.
    in_parts = []
    for p in all_page_nums:
        for ln in pages[p]["lines"]:
            if furniture_match(ln["text"], furniture_compiled):
                continue
            in_parts.append(ln["text"])
    in_text = "\n".join(in_parts)

    # Chunks in page order, one per page_ranges entry.
    ranges_in_order = sorted(policy["page_ranges"], key=lambda r: parse_page_spec(r["pages"])[0])

    all_units = []
    joins_made = 0
    hyphens_resolved = 0
    lines_out_total = 0
    for r in ranges_in_order:
        lo, hi = parse_page_spec(r["pages"])
        page_nums = [p for p in range(lo, hi + 1) if p in pages]
        units, joins, hyph, lines_out = process_chunk(
            r, page_nums, pages, policy.get("reflow", "sentence"),
            policy.get("dehyphenate", False), policy.get("dehyphenate_exceptions", []),
            furniture_compiled, furniture_counts,
        )
        all_units.extend(units)
        joins_made += joins
        hyphens_resolved += hyph
        lines_out_total += lines_out

    # Normalize last, on the actual output content.
    normalize_table = policy.get("normalize", {})
    normalize_counts = {}
    normalized_units = []
    for kind, text in all_units:
        normalized_units.append((kind, apply_normalize(text, normalize_table, normalize_counts)))
    all_units = normalized_units

    out_text = "\n".join(text for _, text in all_units)
    norm_in = apply_normalize(in_text, normalize_table)
    norm_out = out_text  # already normalized above

    chars_in = nonwhitespace_chars(in_text)
    chars_out = nonwhitespace_chars(out_text)
    char_ratio = (chars_out / chars_in) if chars_in else 1.0

    grams_in = word_ngrams(norm_in)
    grams_out = word_ngrams(norm_out)
    ngram_containment = (len(grams_in & grams_out) / len(grams_in)) if grams_in else 1.0

    gate_pass = 0.98 <= char_ratio <= 1.02 and ngram_containment >= 0.995

    lines_in_total = sum(len(pages[p]["lines"]) for p in all_page_nums)
    paragraphs_emitted = sum(1 for kind, _ in all_units if kind == "para") + \
        sum(1 for kind, _ in all_units if kind == "verse")

    report = {
        "source": str(extract_dir),
        "lines_in": lines_in_total,
        "lines_out": lines_out_total,
        "furniture_dropped": furniture_counts,
        "joins_made": joins_made,
        "hyphens_resolved": hyphens_resolved,
        "normalizations": normalize_counts,
        "paragraphs_emitted": paragraphs_emitted,
        "char_ratio": round(char_ratio, 4),
        "ngram_containment": round(ngram_containment, 4),
        "gate_pass": gate_pass,
    }
    markdown = render_markdown(all_units)
    return report, markdown


def summarize(report: dict) -> str:
    status = "PASS" if report["gate_pass"] else "FAIL"
    return (
        f"lines {report['lines_in']} -> {report['lines_out']}, "
        f"{report['paragraphs_emitted']} paragraphs, "
        f"{report['joins_made']} joins, {report['hyphens_resolved']} hyphens resolved\n"
        f"furniture dropped: {report['furniture_dropped'] or 'none'}\n"
        f"normalizations: {report['normalizations'] or 'none'}\n"
        f"fidelity gate: {status}  "
        f"(char_ratio={report['char_ratio']}, ngram_containment={report['ngram_containment']})"
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("extract_dir", type=Path)
    ap.add_argument("--policy", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    policy = json.loads(args.policy.read_text(encoding="utf-8"))
    try:
        report, markdown = restore(args.extract_dir, policy)
    except RestoreError as e:
        print(f"restore error: {e}", file=sys.stderr)
        return 1

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "restored.md").write_text(markdown, encoding="utf-8")
    (args.out / "restore-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2)
    )

    print(summarize(report))
    print(f"wrote {args.out / 'restored.md'} + restore-report.json")
    return 0 if report["gate_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
