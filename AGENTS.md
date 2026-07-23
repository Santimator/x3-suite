# AGENTS.md

Guidance for any coding agent working in this repository. (Human-readable too —
it's just how the repo is laid out and how to work in it.)

## What this is

A suite that produces EPUBs for an **Xteink X3** e-ink reader: one shared
**builder** and two **services** (AI-assisted tools) that target its format.
Read [`README.md`](README.md) for the full picture; the guiding idea is *LLM
judgement, deterministic scripts for mechanics, and a deterministic gate after
every LLM step*.

## Layout

```
epub-builder/            shared infrastructure: the book-format contract
                         (FORMAT.md) + build_epub.py + verify_epub.py
services/
  graded-reader/         service: writes leveled Chinese readers
  pdf2epub/              service: converts PDFs into clean EPUBs
workspace/<slug>/        one folder per book/job (source, chapters/, book.json,
                         build/ outputs)
reference/               device notes (readers.md) + SD-ready .cpfont fonts
```

Each of the three top-level units has a **`SKILL.md`** that is its canonical,
self-contained documentation — read it before changing that unit.
`.claude/skills/` contains symlinks to these three so Claude Code auto-loads
them; the real content lives in the directories above.

## How a service is built (the pattern to follow)

Every service is the same three things:

1. **A briefing** (`SKILL.md`, plus `prompts/` for graded-reader) — how to do
   the task and when to defer to a tool.
2. **Deterministic tools** (`scripts/`) for the parts models are unreliable at
   — they measure, transform, and check; they never invent.
3. **A deterministic gate after every model step.** The model proposes; a gate
   disposes. Trust comes from the gates, not the model.

When the model must touch prose directly (e.g. a one-off OCR fix), it goes
through a *guarded* path bounded and printed by a deterministic check — see
pdf2epub's stage 2b.

## Working here

```bash
python3 -m venv .venv
.venv/bin/pip install -r services/graded-reader/requirements.txt
.venv/bin/pip install -r services/pdf2epub/requirements.txt
```

Run a service's self-test after changing it — this is the acceptance check:

```bash
.venv/bin/python services/graded-reader/scripts/selftest.py
.venv/bin/python services/pdf2epub/scripts/selftest.py
```

Changes under `epub-builder/` are shared infrastructure: run **both**
self-tests, and keep the annotated (graded-reader) EPUB output byte-identical
(`workspace/yugong-mountain` is the canary).

## Conventions

- Deterministic scripts have typed JSON/file I/O and exit non-zero on a failed
  gate — any shell-capable agent can drive them; no framework required.
- Device constraints shape everything — read `reference/readers.md` before
  touching anything that affects the EPUB output or fonts.
- Licensing: code is MIT; bundled fonts are OFL (`reference/fonts/ATTRIBUTION.md`).
