"""
MCP client — lets harness agents use tools from ANY external MCP server
(filesystem, GitHub, Slack, databases, whatever you have installed).

harness.yaml:
    mcp_servers:
      - name: fs
        command: npx
        args: ["-y", "@modelcontextprotocol/server-filesystem", "/data"]

    agents:
      - name: researcher
        tools: [web_search, "mcp:fs"]     # <- exposes ALL of that server's
                                           #    tools to this agent as fs__<tool>

Protocol: JSON-RPC 2.0 over stdio (MCP spec 2024-11-05):
initialize -> notifications/initialized -> tools/list -> tools/call.
"""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path


class MCPClient:
    def __init__(self, name: str, command: str, args: list[str] | None = None,
                 cwd: str | None = None):
        self.name = name
        self.proc = subprocess.Popen(
            [command] + (args or []),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, cwd=cwd, bufsize=1)
        self._id = 0
        self._lock = threading.Lock()
        self._initialize()

    # ------------------------------------------------------------ jsonrpc
    def _send(self, method: str, params: dict | None = None,
              notification: bool = False) -> dict | None:
        with self._lock:
            msg: dict = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                msg["params"] = params
            if not notification:
                self._id += 1
                msg["id"] = self._id
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()
            if notification:
                return None
            # read lines until the matching response id (skip server notifications)
            while True:
                line = self.proc.stdout.readline()
                if not line:
                    raise RuntimeError(f"MCP server '{self.name}' closed stdout")
                try:
                    resp = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if resp.get("id") == self._id:
                    if "error" in resp:
                        raise RuntimeError(f"MCP error from '{self.name}': "
                                           f"{resp['error']}")
                    return resp.get("result", {})

    def _initialize(self):
        self._send("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "harness-builder", "version": "0.1.0"}})
        self._send("notifications/initialized", {}, notification=True)

    # -------------------------------------------------------------- tools
    def list_tools(self) -> list[dict]:
        """Neutral schemas, names prefixed '<server>__<tool>'."""
        result = self._send("tools/list", {})
        out = []
        for t in result.get("tools", []):
            out.append({
                "name": f"{self.name}__{t['name']}",
                "description": f"[MCP:{self.name}] {t.get('description', '')}"[:1000],
                "parameters": t.get("inputSchema",
                                    {"type": "object", "properties": {}}),
            })
        return out

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        """tool_name WITHOUT the server prefix."""
        result = self._send("tools/call",
                            {"name": tool_name, "arguments": arguments})
        parts = []
        for c in result.get("content", []):
            if c.get("type") == "text":
                parts.append(c.get("text", ""))
            else:
                parts.append(json.dumps(c))
        if result.get("isError"):
            return "MCP TOOL ERROR: " + "\n".join(parts)
        return "\n".join(parts) or "(empty result)"

    def close(self):
        try:
            self.proc.terminate()
        except Exception:
            pass


class MCPPool:
    """Connects the servers a spec declares; routes prefixed tool calls."""

    def __init__(self, mcp_servers: list[dict], base_dir: Path | None = None):
        self.clients: dict[str, MCPClient] = {}
        for cfg in mcp_servers or []:
            try:
                self.clients[cfg["name"]] = MCPClient(
                    cfg["name"], cfg["command"], cfg.get("args"),
                    cwd=str(base_dir) if base_dir else None)
                print(f"  [mcp] connected: {cfg['name']} "
                      f"({len(self.clients[cfg['name']].list_tools())} tools)")
            except Exception as e:
                print(f"  [mcp] FAILED to connect '{cfg.get('name')}': {e}")

    def tools_for(self, agent_tool_list: list[str]) -> list[dict]:
        """Expand 'mcp:<server>' entries into that server's tool schemas."""
        schemas = []
        for entry in agent_tool_list:
            if entry.startswith("mcp:"):
                server = entry[4:]
                if server in self.clients:
                    schemas.extend(self.clients[server].list_tools())
        return schemas

    def call(self, prefixed_name: str, arguments: dict) -> tuple[str, bool]:
        server, _, tool = prefixed_name.partition("__")
        if server not in self.clients:
            return f"no MCP server '{server}' connected", True
        try:
            return self.clients[server].call_tool(tool, arguments), False
        except Exception as e:
            return f"{type(e).__name__}: {e}", True

    def owns(self, tool_name: str) -> bool:
        server = tool_name.split("__")[0]
        return "__" in tool_name and server in self.clients

    def close(self):
        for c in self.clients.values():
            c.close()
