#!/usr/bin/env python3
"""Configuration, and where the bot is allowed to put its hands.

The same seam `opds-server` and `services/graded-reader/headless` use: an
annotated `config.example.json`, a gitignored `config.json` beside it, and the
secret itself never in either — only a path to a gitignored file holding it,
with an environment variable as the fallback.

Everything has a working default except the one thing that cannot have one:
`telegram.user_id`. A bot that answers everybody is not a bot with a default,
it is a bot with a hole, so the loader refuses to start without it.
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
        "poll_timeout": 30,
    },
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
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path} is not valid JSON: {exc}") from exc
    cfg = _merge(DEFAULTS, raw)
    cfg["_config_path"] = str(path) if path.exists() else "(defaults)"

    if not cfg["telegram"].get("user_id"):
        raise ConfigError(
            f"no telegram.user_id in {path}.\n"
            "  This bot serves exactly one person and refuses to start without\n"
            "  knowing who. Message @userinfobot on Telegram to get your\n"
            "  numeric id, then copy config.example.json to config.json and\n"
            "  put it in telegram.user_id.")

    cfg["workspace_path"] = _abs(cfg["workspace"])
    cfg["state_path"] = _abs(cfg["state_dir"])
    return cfg


def _abs(value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else (REPO_ROOT / p)


def resolve_token(cfg: dict) -> str:
    """Key file (gitignored) first, then env var. Never inline in config.

    Same order as opds-server's password and graded-reader's API key, for the
    same reason: a config file gets pasted into a chat window one day, and a
    path is harmless where a token is not.
    """
    tg = cfg.get("telegram", {})
    rel = tg.get("token_file")
    if rel:
        p = Path(rel)
        token_path = p if p.is_absolute() else (SKILL_DIR / p)
        if token_path.exists():
            token = token_path.read_text(encoding="utf-8").strip()
            if token:
                return token
    env = tg.get("token_env")
    if env and os.environ.get(env):
        return os.environ[env].strip()
    raise ConfigError(
        f"no bot token. Put it in tgbot/{tg.get('token_file')} "
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
