"""
Unified multi-provider LLM API (inspired by opencode + @earendil-works/pi-ai).

One interface, many providers. Model strings route automatically:

    anthropic/claude-sonnet-4-6
    openai/gpt-4o
    groq/llama-3.3-70b-versatile
    openrouter/anthropic/claude-sonnet-4-6
    ollama/llama3.1

Every provider normalizes to the same response shape (ChatResponse), so the
agent loop, orchestrator, and ralph loop never care which model is running.
This is the CORE abstraction that makes harnesses model-agnostic.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Transient-error retry — rate limits (429), overloaded (529), 5xx, timeouts.
# Both SDKs retry a couple of times internally; that is not enough when a big
# request trips a tokens-per-minute window, so we back off up to ~1 minute and
# honor the server's retry-after when it sends one.
# ---------------------------------------------------------------------------
RETRY_STATUS = {429, 500, 502, 503, 529}
RETRY_NAMES = {"APIConnectionError", "APITimeoutError", "InternalServerError",
               "OverloadedError",
               # raw httpx/httpcore transport errors (Codex provider streams
               # over httpx directly) — "connection reset by peer" & friends
               "ConnectError", "ConnectTimeout", "ReadError", "ReadTimeout",
               "WriteError", "PoolTimeout", "RemoteProtocolError"}


def _retry_wait(exc: Exception, attempt: int) -> float:
    resp = getattr(exc, "response", None)
    retry_after = getattr(getattr(resp, "headers", None), "get", lambda k: None)(
        "retry-after")
    if retry_after:
        try:
            return min(float(retry_after) + 1, 120)
        except ValueError:
            pass
    return min(5 * 2 ** attempt, 60)


def call_with_retries(fn, *, attempts: int = 5):
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as e:
            status = getattr(e, "status_code", None)
            retryable = (status in RETRY_STATUS
                         or (status is None and type(e).__name__ in RETRY_NAMES))
            if not retryable or attempt == attempts - 1:
                raise
            wait = _retry_wait(e, attempt)
            print(f"  ⏳ {type(e).__name__}"
                  + (f" ({status})" if status else "")
                  + f" — retrying in {wait:.0f}s ({attempt + 1}/{attempts - 1})")
            time.sleep(wait)


# ---------------------------------------------------------------------------
# Normalized response shape — the whole system speaks only this dialect.
# ---------------------------------------------------------------------------
@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class ChatResponse:
    text: str                          # concatenated assistant text
    tool_calls: list[ToolCall]         # requested tool invocations
    stop_reason: str                   # "end_turn" | "tool_use" | "max_tokens" | "error"
    input_tokens: int = 0
    output_tokens: int = 0
    raw_assistant_message: Any = None  # provider-native message, replayed verbatim


class Provider:
    """Interface every provider implements. on_token, when given, receives
    text deltas as they stream (providers without streaming may ignore it)."""

    def chat(self, *, model: str, system: str, messages: list,
             tools: list[dict] | None = None, max_tokens: int = 4096,
             on_token=None) -> ChatResponse:
        raise NotImplementedError

    def tool_result_message(self, results: list[dict]) -> dict:
        """Wrap executed tool results as the next message in provider dialect.
        Each result: {"id": tool_call_id, "content": str, "is_error": bool}"""
        raise NotImplementedError

    PRUNED = "[old tool result pruned to save context]"

    def prune_tool_results(self, messages: list, keep_last: int = 4) -> list:
        """Context management: replace all but the newest N tool results with
        a placeholder. The model's own text (its working notes) is never
        touched — agents are told results may vanish, so they extract what
        they need into their own messages as they go."""
        return messages


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------
class AnthropicProvider(Provider):
    def __init__(self):
        import anthropic

        from ..core import auth
        base = os.environ.get("ANTHROPIC_BASE_URL")
        mode, secret, self.source = auth.discover_anthropic()
        kw = {}
        if base:
            kw["base_url"] = base
        if mode == "oauth":
            # SDK sends Authorization: Bearer <token> when auth_token is set;
            # the oauth beta header opts the request into token auth.
            kw["auth_token"] = secret
            kw["default_headers"] = {"anthropic-beta": "oauth-2025-04-20"}
        elif mode == "api_key":
            kw["api_key"] = secret
        elif not base:
            raise RuntimeError(
                "no Anthropic auth found — set ANTHROPIC_API_KEY, run "
                "`claude setup-token` for an OAuth token, log in with the "
                "Claude Code CLI, or set ANTHROPIC_BASE_URL for a gateway")
        self.client = anthropic.Anthropic(**kw)

    def _stream_once(self, kwargs: dict, on_token):
        with self.client.messages.stream(**kwargs) as s:
            for t in s.text_stream:
                on_token(t)
            return s.get_final_message()

    def chat(self, *, model, system, messages, tools=None, max_tokens=4096,
             on_token=None) -> ChatResponse:
        kwargs = dict(model=model, system=system, messages=messages, max_tokens=max_tokens)
        if tools:
            kwargs["tools"] = [
                {"name": t["name"], "description": t["description"],
                 "input_schema": t["parameters"]}
                for t in tools
            ]
        if on_token:
            resp = call_with_retries(lambda: self._stream_once(kwargs, on_token))
        else:
            resp = call_with_retries(lambda: self.client.messages.create(**kwargs))
        text = "".join(b.text for b in resp.content if b.type == "text")
        calls = [ToolCall(id=b.id, name=b.name, input=b.input)
                 for b in resp.content if b.type == "tool_use"]
        return ChatResponse(
            text=text, tool_calls=calls,
            stop_reason="tool_use" if calls else "end_turn",
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            raw_assistant_message={"role": "assistant", "content": resp.content},
        )

    def tool_result_message(self, results):
        return {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": r["id"],
             "content": r["content"], "is_error": r.get("is_error", False)}
            for r in results
        ]}

    def prune_tool_results(self, messages, keep_last=4):
        locs = []
        for mi, m in enumerate(messages):
            if isinstance(m, dict) and m.get("role") == "user" \
               and isinstance(m.get("content"), list):
                for bi, b in enumerate(m["content"]):
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        locs.append((mi, bi))
        for mi, bi in locs[:-keep_last] if keep_last else locs:
            b = messages[mi]["content"][bi]
            if b.get("content") != self.PRUNED:
                b["content"] = self.PRUNED
        return messages


# ---------------------------------------------------------------------------
# Codex — the ChatGPT-login OAuth backend the Codex CLI uses. NOT the OpenAI
# API: different host, Responses dialect, SSE-only, Bearer token + account id.
# Lets `codex/gpt-5.5` style models run off an existing `codex login`.
# ---------------------------------------------------------------------------
class CodexError(Exception):
    def __init__(self, status_code: int, message: str, response=None):
        super().__init__(f"Codex backend {status_code}: {message[:400]}")
        self.status_code = status_code
        self.response = response


class CodexProvider(Provider):
    URL = "https://chatgpt.com/backend-api/codex/responses"

    def __init__(self):
        from ..core import auth
        self._auth = auth
        data, self.auth_path, self.source = auth.discover_codex()
        if not data:
            raise RuntimeError(
                "no Codex login found — run `codex login`, or set "
                "OPENAI_API_KEY to use the plain openai provider instead")
        self.tokens = data["tokens"]

    def _headers(self) -> dict:
        import uuid
        return {
            "Authorization": f"Bearer {self.tokens['access_token']}",
            "chatgpt-account-id": self.tokens["account_id"],
            "OpenAI-Beta": "responses=experimental",
            "originator": "codex_cli_rs",
            "accept": "text/event-stream",
            "session_id": str(uuid.uuid4()),
        }

    def _to_input(self, messages: list) -> list:
        """Chat-style messages -> Responses input items. Items we produced
        earlier (raw assistant turns, function_call_output) pass through."""
        items: list = []
        for m in messages:
            if not isinstance(m, dict):
                continue
            if "_codex_items" in m:
                items.extend(m["_codex_items"])
            elif "type" in m:
                items.append(m)
            else:
                role = m.get("role", "user")
                kind = "output_text" if role == "assistant" else "input_text"
                items.append({"type": "message", "role": role,
                              "content": [{"type": kind,
                                           "text": str(m.get("content", ""))}]})
        return items

    def _once(self, body: dict, on_token=None) -> dict:
        import json as _json

        import httpx

        # This backend sends the real items as response.output_item.done
        # events and leaves response.completed's `output` array EMPTY, so we
        # accumulate items off the stream and only take usage from completed.
        items: list = []
        with httpx.Client(timeout=httpx.Timeout(600, connect=30)) as client:
            with client.stream("POST", self.URL, headers=self._headers(),
                               json=body) as r:
                if r.status_code != 200:
                    raise CodexError(r.status_code,
                                     r.read().decode("utf-8", "replace"), r)
                for line in r.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        ev = _json.loads(payload)
                    except _json.JSONDecodeError:
                        continue
                    etype = ev.get("type")
                    if etype == "response.output_text.delta" and on_token:
                        on_token(ev.get("delta", ""))
                    elif etype == "response.output_item.done":
                        items.append(ev.get("item") or {})
                    elif etype == "response.completed":
                        resp = ev.get("response") or {}
                        if not resp.get("output"):
                            resp["output"] = items
                        return resp
                    elif etype in ("response.failed", "error"):
                        err = (ev.get("response", {}) or {}).get("error") \
                            or ev.get("error") or {}
                        raise CodexError(502, str(err.get("message") or err))
        raise CodexError(502, "stream ended without response.completed")

    def _request(self, body: dict, on_token=None) -> dict:
        try:
            return self._once(body, on_token)
        except CodexError as e:
            if e.status_code == 401:
                new = self._auth.refresh_codex(self.auth_path)
                if new:
                    self.tokens = new
                    return self._once(body, on_token)
            raise

    def chat(self, *, model, system, messages, tools=None, max_tokens=4096,
             on_token=None) -> ChatResponse:
        import json as _json
        if not model:
            model = self._auth.codex_default_model()
        body = {
            "model": model,
            "instructions": system,
            "input": self._to_input(messages),
            "tools": [{"type": "function", "name": t["name"],
                       "description": t["description"],
                       "parameters": t["parameters"], "strict": False}
                      for t in (tools or [])],
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "store": False,
            "stream": True,
            "include": ["reasoning.encrypted_content"],
            "reasoning": {"effort": os.environ.get("CODEX_REASONING_EFFORT",
                                                   "medium")},
        }
        resp = call_with_retries(lambda: self._request(body, on_token))

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        raw_items: list[dict] = []
        for item in resp.get("output", []):
            t = item.get("type")
            if t == "message":
                raw_items.append(item)
                text_parts += [c.get("text", "") for c in item.get("content", [])
                               if c.get("type") == "output_text"]
            elif t == "function_call":
                raw_items.append(item)
                try:
                    args = _json.loads(item.get("arguments") or "{}")
                except _json.JSONDecodeError:
                    args = {}
                calls.append(ToolCall(id=item.get("call_id") or item.get("id"),
                                      name=item["name"], input=args))
            elif t == "reasoning":
                raw_items.append(item)   # must be replayed with store=false
        usage = resp.get("usage") or {}
        return ChatResponse(
            text="".join(text_parts), tool_calls=calls,
            stop_reason="tool_use" if calls else "end_turn",
            input_tokens=usage.get("input_tokens", 0) or 0,
            output_tokens=usage.get("output_tokens", 0) or 0,
            raw_assistant_message={"role": "assistant",
                                   "_codex_items": raw_items},
        )

    def tool_result_message(self, results):
        # Responses dialect: one function_call_output item per result; the
        # loop flattens this list (see core/loop.py).
        return [{"type": "function_call_output", "call_id": r["id"],
                 "output": r["content"]} for r in results]

    def prune_tool_results(self, messages, keep_last=4):
        locs = [i for i, m in enumerate(messages)
                if isinstance(m, dict)
                and m.get("type") == "function_call_output"]
        for i in locs[:-keep_last] if keep_last else locs:
            if messages[i].get("output") != self.PRUNED:
                messages[i]["output"] = self.PRUNED
        return messages


# ---------------------------------------------------------------------------
# OpenAI-compatible — covers OpenAI, Groq, OpenRouter, Ollama, Azure-ish, etc.
# One class, different base_url + api_key. This is how opencode supports
# a dozen providers without a dozen integrations.
# ---------------------------------------------------------------------------
class OpenAICompatProvider(Provider):
    def __init__(self, api_key_env: str, base_url: str | None = None):
        from openai import OpenAI

        from ..core import auth
        provider = {"OPENAI_API_KEY": "openai", "GROQ_API_KEY": "groq",
                    "OPENROUTER_API_KEY": "openrouter"}.get(api_key_env)
        if provider:
            key, self.source = auth.discover_key(provider)
            key = key or os.environ.get(api_key_env)
        else:  # ollama — any non-empty key works
            key, self.source = os.environ.get(api_key_env, "ollama"), "local"
        self.client = OpenAI(api_key=key or "ollama", base_url=base_url)

    def chat(self, *, model, system, messages, tools=None, max_tokens=4096,
             on_token=None) -> ChatResponse:
        # on_token accepted for interface parity; this path doesn't stream
        import json
        full = [{"role": "system", "content": system}] + messages
        kwargs = dict(model=model, messages=full, max_tokens=max_tokens)
        if tools:
            kwargs["tools"] = [
                {"type": "function",
                 "function": {"name": t["name"], "description": t["description"],
                              "parameters": t["parameters"]}}
                for t in tools
            ]
        resp = call_with_retries(
            lambda: self.client.chat.completions.create(**kwargs))
        msg = resp.choices[0].message
        calls = [ToolCall(id=tc.id, name=tc.function.name,
                          input=json.loads(tc.function.arguments or "{}"))
                 for tc in (msg.tool_calls or [])]
        usage = resp.usage
        return ChatResponse(
            text=msg.content or "", tool_calls=calls,
            stop_reason="tool_use" if calls else "end_turn",
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            raw_assistant_message={
                "role": "assistant", "content": msg.content,
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name,
                                  "arguments": tc.function.arguments}}
                    for tc in (msg.tool_calls or [])
                ] or None,
            },
        )

    def tool_result_message(self, results):
        # OpenAI dialect: one 'tool' message per result. We return a LIST here;
        # the loop flattens it (see core/loop.py).
        return [{"role": "tool", "tool_call_id": r["id"], "content": r["content"]}
                for r in results]

    def prune_tool_results(self, messages, keep_last=4):
        locs = [i for i, m in enumerate(messages)
                if isinstance(m, dict) and m.get("role") == "tool"]
        for i in locs[:-keep_last] if keep_last else locs:
            if messages[i].get("content") != self.PRUNED:
                messages[i]["content"] = self.PRUNED
        return messages


# ---------------------------------------------------------------------------
# Routing — "provider/model" strings, opencode style.
# ---------------------------------------------------------------------------
_REGISTRY: dict[str, tuple] = {
    "anthropic":  (AnthropicProvider, {}),
    "codex":      (CodexProvider, {}),
    "openai":     (OpenAICompatProvider, {"api_key_env": "OPENAI_API_KEY"}),
    "groq":       (OpenAICompatProvider, {"api_key_env": "GROQ_API_KEY",
                                          "base_url": "https://api.groq.com/openai/v1"}),
    "openrouter": (OpenAICompatProvider, {"api_key_env": "OPENROUTER_API_KEY",
                                          "base_url": "https://openrouter.ai/api/v1"}),
    "ollama":     (OpenAICompatProvider, {"api_key_env": "OLLAMA_API_KEY",
                                          "base_url": os.environ.get(
                                              "OLLAMA_BASE_URL", "http://localhost:11434/v1")}),
}
_instances: dict[str, Provider] = {}


def resolve(model_string: str) -> tuple[Provider, str]:
    """'anthropic/claude-sonnet-4-6' -> (AnthropicProvider instance, 'claude-sonnet-4-6')
       'openrouter/anthropic/claude-sonnet-4-6' -> (..., 'anthropic/claude-sonnet-4-6')"""
    prefix, _, model = model_string.partition("/")
    if prefix not in _REGISTRY:
        raise ValueError(
            f"Unknown provider '{prefix}'. Known: {', '.join(_REGISTRY)}. "
            f"Use 'provider/model', e.g. 'anthropic/claude-sonnet-4-6'.")
    if prefix not in _instances:
        cls, kw = _REGISTRY[prefix]
        _instances[prefix] = cls(**kw)
    return _instances[prefix], model


# ---------------------------------------------------------------------------
# Cross-provider fallback — when one login's rate limit is exhausted even
# after the retry ladder, finish the work on another detected login
# (anthropic <-> codex) instead of dying. A provider that 429s out goes into
# a cooldown window so subsequent calls skip straight to the alternate.
# ---------------------------------------------------------------------------
_COOLDOWN: dict[str, float] = {}          # provider prefix -> down-until (unix)
FALLBACK_COOLDOWN = 180.0


def mark_rate_limited(model_string: str):
    _COOLDOWN[model_string.partition("/")[0]] = time.time() + FALLBACK_COOLDOWN


def fallback_for(model_string: str) -> str | None:
    """An alternate 'provider/model' on a DIFFERENT detected login, if any."""
    from ..core import auth
    prefix = model_string.partition("/")[0]
    avail = auth.available()

    def usable(p: str) -> bool:
        return (p != prefix and avail.get(p, (False, ""))[0]
                and _COOLDOWN.get(p, 0) <= time.time())

    if usable("codex"):
        return "codex/" + auth.codex_default_model()
    if usable("anthropic"):
        return "anthropic/claude-sonnet-4-6"
    return None


def effective_model(model_string: str) -> str:
    """The model to actually run: the requested one, unless its provider is
    cooling down from a rate limit and another login can take the run."""
    if _COOLDOWN.get(model_string.partition("/")[0], 0) > time.time():
        fb = fallback_for(model_string)
        if fb:
            print(f"  ⇄ {model_string} cooling down — using {fb}")
            return fb
    return model_string


def chat_simple(model_string: str, *, system: str, messages: list,
                max_tokens: int = 4096, on_token=None) -> ChatResponse:
    """One-shot chat for plain {'role','content'} messages (architect, critic,
    judge, router, PRD compiler) with automatic cross-provider failover."""
    ms = effective_model(model_string)
    provider, model = resolve(ms)
    try:
        return provider.chat(model=model, system=system, messages=messages,
                             max_tokens=max_tokens, on_token=on_token)
    except Exception as e:
        if getattr(e, "status_code", None) != 429:
            raise
        mark_rate_limited(ms)
        fb = fallback_for(ms)
        if not fb:
            raise
        print(f"  ⇄ {ms} rate-limited — falling back to {fb}")
        provider, model = resolve(fb)
        return provider.chat(model=model, system=system, messages=messages,
                             max_tokens=max_tokens, on_token=on_token)
