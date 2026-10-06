#!/usr/bin/env python3
"""pdf2epub: set a conversion up — one command from source.pdf to chunk inputs.

Runs, into WORKDIR (workspace/<slug>/, holding source.pdf):
  triage.py       -> build/triage.json
  extract_text.py -> extract/ (column-aware; space recovery if triage flags it)
  render_pages.py -> pages/pNNN.png (what the model reads)
  figures.py      -> figures/candidates.json + previews
and writes job.json plus chunks/cNN/input.json: every PAGES_PER_CHUNK pages
become one chunk, the unit a model transcribes and the gate checks.

--device and --figures have no default on purpose: they are the reader's
choice (X3 or X4 Pro screen; tables and figures on one page or split across
two), so the driver must ask before planning. Title/author/language default to
the PDF's metadata and triage's language guess; pass them when you know better.

A page counts as having a trustworthy text layer when triage found text on it.
`--untrusted-text` overrides that for the whole book (a scan whose embedded
OCR is garbage): those chunks get no fidelity baseline and need a review.

Re-running is refused when job.json exists; --force re-plans, keeping each
chunk's out.md whose pages did not change.

Usage:
  plan.py WORKDIR --device x3|x4pro --figures single|double
          [--pages-per-chunk 2] [--pages A-B] [--title T] [--author A]
          [--language es] [--untrusted-text] [--dpi 150] [--force]
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import extract_text
import figures
import render_pages
import triage
from harness import (BUILDER_SCRIPTS, FIGURE_MODES, chunk_dir, load_json,
                     save_json)

sys.path.insert(0, str(BUILDER_SCRIPTS))
from devices import DEVICES  # noqa: E402


def pretty_slug(slug: str) -> str:
    return slug.replace("-", " ").replace("_", " ").strip().capitalize()


def plan(workdir: Path, args) -> dict:
    pdf = workdir / "source.pdf"
    if not pdf.exists():
        raise SystemExit(f"plan: no {pdf}")
    old_job = load_json(workdir / "job.json")
    if old_job and not args.force:
        raise SystemExit(f"plan: {workdir / 'job.json'} exists — pass --force to re-plan "
                         f"(written chunks whose pages are unchanged are kept)")

    report = triage.triage(pdf, samples=3)
    save_json(workdir / "build" / "triage.json", report)
    if report["route"] == "TRANSCRIPT":
        print("note: a source-transcript sidecar exists — read it alongside the pages "
              "(SKILL.md § Bring your own transcript)")

    n_pages = report["pages"]
    pages = render_pages.parse_pages(args.pages, n_pages)
    if not pages or pages[0] < 1 or pages[-1] > n_pages:
        raise SystemExit(f"plan: --pages {args.pages} is outside 1..{n_pages}")

    space_recover = "broken_spacing" in report["flags"]
    extract_text.run(pdf, workdir / "extract", args.pages, "auto",
                     space_recover=space_recover)
    render_pages.run(pdf, workdir / "pages", args.pages, args.dpi)
    cands = figures.detect(workdir)["candidates"]

    per_page = report["per_page"]
    has_text = {p: (per_page[p - 1]["chars"] >= triage.MIN_TEXT_CHARS
                    and not args.untrusted_text) for p in pages}

    meta = report["metadata"]
    job = {
        "slug": workdir.name,
        "source": "source.pdf",
        "device": args.device,
        "figures": args.figures,
        "title": args.title or meta.get("Title", "").strip() or pretty_slug(workdir.name),
        "title_from": ("given" if args.title else
                       "metadata" if meta.get("Title", "").strip() else "folder"),
        "author": args.author if args.author is not None else meta.get("Author", "").strip(),
        "language": args.language or (report["language_guess"]
                                      if report["language_guess"] != "unknown" else "en"),
        "route": report["route"],
        "pages_per_chunk": args.pages_per_chunk,
        "chunks": [],
    }

    old_pages = {c["id"]: c["pages"] for c in (old_job or {}).get("chunks", [])}
    for i in range(0, len(pages), args.pages_per_chunk):
        group = pages[i:i + args.pages_per_chunk]
        cid = f"c{len(job['chunks']) + 1:02d}"
        chunk = {"id": cid, "pages": group, "text_layer": all(has_text[p] for p in group)}
        job["chunks"].append(chunk)
        d = chunk_dir(workdir, cid)
        if old_pages.get(cid) != group and d.exists():
            shutil.rmtree(d)          # its pages changed: nothing in it applies
        save_json(d / "input.json", {
            "id": cid,
            "pages": group,
            "page_images": [f"pages/p{p:03d}.png" for p in group],
            "text_layer": chunk["text_layer"],
            "text": [f"extract/text/p{p:03d}.txt" for p in group] if chunk["text_layer"] else [],
            "figure_candidates": [c["id"] for c in cands if c["page"] in group],
        })
    for stale in sorted((workdir / "chunks").glob("c*")):
        if stale.name not in {c["id"] for c in job["chunks"]}:
            shutil.rmtree(stale)

    save_json(workdir / "job.json", job)
    return job


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workdir", type=Path)
    ap.add_argument("--device", choices=sorted(DEVICES), required=True,
                    help="target reader — ask the user")
    ap.add_argument("--figures", choices=FIGURE_MODES, required=True,
                    help="figures/tables on one page, or split over two — ask the user")
    ap.add_argument("--pages-per-chunk", type=int, default=2)
    ap.add_argument("--pages", help="only these pages, A-B (default all)")
    ap.add_argument("--title")
    ap.add_argument("--author")
    ap.add_argument("--language")
    ap.add_argument("--untrusted-text", action="store_true",
                    help="ignore the text layer (garbage OCR): every chunk needs a review")
    ap.add_argument("--dpi", type=int, default=150, help="page images for the model")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    if args.pages_per_chunk < 1:
        ap.error("--pages-per-chunk must be at least 1")

    job = plan(args.workdir, args)
    n_review = sum(not c["text_layer"] for c in job["chunks"])
    print(f"{job['slug']}: {len(job['chunks'])} chunk(s) of {job['pages_per_chunk']} page(s), "
          f"device {job['device']} ({DEVICES[job['device']]['name']}), figures {job['figures']}")
    print(f"title: {job['title']!r}  author: {job['author']!r}  language: {job['language']}")
    print(f"route {job['route']}; chunks without a text layer (review required): {n_review}")
    print("next: write chunks/cNN/out.md, gate each with check_chunk.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
