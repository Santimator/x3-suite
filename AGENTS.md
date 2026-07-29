# AGENTS.md

Rules for **changing** this repository. For what it is and how to run it, read
[`README.md`](README.md) — it's the guide, and this file deliberately doesn't
repeat it.

Each of the four top-level units — `epub-builder/`, `opds-server/`,
`services/graded-reader/`, `services/pdf2epub/` — has a **`SKILL.md`** that is
its canonical documentation. Read the one for the unit you're touching before
you touch it. (`.claude/skills/` symlinks all four so Claude Code loads them as
skills; the real content lives in the directories.)

## What to run before you commit

```bash
python3 -m venv .venv
.venv/bin/pip install -r services/graded-reader/requirements.txt   # jieba, pypinyin
.venv/bin/pip install -r services/pdf2epub/requirements.txt        # + system tesseract
```

The builder and the server are stdlib only — plain `python3`, nothing to install.

How each unit is verified differs, by design:

- **graded-reader** — `.venv/bin/python services/graded-reader/scripts/selftest.py`.
  Its gates are objective (vocabulary level, EPUB integrity), so a self-test
  can hold them.
- **opds-server** — `python3 opds-server/scripts/selftest.py`. Its oracle is a
  port of the *device's own* OPDS client, because a standards-valid feed can
  still lose books on this reader. Anything touching feed markup keeps it
  green; the client contract is tabulated in `reference/readers.md`.
- **pdf2epub** — no self-test, deliberately. Whether it works is whether an
  agent can turn a real PDF into an EPUB a human finds sound, so the proof is
  the worked conversion under `workspace/` plus
  [`CONVERSIONS.md`](services/pdf2epub/CONVERSIONS.md). To sanity-check the
  scripts, re-run a sample by hand and read the result.
- **epub-builder** — shared by everything. Run the graded-reader self-test
  **and** keep its EPUB output byte-identical (`workspace/being-earnest` is the
  canary), then the opds-server self-test, since a builder change alters what
  the server hands the device.

## Conventions

- **Extend the contract before the builder.** `epub-builder/FORMAT.md` is what
  the builder accepts; if a construct isn't described there, add it there first.
- **Scripts measure, transform and check — they never invent.** Typed JSON/file
  I/O, non-zero exit on a failed gate, so any shell-capable agent can drive them
  with no framework.
- **`workspace/` is gitignored except for the committed samples**, which are
  proof and self-test fixtures. Never assume a book you find there is in version
  control; committing a new sample means adding it to the `.gitignore` allowlist
  on purpose.
- **Read `reference/readers.md` before touching anything device-facing** — EPUB
  output, fonts, or the OPDS feed. It records what the device actually does, and
  marks which verdicts are device-confirmed.
- **Licensing:** code is MIT; bundled fonts are OFL
  (`reference/fonts/ATTRIBUTION.md`).
