#!/usr/bin/env python3
"""Configuration — local, gitignored, and optional.

`config.json` sits next to this script's parent directory (`tools/opds-server/`) and
is gitignored, as is `secrets/`; `config.example.json` is the committed copy to
crib from. Everything has a working default, so the server runs with no config
at all: it serves the builder's output folder on port 6737, open on the LAN.

The credential seam mirrors `ai-tools/graded-reader/headless/` so there is one
habit in this repo, not two: the secret is never in the config file, only a
*path* to a gitignored file holding it, with an environment variable as the
fallback.

Auth is off unless a username *and* a password both resolve — that is not our
rule, it is CrossPoint's: `HttpDownloader` sends Basic credentials only when
both are non-empty, so a half-configured pair would lock the device out of a
catalog it can still see.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List, Optional

SERVER_DIR = Path(__file__).resolve().parent.parent   # tools/opds-server/
# Two levels up, because this unit lives under tools/. It used to be one,
# and when opds-server/ became tools/opds-server/ the default library root
# quietly became tools/workspace — a directory that does not exist, so the
# catalog served nothing and said nothing.
REPO_ROOT = SERVER_DIR.parents[1]
DEFAULT_CONFIG = SERVER_DIR / "config.json"

DEFAULTS: Dict = {
    "library_roots": ["workspace"],
    # inbox/ is staging: Telegram deliberately leaves a new-series upload
    # there while it waits for the alias reply, and a rejected upload stays
    # there for inspection. Neither state means "add this to the catalog".
    "exclude": ["*-DIAGNOSTIC.epub", "inbox/*"],
    "host": "0.0.0.0",
    # 6737 is "OPDS" on a phone keypad. Chosen mostly for what it is *not*:
    # 8080 is the most contested port on any machine — dev servers, proxies and
    # other ebook servers all reach for it first. Above 1024 so no root, below
    # 32768 so the kernel's ephemeral range never steals it.
    "port": 6737,
    "page_size": 25,
    "catalog_title": "X3 suite library",
    "public_url": "",
    "require_auth": False,
    "auth": {
        "username": "",
        "password_file": "secrets/opds.password",
        "password_env": "X3_OPDS_PASSWORD",
    },
}


class ConfigError(RuntimeError):
    pass


def _resolve_root(entry: str) -> Path:
    """Relative roots resolve against the repo root, so `"workspace"` means the
    builder's output regardless of where the server was started from."""
    path = Path(entry).expanduser()
    return path if path.is_absolute() else (REPO_ROOT / path)


def load_config(path: Optional[Path] = None) -> Dict:
    """Defaults, overlaid with config.json if it exists. Keys beginning with
    `_` are comments (the example file is full of them) and are ignored."""
    path = Path(path) if path else DEFAULT_CONFIG
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
    cfg["_config_path"] = str(path) if path.exists() else None

    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as e:
            raise ConfigError(f"{path} is not valid JSON: {e}") from e
        if not isinstance(loaded, dict):
            raise ConfigError(f"{path} must contain a JSON object")
        for key, value in loaded.items():
            if key.startswith("_"):
                continue
            if key == "auth" and isinstance(value, dict):
                cfg["auth"].update({k: v for k, v in value.items() if not k.startswith("_")})
            else:
                cfg[key] = value

    roots: List[Path] = [_resolve_root(str(r)) for r in cfg["library_roots"]]
    if not roots:
        raise ConfigError("library_roots is empty — nothing to serve")
    cfg["library_roots"] = roots
    cfg["exclude"] = [str(p) for p in cfg["exclude"]]
    if "inbox/*" not in cfg["exclude"]:
        cfg["exclude"].append("inbox/*")
    cfg["port"] = int(cfg["port"])
    cfg["page_size"] = max(1, int(cfg["page_size"]))
    cfg["public_url"] = str(cfg["public_url"]).rstrip("/")
    return cfg


def resolve_password(cfg: Dict) -> Optional[str]:
    """Gitignored key file first, then the environment. Never inline in config."""
    auth = cfg.get("auth", {})
    rel = auth.get("password_file")
    if rel:
        key_path = Path(rel)
        if not key_path.is_absolute():
            key_path = SERVER_DIR / key_path
        if key_path.exists():
            password = key_path.read_text(encoding="utf-8").strip()
            if password:
                return password
    env = auth.get("password_env")
    if env and os.environ.get(env):
        return os.environ[env].strip()
    return None


def resolve_auth(cfg: Dict) -> Optional[tuple]:
    """(username, password) if Basic auth is fully configured, else None.

    Raises ConfigError when `require_auth` is set but the pair is incomplete —
    fail closed only when the operator asked for closed.
    """
    username = str(cfg.get("auth", {}).get("username", "")).strip()
    password = resolve_password(cfg)

    if username and password:
        return (username, password)

    if cfg.get("require_auth"):
        auth = cfg.get("auth", {})
        missing = "username" if not username else "password"
        raise ConfigError(
            f"require_auth is set but the {missing} is missing. Set "
            f"auth.username in {DEFAULT_CONFIG}, and put the password in "
            f"{SERVER_DIR / auth.get('password_file', 'secrets/opds.password')} "
            f"or ${auth.get('password_env', 'X3_OPDS_PASSWORD')}."
        )
    return None
