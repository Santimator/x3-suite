# AGENTS.md

Rules for **changing** this repo. What it is / how to run it → [`README.md`](README.md).
Per-unit docs → that unit's `SKILL.md`. Read it before touching the unit.
(`.claude/skills/` symlinks every one of them.)

## Units

| unit | deps | verify after changing it |
|---|---|---|
| `epub-builder/` | none, stdlib | graded-reader selftest, **output byte-identical** (canary `workspace/being-earnest`), **and** opds-server selftest |
| `opds-server/` | none, stdlib | `python3 opds-server/scripts/selftest.py` |
| `wallpaper-maker/` | Pillow (decode + resample only) | `.venv/bin/python wallpaper-maker/scripts/selftest.py` **and** tgbot selftest (both import `crosspoint_device`) |
| `tgbot/` | none, stdlib | `python3 tgbot/scripts/selftest.py` |
| `services/graded-reader/` | jieba, pypinyin | `.venv/bin/python services/graded-reader/scripts/selftest.py` |
| `services/pdf2epub/` | pdfplumber, pypdf, pypdfium2, Pillow, pytesseract + system `tesseract-ocr` | no selftest, by design — re-run a `workspace/` sample by hand, read the EPUB ([`CONVERSIONS.md`](services/pdf2epub/CONVERSIONS.md)) |

```bash
python3 -m venv .venv
.venv/bin/pip install -r services/<svc>/requirements.txt
```

## Rules

- New markup → extend `epub-builder/FORMAT.md` first, then the builder. Not described there = not supported.
- Scripts measure, transform, check. Never invent. Typed JSON/file I/O, non-zero exit on failed gate.
- Device-facing change (EPUB output, fonts, feed) → read `reference/readers.md` first. It marks device-confirmed vs inferred; keep that distinction when you edit it.
- Feed markup → the opds-server selftest's oracle is a port of the device's own OPDS client. Valid OPDS ≠ readable by this device. Same pattern for wallpapers: the oracle is a port of the firmware's BMP reader, and valid BMP ≠ drawn as computed.
- Delivery to the device: OPDS is a **book-only pull** (`application/epub+zip`, SD root). Anything else — wallpapers, fonts, settings — is a push to the file-transfer web server. A book *can* also be pushed there (`tgbot/` offers it); if you do, name it exactly as the OPDS client would (`crosspoint_client.opds_book_filename`) or the card ends up with two copies. `reference/readers.md` has the API.
- `tgbot/` is **optional by construction**: it may import from any unit, and no
  unit may import it. Someone who only builds EPUBs must never need a token.
- `workspace/` is gitignored except allowlisted samples (proof + fixtures). Never assume a book there is tracked. New sample → add to `.gitignore` allowlist deliberately.
- Code MIT. Bundled fonts OFL (`reference/fonts/ATTRIBUTION.md`).
