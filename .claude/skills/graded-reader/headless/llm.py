#!/usr/bin/env python3
"""Minimal OpenAI-compatible chat client — the swappable LLM seam.

The pipeline's three model steps (scribe, rework, glossary-editor) all go
through here, so the *driver* is pluggable: point `base_url` at NVIDIA NIM, a
local Ollama / vLLM server, or any OpenAI-compatible endpoint and the rest of
the pipeline is unchanged. (Claude Code, when it drives the loop itself via
SKILL.md, simply *is* the model and bypasses this file.)

No third-party dependency: a chat completion is one HTTPS POST, done with the
standard library so there's nothing extra to install and nothing to break.

Config (see config.example.json):
  base_url      OpenAI-compatible root, e.g. https://integrate.api.nvidia.com/v1
  model         model id, e.g. qwen/qwen3.5-397b-a17b
  api_key_file  path (relative to this headless/ dir) to a gitignored file
                holding the key; falls back to the api_key_env environment var.
  api_key_env   env var name to read the key from if the file is absent.
  temperature, max_tokens, timeout  generation params.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Optional

# config.json + secrets/ live here in headless/, next to this module.
SKILL_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = SKILL_DIR / "config.json"


class LLMError(RuntimeError):
    pass


def load_config(path: Optional[Path] = None) -> Dict:
    path = path or DEFAULT_CONFIG
    if not path.exists():
        raise LLMError(
            f"no config at {path}. Copy config.example.json to config.json and "
            f"set base_url + model, then drop your key in the api_key_file."
        )
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg.setdefault("base_url", "https://integrate.api.nvidia.com/v1")
    cfg.setdefault("model", "qwen/qwen3.5-397b-a17b")
    cfg.setdefault("api_key_file", "secrets/nim.key")
    cfg.setdefault("api_key_env", "NVIDIA_API_KEY")
    cfg.setdefault("temperature", 0.7)
    cfg.setdefault("max_tokens", 2048)
    cfg.setdefault("timeout", 180)
    cfg["_config_path"] = str(path)
    return cfg


def resolve_key(cfg: Dict) -> str:
    """Key file (gitignored) first, then env var. Never inline in config."""
    rel = cfg.get("api_key_file")
    if rel:
        key_path = (SKILL_DIR / rel) if not os.path.isabs(rel) else Path(rel)
        if key_path.exists():
            key = key_path.read_text(encoding="utf-8").strip()
            if key:
                return key
    env = cfg.get("api_key_env")
    if env and os.environ.get(env):
        return os.environ[env].strip()
    raise LLMError(
        f"no API key. Put it in {cfg.get('api_key_file')} (under the skill dir) "
        f"or set ${cfg.get('api_key_env')}."
    )


def _strip_reasoning(text: str) -> str:
    """Drop <think>...</think> blocks some reasoning models prepend."""
    while "<think>" in text and "</think>" in text:
        a = text.index("<think>")
        b = text.index("</think>") + len("</think>")
        text = text[:a] + text[b:]
    return text.strip()


def chat(system: str, user: str, cfg: Dict, *, retries: int = 3) -> str:
    """One chat completion. Returns the assistant message content (cleaned)."""
    key = resolve_key(cfg)
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": cfg["temperature"],
        "max_tokens": cfg["max_tokens"],
    }
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    last_err: Optional[Exception] = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=cfg["timeout"]) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            content = body["choices"][0]["message"]["content"]
            return _strip_reasoning(content)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            last_err = LLMError(f"HTTP {e.code} from {url}: {detail}")
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise last_err
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = LLMError(f"network error to {url}: {e}")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise last_err
    raise last_err or LLMError("chat failed")


def main(argv=None) -> int:
    """Tiny smoke test: llm.py "your prompt" — prints the completion."""
    import sys
    try:
        cfg = load_config()
        prompt = " ".join(argv or sys.argv[1:]) or "Say hi in one short sentence."
        print(chat("You are a helpful assistant.", prompt, cfg))
    except LLMError as e:
        print(f"llm: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
