"""
Local credential auto-detection for the BUILDER itself (the generated
standalone harnesses carry their own copy of this in scaffold.py).

Reuse the LLM logins already on this computer instead of pasting keys. Each
provider resolves from, in order: its env var, then config files written by the
official CLIs. Anthropic additionally supports OAuth: a CLAUDE_CODE_OAUTH_TOKEN
(from `claude setup-token`) or the token the Claude Code CLI stored locally —
used as a Bearer token, subject to provider terms; an API key always works.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _read_json(path) -> dict | None:
    try:
        data = json.loads(Path(path).expanduser().read_text())
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _keychain(service: str) -> str | None:
    if sys.platform != "darwin":
        return None
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", service,
                            "-w"], capture_output=True, text=True, timeout=5)
        out = r.stdout.strip()
        return out if r.returncode == 0 and out else None
    except Exception:
        return None


def _dig(d: dict | None, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k):
            return d[k]
    return None


def discover_anthropic() -> tuple[str | None, str | None, str | None]:
    """(mode, secret, source): mode in {'api_key', 'oauth', None}."""
    k = os.environ.get("ANTHROPIC_API_KEY")
    if k:
        return "api_key", k, "ANTHROPIC_API_KEY env"
    t = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") \
        or os.environ.get("ANTHROPIC_OAUTH_TOKEN")
    if t:
        return "oauth", t, "CLAUDE_CODE_OAUTH_TOKEN env"
    raw = _keychain("Claude Code-credentials")
    data = None
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = None
    data = data or _read_json("~/.claude/.credentials.json") \
        or _read_json("~/.config/claude/.credentials.json")
    if isinstance(data, dict):
        tok = _dig(data.get("claudeAiOauth") or {}, "accessToken", "access_token")
        if tok:
            return "oauth", tok, "Claude Code login"
        ak = _dig(data, "apiKey", "api_key", "ANTHROPIC_API_KEY")
        if ak:
            return "api_key", ak, "~/.claude"
    return None, None, None


def discover_key(provider: str) -> tuple[str | None, str | None]:
    """(api_key, source) for openai/groq/openrouter from env or CLI configs."""
    env = {"openai": "OPENAI_API_KEY", "groq": "GROQ_API_KEY",
           "openrouter": "OPENROUTER_API_KEY"}.get(provider)
    if env and os.environ.get(env):
        return os.environ[env], f"{env} env"
    if provider == "openai":
        for path in ("~/.codex/auth.json", "~/.config/openai/auth.json"):
            data = _read_json(path)
            key = _dig(data, "OPENAI_API_KEY", "api_key", "apiKey")
            if key:
                return key, path
    if provider == "openrouter":
        key = _dig(_read_json("~/.config/openrouter/config.json"), "api_key")
        if key:
            return key, "~/.config/openrouter"
    return None, None


# providers this builder can reach with what's currently on the machine
_ALL = ("anthropic", "openai", "groq", "openrouter", "ollama")


def available() -> dict[str, tuple[bool, str]]:
    """provider -> (detected, source-or-hint). ollama is always 'available'."""
    out: dict[str, tuple[bool, str]] = {}
    mode, _, src = discover_anthropic()
    out["anthropic"] = (bool(mode), (f"{src} ({mode})" if mode else
                        "set ANTHROPIC_API_KEY or run `claude setup-token`"))
    for p in ("openai", "groq", "openrouter"):
        key, src = discover_key(p)
        env = {"openai": "OPENAI_API_KEY", "groq": "GROQ_API_KEY",
               "openrouter": "OPENROUTER_API_KEY"}[p]
        out[p] = (bool(key), src or f"set {env}")
    out["ollama"] = (True, "local (no key needed)")
    return out


def any_available() -> bool:
    return any(ok for p, (ok, _) in available().items() if p != "ollama")
