#!/usr/bin/env python3
"""Headless driver for pdf2epub, via an OpenAI-compatible vision model.

The unattended alternative to Claude Code driving SKILL.md. It fills the same
file seams — chunks/cNN/out.md and review.json — and runs the same scripts
around them, so a conversion can be started by one driver and finished by the
other.

  plan.py           (if there is no job.json; device + figures from config)
  per chunk, in order, skipping chunks already done:
    [writer]        -> out.md               page images + text layer + figure
                                            previews + the previous chunk's end
    check_chunk.py  -> gate; on fail, the writer gets the report and rewrites,
                       up to max_attempts
    [reviewer]      -> review.json          chunks without a text layer (always),
                                            others if review_text_chunks; issues
                                            go back to the writer once
  figures.py prepare, assemble.py, build_epub.py, verify.py

It stops rather than guesses: a chunk still failing after max_attempts stops
the run with the gate's report. Everything done so far stays on disk and the
next run resumes from the first chunk not done. A bad transcription that
"completes" is worse than a run that stops — the reader is the last gate.

Not for the TRANSCRIPT route (a user transcript beside the PDF): matching its
text to chunks is a judgement call for an interactive driver.

Usage:
  run_conversion.py WORKDIR [--config PATH] [--plan-only] [--no-epub]
Exit: 0 EPUB built and verified · 1 stopped (progress saved) · 2 not configured
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
SCRIPTS = SKILL / "scripts"
PROMPTS = SKILL / "prompts"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(SCRIPTS))
import llm as llm_mod  # noqa: E402
from check_chunk import check_chunk, describe  # noqa: E402
from harness import (CONTINUES, REPO_ROOT, chunk_dir, chunk_state,  # noqa: E402
                     load_candidates, load_job, load_json, save_json, sha256)

PY = sys.executable


class Stop(RuntimeError):
    pass


def run(cmd: list) -> str:
    p = subprocess.run([PY] + [str(c) for c in cmd], capture_output=True, text=True)
    out = (p.stdout + p.stderr).strip()
    if p.returncode != 0:
        raise Stop(f"{Path(str(cmd[0])).name} failed:\n{out}")
    return out


def strip_fence(text: str) -> str:
    """Drop one wrapping ```/```markdown fence if the model added it (a
    ```verse fence opening the chunk is content, and is kept)."""
    t = text.strip()
    m = re.match(r"^```(?:markdown|md)?\s*\n(.*)\n```$", t, re.S)
    return m.group(1).strip() if m else t


def parse_review(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise Stop(f"the reviewer did not answer in JSON:\n{text[:400]}")
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise Stop(f"the reviewer's JSON is malformed ({e}):\n{text[:400]}")
    if data.get("verdict") not in ("ok", "issues"):
        raise Stop(f"the reviewer gave no verdict:\n{text[:400]}")
    data.setdefault("issues", [])
    return data


# --------------------------------------------------------------------------- #
# Messages
# --------------------------------------------------------------------------- #
def previous_tail(workdir: Path, job: dict, index: int) -> str:
    if index == 0:
        return "This is the first chunk of the book."
    prev = chunk_dir(workdir, job["chunks"][index - 1]["id"]) / "out.md"
    if not prev.exists():
        return "The previous chunk is not written yet."
    lines = prev.read_text(encoding="utf-8").strip().splitlines()
    cont = bool(lines) and lines[-1].strip() == CONTINUES
    body = "\n".join(lines[:-1] if cont else lines).strip()
    tail = body[-600:]
    notes = re.findall(r"\[\^(\d+)\]:", body)
    s = f"The previous chunk ends:\n«…{tail}»\n"
    s += ("It ends with [[continues]]: your first paragraph begins mid-sentence, "
          "exactly where your first page does.\n" if cont else
          "It ends at a paragraph break.\n")
    if notes:
        s += f"Its last footnote number is {notes[-1]}.\n"
    return s


def writer_messages(workdir: Path, job: dict, index: int, cfg: dict) -> list:
    chunk = job["chunks"][index]
    inp = load_json(chunk_dir(workdir, chunk["id"]) / "input.json")
    cands = load_candidates(workdir)
    dim = cfg["max_image_dim"]
    parts = [llm_mod.text_part(
        f"Book: {job['title']} — language {job['language']}.\n"
        f"Chunk {chunk['id']}: PDF page(s) {', '.join(map(str, chunk['pages']))}, "
        f"{index + 1} of {len(job['chunks'])}.\n\n" + previous_tail(workdir, job, index))]
    for p in chunk["pages"]:
        parts.append(llm_mod.text_part(f"Page {p}:"))
        parts.append(llm_mod.image_part(workdir / f"pages/p{p:03d}.png", dim))
    if inp["text_layer"]:
        layer = "\n\n".join(f"=== text layer, page {p} ===\n"
                            + (workdir / rel).read_text(encoding="utf-8")
                            for p, rel in zip(chunk["pages"], inp["text"]))
        parts.append(llm_mod.text_part(layer))
    else:
        parts.append(llm_mod.text_part("These pages have no trustworthy text layer: "
                                       "read them from the images."))
    if inp["figure_candidates"]:
        parts.append(llm_mod.text_part("Figure candidates on these pages:"))
        for fid in inp["figure_candidates"]:
            c = cands[fid]
            deco = ", small: likely decorative" if c["likely_decorative"] else ""
            parts.append(llm_mod.text_part(
                f"{fid}: {c['kind']} on page {c['page']}, {c['page_fraction']:.0%} of the page{deco}"))
            parts.append(llm_mod.image_part(workdir / "figures" / "previews" / f"{fid}.png", dim))
    else:
        parts.append(llm_mod.text_part("No figure candidates on these pages."))
    parts.append(llm_mod.text_part("Write the chunk."))
    return [{"role": "system", "content": (PROMPTS / "writer.md").read_text(encoding="utf-8")},
            {"role": "user", "content": parts}]


def reviewer_messages(workdir: Path, chunk: dict, md: str, cfg: dict) -> list:
    parts = []
    for p in chunk["pages"]:
        parts.append(llm_mod.text_part(f"Page {p}:"))
        parts.append(llm_mod.image_part(workdir / f"pages/p{p:03d}.png", cfg["max_image_dim"]))
    parts.append(llm_mod.text_part(f"The chunk:\n\n{md}"))
    return [{"role": "system", "content": (PROMPTS / "reviewer.md").read_text(encoding="utf-8")},
            {"role": "user", "content": parts}]


# --------------------------------------------------------------------------- #
# One chunk
# --------------------------------------------------------------------------- #
def write_until_gated(workdir, job, index, cfg, messages) -> dict:
    chunk = job["chunks"][index]
    out = chunk_dir(workdir, chunk["id"]) / "out.md"
    cands = load_candidates(workdir)
    for attempt in range(1, cfg["max_attempts"] + 1):
        reply = strip_fence(llm_mod.chat(messages, cfg))
        out.write_text(reply + "\n", encoding="utf-8")
        result = check_chunk(workdir, chunk, cands)
        print(f"  attempt {attempt}: {'PASS' if result['pass'] else 'FAIL'}")
        if result["pass"]:
            return result
        messages = messages + [
            {"role": "assistant", "content": reply},
            {"role": "user", "content": "The gate rejected that chunk:\n\n" + describe(result)
             + "\n\nFix exactly these problems against the page images and return the "
               "whole corrected chunk."}]
    raise Stop(f"{chunk['id']} still fails its gate after {cfg['max_attempts']} attempts:\n"
               + describe(result))


def convert_chunk(workdir: Path, job: dict, index: int, cfg: dict) -> None:
    chunk = job["chunks"][index]
    d = chunk_dir(workdir, chunk["id"])
    state = chunk_state(workdir, chunk)["state"]
    review_needed = (not chunk["text_layer"]) or cfg.get("review_text_chunks")
    review = load_json(d / "review.json") or {}
    reviewed = review.get("verdict") == "ok" and review.get("sha256") == sha256(d / "out.md")
    if state == "done" and (reviewed or not review_needed):
        print(f"{chunk['id']}: done")
        return
    print(f"{chunk['id']} (pages {chunk['pages'][0]}-{chunk['pages'][-1]}): {state}")

    messages = writer_messages(workdir, job, index, cfg)
    if state not in ("done", "needs-review"):
        write_until_gated(workdir, job, index, cfg, messages)
    if not review_needed:
        return

    for round_ in (1, 2):
        md = (d / "out.md").read_text(encoding="utf-8")
        review = parse_review(llm_mod.chat(reviewer_messages(workdir, chunk, md, cfg), cfg))
        save_json(d / "review.json", {"sha256": sha256(d / "out.md"), "reviewer": cfg["model"],
                                      **review})
        print(f"  review {round_}: {review['verdict']}"
              + (f" ({len(review['issues'])} issue(s))" if review["issues"] else ""))
        if review["verdict"] == "ok":
            return
        if round_ == 2:
            break
        messages = messages + [
            {"role": "assistant", "content": md},
            {"role": "user", "content": "A reviewer compared your chunk with the pages and "
             "found:\n- " + "\n- ".join(review["issues"]) + "\n\nCheck each against the "
             "page images, fix the real ones, and return the whole corrected chunk."}]
        write_until_gated(workdir, job, index, cfg, messages)
    raise Stop(f"{chunk['id']}: the reviewer still reports issues after one rework:\n- "
               + "\n- ".join(review["issues"]))


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
def ensure_job(workdir: Path, cfg: dict) -> dict:
    if (workdir / "job.json").exists():
        return load_job(workdir)
    for key, allowed in (("device", ("x3", "x4pro")), ("figures", ("single", "double"))):
        if cfg.get(key) not in allowed:
            raise Stop(f"config has no valid {key!r} ({' / '.join(allowed)}): the runner "
                       f"cannot ask, so it must be set before a new job is planned")
    print(run([SCRIPTS / "plan.py", workdir, "--device", cfg["device"],
               "--figures", cfg["figures"],
               "--pages-per-chunk", cfg.get("pages_per_chunk", 2)]))
    return load_job(workdir)


def convert(workdir: Path, cfg: dict, plan_only: bool, epub: bool) -> Path | None:
    job = ensure_job(workdir, cfg)
    if job["route"] == "TRANSCRIPT":
        raise Stop("a source-transcript sidecar is present: that route is for an "
                   "interactive driver (SKILL.md § Bring your own transcript)")
    if plan_only:
        return None
    for i in range(len(job["chunks"])):
        convert_chunk(workdir, job, i, cfg)
    print(run([SCRIPTS / "figures.py", "prepare", workdir]))
    print(run([SCRIPTS / "assemble.py", workdir]))
    if not epub:
        return None
    out = workdir / "build" / f"{job['slug']}.epub"
    print(run([REPO_ROOT / "epub-builder" / "scripts" / "build_epub.py", workdir, "--out", out]))
    print(run([SCRIPTS / "verify.py", workdir, "--epub", out]))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workdir", type=Path)
    ap.add_argument("--config", type=Path)
    ap.add_argument("--plan-only", action="store_true")
    ap.add_argument("--no-epub", action="store_true")
    args = ap.parse_args()
    try:
        cfg = llm_mod.load_config(args.config)
        cfg.setdefault("max_attempts", 3)
        if not args.plan_only:
            llm_mod.resolve_key(cfg)
    except llm_mod.LLMError as e:
        print(f"run_conversion: not configured — {e}", file=sys.stderr)
        return 2
    try:
        out = convert(args.workdir.resolve(), cfg, args.plan_only, not args.no_epub)
    except (Stop, llm_mod.LLMError) as e:
        print(f"run_conversion: stopped — {e}\nProgress is saved; re-run to resume.",
              file=sys.stderr)
        return 1
    if out:
        print(f"done: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
