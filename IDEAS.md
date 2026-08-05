# IDEAS.md

A shelf. Things considered for this suite and not built, with enough of the
reasoning kept that picking one up again doesn't start from nothing.

Nothing here is a plan. An idea moves out of this file by becoming a unit with
its own `SKILL.md`, or it stays.

## Ideas that didn't spark

### periodex — a collectable card per chemical element

*Considered 2026-08. Shelved: interesting to design, not interesting enough to
own 118 of.*

A generator producing 528x792 sleep-screen cards, one per element, in decks —
Bohr model / position in the table / chemistry / abundances / emission
spectrum. Ordered by atomic number.

**The one part worth keeping.** The mechanic needs no code on the device: the
firmware already picks a file at random from `/.sleep/` every time the reader
sleeps ([`wallpaper-maker/SKILL.md`](wallpaper-maker/SKILL.md)). Push the deck
and the shuffle is already there. That applies to *any* "collection of cards"
idea, not just this one.

**What the shape would have been**, if it returns or if a different collection
borrows it:

- `services/periodex/`, parallel to `services/graded-reader/` — a generator
  feeding a device-facing unit, no new top-level concept, no `wallpaperdex/`
  parent holding one child.
- One renderer owning the panel contract (528x792, the four levels, margins,
  minimum legible type); each deck a small module composing primitives in code.
  Not a JSON layout DSL.
- Cards bypass `make_wallpaper.py` entirely — autocontrast/gamma/sharpen/dither
  is right for a photograph and destroys 12pt text. Drawn straight in the four
  native levels, encoded by `crosspoint_bmp`. Which buys a stronger gate than
  photos get: decoded output *pixel-identical* to what was drawn, not merely
  close.
- Selection as one expression (`all`, `1-18`, `Fe,Au`, `noble-gases`), not
  separate all-vs-list modes.

**What stalled it.** Not the rendering — the data. Numbers are cheap and
bounded (118 is a closed set; Bohr shells are computed from Z plus ~20 known
exceptions). Prose and photographs are not: ~350 short strings that per
[`AGENTS.md`](AGENTS.md) must be sourced rather than invented, and element
photographs whose good versions are copyrighted, leaving a per-image
license/attribution manifest to maintain. Bounded work, real work, and no
appetite for it.
