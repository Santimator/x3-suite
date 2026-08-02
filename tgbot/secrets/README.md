# secrets/

The Telegram bot token lives here and is **gitignored**. Nothing in this
directory is committed except this README.

1. Talk to [@BotFather](https://t.me/BotFather) on Telegram: `/newbot`, pick a
   name, and it hands you a token that looks like
   `1234567890:AAE...`.

2. Save it here as `telegram.token` — the token only, nothing else:

   ```bash
   printf '%s' '1234567890:AAE...' > tgbot/secrets/telegram.token
   chmod 600 tgbot/secrets/telegram.token
   ```

3. Get your own numeric user id from [@userinfobot](https://t.me/userinfobot)
   and put it in `config.json` as `telegram.user_id`.

Alternatively, skip the file and export `X3_TGBOT_TOKEN`; the bot falls back to
that environment variable.

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
