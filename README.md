# X3 suite

## Preface

Hi fellow readers! Allow me to introduce you to my humble project. In it you'll
find sophisticated tools like an AI-assisted PDF to EPUB converter (that might
or might not work) or something as simple as a custom Chinese font I like.

This is a project I do for myself to make my life around the X3 easier and
better — mine is an **Xteink X3 running the CrossPoint 1.5.0 RC**, and that's what
everything here is built and tested against. Feel free to add, comment, fork,
use… I am not a super huge fan of locking tools or info behind copyright stuff
so whatever I did, it's yours to use. This is particularly true in these new
times where AI is actually doing a lot of it and my Claude is no different from
your Claude. Hopefully we are entering a new era of abundance and cordiality.

It's also all vibe-coded: written by an LLM at my direction, tested by me using
it. So each chapter opens with what's actually been checked — by a script where
a script can check it, by me squinting at an e-ink screen where it can't.

But I digress. Let's move to the guide.

---

## 1. What's here, and the idea behind it

This is a suite of tools. You'll find:

- **`opds-server/`** — serves your built books to the reader over WiFi.
- **`epub-builder/`** — turns the common book format into an EPUB. The services
  hand it their books; you can also run it on your own.
- **`wallpaper-maker/`** — turns any image into a sleep screen for the reader,
  and pushes it onto the device.
- **`services/`** — the AI-assisted tools:
  - **`graded-reader/`** — writes leveled Chinese books.
  - **`pdf2epub/`** — converts PDFs into clean EPUBs.
- **`reference/`** — device notes and things for the X3 itself. Special mention
  to **WenKai**, the Chinese font that makes CrossPoint render hanzi properly,
  plus whatever else I end up adding.
- **`workspace/`** — one folder per book, inputs and `build/` outputs. **Yours:**
  gitignored apart from two committed samples, so your books stay your books.

Each directory has a **`SKILL.md`** that is its real documentation. This guide
tells you what a thing is for and how to run it; `SKILL.md` tells you how it
works. `AGENTS.md` at the root is the same map written for a coding agent.

### The services' philosophy

The builder, the server and the wallpaper maker are plain deterministic tools —
no model anywhere near them. The two services are where an LLM does the work,
and there the rule is to let the model do what models are good at, and nothing
else:

- **The model supplies judgement** — writing prose, inferring chapter structure,
  deciding how to restore mangled text.
- **Scripts supply mechanics** — segmenting vocabulary, extracting a PDF,
  reflowing text, assembling the EPUB. They measure and transform; they never
  invent.
- **A deterministic gate follows every model step.** Vocabulary level, EPUB
  integrity, feed correctness. The model proposes, the gate disposes.

That's why "AI-assisted" here doesn't mean "hope for the best". When a model
must touch prose directly, it goes through a checked path that bounds and
prints the edit — it can re-run with different parameters, it can't quietly
rewrite.

### Setup

`epub-builder` and `opds-server` need nothing installed — they're stdlib only,
run them with plain `python3`. The two services do need packages, and
`wallpaper-maker` needs Pillow, so make a venv once and install whichever
you'll use (chapters 3, 4 and 7 name theirs):

```bash
python3 -m venv .venv
```

---

## 2. The book format

**Tested:** every tool below targets it, and the builder rejects anything else.

One shape, so tools compose. A book is a folder:

```
workspace/<slug>/
  book.json          title, author, language, chapter list
  chapters/ch01.md   one markdown file per chapter, '# Title' on line 1
  build/             outputs — never an input
```

The full contract, including covers, verse, images and endnotes:
[`epub-builder/FORMAT.md`](epub-builder/FORMAT.md). If it isn't described
there, the builder doesn't support it.

---

## 3. Writing a Chinese graded reader

**Tested:** working, with a self-test (`selftest.py`) that grades every
committed book and builds it. The sample, `workspace/being-earnest`, is a
12-chapter HSK 3 retelling of *The Importance of Being Earnest*.

A *planner* outlines the book, a *scribe* drafts each chapter against a
mechanically-built vocabulary brief, validation gates the HSK level
(out-of-list ≤ 5%, stretch ≤ 15%), a *glossary editor* prunes the harvested
glossary, and `annotate.py` marks each glossary word's first appearance with
its pinyin.

```bash
.venv/bin/pip install -r services/graded-reader/requirements.txt
S=services/graded-reader/scripts

.venv/bin/python $S/validate.py workspace/being-earnest   # grade a book
.venv/bin/python $S/selftest.py                           # full pipeline check
```

