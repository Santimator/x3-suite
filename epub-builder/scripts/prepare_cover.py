#!/usr/bin/env python3
"""Prepare a book cover for the Xteink X3 / CrossPoint EPUB path.

Two jobs, in the suite's "bring your own, we make it work" spirit:

  1. **Validate, and fix if needed.** A cover the user (or the agent) supplies
     is used *as-is* when it already meets CrossPoint's EPUB-cover constraints;
     otherwise it is transformed into something that does. The constraints are
     the ones the firmware actually cares about for an *embedded EPUB cover*
     (NOT the .pxc/.bmp sleep-screen wallpaper format — that is a different
     feature):
       - format PNG or baseline JPEG  (progressive JPEG and GIF fall back to an
         [Image] placeholder on-device — confirmed in the CrossPoint guide);
       - grayscale  (the panel is e-ink; colour is wasted bytes);
       - fits within the 528x792 panel  (a ~2000px-tall cover costs ~10s of
         on-device conversion for the sleep-screen/thumbnail — keep it small).
     We emit grayscale PNG <= 528x792, which satisfies all of them by design.

  2. **Optionally draw the title onto it.** For a template cover that leaves a
     blank area (e.g. extras/default-covers/default.png's parchment panel), the book
     title is rendered into a configured box, auto-sized to fit. Rendered at the
     source resolution, then downscaled, so the text stays crisp.

Usage:
  prepare_cover.py INPUT --out images/cover.png
  prepare_cover.py INPUT --out images/cover.png --title "Los alcaldes encontrados"
  prepare_cover.py INPUT --title-config extras/default-covers/default.json --title "..." --out ...
  prepare_cover.py INPUT --check          # report validity only, write nothing
"""

import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# CrossPoint e-ink panel; a cover need never exceed it.
PANEL_W, PANEL_H = 528, 792

# Repo root, so bundled fonts/assets resolve regardless of the caller's cwd.
# This file lives at epub-builder/scripts/prepare_cover.py.
REPO_ROOT = Path(__file__).resolve().parents[2]

# Title-font candidates, best first; `--font` or a config `font` overrides.
# Both bundled faces are OFL (licenses beside them in extras/default-covers/):
# IM Fell English for Latin, LXGW WenKai (WenZilla's kaiti) for CJK. System
# serifs are a last resort so the feature never hard-fails.
FONT_CANDIDATES = [
    "extras/default-covers/IMFellEnglish-Regular.ttf",
    "extras/default-covers/LXGWWenKai-Regular.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
]

# Default title box (fractions of image W/H) and ink, if no --title-config is
# given. Tuned to extras/default-covers/default.png's parchment panel.
DEFAULT_TITLE = {
    "title_box": [0.185, 0.072, 0.815, 0.360],
    "color": [43, 33, 25],
    "uppercase": False,
}


def _resolve_path(p: str) -> Path | None:
    """A repo-relative or absolute path → an existing Path, or None. Relative
    paths are tried against cwd first, then the repo root."""
    cand = Path(p)
    if cand.is_absolute():
        return cand if cand.is_file() else None
    for base in (Path.cwd(), REPO_ROOT):
        if (base / cand).is_file():
            return base / cand
    return None


def is_grayscale(img: Image.Image) -> bool:
    return img.mode in ("L", "1")


def validity(path: Path):
    """Return (ok, reasons). ok=True means usable as an EPUB cover as-is."""
    reasons = []
    try:
        with Image.open(path) as img:
            img.load()
            fmt = (img.format or "").upper()
            if fmt not in ("PNG", "JPEG"):
                reasons.append(f"format {fmt or '?'} (need PNG or baseline JPEG)")
            if fmt == "JPEG" and img.info.get("progression"):
                reasons.append("progressive JPEG (renders as [Image] placeholder)")
            if not is_grayscale(img):
                reasons.append(f"mode {img.mode} (need grayscale)")
            w, h = img.size
            if w > PANEL_W or h > PANEL_H:
                reasons.append(f"size {w}x{h} (exceeds {PANEL_W}x{PANEL_H} panel)")
    except Exception as e:  # unreadable / truncated / unknown format
        return False, [f"cannot open: {e}"]
    return (not reasons), reasons


def to_valid(img: Image.Image) -> Image.Image:
    """Grayscale, fit within the panel (downscale only — never upscale a raster
    cover), flattening any alpha onto white first."""
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, img)
    img = img.convert("L")
    w, h = img.size
    scale = min(PANEL_W / w, PANEL_H / h, 1.0)
    if scale < 1.0:
        img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))),
                         Image.LANCZOS)
    return img


def resolve_font(explicit: str | None) -> str:
    for cand in ([explicit] if explicit else []) + FONT_CANDIDATES:
        if cand and (found := _resolve_path(cand)):
            return str(found)
    raise SystemExit(
        "prepare_cover: no title font found. Pass --font PATH to a .ttf/.otf "
        f"(looked for: {', '.join(FONT_CANDIDATES)})."
    )


