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


def flatten_lines(pages: dict, all_page_nums) -> list:
    """Every line across every page, in document order, each tagged with
    its page number. The single ordered sequence page_ranges spans are cut
    from -- a page-based range and an anchor-based range are just two ways
    of picking a slice of it."""
    flat = []
    for p in all_page_nums:
        for ln in pages[p]["lines"]:
            entry = dict(ln)
            entry["_page"] = p
            flat.append(entry)
    return flat


def find_anchor_line_index(flat_lines: list, anchor: str, label: str,
                            start_from: int = 0, occurrence: int = None) -> int:
    """A page_ranges anchor matches one physical line's text exactly (unlike
    draft.json's anchors, which match any substring of the restored prose --
    here the natural unit already is a single extracted line). start_from
    restricts the search to lines at or after it.

    A repeated line (a refrain, say) is still an error by default -- never
    guessed -- unless the policy explicitly disambiguates with a 1-indexed
    `occurrence` (that's the agent's decision, not the script's)."""
    hits = [i for i in range(start_from, len(flat_lines)) if flat_lines[i]["text"] == anchor]
    if not hits:
        raise RestoreError(f"{label}: anchor not found: {anchor!r}")
    if occurrence is not None:
        if not (1 <= occurrence <= len(hits)):
            raise RestoreError(
                f"{label}: occurrence {occurrence} out of range "
                f"({len(hits)} hits for {anchor!r})"
            )
        return hits[occurrence - 1]
    if len(hits) > 1:
        raise RestoreError(
            f"{label}: anchor ambiguous ({len(hits)} hits): {anchor!r} "
            f"-- disambiguate with an explicit 1-indexed \"occurrence\""
        )
    return hits[0]


def _range_label(r: dict) -> str:
    return f"pages {r['pages']!r}" if "pages" in r else f"anchor {r['start_anchor']!r}"


def resolve_range_span(r: dict, flat_lines: list):
    """Return [start, end) into flat_lines for one page_ranges entry.
    Either "pages": "A-B" (whole pages, the common case) or a
    "start_anchor"/"end_anchor" pair (both required together) isolating an
    exact line span -- e.g. a verse passage embedded mid-page, where
    page-level granularity can't separate it from the surrounding prose."""
    if "start_anchor" in r or "end_anchor" in r:
        if "pages" in r:
            raise RestoreError("page_ranges entry cannot mix 'pages' with start_anchor/end_anchor")
        if "start_anchor" not in r or "end_anchor" not in r:
            raise RestoreError("page_ranges anchor entry needs both start_anchor and end_anchor")
        start = find_anchor_line_index(flat_lines, r["start_anchor"], "page_ranges start_anchor",
                                        occurrence=r.get("start_anchor_occurrence"))
        end = find_anchor_line_index(flat_lines, r["end_anchor"], "page_ranges end_anchor",
                                      start_from=start, occurrence=r.get("end_anchor_occurrence"))
        return start, end + 1
    lo, hi = parse_page_spec(r["pages"])
    idxs = [i for i, ln in enumerate(flat_lines) if lo <= ln["_page"] <= hi]
    if not idxs:
        raise RestoreError(f"page range {r['pages']!r} matches no extracted pages")
    return idxs[0], idxs[-1] + 1


def resolve_ranges(page_ranges: list, flat_lines: list) -> list:
    """Resolve every page_ranges entry to a [start, end) span over
    flat_lines, then require them to exactly partition it in document
    order -- no gaps, no overlaps, whether the entries are page-based,
    anchor-based, or a mix. Ambiguity is a policy bug, never guessed."""
    spans = [(*resolve_range_span(r, flat_lines), r) for r in page_ranges]
    spans.sort(key=lambda s: s[0])

    cursor = 0
    for start, end, r in spans:
        if start != cursor:
            raise RestoreError(
                f"page_ranges gap or overlap at line index {cursor}: "
                f"{_range_label(r)} starts at line index {start}"
            )
        cursor = end
    if cursor != len(flat_lines):
        raise RestoreError(
            f"page_ranges do not cover the whole document: "
            f"covered {cursor} of {len(flat_lines)} lines"
        )
    return spans


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
def process_chunk(range_entry, lines, global_reflow, dehyphenate_on,
                   exceptions, furniture_compiled, furniture_counts):
    treat = range_entry["treat"]
    reflow_mode = range_entry.get("reflow", global_reflow)

    flat_lines = []
    for ln in lines:
        hit = furniture_match(ln["text"], furniture_compiled)
        if hit:
            furniture_counts[hit] = furniture_counts.get(hit, 0) + 1
            continue
        flat_lines.append(dict(ln))

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
    flat_lines = flatten_lines(pages, all_page_nums)

    furniture_compiled = compile_furniture(policy.get("furniture", []))
    furniture_counts = {}

    # Raw "in" text for the fidelity gate: every extracted line, across every
    # page regardless of treat, minus furniture -- a deliberate skip still
    # has to justify itself against this baseline.
    in_text = "\n".join(
        ln["text"] for ln in flat_lines if not furniture_match(ln["text"], furniture_compiled)
    )

    spans = resolve_ranges(policy["page_ranges"], flat_lines)

    all_units = []
    joins_made = 0
    hyphens_resolved = 0
    lines_out_total = 0
    for start, end, r in spans:
        units, joins, hyph, lines_out = process_chunk(
            r, flat_lines[start:end], policy.get("reflow", "sentence"),
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

    lines_in_total = len(flat_lines)
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
