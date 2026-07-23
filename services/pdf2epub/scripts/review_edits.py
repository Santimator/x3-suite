#!/usr/bin/env python3
"""Guard the one place pdf2epub lets the agent touch prose directly.

The pipeline's contract is "every byte traces back to the extraction". The
*favoured* way to correct the text is still `policy.json` — furniture,
reflow, and especially `normalize` (exact string replacements) are
pattern-level fixes that replay from source and belong there. But some OCR
damage is a genuine one-off: a single `teh`, a dropped accent, a name mangled
in exactly one spot. Writing a normalize rule + occurrence disambiguation for
each is more ceremony than the fix, and throws away the model's plasticity.

So the agent may hand-edit a copy of the restored text — `restore/corrected.md`
next to the immutable `restore/restored.md` — and this script is the
deterministic guard that keeps that freedom honest: it bounds the diff to
small, local corrections and *prints it* for review. Big or wholesale
changes fail the gate (exit 1): that's the signal that the change is a
rewrite, not a correction, and belongs in the policy (or shouldn't happen).

The guarantee shifts from "replays byte-for-byte from the PDF" to "every
change is small, local, and shown to you" — plasticity for the one-off,
determinism for the check.

Usage:
  review_edits.py workspace/<slug>/restore            # restored.md vs corrected.md
  review_edits.py --baseline A.md --corrected B.md
"""
from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

# Defaults — a correction is small and local; anything past these is a rewrite.
MAX_CHANGED_RATIO = 0.02   # changed chars / baseline chars
MAX_EDIT_SPAN = 24         # longest single contiguous changed run (chars)
MAX_NET_GROWTH = 0.01      # |len(corrected) - len(baseline)| / baseline


def measure(baseline: str, corrected: str) -> dict:
    sm = difflib.SequenceMatcher(None, baseline, corrected, autojunk=False)
    changed = 0
    max_span = 0
    edits = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        edits += 1
        span = max(i2 - i1, j2 - j1)
        changed += span
        max_span = max(max_span, span)
    base_len = max(len(baseline), 1)
    return {
        "edits": edits,
        "changed_chars": changed,
        "changed_ratio": round(changed / base_len, 4),
        "max_edit_span": max_span,
        "net_growth": round(abs(len(corrected) - len(baseline)) / base_len, 4),
        "baseline_chars": len(baseline),
        "corrected_chars": len(corrected),
    }


def review(baseline: str, corrected: str,
           max_changed_ratio: float = MAX_CHANGED_RATIO,
           max_edit_span: int = MAX_EDIT_SPAN,
           max_net_growth: float = MAX_NET_GROWTH) -> dict:
    m = measure(baseline, corrected)
    reasons = []
    if m["changed_ratio"] > max_changed_ratio:
        reasons.append(f"changed_ratio {m['changed_ratio']} > {max_changed_ratio} "
                       "(too much of the text edited)")
    if m["max_edit_span"] > max_edit_span:
        reasons.append(f"max_edit_span {m['max_edit_span']} > {max_edit_span} "
                       "(an edit is a rewrite, not a local fix — use policy)")
    if m["net_growth"] > max_net_growth:
        reasons.append(f"net_growth {m['net_growth']} > {max_net_growth} "
                       "(text added/removed wholesale, not corrected)")
    m["bounds"] = {"max_changed_ratio": max_changed_ratio,
                   "max_edit_span": max_edit_span, "max_net_growth": max_net_growth}
    m["pass"] = not reasons
    m["reasons"] = reasons
    return m


def unified(baseline: str, corrected: str) -> str:
    return "".join(difflib.unified_diff(
        baseline.splitlines(keepends=True), corrected.splitlines(keepends=True),
        fromfile="restored.md", tofile="corrected.md"))


def summarize(report: dict, diff: str) -> str:
    head = (f"edits={report['edits']} changed={report['changed_chars']} chars "
            f"({report['changed_ratio']:.1%}), longest run={report['max_edit_span']}, "
            f"net growth={report['net_growth']:.1%}")
    status = "PASS" if report["pass"] else "FAIL: " + "; ".join(report["reasons"])
    parts = [diff.rstrip("\n") if diff.strip() else "(no changes)", "", head, status]
    return "\n".join(p for p in parts if p is not None)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("restore_dir", type=Path, nargs="?",
                    help="dir holding restored.md + corrected.md")
    ap.add_argument("--baseline", type=Path, help="override baseline path")
    ap.add_argument("--corrected", type=Path, help="override corrected path")
    ap.add_argument("--max-changed-ratio", type=float, default=MAX_CHANGED_RATIO)
    ap.add_argument("--max-edit-span", type=int, default=MAX_EDIT_SPAN)
    ap.add_argument("--max-net-growth", type=float, default=MAX_NET_GROWTH)
    args = ap.parse_args()

    if args.baseline and args.corrected:
        baseline_path, corrected_path = args.baseline, args.corrected
    elif args.restore_dir:
        baseline_path = args.restore_dir / "restored.md"
        corrected_path = args.restore_dir / "corrected.md"
    else:
        ap.error("give a restore dir, or both --baseline and --corrected")

    if not corrected_path.exists():
        print(f"no corrected.md at {corrected_path} — nothing to review (policy-only path)")
        return 0
    baseline = baseline_path.read_text(encoding="utf-8")
    corrected = corrected_path.read_text(encoding="utf-8")

    report = review(baseline, corrected, args.max_changed_ratio,
                    args.max_edit_span, args.max_net_growth)
    print(summarize(report, unified(baseline, corrected)))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
