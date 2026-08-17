# secrets/

The Telegram bot token lives here and is **gitignored**. Nothing in this
directory is committed except this README.

1. Talk to [@BotFather](https://t.me/BotFather) on Telegram: `/newbot`, pick a
   name, and it hands you a token that looks like
   `1234567890:AAE...`.

2. Save it here as `telegram.token` — the token only, nothing else:

   ```bash
   printf '%s' '1234567890:AAE...' > tools/tgbot/secrets/telegram.token
   chmod 600 tools/tgbot/secrets/telegram.token
   ```

3. Get your own numeric user id from [@userinfobot](https://t.me/userinfobot)
   and put it in `config.json` as `telegram.user_id`.

Alternatively, skip the file and export `X3_TGBOT_TOKEN`; the bot falls back to
that environment variable.

## Keeping both out of the repo entirely

This directory is inside a checkout, and a checkout is the sort of folder you
point a coding agent at. If Claude Code or Codex can read this repo, they can
read a token sitting in it.

So neither secret has to be here. Put one line in `config.json`:

```json
{ "secrets_dir": "/home/you/private/x3" }
```

and create two files in that directory, each holding one value and nothing
else:

```bash
mkdir -p ~/private/x3 && chmod 700 ~/private/x3
printf '%s' '1234567890:AAE...' > ~/private/x3/telegram.token
printf '%s' '123456789'         > ~/private/x3/telegram.user_id
chmod 600 ~/private/x3/*
```

Now the repo holds a path and nothing more. The id file wins over
`telegram.user_id` in `config.json` when both exist, so moving it out actually
moves it rather than leaving a copy behind. The bot prints a warning at startup
if `secrets_dir` turns out to be inside the repo after all, and another if
either file is readable by anyone but you.

To move the *whole* configuration out, keep nothing in the repo at all:

```bash
python3 tools/tgbot/scripts/bot.py --config ~/private/x3/config.json
```

Relative paths inside a config file resolve against that file's own directory,
so `"token_file": "telegram.token"` there means the one next to it.

## Why the token is not in the unit file

A systemd `EnvironmentFile` would work, and it is what most guides suggest. It
is not what this repo does, for a specific reason: an environment variable is
readable from `/proc/<pid>/environ` by anything running as that user, and it is
inherited by every subprocess. This bot spawns plenty — the wallpaper converter,
the EPUB builder, the catalog scanner. A file the bot reads once at startup is
read by the bot and nothing else.

## What the token can do, and what the user id is for

The token is the bot's whole identity: anyone holding it can read every message
sent to the bot and reply as it. Treat it like a password, and regenerate it in
@BotFather (`/revoke`) if it ever lands somewhere it shouldn't.

The **user id whitelist is the actual access control**, and it is independent of
the token. Anyone can find a bot by name and message it; the whitelist is what
means only you are answered. It is checked before any handler runs, on button
presses as well as messages — the buttons are where the delete confirmations
live, so a bot that guarded only messages would look safe and be wide open.
