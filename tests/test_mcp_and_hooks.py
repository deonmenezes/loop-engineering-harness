"""MCP client routing and lifecycle hooks in a generated harness (hermetic:
the MCP server is a stdlib subprocess written to tmp, not a network call)."""
import textwrap

import pytest

from harness_builder.builder.scaffold import scaffold
from harness_builder.core.spec import AgentSpec, HarnessSpec

from conftest import load_generated_app

MCP_SERVER = textwrap.dedent('''
    import json, sys
    def send(o): sys.stdout.write(json.dumps(o)+"\\n"); sys.stdout.flush()
    for line in sys.stdin:
        line=line.strip()
        if not line: continue
        m=json.loads(line); method=m.get("method"); i=m.get("id")
        if method=="initialize":
            send({"jsonrpc":"2.0","id":i,"result":{"protocolVersion":"2024-11-05","capabilities":{},"serverInfo":{"name":"t","version":"1"}}})
        elif method=="notifications/initialized": pass
        elif method=="tools/list":
            send({"jsonrpc":"2.0","id":i,"result":{"tools":[{"name":"echo","description":"echo upper","inputSchema":{"type":"object","properties":{"text":{"type":"string"}}}}]}})
        elif method=="tools/call":
            a=m.get("params",{}).get("arguments",{})
            send({"jsonrpc":"2.0","id":i,"result":{"content":[{"type":"text","text":str(a.get("text","")).upper()}]}})
        else:
            send({"jsonrpc":"2.0","id":i,"error":{"code":-32601,"message":"nope"}})
''')


def _app_with_mcp(tmp_path):
    server = tmp_path / "mcp_server.py"
    server.write_text(MCP_SERVER)
    spec = HarnessSpec(name="mcpdemo", description="d", pattern="pipeline",
                       command="md",
                       agents=[AgentSpec(name="a", role="r", system_prompt="s",
                                         tools=["mcp:t"])],
                       flow=["a"])
    d = scaffold(spec, tmp_path / "mcpdemo")
    import json
    (d / "mcp.json").write_text(json.dumps(
        [{"name": "t", "command": "python3", "args": [str(server)]}]))
    return load_generated_app(d / "app.py")


def test_mcp_connect_list_and_call(tmp_path):
    app = _app_with_mcp(tmp_path)
    app.MCP.connect()
    try:
        assert "t" in app.MCP.clients and not app.MCP.errors
        names = [s["name"] for s in app.MCP.tools_for(["mcp:t"])]
        assert names == ["t__echo"]
        assert app.MCP.owns("t__echo")
        out, err = app.execute_tool("t__echo", {"text": "hi"})
        assert not err and out == "HI"
    finally:
        app.MCP.close()


def test_mcp_unconfigured_is_clean(tmp_path):
    spec = HarnessSpec(name="nomcp", description="d", pattern="pipeline",
                       command="nm",
                       agents=[AgentSpec(name="a", role="r", system_prompt="s")],
                       flow=["a"])
    d = scaffold(spec, tmp_path / "nomcp")
    app = load_generated_app(d / "app.py")
    assert app.MCP.server_configs() == []
    assert not app.MCP.owns("x__y")


def test_hooks_fire_from_dir_and_config(tmp_path):
    spec = HarnessSpec(name="hookdemo", description="d", pattern="pipeline",
                       command="hd",
                       agents=[AgentSpec(name="a", role="r", system_prompt="s")],
                       flow=["a"])
    d = scaffold(spec, tmp_path / "hookdemo")
    hook = d / "hooks" / "pre_run"
    hook.write_text("#!/usr/bin/env bash\ncat >/dev/null\necho FIRED $1\n")
    hook.chmod(0o755)
    app = load_generated_app(d / "app.py")
    notes = []
    app.BUS.set(lambda k, kw: notes.append(kw.get("text", ""))
                if k == "note" else None)
    app.run_hook("pre_run", {"task": "x"})
    app.run_hook("post_run", {"task": "x"})  # no hook -> silent
    fired = [n for n in notes if "hook[" in n]
    assert any("FIRED pre_run" in n for n in fired)
    assert not any("post_run" in n for n in fired)


def test_hooks_absent_is_noop(tmp_path):
    spec = HarnessSpec(name="nohook", description="d", pattern="pipeline",
                       command="nh",
                       agents=[AgentSpec(name="a", role="r", system_prompt="s")],
                       flow=["a"])
    d = scaffold(spec, tmp_path / "nohook")
    # remove the example so the dir has no active hooks
    app = load_generated_app(d / "app.py")
    app.BUS.set(lambda k, kw: None)
    app.run_hook("pre_run", {"task": "x"})  # must not raise
