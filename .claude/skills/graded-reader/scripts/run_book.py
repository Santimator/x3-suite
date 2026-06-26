#!/usr/bin/env python3
"""Headless driver for the graded-reader loop, via an OpenAI-compatible model.

This is the *runner* alternative to Claude Code driving SKILL.md by hand. It
wears the three model hats (scribe, rework, glossary-editor) by calling an
OpenAI-compatible endpoint (NVIDIA NIM by default — see config.example.json) and
runs the deterministic scripts between them as subprocesses. The seam is
file-based, so the same scripts serve both drivers; only *who fills the LLM
steps* changes.

Per chapter:
  gen_context.py  -> brief.md                      (deterministic)
  [scribe]        -> chNN.md                        (model)
  validate.py     -> report; while fail < cap:      (deterministic)
      [rework]    -> chNN.md                         (model, given flagged words)
  update_state.py -> raw glossary + plan/book        (deterministic)
  [gloss-editor]  -> curated chNN-glossary.tsv       (model)
Then once, after all chapters: build_epub.py.

Add-and-gloss (forcing a topic-essential above-level word into personal.tsv) is
a real judgement call; the runner does NOT do it silently. If a chapter still
fails after the rework cap, the runner stops on that chapter and reports the
sticky flagged words so a human (or Claude Code) can decide. Everything up to
that point is saved, so the run resumes cleanly.

Usage:
  run_book.py BOOKDIR [--chapter N] [--from N] [--config PATH]
              [--length 220] [--no-epub] [--dry-run]
  (no --chapter/--from: process every outline entry not yet accepted)
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import llm as llm_mod  # noqa: E402

PROMPTS = SCRIPTS.parent / "prompts"
PY = sys.executable  # run sibling scripts with the same interpreter (the venv)


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def strip_fences(text: str) -> str:
    """Drop a single wrapping ```...``` fence if the model added one."""
    t = text.strip()
    if t.startswith("```"):
        lines = t.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return t


def run_script(args: List[str], capture_json: bool = False) -> Dict:
    """Run a sibling script with the venv interpreter. Return parsed JSON if asked."""
    proc = subprocess.run([PY, *args], capture_output=True, text=True)
    if capture_json:
        # validate.py exits 1 on fail but still prints the JSON report
        out = proc.stdout.strip()
        if not out:
            raise SystemExit(f"no JSON from {args}:\n{proc.stderr}")
        return json.loads(out)
    if proc.returncode != 0:
        raise SystemExit(f"command failed: {' '.join(args)}\n{proc.stderr}")
    if proc.stdout:
        print(proc.stdout.rstrip())
    return {}


def pending_chapters(plan: Dict, only: Optional[int], frm: Optional[int]) -> List[int]:
    ns = [c["n"] for c in plan.get("outline", [])]
    if only is not None:
        return [only]
    if frm is not None:
        return [n for n in ns if n >= frm]
    done = {"accepted", "drafted-and-validated"}
    return [c["n"] for c in plan.get("outline", []) if c.get("status") not in done]


def scribe_chapter(brief: str, cfg: Dict) -> str:
    system = read(PROMPTS / "scribe.md")
    return strip_fences(llm_mod.chat(system, brief, cfg))


def rework_chapter(brief: str, draft: str, flagged: List[str], cfg: Dict) -> str:
    system = read(PROMPTS / "scribe.md")
    user = (
        brief
        + "\n\n## Your previous draft FAILED validation\n"
        + "These tokens are out of level — rewrite the chapter to avoid every one "
        + "of them, keeping the same story and staying in the permitted vocabulary:\n"
        + "  " + " ".join(flagged) + "\n\n"
        + "Here is your previous draft to revise:\n\n" + draft
    )
    return strip_fences(llm_mod.chat(system, user, cfg))


def edit_glossary(chapter_text: str, raw_tsv: str, cfg: Dict) -> str:
    system = read(PROMPTS / "glossary_editor.md")
    user = (
        "## Chapter text\n\n" + chapter_text + "\n\n"
        "## Proposed glossary (curate this)\n\n" + raw_tsv
    )
    return strip_fences(llm_mod.chat(system, user, cfg))


def process_chapter(book: Path, n: int, cfg: Dict, length: int, dry_run: bool) -> bool:
    plan = json.loads(read(book / "plan.json"))
    val = plan.get("validation", {})
    threshold = str(val.get("threshold", 0.05))
    max_stretch = str(val.get("max_stretch", 0.15))
    cap = int(val.get("rework_cap", 3))

    build = book / "build"
    build.mkdir(parents=True, exist_ok=True)
    brief_path = build / f"ch{n:02d}-brief.md"
    ch_path = book / "chapters" / f"ch{n:02d}.md"
    gloss_path = build / f"ch{n:02d}-glossary.tsv"

    print(f"\n=== chapter {n} ===")
    # 1. brief (deterministic)
    run_script([str(SCRIPTS / "gen_context.py"), str(book), "--chapter", str(n),
                "--out", str(brief_path), "--length", str(length)])
    brief = read(brief_path)

    if dry_run:
        print("  [dry-run] would call scribe + validate + gloss-editor")
        return True

    # 2. scribe (model)
    ch_path.parent.mkdir(parents=True, exist_ok=True)
    print("  scribe: drafting...")
    ch_path.write_text(scribe_chapter(brief, cfg) + "\n", encoding="utf-8")

    # 3. validate + rework loop (deterministic grade, model rework)
    for attempt in range(cap + 1):
        report = run_script(
            [str(SCRIPTS / "validate.py"), str(ch_path), "--threshold", threshold,
             "--max-stretch", max_stretch, "--json"], capture_json=True)
        print(f"  validate (try {attempt}): out-of-list {report['out_of_list_rate']:.1%}, "
              f"stretch {report['stretch_rate']:.1%} -> {'PASS' if report['passed'] else 'FAIL'}")
        if report["passed"]:
            break
        if attempt == cap:
            print("  rework cap reached; chapter still failing on: "
                  + " ".join(report["flagged_tokens"][:20]))
            print("  -> stopping. Decide add-and-gloss (add a topic-essential word to "
                  "lists/personal.tsv) or hand to Claude Code, then re-run from this chapter.")
            return False
        print(f"  rework {attempt + 1}/{cap}: avoiding " + " ".join(report["flagged_tokens"][:12]))
        ch_path.write_text(
            rework_chapter(brief, read(ch_path), report["flagged_tokens"], cfg) + "\n",
            encoding="utf-8")

    # 4. update_state (deterministic): writes raw glossary, updates plan/book, files recap
    run_script([str(SCRIPTS / "update_state.py"), str(book), "--chapter", str(n)])

    # 5. glossary editor (model): prune transparent rows, fill blanks
    if gloss_path.exists():
        raw_tsv = read(gloss_path)
        if len([ln for ln in raw_tsv.splitlines() if ln.strip()]) > 1:
            print("  gloss-editor: curating glossary...")
            curated = edit_glossary(read(ch_path), raw_tsv, cfg)
            if curated.lower().startswith("word\t"):
                gloss_path.write_text(curated.rstrip() + "\n", encoding="utf-8")
                kept = len([ln for ln in curated.splitlines() if ln.strip()]) - 1
                print(f"  glossary curated -> {gloss_path.name} ({kept} rows kept)")
            else:
                print("  WARN: gloss-editor output didn't look like a TSV; kept raw glossary")
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Headless graded-reader runner (OpenAI-compatible model).")
    ap.add_argument("book_dir", type=Path)
    ap.add_argument("--chapter", type=int, default=None, help="just this chapter")
    ap.add_argument("--from", dest="frm", type=int, default=None, help="from this chapter to the end")
    ap.add_argument("--config", type=Path, default=None, help="LLM config (default: skill config.json)")
    ap.add_argument("--length", type=int, default=220, help="target chapter length in characters")
    ap.add_argument("--no-epub", action="store_true", help="skip the final EPUB build")
    ap.add_argument("--dry-run", action="store_true", help="build briefs only; no model calls")
    args = ap.parse_args(argv)

    book = args.book_dir
    plan = json.loads(read(book / "plan.json"))
    cfg = llm_mod.load_config(args.config) if not args.dry_run else {}
    if not args.dry_run:
        print(f"model: {cfg['model']}  @ {cfg['base_url']}")

    todo = pending_chapters(plan, args.chapter, args.frm)
    if not todo:
        print("nothing to do — all outline chapters already accepted.")
    print(f"chapters to process: {todo}")

    completed = 0
    for n in todo:
        if not process_chapter(book, n, cfg, args.length, args.dry_run):
            print(f"\nstopped at chapter {n}. Resume with: run_book.py {book} --from {n}")
            return 1
        completed += 1

    if completed and not args.no_epub and not args.dry_run:
        out = book / "build" / f"{book.name}.epub"
        print(f"\n=== assembling EPUB ===")
        run_script([str(SCRIPTS / "build_epub.py"), str(book), "--out", str(out),
                    "--pinyin-mode", "interlinear"])
    print("\ndone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
