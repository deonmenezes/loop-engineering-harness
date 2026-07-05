"""The tools embedded in a generated harness actually work (hermetic:
apply_patch/python_exec/plan/generate_video need no network)."""
import pytest

from harness_builder.builder.scaffold import scaffold
from harness_builder.core.spec import AgentSpec, HarnessSpec

from conftest import load_generated_app


@pytest.fixture
def app(tmp_path):
    spec = HarnessSpec(
        name="tooltest", description="t", pattern="pipeline", command="tt",
        agents=[AgentSpec(name="a", role="r", system_prompt="s",
                          tools=["write_file", "apply_patch", "python_exec",
                                 "plan", "generate_video"])],
        flow=["a"])
    d = scaffold(spec, tmp_path / "tooltest")
    mod = load_generated_app(d / "app.py")
    mod.WORKSPACE.mkdir(parents=True, exist_ok=True)
    return mod


def test_write_then_patch(app):
    out, err = app.execute_tool("write_file",
                                {"path": "f.py", "content": "x = 1\n"})
    assert not err
    out, err = app.execute_tool("apply_patch",
                                {"path": "f.py", "find": "x = 1",
                                 "replace": "x = 99"})
    assert not err
    assert (app.WORKSPACE / "f.py").read_text() == "x = 99\n"


def test_patch_missing_snippet_is_feedback_not_crash(app):
    app.execute_tool("write_file", {"path": "g.py", "content": "a\n"})
    out, err = app.execute_tool("apply_patch",
                                {"path": "g.py", "find": "zzz", "replace": "b"})
    assert not err  # returned as guidance text, not an exception
    assert "not present" in out


def test_patch_ambiguous_requires_context(app):
    app.execute_tool("write_file", {"path": "h.py", "content": "z\nz\n"})
    out, _ = app.execute_tool("apply_patch",
                              {"path": "h.py", "find": "z", "replace": "y"})
    assert "appears" in out and "unique" in out
    out, _ = app.execute_tool("apply_patch",
                              {"path": "h.py", "find": "z", "replace": "y",
                               "replace_all": True})
    assert (app.WORKSPACE / "h.py").read_text() == "y\ny\n"


def test_python_exec(app):
    out, err = app.execute_tool("python_exec", {"code": "print(6*7)"})
    assert not err
    assert "42" in out


def test_plan_persists(app):
    out, err = app.execute_tool("plan", {"plan": "- [ ] one\n- [ ] two"})
    assert not err
    assert (app.WORKSPACE / "PLAN.md").exists()


def test_generate_video_fallback_writes_spec(app, monkeypatch):
    monkeypatch.delenv("VIDEO_API_URL", raising=False)
    out, err = app.execute_tool("generate_video",
                                {"prompt": "neon alley", "camera": "dolly-in"})
    assert not err
    assert "video_prompt.txt" in out
    assert (app.WORKSPACE / "video_prompt.txt").exists()


def test_path_escape_blocked(app):
    out, err = app.execute_tool("write_file",
                                {"path": "../escape.txt", "content": "x"})
    assert err  # ValueError surfaced as tool error
