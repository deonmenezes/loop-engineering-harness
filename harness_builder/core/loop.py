"""
The inner agent loop — one agent, one task, until done or a guardrail fires.
Provider-agnostic: works identically over Anthropic, OpenAI, Groq, Ollama, ...
because it only speaks the normalized ChatResponse dialect.

Everything inside a run is ephemeral (architecture diagram: "AI Agent Run").
Durable state exits the run only via memory tools and the returned reply.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..providers import api
from . import tools as toolreg


@dataclass
class RunResult:
    reply: str
    turns: int
    input_tokens: int = 0
    output_tokens: int = 0
    stopped_by: str = "end_turn"     # end_turn | max_turns | token_budget | wall_clock
    tool_calls: int = 0


def run_agent(*, agent_spec, system: str, task: str, ctx: toolreg.ToolContext,
              guardrails, trace=None, token_budget_used: int = 0) -> RunResult:
    provider, model = api.resolve(agent_spec.model)
    builtin = [t for t in agent_spec.tools if not t.startswith("mcp:")]
    tool_schemas = toolreg.schemas_for(builtin) if builtin else []
    if ctx.mcp_pool is not None:
        tool_schemas += ctx.mcp_pool.tools_for(agent_spec.tools)
    tool_schemas = tool_schemas or None
    messages = [{"role": "user", "content": task}]
    result = RunResult(reply="", turns=0)
    started = time.monotonic()

    for turn in range(agent_spec.max_turns):
        # ---- guardrails checked BEFORE every model call (limits live in code)
        total_tokens = token_budget_used + result.input_tokens + result.output_tokens
        if total_tokens > guardrails.max_total_tokens:
            result.stopped_by = "token_budget"
            break
        if time.monotonic() - started > guardrails.max_wall_seconds:
            result.stopped_by = "wall_clock"
            break

        # context management: old tool outputs are stale ballast — prune them
        messages = provider.prune_tool_results(messages, keep_last=4)
        resp = provider.chat(model=model, system=system, messages=messages,
                             tools=tool_schemas)
        result.turns = turn + 1
        result.input_tokens += resp.input_tokens
        result.output_tokens += resp.output_tokens
        if trace:
            trace.log("model_turn", agent=agent_spec.name, turn=turn,
                      model=agent_spec.model, stop_reason=resp.stop_reason,
                      in_tokens=resp.input_tokens, out_tokens=resp.output_tokens)

        messages.append(resp.raw_assistant_message)

        if resp.stop_reason != "tool_use":
            result.reply = resp.text
            result.stopped_by = "end_turn"
            return result

        # ---- execute requested tools; errors flow back as feedback
        results = []
        for call in resp.tool_calls:
            if trace:
                trace.log("tool_call", agent=agent_spec.name, tool=call.name,
                          input=call.input)
            out, is_error = toolreg.execute(ctx, call.name, call.input)
            result.tool_calls += 1
            if trace:
                trace.log("tool_result", agent=agent_spec.name, tool=call.name,
                          is_error=is_error, result=out[:400])
            results.append({"id": call.id, "content": out, "is_error": is_error})

        wrapped = provider.tool_result_message(results)
        # Anthropic returns one dict; OpenAI dialect returns a list of dicts
        messages.extend(wrapped if isinstance(wrapped, list) else [wrapped])
    else:
        result.stopped_by = "max_turns"

    result.reply = result.reply or f"(stopped: {result.stopped_by})"
    return result
