"""
MCP server — DEPLOYS the loop engineer as an MCP server over stdio, so any
MCP host (Claude Code, Claude Desktop, Codex, opencode) can drive it:

    claude mcp add loop-engineer -- python -m harness_builder.mcp.server

Exposed tools:
  build_harness(prompt)                    architect a new harness
  list_harnesses()                         what's available in ./harnesses
  run_<harness>(task)                      one tool per local harness
  loop_<harness>(goal, fresh?)             the external loop, per harness

JSON-RPC 2.0 over stdio, MCP spec 2024-11-05.
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROTOCOL = "2024-11-05"


def _harnesses() -> list[Path]:
    root = Path("harnesses")
    return sorted(p.parent for p in root.glob("*/harness.yaml")) \
        if root.exists() else []


def _tools() -> list[dict]:
    task_schema = {"type": "object", "required": ["task"],
                   "properties": {"task": {"type": "string",
                                           "description": "the task to run"}}}
    goal_schema = {"type": "object", "required": ["goal"],
                   "properties": {"goal": {"type": "string"},
                                  "fresh": {"type": "boolean",
                                            "description": "discard saved plan"}}}
    tools = [
        {"name": "build_harness",
         "description": "Design a new domain-specific agent harness from a "
                        "one-paragraph description (architect agent picks the "
                        "team pattern, writes system prompts, skills, gate).",
         "inputSchema": {"type": "object", "required": ["prompt"],
                         "properties": {"prompt": {"type": "string"}}}},
        {"name": "list_harnesses",
         "description": "List available harnesses and their team patterns.",
         "inputSchema": {"type": "object", "properties": {}}},
    ]
    from ..core.spec import HarnessSpec
    for d in _harnesses():
        try:
            spec = HarnessSpec.load(d)
        except Exception:
            continue
        tools.append({"name": f"run_{spec.name}",
                      "description": f"Run the '{spec.name}' agent team "
                                     f"({spec.pattern}) on one task. "
                                     f"{spec.description}"[:1000],
                      "inputSchema": task_schema})
        tools.append({"name": f"loop_{spec.name}",
                      "description": f"Drive '{spec.name}' through the EXTERNAL "
                                     "loop: plan the goal into a checklist, one "
                                     "team run per step, resumable.",
                      "inputSchema": goal_schema})
    return tools


def _call(name: str, args: dict) -> str:
    from ..core.spec import HarnessSpec
    buf = io.StringIO()
    with redirect_stdout(buf):          # keep stdout clean for JSON-RPC
        if name == "build_harness":
            from ..builder.architect import build_harness
            path = build_harness(args["prompt"])
            result = f"harness created at {path}\n\n{buf.getvalue()}"
        elif name == "list_harnesses":
            lines = []
            for d in _harnesses():
                s = HarnessSpec.load(d)
                lines.append(f"{s.name} [{s.pattern}] — {s.description}")
            result = "\n".join(lines) or "no harnesses yet — use build_harness"
        elif name.startswith("run_") or name.startswith("loop_"):
            hname = name.split("_", 1)[1]
            hdir = Path("harnesses") / hname
            spec = HarnessSpec.load(hdir)
            from ..runtime.orchestrator import Orchestrator
            if name.startswith("run_"):
                result = Orchestrator(spec, hdir).run(args["task"])
            else:
                from ..core.external_loop import run_external_loop
                state = run_external_loop(
                    spec=spec, harness_dir=hdir, goal=args["goal"],
                    fresh=bool(args.get("fresh")),
                    run_harness_fn=lambda t: Orchestrator(spec, hdir).run(t))
                done = sum(s.status == "done" for s in state.steps)
                result = (f"external loop: {done}/{len(state.steps)} steps "
                          f"complete\n\n{state.render()}\n\nnotes:\n"
                          + "\n".join(f"- {s.title}: {s.note}"
                                      for s in state.steps if s.note))
        else:
            raise ValueError(f"unknown tool '{name}'")
    return result


def serve():
    """stdio JSON-RPC loop. stdout carries ONLY protocol messages."""
    out = sys.stdout

    def reply(id_, result=None, error=None):
        msg = {"jsonrpc": "2.0", "id": id_}
        if error is not None:
            msg["error"] = {"code": -32000, "message": str(error)[:2000]}
        else:
            msg["result"] = result
        out.write(json.dumps(msg) + "\n")
        out.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        method, id_ = req.get("method"), req.get("id")
        try:
            if method == "initialize":
                reply(id_, {"protocolVersion": PROTOCOL,
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "loop-engineer",
                                           "version": "0.1.0"}})
            elif method == "notifications/initialized":
                continue
            elif method == "tools/list":
                reply(id_, {"tools": _tools()})
            elif method == "tools/call":
                p = req.get("params", {})
                text = _call(p.get("name", ""), p.get("arguments", {}) or {})
                reply(id_, {"content": [{"type": "text",
                                         "text": text[:100_000]}]})
            elif method == "ping":
                reply(id_, {})
            elif id_ is not None:
                reply(id_, error=f"method not supported: {method}")
        except Exception as e:
            if id_ is not None:
                reply(id_, error=e)


if __name__ == "__main__":
    serve()
