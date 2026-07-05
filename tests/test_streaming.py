"""The generated providers' streaming path accumulates deltas + tool calls
correctly. Hermetic: we monkeypatch _post_stream to yield canned SSE events."""
import pytest

from harness_builder.builder.scaffold import scaffold
from harness_builder.core.spec import AgentSpec, HarnessSpec

from conftest import load_generated_app


@pytest.fixture
def app(tmp_path):
    spec = HarnessSpec(name="strtest", description="s", pattern="pipeline",
                       command="st",
                       agents=[AgentSpec(name="a", role="r", system_prompt="s")],
                       flow=["a"])
    d = scaffold(spec, tmp_path / "strtest")
    return load_generated_app(d / "app.py")


def test_openai_stream_accumulates_text(app, monkeypatch):
    events = [
        {"choices": [{"delta": {"content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo "}}]},
        {"choices": [{"delta": {"content": "world"}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 3}},
    ]
    monkeypatch.setattr(app, "_post_stream", lambda *a, **k: iter(events))
    prov = app.OpenAICompat.__new__(app.OpenAICompat)
    prov.base, prov.key = "http://x/v1", "k"
    chunks = []
    reply = prov.chat("m", "sys", [{"role": "user", "content": "hi"}],
                      on_delta=chunks.append)
    assert "".join(chunks) == "Hello world"
    assert reply.text == "Hello world"
    assert reply.stop == "end"
    assert reply.out_tok == 3


def test_openai_stream_reassembles_tool_call(app, monkeypatch):
    events = [
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c1", "function": {"name": "read_file",
                                                  "arguments": ""}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '{"path":'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '"f.py"}'}}]},
            "finish_reason": "tool_calls"}]},
    ]
    monkeypatch.setattr(app, "_post_stream", lambda *a, **k: iter(events))
    prov = app.OpenAICompat.__new__(app.OpenAICompat)
    prov.base, prov.key = "http://x/v1", "k"
    reply = prov.chat("m", "sys", [{"role": "user", "content": "hi"}],
                      tools=None, on_delta=lambda c: None)
    assert reply.stop == "tool_use"
    assert len(reply.tool_calls) == 1
    call = reply.tool_calls[0]
    assert call["name"] == "read_file"
    assert call["input"] == {"path": "f.py"}


def test_anthropic_stream_text_and_tool(app, monkeypatch):
    events = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 7}}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": "Thinking. "}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1,
         "content_block": {"type": "tool_use", "id": "t1", "name": "list_files"}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "input_json_delta", "partial_json": "{}"}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
         "usage": {"output_tokens": 11}},
    ]
    monkeypatch.setattr(app, "_post_stream", lambda *a, **k: iter(events))
    prov = app.Anthropic.__new__(app.Anthropic)
    prov.base, prov.mode, prov.secret, prov.source = "http://x", "api_key", "k", "t"
    chunks = []
    reply = prov.chat("m", "sys", [{"role": "user", "content": "hi"}],
                      on_delta=chunks.append)
    assert "".join(chunks) == "Thinking. "
    assert reply.stop == "tool_use"
    assert reply.tool_calls[0]["name"] == "list_files"
    assert reply.in_tok == 7 and reply.out_tok == 11


def test_stream_falls_back_on_error(app, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("stream down")
    monkeypatch.setattr(app, "_post_stream", boom)
    monkeypatch.setattr(app, "_post", lambda *a, **k: {
        "choices": [{"message": {"role": "assistant", "content": "fallback"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
    prov = app.OpenAICompat.__new__(app.OpenAICompat)
    prov.base, prov.key = "http://x/v1", "k"
    chunks = []
    reply = prov.chat("m", "sys", [{"role": "user", "content": "hi"}],
                      on_delta=chunks.append)
    assert reply.text == "fallback"
    assert "".join(chunks) == "fallback"  # fallback still feeds on_delta once
