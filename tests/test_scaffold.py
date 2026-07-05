"""Scaffolding a spec produces a complete, self-consistent standalone app."""
import json
import py_compile
import stat

import pytest

from harness_builder.builder.scaffold import (PI_THEMES, derive_command,
                                              scaffold)
from harness_builder.core.spec import AgentSpec, HarnessSpec


@pytest.mark.parametrize("name,expected", [
    ("youtube_content", "youcon"),
    ("mantis", "mantis"),
    ("deep_research", "deeres"),
    ("code_review", "codrev"),
])
def test_derive_command(name, expected):
    assert derive_command(name) == expected


def _demo_spec():
    return HarnessSpec(
        name="demo_harness", description="A demo.", pattern="pipeline",
        command="demo",
        agents=[AgentSpec(name="writer", role="writes", system_prompt="You write.",
                          tools=["write_file", "apply_patch"], skills=["w"]),
                AgentSpec(name="editor", role="edits", system_prompt="You edit.",
                          tools=["read_file"], skills=[])],
        flow=["writer", "editor"])


@pytest.fixture
def built(tmp_path):
    return scaffold(_demo_spec(), tmp_path / "demo_harness")


def test_core_files_written(built):
    for f in ["app.py", "harness.json", "README.md", "install.sh",
              ".env.example", ".gitignore"]:
        assert (built / f).exists(), f


def test_launcher_named_after_command_and_executable(built):
    launcher = built / "demo"
    assert launcher.exists()
    assert launcher.stat().st_mode & stat.S_IXUSR


def test_app_compiles(built):
    py_compile.compile(str(built / "app.py"), doraise=True)


def test_themes_cloned(built):
    for tname in PI_THEMES:
        f = built / "themes" / f"{tname}.json"
        assert f.exists()
        data = json.loads(f.read_text())
        assert "colors" in data and "accent" in data["colors"]


def test_harness_json_has_command(built):
    cfg = json.loads((built / "harness.json").read_text())
    assert cfg["command"] == "demo"
    assert cfg["pattern"] == "pipeline"


def test_harness_json_has_mcp_and_loop(built):
    cfg = json.loads((built / "harness.json").read_text())
    assert "mcp_servers" in cfg
    assert "loop" in cfg and "max_cycles" in cfg["loop"]


def test_mcp_and_hooks_scaffolded(built):
    assert (built / "mcp.json.example").exists()
    assert (built / "hooks" / "README.md").exists()
    assert (built / "PRD.md.example").exists()


def test_autonomous_engines_and_mcp_in_app(built):
    src = (built / "app.py").read_text()
    for marker in ["def goal_loop", "def improve_prompts", "def uploop",
                   "def run_hook", "class MCPPool", "def ensure_mcp"]:
        assert marker in src, marker
    for cmd in ['"/goal"', '"/improve"', '"/uploop"', '"/mcp"']:
        assert cmd in src, cmd


def test_streaming_code_present(built):
    src = (built / "app.py").read_text()
    assert "_post_stream" in src
    assert "_chat_stream" in src
    assert "text_delta" in src
    assert "stream_start" in src


def test_pi_tui_present(built):
    src = (built / "app.py").read_text()
    assert "class PiTUI" in src
    assert "SPINNER_FRAMES" in src
    assert "1049h" in src  # alternate screen


def test_new_tools_present(built):
    src = (built / "app.py").read_text()
    for t in ["apply_patch", "python_exec", "plan", "generate_image",
              "generate_video", "http_get"]:
        assert f'"{t}"' in src, t


def test_prompts_and_anatomy(built):
    assert (built / "prompts" / "ANATOMY.md").exists()
    assert (built / "prompts" / "writer.md").exists()
    anatomy = (built / "prompts" / "ANATOMY.md").read_text()
    for slot in ["{{IDENTITY}}", "{{ENVIRONMENT}}", "{{BEHAVIORAL_RULES}}",
                 "{{OUTPUT_FORMAT}}", "{{SAFETY}}"]:
        assert slot in anatomy


def test_rescaffold_preserves_prompt_edits(built):
    pf = built / "prompts" / "writer.md"
    pf.write_text("HUMAN EDITED IDENTITY")
    scaffold(_demo_spec(), built)  # re-scaffold same harness
    assert pf.read_text() == "HUMAN EDITED IDENTITY"