Drive it with Claude Code, or with the optional headless runner in
`services/graded-reader/headless/` against any OpenAI-compatible endpoint. On
Debian/Ubuntu install `jieba` inside a venv — system setuptools breaks its
legacy `setup.py`.

Docs: [`services/graded-reader/SKILL.md`](services/graded-reader/SKILL.md)

---

## 4. Converting a PDF

**Tested:** by worked conversion rather than self-test — whether it works is
whether a real PDF becomes an EPUB a human finds sound. The committed proof is
`workspace/alcaldes-encontrados`, a 1793 Spanish entremés, written up with the
gaps it surfaced in
[`CONVERSIONS.md`](services/pdf2epub/CONVERSIONS.md).

A PDF says where ink goes; an EPUB says what the text is. The pipeline recovers
the second from the first: triage characterises the source, scripts extract and
restore on the happy path, and the agent confirms routes, diagnoses failures and
emits decisions that scripts apply. The model never bulk-generates — every byte
traces back to the extraction, residual OCR noise included.

```bash
.venv/bin/pip install -r services/pdf2epub/requirements.txt
# the OCR route also needs the system binary:
#   apt install tesseract-ocr tesseract-ocr-spa   (or your source language)
```

Docs: [`services/pdf2epub/SKILL.md`](services/pdf2epub/SKILL.md) · design and
open questions: [`DESIGN.md`](services/pdf2epub/DESIGN.md)

---

## 5. Building the EPUB

**Tested:** shared by both services, so both self-tests exercise it. Output is
deterministic — the same source builds a byte-identical file, which is also how
regressions get caught.

