# Default cover assets

Used by `services/pdf2epub/scripts/prepare_cover.py` as the last-resort cover
when a book has no `source-cover.*` sidecar and no usable cover inside its PDF.

- **`default.png`** — the project's default cover template: a reading-nook scene
  with a blank parchment panel for the title and a "SANTIMATOR" author
  nameplate. A project asset (not third-party).
- **`default.json`** — where the title goes on `default.png`: `title_box`
  (fractions of the image `[x0,y0,x1,y1]`), `color` (RGB ink), `uppercase`, and
  `font`. Swap the image and adjust these to re-home the title.
- **`IMFellEnglish-Regular.ttf`** — the title face, baked into the cover PNG at
  build time (it never ships to the device). A digital revival of the
  17th-century Fell types, fitting for period texts.

## Font license

**IM Fell English** — © 2010 Igino Marini (mail@iginomarini.com), licensed
under the **SIL Open Font License 1.1**; full text in
[`IMFellEnglish-OFL.txt`](IMFellEnglish-OFL.txt). Not covered by the
repository's top-level MIT license. To use a different title font, drop its
`.ttf`/`.otf` here (with its license) and point `default.json`'s `font` at it,
or pass `--font` to `prepare_cover.py`.
