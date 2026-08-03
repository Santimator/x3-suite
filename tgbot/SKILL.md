---
name: tgbot
description: >-
  Operate the whole suite from a phone over Telegram — build a wallpaper from a
  photo, queue it, push it to the X3, browse and rename files on the device,
  drop books into the catalog, stage a PDF for conversion. Use when the reader
  should be managed without a terminal, when changing what the bot offers or
  how it talks to the device, or when a queued item never arrived. Triggers
  include "telegram bot", "from my phone", "push the wallpaper", "rename it on
  the device", "the bot didn't answer".
---

# tgbot — the suite, operated from a phone

Suite infrastructure, not a task, and **optional by construction**: nothing
else in this repo imports it, nothing else needs a token, and deleting the
directory leaves every other unit working. It exists so the common chores —
a new sleep screen, a book onto the catalog, an ugly filename on the SD card —
do not require sitting down at a computer.

Everything it does, a script here already did. The bot adds buttons, a queue,
and someone to tell you when a job finished. It adds no new way to make an
EPUB or a wallpaper, and **no model is anywhere in its control flow** — where
AI work is involved it calls a service's entry point and reports what that
says, which is the same seam Claude Code drives by hand.

Deterministic like the builder and the server: stdlib only, no dependencies,
nothing to install. `python3 tgbot/scripts/bot.py` and it runs.

## Usage

```bash
python3 tgbot/scripts/bot.py             # run it
python3 tgbot/scripts/selftest.py        # the gate; exit 0 = sound
```

Setup is two facts it cannot guess:

