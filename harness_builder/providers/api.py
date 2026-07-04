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
from dataclasses import dataclass, field
from typing import Any


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
    """Interface every provider implements."""

    def chat(self, *, model: str, system: str, messages: list,
             tools: list[dict] | None = None, max_tokens: int = 4096) -> ChatResponse:
        raise NotImplementedError

    def tool_result_message(self, results: list[dict]) -> dict:
        """Wrap executed tool results as the next message in provider dialect.
        Each result: {"id": tool_call_id, "content": str, "is_error": bool}"""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------
class AnthropicProvider(Provider):
    def __init__(self):
        import anthropic
        self.client = anthropic.Anthropic()

    def chat(self, *, model, system, messages, tools=None, max_tokens=4096) -> ChatResponse:
        kwargs = dict(model=model, system=system, messages=messages, max_tokens=max_tokens)
        if tools:
            kwargs["tools"] = [
                {"name": t["name"], "description": t["description"],
                 "input_schema": t["parameters"]}
                for t in tools
            ]
        resp = self.client.messages.create(**kwargs)
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


# ---------------------------------------------------------------------------
# OpenAI-compatible — covers OpenAI, Groq, OpenRouter, Ollama, Azure-ish, etc.
# One class, different base_url + api_key. This is how opencode supports
# a dozen providers without a dozen integrations.
# ---------------------------------------------------------------------------
class OpenAICompatProvider(Provider):
    def __init__(self, api_key_env: str, base_url: str | None = None):
        from openai import OpenAI
        key = os.environ.get(api_key_env, "ollama")  # ollama needs any non-empty key
        self.client = OpenAI(api_key=key, base_url=base_url)

    def chat(self, *, model, system, messages, tools=None, max_tokens=4096) -> ChatResponse:
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
        resp = self.client.chat.completions.create(**kwargs)
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


# ---------------------------------------------------------------------------
# Routing — "provider/model" strings, opencode style.
# ---------------------------------------------------------------------------
_REGISTRY: dict[str, tuple] = {
    "anthropic":  (AnthropicProvider, {}),
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
