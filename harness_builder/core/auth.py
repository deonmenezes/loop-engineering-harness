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


CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"   # the Codex CLI's OAuth app


def discover_codex() -> tuple[dict | None, str | None, str | None]:
    """(auth_data, auth_path, source) for a ChatGPT-login Codex CLI session.

    This is OAuth against the ChatGPT Codex backend — a different animal from
    an OPENAI_API_KEY (which discover_key('openai') handles): the tokens only
    work on chatgpt.com/backend-api/codex, via providers.api.CodexProvider.
    """
    for p in ("~/.codex/auth.json",):
        data = _read_json(p)
        toks = (data or {}).get("tokens") or {}
        if toks.get("access_token") and toks.get("account_id"):
            return data, str(Path(p).expanduser()), "Codex CLI (ChatGPT login)"
    return None, None, None


def refresh_codex(auth_path: str) -> dict | None:
    """Refresh the Codex OAuth access token in place; returns the new tokens."""
    import httpx
    data = _read_json(auth_path) or {}
    toks = data.get("tokens") or {}
    if not toks.get("refresh_token"):
        return None
    try:
        r = httpx.post("https://auth.openai.com/oauth/token",
                       json={"client_id": CODEX_CLIENT_ID,
                             "grant_type": "refresh_token",
                             "refresh_token": toks["refresh_token"],
                             "scope": "openid profile email"},
                       timeout=30)
        r.raise_for_status()
    except Exception:
        return None
    new = r.json()
    toks.update({k: new[k] for k in ("access_token", "id_token", "refresh_token")
                 if new.get(k)})
    data["tokens"] = toks
    from datetime import datetime, timezone
    data["last_refresh"] = datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")
    Path(auth_path).write_text(json.dumps(data, indent=2))
    return toks


def codex_default_model() -> str:
    """The model the user's Codex CLI is configured for."""
    try:
        import tomllib
        cfg = tomllib.loads(
            Path("~/.codex/config.toml").expanduser().read_text())
        if isinstance(cfg.get("model"), str) and cfg["model"]:
            return cfg["model"]
    except Exception:
        pass
    return "gpt-5.5"


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
_ALL = ("anthropic", "codex", "openai", "groq", "openrouter", "ollama")


def available() -> dict[str, tuple[bool, str]]:
    """provider -> (detected, source-or-hint). ollama is always 'available'."""
    out: dict[str, tuple[bool, str]] = {}
    mode, _, src = discover_anthropic()
    out["anthropic"] = (bool(mode), (f"{src} ({mode})" if mode else
                        "set ANTHROPIC_API_KEY or run `claude setup-token`"))
    cdata, _, csrc = discover_codex()
    out["codex"] = (bool(cdata), csrc or "log in with the Codex CLI")
    for p in ("openai", "groq", "openrouter"):
        key, src = discover_key(p)
        env = {"openai": "OPENAI_API_KEY", "groq": "GROQ_API_KEY",
               "openrouter": "OPENROUTER_API_KEY"}[p]
        out[p] = (bool(key), src or f"set {env}")
    out["ollama"] = (True, "local (no key needed)")
    return out


def any_available() -> bool:
    return any(ok for p, (ok, _) in available().items() if p != "ollama")