1. `config.example.json` → `config.json` (gitignored), and put your numeric
   Telegram id in `telegram.user_id`. [@userinfobot](https://t.me/userinfobot)
   tells you what it is.
2. The token from [@BotFather](https://t.me/BotFather) into
   `secrets/telegram.token`, mode 600. See [`secrets/README.md`](secrets/README.md).

Then message the bot. It answers `/start` with **Let's read!** and the menu.

## The two scopes

This is the whole design, and everything else follows from it.

**Manipulate** is available any time and never touches the reader. Build a
wallpaper, file a book where the catalog will find it, stage a PDF, rename or
delete inside `workspace/`. Work that is *bound for the device* is **queued**,
not sent.

**Push** happens only when you ask, and only while the X3 sits on **Home →
File Transfer → Join a Network** — the firmware runs its web server only on
that screen and stops the moment you leave it. If the reader cannot be found,
**the queue is left exactly as it was**.

That last promise is the reason the queue is worth having. You can send photos
all week from wherever you are, and push once when you are next near the
device, without ever wondering whether something was half-delivered.

**A book is filed before it is anything else.** Every EPUB that arrives goes
into `workspace/library/`, where `opds-server` scans — always, first, without
being asked. The catalog is the library; the card is a convenience. So there
is no path through this bot that puts a book on the device and nowhere else.

Copying it onto the card *as well* is then an offer, not a default, and it is
safe because the upload borrows the OPDS client's own naming (see below). Push
and pull converge on one file rather than racing to make two.

## What the buttons do

```
📚 Library    what the catalog would serve — rename or delete, server-side
🖼 Queue      what is waiting for the reader; push or clear it
📥 Inbox      files you dropped in workspace/inbox/ by hand
📲 Device     find it, browse the SD card, push the queue
🔤 Fonts      families in this repo; send one and verify it landed
⚙️ Status     catalog up? queue depth? AI configured? last push?
```

### A photo becomes a sleep screen

Send one. The bot asks `make_wallpaper.py --probe` whether the image fills the
panel, and only then decides what to ask you:

- **It fills** (528×792 after at most 1.5× enlargement) — built, previewed,
  and offered with **✓ Queue it**.
- **It does not** — previewed on plain white, so you can see exactly what is
  yours and what is filler, with the four mats as buttons: **≈ waves ▣ edges
  ◌ blur — white**. Tapping one builds *and* queues in the same tap.

The threshold is asked, never copied. "Smaller than 528×792" is the wrong
test and would misfire on real photos: a 500×750 image is smaller in both
dimensions and still fills, because it only has to grow 1.06×. `MAX_UPSCALE`
and `MIN_MAT_AREA` are judgement calls that live in `make_wallpaper.py`, and
`probe()` exists so this bot can ask rather than re-derive them.

### A push session

**📲 Device → Push queue** tells you to open File Transfer on the X3, then
waits for **✅ Ready**. Then it runs `push_wallpaper.py --json` and reports a
line per file. Only the files the push script says landed leave the queue —
a partial success removes exactly the part that succeeded, and a device that
never answered removes nothing.

### Browsing and renaming on the device

**📂 Browse** lists the SD card. Folders navigate; files offer **✏️ Rename**,
**🗑 Delete** (twice, always), and **⬇️ Pull to server**.

This is the half of the bot with no alternative anywhere else in the repo.
Everything else is a nicer front end for a script you could run over ssh;
there is no CLI for "rename this file on the reader", and the reason you want
one is that the device names OPDS downloads `<author> - <title>.epub`, and
books from other catalogs arrive with names like
`Historia-de-la-magia-resumen-de-sus-procedimientos-...-Rafael-Urbano.epub`.

**🖼 Wallpapers** jumps straight to the folder the device is actually reading —
`/.sleep` if it exists, `/sleep` otherwise. That shortcut is not a convenience:
`/api/files` hides dot-prefixed entries, so `/.sleep` never appears in a
listing and cannot be reached by browsing at all.

Any `.bmp` offers **👁 Preview**, and a folder full of them offers *Preview
all* — which is the answer to three wallpapers with names that say nothing.
The picture is rendered by `crosspoint_bmp.py`, the port of the firmware's own
reader, so what arrives in the chat is what the panel draws: the same
native-palette direct map, and an under-size wallpaper shown in the black field
it will actually sit in. If the file is one the device would *re-dither* — a
foreign BMP, a non-native palette — the preview falls back to an ordinary
decode and says so, because identifying the picture is still the job and "the
device will redo this one" is worth knowing.

**Folders can be renamed, but only some.** `/rename` and `/move` refuse
directories outright ("Only files can be renamed"), so a folder goes through
WebDAV `MOVE`, which has no such check. WebDAV's own guard is stricter in the
other direction: it rejects any path containing a dot-prefixed segment, so
`/sleep` can be renamed and `/.sleep` cannot, by any route. The bot turns that
down with the reason rather than letting you discover a 403.

**Device names are opaque tokens.** Whatever `/api/files` returns is what the
SD card holds, byte for byte, and it is exactly what goes back out in a
`path=`. This is not fastidiousness: real books on a real device carry
decomposed accents — `é` as `e` + U+0301 — which look identical on screen to
the composed form and compare unequal in the firmware's `Storage.exists()`.
Normalize one on the way through and you get `Item not found` for a file
plainly sitting there. So the bot never retypes a device name: it shows it,
carries it in a token, and sends it back untouched. You only ever type the
*new* name.

That token is also how a 200-character filename fits in a button at all —
Telegram caps `callback_data` at 64 bytes.

### A font

**🔤 Fonts** lists the families in `reference/fonts/` with their sizes and
weight, and flags any that lacks 8/10/12 pt — those render books perfectly and
leave the chapter list **blank** for CJK, which is the least obvious failure
this device has.

Sending one is not like sending a wallpaper: a family is ~24 MB in six files,
the largest 6.8 MB, so it takes minutes and the reader has to stay on the File
Transfer screen throughout. Files go one at a time with a tick each, through
`POST /api/fonts/upload` — the firmware creates the family directory itself and
checks the `CPFONT\0\0` magic, so it refuses a "save link as" HTML page
outright.

**Then it verifies, and that is the point of the whole feature.** The reader
lists fonts by *filename only* — it never opens them — so a truncated copy
appears in the picker and only fails when selected, at which moment the family
silently reverts to built-in Noto and looks for all the world like a bad font.
The magic-byte check cannot catch it: the magic is fine and the file is short.
So after the push, `GET /api/fonts` byte counts are compared against
`reference/fonts/CHECKSUMS.tsv`, file by file, and you get either "all six,
byte for byte" or exactly which one is wrong.

Then it **selects the family for you** and says it takes effect at the next
power-cycle. Not asked, done: nobody spends four minutes uploading a font they
did not intend to read with, and it is two taps to change on the device.

That this works at all is a nicety of the firmware. `fontFamily` looks like an
enum index over the scanned registry, which a family uploaded seconds ago is
not reliably part of — but the *setter stores the name*
(`SETTINGS.sdFontFamilyName`) and the getter resolves it back each time. So the
choice can be made before the reboot that makes the font usable: it persists,
and the next boot's scan makes it real. The bot finds the index by looking the
family up in the enum's own `options` list rather than adding to a built-in
count, so a firmware that ships another built-in face cannot silently move the
selection onto the wrong family — and it reads the value back, so a family the
registry has not picked up is reported rather than assumed.

Deleting a family is in the browse view — walk to `/fonts/<Family>` and the
folder's own **🗑 Delete this folder** becomes a family delete, routed through
`POST /api/fonts/delete` so the firmware's `FontInstaller` does it and marks the
registry dirty. For any other folder the same button empties it and removes it,
because `/delete` only takes a directory that is already empty.

### A book

Send an `.epub`. It is filed into `workspace/library/`, checked with the
shared `epub-builder/scripts/verify_epub.py`, and the bot replies with the
title and author **as the device will see them** (read through the catalog's
own scanner, from the OPF) plus the filename it will get on the SD card. Then
it says whether `opds-server` is actually answering — because "it's on the
catalog" is only true if that server is up.

Then it offers **📤 Also send to device**, which queues the book for the SD
root alongside any wallpapers. Worth having when the reader is off your LAN, or
when opds-server isn't running, or when you simply have the device in your
hands. The catalog copy stays either way.

The name it lands under is not ours to choose: it comes from
`crosspoint_client.opds_book_filename`, the port of the firmware's own
`opdsBookFilename`, byte budget and trailing-dot trim included. Get that wrong
and the reader ends up holding two copies of every book you touched twice —
one from the push, one from a later download. Since 1.5.0 the layout is a
*device setting* (`opdsFilenameFormat`: author–title, title–author, or title
alone), so the bot reads it from `/api/settings` while the reader is in front
of it rather than assuming the default.

### A PDF

Staged, not converted. The bot creates `workspace/<slug>/source.pdf`, runs
`triage.py`, and reports the class and route it found. Then it is honest about
what happens next: `services/pdf2epub` has no headless runner yet
([`DESIGN.md`](../services/pdf2epub/DESIGN.md), open question 6), so the job
waits for a driver — which today means pointing Claude Code at the folder and
reading that skill. When the runner lands, the bot calls it and this paragraph
gets shorter.

**The 20 MB wall.** Telegram refuses to let *any* bot download a file bigger
than 20 MB, whatever your account managed to upload. Scanned PDFs live right
on that line. The bot checks the size first and says so rather than failing
mid-download; put the file in `workspace/inbox/` on the server and tap
**📥 Inbox**, which lists what is there and routes it the same way.

## Configuration

`config.example.json` → `config.json`, gitignored, as is `secrets/`. The
credential seam is the one `opds-server` and `services/graded-reader/headless`
use: the secret is never in the config file, only a path to a gitignored file
holding it, with an environment variable as the fallback.

### Secrets outside the checkout

This repo is the sort of folder you point a coding agent at, and a token in it
is a token that agent can read. So neither the token nor the id has to live
here. One line in `config.json`:

```json
{ "secrets_dir": "/home/you/private/x3" }
```

and the bot reads `telegram.token` and `telegram.user_id` from that directory
instead — each holding one value and nothing else, in the same style as
`opds-server`'s password file. What stays in the repo is a path.

The id file **wins over** `telegram.user_id` in `config.json` when both exist,
so moving it out actually moves it rather than leaving a copy behind. The bot
warns at startup if `secrets_dir` turns out to be inside the repo anyway, and
if either file is readable by anyone but you.

For the complete version, `--config /somewhere/else.json` moves the whole
configuration out: relative paths inside a config resolve against *that file's*
directory, not against `tgbot/`.

**The bot owns no AI configuration.** There is no `ai` block here, on purpose.
`graded-reader` already has exactly that seam in its own `headless/config.json`
— base URL, model, key file, provider-neutral — and duplicating it would create
two places to be wrong. The bot calls the service's entry point and relays what
it says, including "not configured".

## Safety

- **One Telegram id**, checked before any handler runs, on messages *and* on
  button presses. Callbacks are where the delete confirmations live, so a bot
  that guarded only messages would look safe and be wide open. The owner's id
  arriving from a group chat is also refused — sender and chat must both be
  you.
- **Rejections are silent to the sender and logged here.** `journalctl -u
  tgbot` shows `ignored update from user=NNN`, which is how you discover that
  NNN is your own id and you typed it wrong.
- **Server-side writes and deletes are confined to `workspace/`**, resolved
  through symlinks. A path that would land outside is refused, not sanitized.
- **Every delete asks twice**, on the server and on the device alike.
- **The queue is written atomically** — a temporary file in the same directory,
  then `os.replace`, which is atomic on POSIX. A crash mid-write leaves the
  previous queue intact rather than half a file. That is what the
  never-drain-partially promise rests on.

## Running it as a service

[`tgbot.service`](tgbot.service) is a systemd **system unit**, run as your own
account — the workspace and the books live in your home. Its two
placeholders are filled in by the shell rather than by hand, because a path
typed twice is a path typed differently once:

```bash
mkdir -p tgbot/state
sed -e "s|CHANGEME_REPO|$PWD|g" -e "s|CHANGEME_USER|$(id -un)|g" \
    tgbot/tgbot.service | sudo tee /etc/systemd/system/tgbot.service
sudo systemctl daemon-reload
sudo systemctl enable --now tgbot
sudo journalctl -u tgbot -f
```

It is **not** a copy of `opds-server.service`, and it must not be. That unit is
`ProtectSystem=strict` with `ProtectHome=read-only` precisely because the
server only ever reads. This bot writes — wallpapers and staged PDFs into
`workspace/`, its queue into `tgbot/state/`, the reader's remembered address
into `wallpaper-maker/`. Those three paths are named in `ReadWritePaths=`;
everything else stays read-only. Copy the other unit verbatim and you get a
bot that dies on its first photo.

The unit runs the script **from the repo**, so updating the bot is
`git pull && sudo systemctl restart tgbot` and nothing else — no reinstall. The
one exception is this unit file, which installing *copied* to
`/etc/systemd/system/`: if a pull changes `tgbot.service`, re-copy it,
`daemon-reload`, and restart. It is a copy rather than a symlink deliberately,
because the unit carries your account name and clone path, and a tracked file
you must edit locally is a file that fights every pull.

[`helper-info.txt`](helper-info.txt) sits next to the unit and explains each
command, the day-to-day ones, what a `git pull` does and does not need, and the
handful of failures worth recognising on sight.

## How it talks to the rest of the suite

Two kinds of call, and the difference is deliberate.

**Pipelines are subprocesses.** `make_wallpaper.py`, `push_wallpaper.py`,
`library.py`, `verify_epub.py`, `triage.py`, `run_book.py` — each has a command
line, typed output and a non-zero exit, and calling them any other way would
make the bot a second place where a pipeline's steps are written down. This is
what `services/graded-reader/headless/run_book.py` already does with the same
scripts. It also means a failure arrives as text that can go straight to the
chat.

**The device is imported.**
[`wallpaper-maker/scripts/crosspoint_device.py`](../wallpaper-maker/scripts/crosspoint_device.py)
is transport, not a pipeline: there is no CLI for "rename this file on the
reader", and inventing one so the bot could subprocess it would be theatre.
It is the same module `push_wallpaper.py` uses, so the repo has exactly one
description of the reader's API — `reference/readers.md` documents it, that
file implements it, and both scripts drink from it.

The dependency points one way, always. Someone who only builds EPUBs must
never need a token.

## The gate

[`scripts/selftest.py`](scripts/selftest.py) runs with no token, no network and
no reader — a fake Telegram that records what would have been sent, and a
temporary workspace. It grades the four things that would actually hurt:

1. **The whitelist**, across every update type including button presses, plus
   the owner's id arriving from a foreign chat.
2. **The path jail**: `../`, absolute paths, and a symlink that leaves the
   root — refused by resolution, not by string matching.
3. **The queue survives a restart**, keeps its order, leaves no temporary
   files, and never takes a book without being asked.
4. **A failed push changes nothing** — wallpapers or books — a partial push
   removes exactly what landed, and a queued file that vanished from disk is
   dropped by name rather than pushed.
5. **A pushed book gets the name the OPDS client would give it**, checked
   against that port directly, including the 100-byte budget and a title of
   nothing but dots.

Plus: a 200-character device name with decomposed accents round-trips through a
button token byte-identically, a stale token is answered rather than guessed
at, nothing outside `tgbot/` imports `tgbot`, and — the checks that matter if
you moved the secrets out — the id and token are read from outside the repo,
neither value is left in the config that stays behind, the outside file beats a
leftover id, a relocated config resolves paths against itself, and a
`secrets_dir` pointing back inside the repo is called out.

**Status: the flows above are implemented and self-tested; the device half is
built on endpoints device-confirmed on a 1.5.0 RC (2026-08) —
`/api/status`, `/api/files`, `/upload`, `/delete`, `/mkdir`, `/api/settings`
through `push_wallpaper.py`, and `/rename` directly.** `/download` and `/move`
are source-confirmed and wired but have not been driven against a device.

## Files

```
SKILL.md                     this file
config.example.json          copy to config.json (gitignored)
tgbot.service                systemd unit (Debian; edit five lines)
helper-info.txt              what each systemctl command does, and what breaks
secrets/                     gitignored; the bot token
state/                       gitignored; the delivery queue
scripts/
  bot.py                     the loop, the whitelist, the menus, the flows
  telegram.py                the Bot API, in as much of it as this needs
  suite.py                   every call into the rest of the repo
  state.py                   durable queue, notes, button tokens
  config.py                  config, the token seam, the path jail
  selftest.py                the gate
```
