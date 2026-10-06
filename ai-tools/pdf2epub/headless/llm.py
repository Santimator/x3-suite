#!/usr/bin/env python3
"""Minimal OpenAI-compatible chat client with image input — the swappable
LLM seam of the pdf2epub runner.

The same shape as graded-reader's headless/llm.py (stdlib only, one HTTPS
POST per completion, key from a gitignored file or an env var), plus what a
page-reading pipeline needs: message lists for multi-turn rework, and user
content as a list of parts — text and `image_url` parts carrying base64 PNGs,
which OpenAI-compatible servers with vision models accept. A copy rather than
an import, so neither runner's needs bend the other's seam. (Claude Code,
driving SKILL.md itself, is the model and bypasses this file.)

Config (see config.example.json): base_url, model, api_key_file, api_key_env,
temperature, max_tokens, timeout, max_image_dim.
"""
from __future__ import annotations

import base64
import io
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "config.json"


class LLMError(RuntimeError):
    pass


def load_config(path: Optional[Path] = None) -> Dict:
    path = path or DEFAULT_CONFIG
    if not path.exists():
        raise LLMError(
            f"no config at {path}. Copy config.example.json to config.json, set "
            f"base_url + a vision-capable model + device + figures, and drop your "
            f"key in the api_key_file."
        )
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg.setdefault("api_key_file", "secrets/api.key")
    cfg.setdefault("api_key_env", "PDF2EPUB_API_KEY")
    cfg.setdefault("temperature", 0.2)
    cfg.setdefault("max_tokens", 8192)
    cfg.setdefault("timeout", 300)
    cfg.setdefault("max_image_dim", 1600)
    for key in ("base_url", "model"):
        if not cfg.get(key):
            raise LLMError(f"config {path} has no {key!r}")
    cfg["_config_path"] = str(path)
    return cfg


def resolve_key(cfg: Dict) -> str:
    """Key file (gitignored) first, then env var. Never inline in config."""
    rel = cfg.get("api_key_file")
    if rel:
        key_path = (HERE / rel) if not os.path.isabs(rel) else Path(rel)
        if key_path.exists():
            key = key_path.read_text(encoding="utf-8").strip()
            if key:
                return key
    env = cfg.get("api_key_env")
    if env and os.environ.get(env):
        return os.environ[env].strip()
    raise LLMError(f"no API key. Put it in headless/{cfg.get('api_key_file')} "
                   f"or set ${cfg.get('api_key_env')}.")


def text_part(text: str) -> Dict:
    return {"type": "text", "text": text}


def image_part(path: Path, max_dim: int) -> Dict:
    """A PNG as a base64 data URL, shrunk so its longer side is <= max_dim."""
    from PIL import Image
    img = Image.open(path)
    img.load()
    if max(img.size) > max_dim:
        s = max_dim / max(img.size)
        img = img.resize((max(1, round(img.width * s)), max(1, round(img.height * s))),
                         Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    data = base64.b64encode(buf.getvalue()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}}


def _strip_reasoning(text: str) -> str:
    """Drop <think>...</think> blocks some reasoning models prepend."""
    while "<think>" in text and "</think>" in text:
        a = text.index("<think>")
        b = text.index("</think>") + len("</think>")
        text = text[:a] + text[b:]
    return text.strip()


def chat(messages: List[Dict], cfg: Dict, *, retries: int = 3) -> str:
    """One chat completion over a full message list. Returns the reply text."""
    key = resolve_key(cfg)
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": cfg["model"],
        "messages": messages,
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
            if isinstance(content, list):   # some servers return parts
                content = "".join(p.get("text", "") for p in content)
            return _strip_reasoning(content or "")
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
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            raise LLMError(f"unexpected response from {url}: {e}") from None
    raise last_err or LLMError("chat failed")
