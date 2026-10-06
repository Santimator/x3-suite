#!/usr/bin/env python3
"""pdf2epub: figures and tables as device-sized images.

A small e-ink screen cannot show a printed figure or table at print size, and
the builder has no tables at all, so both travel as images, made as large as
the panel allows. Three commands:

  detect   (plan.py runs it) list candidate regions per page — embedded
           images and ruled tables pdfplumber can find — into
           figures/candidates.json, with a preview of each in figures/previews/.
  add      a region the detector missed (a vector diagram, an unruled table):
           --page N --bbox x0,top,x1,bottom in PDF points (pages.jsonl's units;
           measure on the page PNG: points = pixels * 72 / dpi).
  prepare  render every candidate a chunk placed with [[fig:ID | caption]] for
           the job's device and figure mode (or the placeholder's own
           `| single` / `| double`), into images/, and record the markdown
           each placeholder becomes in figures/prepared.json.

Fitting (prepare). The firmware draws an image at its own pixel size, only
ever shrinking it to the reader's viewport (no upscaling) and starting a new
page when it does not fit what is left of the current one — read from
CrossPoint's ChapterHtmlSlimParser, see extras/readers.md. So a figure is
rendered from the PDF at the scale that fills the panel, and:

  single  one image. Turned 90° (top to the left: the reader turns the device
          clockwise) when that makes it at least ROTATE_GAIN larger — wide
          tables, mostly.
  double  as single, unless cutting it in two halves (with OVERLAP shared
          between them, so nothing is lost on the cut) and giving each half a
          page makes it at least SPLIT_GAIN larger than single would. The two
          halves sit on consecutive pages: page back and forth to read it.

  inline  (per figure only, `| inline`) the panel's width at most, never
          turned or split: an illustration, not something to study.

Page-sized images take no <figcaption> (it would land alone on the next page):
the caption becomes an italic paragraph right before the image. An inline one
keeps its caption underneath.

Usage:
  figures.py detect  WORKDIR
  figures.py add     WORKDIR --page 12 --bbox 40,120,440,380 [--kind table]
  figures.py prepare WORKDIR
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pdfplumber
import pypdfium2 as pdfium
from PIL import Image

from harness import (BUILDER_SCRIPTS, HarnessError, chunk_dir, load_candidates,
                     load_job, load_json, placeholders, save_json)

sys.path.insert(0, str(BUILDER_SCRIPTS))
from devices import panel  # noqa: E402

ROTATE_GAIN = 1.25
SPLIT_GAIN = 1.25
OVERLAP = 0.04          # of the cut dimension, shared by both halves
MIN_AREA = 0.002        # of the page: smaller embedded images are not listed
SMALL_AREA = 0.02       # of the page: listed, but flagged likely decorative
PREVIEW_DPI = 100


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def render_region(doc: pdfium.PdfDocument, page_num: int, bbox, scale: float) -> Image.Image:
    """Render bbox (x0, top, x1, bottom in points, top-left origin) of a
    1-indexed page at `scale` pixels per point, grayscale."""
    page = doc[page_num - 1]
    w, h = page.get_size()
    x0, top, x1, bottom = bbox
    crop = (max(0.0, x0), max(0.0, h - bottom), max(0.0, w - x1), max(0.0, top))
    return page.render(scale=scale, crop=crop, grayscale=True).to_pil().convert("L")


def write_preview(doc, cand, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    render_region(doc, cand["page"], cand["bbox"], PREVIEW_DPI / 72).save(
        out_dir / f"{cand['id']}.png")


# --------------------------------------------------------------------------- #
# detect / add
# --------------------------------------------------------------------------- #
def detect(workdir: Path) -> dict:
    pdf_path = workdir / "source.pdf"
    previous = load_json(workdir / "figures" / "candidates.json", {"candidates": []})
    manual = [c for c in previous["candidates"] if c["kind"] == "manual"]
    cands = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            n = page.page_number
            area = float(page.width * page.height)
            found = []
            for im in page.images:
                bbox = [im["x0"], im["top"], im["x1"], im["bottom"]]
                frac = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) / area
                if frac >= MIN_AREA:
                    found.append(("image", bbox, frac))
            for t in page.find_tables():
                x0, top, x1, bottom = t.bbox
                found.append(("table", [x0, top, x1, bottom],
                              (x1 - x0) * (bottom - top) / area))
            found.sort(key=lambda f: (f[1][1], f[1][0]))
            for i, (kind, bbox, frac) in enumerate(found, 1):
                cands.append({
                    "id": f"p{n:03d}-{i}",
                    "page": n,
                    "kind": kind,
                    "bbox": [round(v, 1) for v in bbox],
                    "page_fraction": round(frac, 3),
                    "likely_decorative": frac < SMALL_AREA,
                })
    cands.extend(manual)
    doc = pdfium.PdfDocument(pdf_path)
    for c in cands:
        write_preview(doc, c, workdir / "figures" / "previews")
    data = {"candidates": cands}
    save_json(workdir / "figures" / "candidates.json", data)
    return data


def add(workdir: Path, page_num: int, bbox, kind: str) -> dict:
    data = load_json(workdir / "figures" / "candidates.json", {"candidates": []})
    taken = {c["id"] for c in data["candidates"]}
    i = 1
    while f"p{page_num:03d}-m{i}" in taken:
        i += 1
    doc = pdfium.PdfDocument(workdir / "source.pdf")
    if not 1 <= page_num <= len(doc):
        raise HarnessError(f"page {page_num} is outside 1..{len(doc)}")
    w, h = doc[page_num - 1].get_size()
    x0, top, x1, bottom = bbox
    if not (0 <= x0 < x1 <= w + 1 and 0 <= top < bottom <= h + 1):
        raise HarnessError(f"bbox {bbox} is not inside the {w:.0f}x{h:.0f}pt page")
    cand = {"id": f"p{page_num:03d}-m{i}", "page": page_num, "kind": "manual",
            "label": kind, "bbox": [round(v, 1) for v in bbox],
            "page_fraction": round((x1 - x0) * (bottom - top) / (w * h), 3),
            "likely_decorative": False}
    data["candidates"].append(cand)
    write_preview(doc, cand, workdir / "figures" / "previews")
    save_json(workdir / "figures" / "candidates.json", data)
    return cand


# --------------------------------------------------------------------------- #
# Fitting
# --------------------------------------------------------------------------- #
def fit_scale(w: float, h: float, box) -> float:
    return min(box[0] / w, box[1] / h)


def best_single(w, h, box):
    """(scale, rotated) for one image of w x h points."""
    straight = fit_scale(w, h, box)
    turned = fit_scale(h, w, box)
    if turned >= ROTATE_GAIN * straight:
        return turned, True
    return straight, False


def best_split(w, h, box):
    """(scale, axis, rotated) for two halves; axis 'x' cuts left|right,
    'y' cuts top/bottom."""
    keep = 0.5 + OVERLAP / 2
    options = []
    for axis, (hw, hh) in (("x", (w * keep, h)), ("y", (w, h * keep))):
        s, rotated = best_single(hw, hh, box)
        options.append((s, axis, rotated))
    return max(options, key=lambda o: o[0])


def plan_figure(bbox, box, mode: str) -> dict:
    x0, top, x1, bottom = bbox
    w, h = x1 - x0, bottom - top
    if mode == "inline":
        return {"layout": "inline", "scale": fit_scale(w, h, box), "rotated": False,
                "parts": [list(bbox)]}
    s1, rot1 = best_single(w, h, box)
    if mode == "double":
        s2, axis, rot2 = best_split(w, h, box)
        if s2 >= SPLIT_GAIN * s1:
            keep = 0.5 + OVERLAP / 2
            if axis == "x":
                parts = [[x0, top, x0 + w * keep, bottom], [x1 - w * keep, top, x1, bottom]]
            else:
                parts = [[x0, top, x1, top + h * keep], [x0, bottom - h * keep, x1, bottom]]
            return {"layout": f"split-{axis}", "scale": s2, "rotated": rot2, "parts": parts}
    return {"layout": "single", "scale": s1, "rotated": rot1, "parts": [list(bbox)]}


def render_part(doc, page_num, bbox, scale, rotated, box) -> Image.Image:
    img = render_region(doc, page_num, bbox, scale)
    if rotated:
        img = img.transpose(Image.Transpose.ROTATE_90)  # top edge -> left edge
    # Rounding can overshoot the panel by a pixel; never hand the device more.
    if img.width > box[0] or img.height > box[1]:
        s = min(box[0] / img.width, box[1] / img.height)
        img = img.resize((max(1, int(img.width * s)), max(1, int(img.height * s))),
                         Image.LANCZOS)
    return img


def used_figures(workdir: Path, job: dict) -> dict:
    """{id: (caption, mode)} for every placeholder in the chunks, in book
    order; mode is the placeholder's override or the job's. A figure placed
    twice is an error (one image file, one place)."""
    used = {}
    for chunk in job["chunks"]:
        out = chunk_dir(workdir, chunk["id"]) / "out.md"
        if not out.exists():
            continue
        for fid, caption, mode in placeholders(out.read_text(encoding="utf-8")):
            if fid in used:
                raise HarnessError(f"figure {fid} is placed twice ({chunk['id']})")
            used[fid] = (caption, mode or job["figures"])
    return used


def snippet(files, caption: str, layout: str) -> str:
    if layout == "inline":       # small enough to share a page with its caption
        return f"![{caption}](../images/{files[0]})"
    lines = [f"*{caption}*"] if caption else []
    lines += [f"![](../images/{f})" for f in files]
    return "\n\n".join(lines)


def prepare(workdir: Path) -> dict:
    job = load_job(workdir)
    box = panel(job["device"])
    cands = load_candidates(workdir)
    used = used_figures(workdir, job)
    unknown = sorted(set(used) - set(cands))
    if unknown:
        raise HarnessError(f"placeholders name no candidate: {', '.join(unknown)} "
                           f"(see figures/candidates.json; figures.py add for a new region)")
    images = workdir / "images"
    images.mkdir(exist_ok=True)
    for old in images.glob("fig-*.png"):
        old.unlink()
    doc = pdfium.PdfDocument(workdir / "source.pdf")
    prepared = {}
    for fid, (caption, mode) in used.items():
        c = cands[fid]
        plan = plan_figure(c["bbox"], box, mode)
        files = []
        for i, part in enumerate(plan["parts"], 1):
            name = f"fig-{fid}.png" if len(plan["parts"]) == 1 else f"fig-{fid}-{i}.png"
            img = render_part(doc, c["page"], part, plan["scale"], plan["rotated"], box)
            img.save(images / name, optimize=True)
            files.append({"file": name, "size": list(img.size)})
        prepared[fid] = {
            "mode": mode,
            "layout": plan["layout"],
            "rotated": plan["rotated"],
            "files": files,
            "caption": caption,
            "markdown": snippet([f["file"] for f in files], caption, plan["layout"]),
        }
    data = {"device": job["device"], "figures": job["figures"], "panel": list(box),
            "prepared": prepared}
    save_json(workdir / "figures" / "prepared.json", data)
    return data


def parse_bbox(text: str):
    vals = [float(v) for v in re.split(r"[,\s]+", text.strip()) if v]
    if len(vals) != 4:
        raise argparse.ArgumentTypeError("bbox is four numbers: x0,top,x1,bottom")
    return vals


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("detect", "add", "prepare"):
        sp = sub.add_parser(name)
        sp.add_argument("workdir", type=Path)
        if name == "add":
            sp.add_argument("--page", type=int, required=True)
            sp.add_argument("--bbox", type=parse_bbox, required=True,
                            help="x0,top,x1,bottom in PDF points")
            sp.add_argument("--kind", default="figure", help="label only: figure|table|…")
    args = ap.parse_args()
    try:
        if args.cmd == "detect":
            data = detect(args.workdir)
            for c in data["candidates"]:
                flag = "  (small: likely decorative)" if c["likely_decorative"] else ""
                print(f"{c['id']}: {c['kind']} on page {c['page']}, "
                      f"{c['page_fraction']:.0%} of the page{flag}")
            print(f"{len(data['candidates'])} candidates; previews in figures/previews/")
        elif args.cmd == "add":
            c = add(args.workdir, args.page, args.bbox, args.kind)
            print(f"added {c['id']}; look at figures/previews/{c['id']}.png")
        else:
            data = prepare(args.workdir)
            for fid, p in data["prepared"].items():
                sizes = ", ".join(f"{f['size'][0]}x{f['size'][1]}" for f in p["files"])
                turn = ", turned 90°" if p["rotated"] else ""
                print(f"{fid}: {p['layout']}{turn} -> {sizes}")
            print(f"{len(data['prepared'])} figure(s) for {data['device']} "
                  f"({data['panel'][0]}x{data['panel'][1]}), mode {data['figures']}")
    except HarnessError as e:
        print(f"figures: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