def _is_cjk(ch: str) -> bool:
    """CJK ideographs and CJK punctuation — characters we may break between."""
    o = ord(ch)
    return (0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF     # unified + ext-A
            or 0xF900 <= o <= 0xFAFF                            # compat ideographs
            or 0x3000 <= o <= 0x303F or 0xFF00 <= o <= 0xFFEF)  # CJK/full-width punct


def _atoms(text):
    """Break `text` into wrap units, each (chunk, space_before). A CJK char is
    its own unit (Chinese has no spaces, so any han boundary is a legal break);
    a run of non-space, non-CJK characters (a Latin word) stays whole."""
    out, buf, buf_space, pending_space = [], "", False, False
    for ch in text:
        if ch.isspace():
            if buf:
                out.append([buf, buf_space]); buf = ""
            pending_space = True
        elif _is_cjk(ch):
            if buf:
                out.append([buf, buf_space]); buf = ""
            out.append([ch, pending_space]); pending_space = False
        else:                       # Latin / punctuation / digits
            if not buf:
                buf_space = pending_space
            buf += ch
            pending_space = False
    if buf:
        out.append([buf, buf_space])
    return out


def wrap_to_width(text, font, max_w, draw):
    """Greedy wrap so each line's rendered width <= max_w. Breaks between words
    (space-separated) and between CJK characters."""
    lines, cur = [], ""
    for chunk, space_before in _atoms(text):
        sep = " " if (space_before and cur) else ""
        trial = cur + sep + chunk
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = chunk
    if cur:
        lines.append(cur)
    return lines


def fit_title(draw, text, box_px, font_path, line_spacing=1.15):
    """Largest font size at which `text` wraps inside box_px=(w,h). Returns
    (font, lines, line_height)."""
    box_w, box_h = box_px
    for size in range(160, 11, -2):
        font = ImageFont.truetype(font_path, size)
        lines = wrap_to_width(text, font, box_w, draw)
        asc, desc = font.getmetrics()
        line_h = int((asc + desc) * line_spacing)
        if all(draw.textlength(ln, font=font) <= box_w for ln in lines) \
                and line_h * len(lines) <= box_h:
            return font, lines, line_h
    font = ImageFont.truetype(font_path, 12)
    return font, wrap_to_width(text, font, box_w, draw), int(sum(font.getmetrics()) * line_spacing)


def draw_title(img: Image.Image, title: str, cfg: dict, font_path: str) -> Image.Image:
    """Render `title` centered in cfg['title_box'] (fractions of image size)."""
    img = img.convert("RGB")
    draw = ImageDraw.Draw(img)
    W, H = img.size
    fx0, fy0, fx1, fy1 = cfg["title_box"]
    box = (int(fx0 * W), int(fy0 * H), int(fx1 * W), int(fy1 * H))
    box_w, box_h = box[2] - box[0], box[3] - box[1]
    text = title.upper() if cfg.get("uppercase") else title
    font, lines, line_h = fit_title(draw, text, (box_w, box_h), font_path)
    color = tuple(cfg.get("color", DEFAULT_TITLE["color"]))

    total_h = line_h * len(lines)
    y = box[1] + (box_h - total_h) // 2      # vertically centered in the box
    for ln in lines:
        lw = draw.textlength(ln, font=font)
        x = box[0] + (box_w - lw) / 2         # horizontally centered
        draw.text((x, y), ln, font=font, fill=color)
        y += line_h
    return img


def load_title_cfg(path: str | None) -> dict:
    if not path:
        return dict(DEFAULT_TITLE)
    cfg = json.loads(Path(path).read_text())
    return {**DEFAULT_TITLE, **cfg}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", type=Path)
    ap.add_argument("--out", type=Path, help="output PNG (grayscale, <=528x792)")
    ap.add_argument("--check", action="store_true",
                    help="report validity only; write nothing (exit 0 valid / 1 not)")
    ap.add_argument("--title", help="draw this title onto the cover")
    ap.add_argument("--title-config", help="JSON with title_box/color/uppercase")
    ap.add_argument("--font", help="path to a .ttf/.otf for the title")
    args = ap.parse_args()

    ok, reasons = validity(args.input)

    if args.check:
        print(f"{args.input}: {'VALID' if ok else 'INVALID'}"
              + ("" if ok else "  — " + "; ".join(reasons)))
        return 0 if ok else 1

    if not args.out:
        ap.error("--out is required unless --check")

    img = Image.open(args.input)
    img.load()

    if args.title:
        cfg = load_title_cfg(args.title_config)
        img = draw_title(img, args.title, cfg, resolve_font(args.font or cfg.get("font")))
        note = "titled"
    elif ok:
        note = "already valid, passed through"
    else:
        note = "transformed: " + "; ".join(reasons)

    out_img = to_valid(img)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    out_img.save(args.out, "PNG", optimize=True)
    print(f"wrote {args.out}  ({out_img.size[0]}x{out_img.size[1]} grayscale PNG; {note})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
