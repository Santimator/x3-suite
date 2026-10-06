#!/usr/bin/env python3
"""pdf2epub: chunks -> the common book format (chapters/*.md + book.json).

Refuses, listing every reason, unless:
  - every chunk is done: check.json passed on the current out.md, and, for a
    chunk with no text layer, review.json says "ok" on that same out.md;
  - figures/prepared.json covers every placeholder, for the job's current
    device and figure mode (re-run figures.py prepare after changing either).

Then, mechanically:
  - joins the chunks in order; a chunk ending in [[continues]] has its last
    paragraph joined to the next chunk's first (a trailing hyphen before a
    lowercase letter is a split word: joined without it);
  - replaces each [[fig:ID | caption]] with its prepared markdown;
  - cuts chapters at every `# ` heading (text before the first heading opens
    the first chapter; a book with no heading gets one, the job's title);
  - writes chapters/chNN.md and book.json (tight line spacing, the job's
    metadata; a title plan.py only had the folder name for is replaced by the
    book's first `# ` heading) and, unless images/cover.png exists, a cover for the job's
    device: a source-cover.* sidecar if present, else the default template
    with the title drawn in (remade when the device or title changes; a cover
    you placed at images/cover.png yourself is never touched).

Next: epub-builder/scripts/build_epub.py WORKDIR --out WORKDIR/build/<slug>.epub
and verify.py.

Usage:
  assemble.py WORKDIR [--no-cover]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from harness import (BUILDER_SCRIPTS, CONTINUES, PLACEHOLDER_RE, REPO_ROOT,
                     HarnessError, chunk_dir, chunk_state, load_job,
                     load_json, save_json)

sys.path.insert(0, str(BUILDER_SCRIPTS))
import prepare_cover  # noqa: E402

COVER_EXTS = ("png", "jpg", "jpeg", "webp", "tiff", "tif", "bmp", "gif")
DEFAULT_COVER = REPO_ROOT / "extras" / "default-covers" / "default.png"
DEFAULT_COVER_CFG = REPO_ROOT / "extras" / "default-covers" / "default.json"


def readiness(workdir: Path, job: dict) -> list:
    problems = []
    for chunk in job["chunks"]:
        st = chunk_state(workdir, chunk)["state"]
        if st != "done":
            hint = {
                "todo": "not written",
                "unchecked": "out.md changed since its check — run check_chunk.py",
                "failed": "failed its check",
                "needs-review": "no text layer: needs review.json (verdict ok) on this out.md",
            }[st]
            problems.append(f"{chunk['id']} (pages {chunk['pages'][0]}-{chunk['pages'][-1]}): {hint}")
    return problems


def join_continued(prev: str, nxt: str) -> str:
    """prev's last paragraph runs on into nxt's first."""
    prev = prev.rstrip()
    nxt = nxt.lstrip()
    if re.search(r"[^\W\d_]-$", prev) and re.match(r"[a-záéíóúñüàèìòùçâêîôûäëïöü]", nxt):
        return prev[:-1] + nxt
    return prev + " " + nxt


def gather(workdir: Path, job: dict) -> str:
    text = ""
    continues = False
    for chunk in job["chunks"]:
        md = (chunk_dir(workdir, chunk["id"]) / "out.md").read_text(encoding="utf-8").strip()
        lines = md.splitlines()
        this_continues = bool(lines) and lines[-1].strip() == CONTINUES
        if this_continues:
            md = "\n".join(lines[:-1]).rstrip()
        if continues:
            text = join_continued(text, md)
        else:
            text = (text + "\n\n" + md) if text else md
        continues = this_continues
    if continues:
        raise HarnessError(f"the last chunk ends in {CONTINUES}, but nothing follows it")
    return text


def expand_figures(md: str, prepared: dict) -> str:
    out = []
    for block in re.split(r"\n\s*\n", md):
        m = PLACEHOLDER_RE.match(block.strip())
        out.append(prepared[m.group(1)]["markdown"] if m else block)
    return "\n\n".join(out)


def cut_chapters(md: str, title: str) -> list:
    chapters, cur, in_verse = [], [], False
    for line in md.splitlines():
        if line.strip().startswith("```"):
            in_verse = not in_verse
        heading = not in_verse and line.startswith("# ")
        if heading and any(l.startswith("# ") for l in cur):
            chapters.append(cur)
            cur = []
        cur.append(line)
    chapters.append(cur)
    if not any(l.startswith("# ") for l in chapters[0]):
        chapters[0][:0] = [f"# {title}", ""]
    return ["\n".join(c).strip() + "\n" for c in chapters]


def make_cover(workdir: Path, job: dict) -> str:
    """A cover this script made is remade when the device changes; one the
    user put at images/cover.png themselves (no marker) is left alone."""
    out = workdir / "images" / "cover.png"
    marker = workdir / "build" / "cover.json"
    made = load_json(marker)
    if out.exists() and not made:
        return "kept images/cover.png (yours)"
    if out.exists() and made.get("device") == job["device"] and made.get("title") == job["title"]:
        return "kept images/cover.png"
    sidecar = next((workdir / f"source-cover.{e}" for e in COVER_EXTS
                    if (workdir / f"source-cover.{e}").exists()), None)
    from PIL import Image
    if sidecar:
        img = Image.open(sidecar)
        img.load()
        note = f"from {sidecar.name}"
    else:
        img = Image.open(DEFAULT_COVER)
        img.load()
        cfg = prepare_cover.load_title_cfg(str(DEFAULT_COVER_CFG))
        img = prepare_cover.draw_title(img, job["title"], cfg,
                                       prepare_cover.resolve_font(cfg.get("font")))
        note = "default template, titled"
    out.parent.mkdir(parents=True, exist_ok=True)
    prepare_cover.to_valid(img, job["device"]).save(out, "PNG", optimize=True)
    save_json(marker, {"device": job["device"], "title": job["title"], "source": note})
    return f"wrote images/cover.png ({note}, sized for {job['device']})"


def assemble(workdir: Path, cover: bool = True) -> dict:
    job = load_job(workdir)
    problems = readiness(workdir, job)

    md_by_chunk = {c["id"]: (chunk_dir(workdir, c["id"]) / "out.md") for c in job["chunks"]}
    used = []
    for path in md_by_chunk.values():
        if path.exists():
            used += [m.group(1) for b in re.split(r"\n\s*\n", path.read_text(encoding="utf-8"))
                     if (m := PLACEHOLDER_RE.match(b.strip()))]
    prepared_doc = load_json(workdir / "figures" / "prepared.json")
    prepared = {}
    if used:
        if not prepared_doc:
            problems.append("figures are placed but not prepared — run figures.py prepare")
        elif (prepared_doc["device"], prepared_doc["figures"]) != (job["device"], job["figures"]):
            problems.append(f"figures were prepared for {prepared_doc['device']}/"
                            f"{prepared_doc['figures']}, the job is {job['device']}/"
                            f"{job['figures']} — re-run figures.py prepare")
        else:
            prepared = prepared_doc["prepared"]
            missing = [f for f in used if f not in prepared]
            if missing:
                problems.append(f"figures not prepared: {', '.join(missing)} — "
                                f"re-run figures.py prepare")
            for p in prepared.values():
                for f in p["files"]:
                    if not (workdir / "images" / f["file"]).exists():
                        problems.append(f"images/{f['file']} is missing — re-run figures.py prepare")
    if problems:
        raise HarnessError("not ready to assemble:\n  " + "\n  ".join(problems))

    md = expand_figures(gather(workdir, job), prepared)
    if job.get("title_from") == "folder":
        # Only a folder name to go on: the book's own first title beats it.
        m = re.search(r"(?m)^# (.+)$", md)
        if m:
            job["title"] = m.group(1).strip()
    chapters = cut_chapters(md, job["title"])

    ch_dir = workdir / "chapters"
    ch_dir.mkdir(exist_ok=True)
    for old in ch_dir.glob("ch*.md"):
        old.unlink()
    entries = []
    for i, text in enumerate(chapters, 1):
        defs = re.findall(r"(?m)^\[\^([^\]]+)\]:", text)
        dup = sorted({d for d in defs if defs.count(d) > 1})
        if dup:
            raise HarnessError(f"chapter {i}: footnote id(s) {', '.join(dup)} used twice — "
                               f"number footnotes uniquely within a chapter")
        name = f"ch{i:02d}.md"
        (ch_dir / name).write_text(text, encoding="utf-8")
        entries.append({"source": f"chapters/{name}"})

    cover_note = make_cover(workdir, job) if cover else "no cover (--no-cover)"
    book = {
        "title": job["title"],
        "author": job["author"],
        "language": job["language"],
        "line_spacing": "tight",
        "chapters": entries,
    }
    if (workdir / "images" / "cover.png").exists():
        book["cover"] = "images/cover.png"
    save_json(workdir / "book.json", book)
    return {"chapters": len(entries), "figures": len(used), "cover": cover_note}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workdir", type=Path)
    ap.add_argument("--no-cover", action="store_true")
    args = ap.parse_args()
    try:
        r = assemble(args.workdir, cover=not args.no_cover)
    except HarnessError as e:
        print(f"assemble: {e}", file=sys.stderr)
        return 1
    slug = args.workdir.name
    print(f"{r['chapters']} chapter(s), {r['figures']} figure(s); {r['cover']}")
    print(f"next: .venv/bin/python epub-builder/scripts/build_epub.py {args.workdir} "
          f"--out {args.workdir}/build/{slug}.epub")
    return 0


if __name__ == "__main__":
    sys.exit(main())