One builder for everything. Hand-built XHTML/OPF, simple CSS, no embedded fonts
(the reader can't use them). It also ships the single structural check both
services rely on: mimetype, manifest⇄zip parity, well-formed XML, links resolve.

```bash
python3 epub-builder/scripts/build_epub.py \
    workspace/<slug> --out workspace/<slug>/build/<slug>.epub
python3 epub-builder/scripts/verify_epub.py \
    workspace/<slug>/build/<slug>.epub
```

Docs: [`epub-builder/SKILL.md`](epub-builder/SKILL.md)

---

## 6. Onto the reader

**Tested:** device-confirmed, 2026-07 — an X3 browsed a served catalog and
downloaded books from it. The self-test walks a real library through a port of
the reader's *own* OPDS client, because a standards-valid feed can still lose
books on this device.

Serves `workspace/*/build/*.epub` as an OPDS catalog the X3 browses directly.
Build a book and it's on the reader by the next page turn. Stdlib only.

```bash
python3 opds-server/scripts/serve_opds.py     # serve workspace/ on :6737
python3 opds-server/scripts/library.py        # what would be served
```

### On the device

Settings → System → OPDS Servers → Add Server, and enter the URL the server
prints — **starting with `http://`**. The reader only does verified HTTPS and
can't be given a self-signed certificate, so `https://` fails with a generic
"Failed to fetch feed".

### Configuring

**No config file needed.** Defaults: serves `workspace/`, binds `0.0.0.0:6737`
(6737 is "OPDS" on a phone keypad — not 8080, which everything else wants),
open, 25 entries per page. The startup banner says `config (defaults)` when
that's what you're on.

Change something once with a flag — `--root DIR` (repeatable), `--port`
(`0` picks a free one and prints it), `--host`, `--page-size`. Change it
permanently by copying `config.example.json` to `config.json`, which is
gitignored and annotated key by key. Basic auth lives there too, with the
password in `secrets/` — over plain HTTP it's cleartext, which is a real limit
of the device and not sloppiness.

### Leaving it running

`opds-server/opds-server.service` is a systemd unit: edit two lines,
`systemctl enable --now opds-server`. The commands and what they do
are in [`helper-info.txt`](opds-server/helper-info.txt). The unit passes no
flags, so under systemd anything non-default belongs in `config.json`. It's
written for Debian; elsewhere, hand it to your favourite AI.

Docs: [`opds-server/SKILL.md`](opds-server/SKILL.md)

---

## 7. The sleep screen

**Tested:** by self-test, not yet by a device. Every rule below was read from
the firmware source, and the gate enforces all of them; nobody has yet
photographed an X3 drawing one of these.

Drop pictures in `workspace/wallpapers/`, run one command, and they're files
the reader can draw. Run the second and they're on it.

```bash
.venv/bin/pip install -r wallpaper-maker/requirements.txt
.venv/bin/python wallpaper-maker/scripts/make_wallpaper.py   # -> .../build/*.bmp
python3 wallpaper-maker/scripts/push_wallpaper.py            # -> the device
```

Nothing to configure and nothing to answer: 528×792, four grey levels,
Floyd–Steinberg, cover-cropped, tone-stretched for e-ink. Each of those has a
right answer on this panel, so it's already chosen — the table in the SKILL
says why each one and not the alternative. Add `--preview` to get a PNG of the
result you can look at before it goes anywhere near the device.

An image too small to fill the panel isn't blown up to fit — enlargement stops
at 1.5x and the rest becomes a **mat**: four sectors mitred from the panel
corners, each continuing the image edge it touches. By default the edge
*travels* into its sector along a wandering path — one pixel out, at most one
across — so the picture seems to keep going in all four directions while
rippling, like looking at it through distorted glass.

`--mat edges` is the quiet version: one flat level per sector, snapped to one
of the panel's four so it draws without a trace of dither grain. `--mat blur`
washes an enlarged copy of the image behind it instead; `--mat none` gives
plain white.

This is the *sleep screen*, not an EPUB cover — a separate firmware feature,
different format, different folder. And a warning worth repeating: **`.pxc` is
not a wallpaper format** on this firmware, whatever the converters on the web
offer. It's the EPUB reader's internal pixel cache, and the sleep-screen scan
skips it in silence.

### Onto the device — which is not OPDS

The catalog can't carry it. The X3's OPDS client only follows links typed
exactly `application/epub+zip` and only ever saves to the SD root, so there's
no content type it accepts and no destination it can be aimed at. A catalog is
a pull, and this device only pulls books.

So it's a push instead, into the file-transfer web server the firmware already
ships. On the device: **Home → File Transfer → Join a Network**; it prints an
address and holds the server up while that screen is open. `push_wallpaper.py`
uploads into `/.sleep/` — or into `/sleep/` if you already keep wallpapers
there, since creating `/.sleep` would silently shadow them — and sets the sleep
screen to Custom so the pool is actually used.

To find the reader it tries the address that answered last time, then
`crosspoint.local`, then the firmware's own UDP discovery ping. If mDNS doesn't
work on your network — a local DNS filter or reverse proxy will cheerfully
answer for a `.local` name and never mention the reader — read the address off
the File Transfer screen and pass `--ip 192.168.x.x` once. It's remembered in a
gitignored file, so the next run finds it on its own.

Docs: [`wallpaper-maker/SKILL.md`](wallpaper-maker/SKILL.md) · device rules:
[`reference/readers.md`](reference/readers.md)

---

## 8. Fonts

**Tested:** device-confirmed. Stock firmware shows hanzi as tofu boxes; these
render.

Copy a family folder from `reference/fonts/` to the SD card under `/fonts/`,
power-cycle (fonts are scanned once at boot), then pick it under Settings →
Reader → Font Family.

- **`WenZilla/`** — the recommended Chinese font. LXGW WenKai kaiti for hanzi,
  NV Zilla Slab for Latin and pinyin, so mixed text reads well.
- **`WenKaiFull/`** — pure kaiti baseline.

Glyphs stream from the SD card, so a big font costs no RAM. Build rules for
making your own — including the trap where a subset font loads in the picker and
then silently fails — are in [`reference/readers.md`](reference/readers.md),
along with the rendering verdicts every tool here is shaped by: embedded EPUB
fonts are ignored, ruby and interlinear pinyin are broken, RAM is ~400 KB.

---

## 9. Making it yours

To add a tool, copy the shape: a `SKILL.md` briefing, deterministic scripts for
the parts models are bad at, and a gate after every model step. Everything
speaks plain files and JSON, so any agent that can run a shell drives it.

The code and documentation are **MIT** licensed — see [`LICENSE`](LICENSE).

> Offered in the spirit of a shift already underway: as AI lets anyone
> generate software tailored to their own needs, the scarcity that
> intellectual property was built to protect is fading. Take it, adapt it,
> make it yours.

Two kinds of bundled content keep their own terms:

- **Fonts**, under the **SIL Open Font License 1.1** — the `.cpfont` families in
  `reference/fonts/` (from LXGW WenKai, Zilla Slab, Noto CJK) and
  `reference/covers/IMFellEnglish-Regular.ttf` for cover titles. Notices in
  [`ATTRIBUTION.md`](reference/fonts/ATTRIBUTION.md),
  [`OFL.txt`](reference/fonts/OFL.txt) and
  [`IMFellEnglish-OFL.txt`](reference/covers/IMFellEnglish-OFL.txt).
- **Sample texts** under `workspace/`, either original here or public domain —
  Oscar Wilde's *The Importance of Being Earnest*, retold in Chinese, and the
  1793 entremés *Los alcaldes encontrados*.

Issues and pull requests are welcome, very much including the ones that just
say this is wrong.
