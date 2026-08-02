#!/usr/bin/env python3
"""Configuration, and where the bot is allowed to put its hands.

The same seam `opds-server` and `services/graded-reader/headless` use: an
annotated `config.example.json`, a gitignored `config.json` beside it, and the
secret itself never in either — only a path to a gitignored file holding it,
with an environment variable as the fallback.

Everything has a working default except the one thing that cannot have one:
the Telegram user id. A bot that answers everybody is not a bot with a default,
it is a bot with a hole, so the loader refuses to start without one.

**Keeping the secrets out of the checkout.** This repo is the sort of folder
you point a coding agent at, and a token sitting in it is a token that agent
can read. So neither the token nor the id has to live here:

    "secrets_dir": "/home/you/private/x3"

moves both — the loader then looks for `telegram.token` and `telegram.user_id`
there, each holding one value and nothing else, and warns at startup if that
directory turns out to be inside the repo after all. An absolute `token_file`
does the same for the token alone, and `--config /somewhere/else.json` moves
the whole configuration out, since a relative path inside a config file
resolves against *that file's* directory rather than against this one.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]      # tgbot/
REPO_ROOT = SKILL_DIR.parent
DEFAULT_CONFIG = SKILL_DIR / "config.json"

DEFAULTS = {
    "telegram": {
        "user_id": 0,
        "token_file": "secrets/telegram.token",
        "token_env": "X3_TGBOT_TOKEN",
        "user_id_file": "telegram.user_id",
        "user_id_env": "X3_TGBOT_USER_ID",
        "poll_timeout": 30,
    },
    "secrets_dir": "",
    "workspace": "workspace",
    "state_dir": "tgbot/state",
    "opds_url": "http://127.0.0.1:6737/opds",
    "max_download_mb": 20,
}


class ConfigError(RuntimeError):
    pass


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        elif not k.startswith("_"):          # _comment / _note keys are prose
            out[k] = v
    return out


def load(path: Path | None = None) -> dict:
    path = Path(path) if path else DEFAULT_CONFIG
    raw = {}
    if path.exists():
        text = path.read_text(encoding="utf-8")
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigError(_json_complaint(path, text, exc)) from None
    cfg = _merge(DEFAULTS, raw)
    cfg["_config_path"] = str(path) if path.exists() else "(defaults)"
    # A relative path in the config is relative to *that config*, not to this
    # source file. It is the difference between a config you can move out of
    # the repo and one that only pretends to be movable.
    cfg["_config_dir"] = (path.parent.resolve() if path.exists() else SKILL_DIR)
    cfg["secrets_path"] = _secrets_dir(cfg)

    cfg["telegram"]["user_id"] = _resolve_user_id(cfg, path)
    cfg["workspace_path"] = _abs(cfg["workspace"])
    cfg["state_path"] = _abs(cfg["state_dir"])
    return cfg


def _json_complaint(path: Path, text: str, exc: json.JSONDecodeError) -> str:
    """Point at the line, and name the likely cause.

    This config is a long annotated file that people edit by hand, and JSON has
    no comments and no forgiveness about commas. "Expecting ',' delimiter: line
    19" is technically a complete description and practically a puzzle, because
    the *missing* comma is at the end of the line before the one it names —
    that is where the parser was still happy. Showing both lines turns a
    five-minute hunt into a glance.
    """
    lines = text.splitlines()
    out = [f"{path} is not valid JSON.", f"  {exc.msg}, line {exc.lineno}:", ""]
    for number in (exc.lineno - 1, exc.lineno):
        if 1 <= number <= len(lines):
            marker = "->" if number == exc.lineno else "  "
            out.append(f"  {marker} {number:>3} | {lines[number - 1].rstrip()[:100]}")
    out.append("")
    if "delimiter" in exc.msg:
        out.append("  A missing comma at the end of the line above the arrow is "
                   "the usual cause.")
    else:
        out.append("  Watch for a trailing comma before a closing brace, or a "
                   "quote that never closed.")
    return "\n".join(out)


def _abs(value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else (REPO_ROOT / p)


def _secrets_dir(cfg: dict) -> Path | None:
    value = (cfg.get("secrets_dir") or "").strip()
    if not value:
        return None
    return Path(value).expanduser() if Path(value).expanduser().is_absolute() \
        else (REPO_ROOT / value)


def warnings(cfg: dict) -> list:
    """Things worth saying out loud at startup, none of them fatal.

    Kept separate from loading so the checks can be read as a list and tested
    as one, and so a permissions grumble never stops a bot from starting.
    """
    out = []
    secrets = cfg.get("secrets_path")
    if secrets and inside(REPO_ROOT, secrets):
        out.append(f"secrets_dir ({secrets}) is inside the repo — anything with "
                   f"read access to this checkout can see the token")
    for label, path in (("token", locate(cfg, cfg["telegram"].get("token_file"))),
                        ("user id", locate(cfg, cfg["telegram"].get("user_id_file")))):
        if path and path.exists():
            try:
                mode = path.stat().st_mode & 0o077
            except OSError:
                continue
            if mode:
                out.append(f"the {label} file {path} is readable by others "
                           f"(chmod 600 it)")
    return out


def locate(cfg: dict, name: str | None) -> Path | None:
    """Where a named secret actually lives, given this config.

    Three arrangements, one rule — the first of these that exists wins:

      1. an absolute path, used exactly as written;
      2. `secrets_dir/<basename>`, when secrets_dir is set — which is how the
         token and the user id get to live outside the repo entirely, so that
         an agent with read access to this checkout has nothing to find;
      3. the path relative to the config file's own directory (the default:
         `tgbot/secrets/telegram.token` beside `tgbot/config.json`).

    Returns the first candidate that exists, or the last one tried, so the
    error message can name where it looked.
    """
    if not name:
        return None
    p = Path(name).expanduser()
    if p.is_absolute():
        return p
    candidates = []
    if cfg.get("secrets_path"):
        candidates.append(cfg["secrets_path"] / p.name)
    candidates.append(Path(cfg.get("_config_dir", SKILL_DIR)) / p)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[-1]


def _read_secret(path: Path | None) -> str:
    if not path or not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _resolve_user_id(cfg: dict, config_path: Path) -> int:
    """Who the bot serves: a file outside the repo, the config, or the env.

    The id is not a credential — knowing it lets nobody message as you — but it
    identifies a real account, so it gets the same escape hatch as the token
    rather than being pinned inside a checkout other tools can read.
    """
    tg = cfg["telegram"]
    raw = _read_secret(locate(cfg, tg.get("user_id_file")))
    if not raw and tg.get("user_id"):
        raw = str(tg["user_id"])
    if not raw and tg.get("user_id_env"):
        raw = os.environ.get(tg["user_id_env"], "").strip()

    try:
        user_id = int(raw)
    except ValueError:
        raise ConfigError(
            f"telegram user id {raw!r} is not a number. It is the numeric id "
            f"@userinfobot gives you, not your @username.") from None
    if not user_id:
        where = locate(cfg, tg.get("user_id_file"))
        raise ConfigError(
            f"no telegram user id.\n"
            f"  This bot serves exactly one person and refuses to start without\n"
            f"  knowing who. Message @userinfobot on Telegram to get your\n"
            f"  numeric id, then put it in any one of:\n"
            f"    - telegram.user_id in {config_path}\n"
            f"    - {where}\n"
            f"    - ${tg.get('user_id_env')}")
    return user_id


def resolve_token(cfg: dict) -> str:
    """The token file first, then the environment. Never inline in config.

    Same order as opds-server's password and graded-reader's API key, for the
    same reason: a config file gets pasted into a chat window one day, and a
    path is harmless where a token is not.
    """
    tg = cfg.get("telegram", {})
    token = _read_secret(locate(cfg, tg.get("token_file")))
    if token:
        return token
    env = tg.get("token_env")
    if env and os.environ.get(env):
        return os.environ[env].strip()
    raise ConfigError(
        f"no bot token. Put it in {locate(cfg, tg.get('token_file'))} "
        f"(chmod 600) or set ${tg.get('token_env')}. "
        "@BotFather issues it.")


def inside(root: Path, candidate: Path) -> bool:
    """Is `candidate` really under `root`, symlinks and ../ resolved?

    The bot's whole destructive surface is confined this way. Written as a
    predicate rather than a try/except so the call sites read as the rule they
    are enforcing.
    """
    try:
        root = root.resolve()
        candidate = candidate.resolve()
    except OSError:
        return False
    return root == candidate or root in candidate.parents


def safe_join(root: Path, *parts: str) -> Path:
    """Join under `root`, or raise. Names from chat are never trusted."""
    candidate = root.joinpath(*parts)
    if not inside(root, candidate):
        raise ConfigError(f"refusing a path outside {root}: {candidate}")
    return candidate
