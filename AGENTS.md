# AGENTS.md

Rules for **changing** this repo. What it is / how to run it → [`README.md`](README.md).
Per-unit docs → that unit's `SKILL.md`. Read it before touching the unit.
(`.claude/skills/` symlinks every one of them.)

## Layout

Three groups at the root, and which one a thing belongs to is a real question,
not a filing preference:

- **`tools/`** — what *you* operate. Deterministic, no model anywhere near them.
- **`ai-tools/`** — what an *agent* operates: the two pipelines where an LLM
  supplies judgement, each with a deterministic gate after every model step.
- **`extras/`** — not code. Device notes, the `.cpfont` families, cover
  templates.

**`epub-builder/` sits at the root on purpose, and is not a tool.** It owns the
common book format (`FORMAT.md`) and the reference implementation of it: the
AI tools write that format, the builder consumes it, `tools/opds-server/`
serves what it produced, and `tools/tgbot/` borrows its verifier. Something
every other unit feeds or reads is not a peer of the things you run. Leave it
where it is.

## Units

| unit | deps | verify after changing it |
|---|---|---|
| `epub-builder/` | stdlib; Pillow for `prepare_cover.py` only | graded-reader selftest, **output byte-identical** (canary `workspace/being-earnest`), **and** opds-server selftest |
| `tools/opds-server/` | none, stdlib | `python3 tools/opds-server/scripts/selftest.py` |
| `tools/wallpaper-maker/` | Pillow (decode + resample only) | `.venv/bin/python tools/wallpaper-maker/scripts/selftest.py` **and** tgbot selftest (both import `crosspoint_device`) |
| `tools/epub-slimmer/` | Pillow | `.venv/bin/python tools/epub-slimmer/scripts/selftest.py` |
| `tools/tgbot/` | none, stdlib | `python3 tools/tgbot/scripts/selftest.py` |
| `ai-tools/graded-reader/` | jieba, pypinyin | `.venv/bin/python ai-tools/graded-reader/scripts/selftest.py` |
| `ai-tools/pdf2epub/` | pdfplumber, pypdf, pypdfium2, Pillow, pytesseract + system `tesseract-ocr` | no selftest, by design — re-run a `workspace/` sample by hand, read the EPUB ([`CONVERSIONS.md`](ai-tools/pdf2epub/CONVERSIONS.md)) |

```bash
python3 -m venv .venv
.venv/bin/pip install -r ai-tools/<svc>/requirements.txt
```

## Rules

- New markup → extend `epub-builder/FORMAT.md` first, then the builder. Not described there = not supported.
- Scripts measure, transform, check. Never invent. Typed JSON/file I/O, non-zero exit on failed gate.
- Device-facing change (EPUB output, fonts, feed) → read `extras/readers.md` first. It marks device-confirmed vs inferred; keep that distinction when you edit it.
- Feed markup → the opds-server selftest's oracle is a port of the device's own OPDS client. Valid OPDS ≠ readable by this device. Same pattern for wallpapers: the oracle is a port of the firmware's BMP reader, and valid BMP ≠ drawn as computed.
- Delivery to the device: OPDS is a **book-only pull** (`application/epub+zip`, SD root). Anything else — wallpapers, fonts, settings — is a push to the file-transfer web server. A book *can* also be pushed there (`tools/tgbot/` offers it); if you do, name it exactly as the OPDS client would (`crosspoint_client.opds_book_filename`) or the card ends up with two copies. `extras/readers.md` has the API.
- `tools/tgbot/` is **optional by construction**: it may import from any unit, and no
  unit may import it. Someone who only builds EPUBs must never need a token.
- `workspace/` is gitignored except allowlisted samples (proof + fixtures). Never assume a book there is tracked. New sample → add to `.gitignore` allowlist deliberately.
- Code MIT. Bundled fonts OFL (`extras/fonts/ATTRIBUTION.md`).
