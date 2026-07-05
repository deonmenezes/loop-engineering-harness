"""Credential auto-detection in a generated harness: env vars + local CLI
logins (Claude Code OAuth, Codex), priority order, and OAuth header shaping.
Hermetic — writes fake credential files into a tmp HOME, no real keys."""
import json

import pytest

from harness_builder.builder.scaffold import scaffold
from harness_builder.core.spec import AgentSpec, HarnessSpec

from conftest import load_generated_app


@pytest.fixture
def app(tmp_path):
    spec = HarnessSpec(name="authdemo", description="d", pattern="pipeline",
                       command="au",
                       agents=[AgentSpec(name="a", role="r", system_prompt="s")],
                       flow=["a"])
    d = scaffold(spec, tmp_path / "authdemo")
    return load_generated_app(d / "app.py")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
              "ANTHROPIC_OAUTH_TOKEN", "OPENAI_API_KEY", "GROQ_API_KEY",
              "OPENROUTER_API_KEY"):
        monkeypatch.delenv(k, raising=False)


def test_anthropic_env_key_wins(app, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
    assert app.discover_anthropic() == ("api_key", "sk-ant-env",
                                        "ANTHROPIC_API_KEY env")


def test_anthropic_oauth_env_token(app, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-tok")
    mode, secret, _ = app.discover_anthropic()
    assert mode == "oauth" and secret == "oauth-tok"


def test_anthropic_from_claude_code_login(app, monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text(json.dumps(
        {"claudeAiOauth": {"accessToken": "local-oauth"}}))
    monkeypatch.setenv("HOME", str(home))
    mode, secret, source = app.discover_anthropic()
    assert mode == "oauth" and secret == "local-oauth"
    assert "Claude Code" in source


def test_openai_from_codex_login(app, monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text(json.dumps(
        {"OPENAI_API_KEY": "sk-codex"}))
    monkeypatch.setenv("HOME", str(home))
    key, source = app.discover_key("openai")
    assert key == "sk-codex" and "codex" in source


def test_none_when_nothing_present(app, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "empty"))
    assert app.discover_anthropic() == (None, None, None)
    assert app.discover_key("openai") == (None, None)


def test_oauth_headers_use_bearer_not_apikey(app, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    prov = app.Anthropic()
    h = prov._headers()
    assert h["Authorization"] == "Bearer tok"
    assert h["anthropic-beta"] == "oauth-2025-04-20"
    assert "x-api-key" not in h


def test_apikey_headers_use_x_api_key(app, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    prov = app.Anthropic()
    h = prov._headers()
    assert h["x-api-key"] == "sk-ant"
    assert "Authorization" not in h


def test_missing_auth_raises_helpful_error(app, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "empty"))
    with pytest.raises(RuntimeError, match="no Anthropic auth"):
        app.Anthropic()


def test_auth_status_reports_detection(app, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    st = app.auth_status()
    assert "anthropic" in st
    ok, src = st["anthropic"]
    assert ok and "ANTHROPIC_API_KEY" in src
    assert app.missing_keys() == [] or all("anthropic" not in m
                                           for m in app.missing_keys())
