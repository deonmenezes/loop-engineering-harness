"""The autonomous engines in a generated harness — goal loop, /improve
(metaprompting), /uploop — driven hermetically via a fake in-process provider
that answers each control prompt by inspecting its system text."""
import pytest

from harness_builder.builder.scaffold import scaffold
from harness_builder.core.spec import AgentSpec, EvalSpec, HarnessSpec

from conftest import load_generated_app


def _make_app(tmp_path, app_module_target, with_criteria=False):
    ev = EvalSpec(quality_criteria=["every claim cites a source"],
                  pass_threshold=7.0) if with_criteria else EvalSpec()
    spec = HarnessSpec(
        name="autodemo", description="d", pattern="pipeline", command="ad",
        agents=[AgentSpec(name="researcher", role="researches",
                          system_prompt="You research.", tools=["web_search"],
                          skills=["research_skill"], output_format="facts")],
        flow=["researcher"], eval=ev)
    d = scaffold(spec, tmp_path / "autodemo")
    (d / "skills" / "research_skill.md").write_text("Original skill.")
    app = load_generated_app(d / "app.py")
    _install_fake_provider(app)
    return app


def _install_fake_provider(app):
    """Route every model call to an in-process fake keyed on system text."""
    calls = {"quality": 0}

    class Fake:
        def chat(self, model, system, messages, tools=None, max_tokens=8000,
                 on_delta=None):
            import json
            if "planner for an autonomous agent loop" in system:
                text = json.dumps({"steps": [
                    {"title": "research", "details": "gather",
                     "done_when": "facts"},
                    {"title": "draft", "details": "write", "done_when": "draft"}]})
            elif "strict per-step gate" in system:
                text = json.dumps({"passed": True, "note": "done"})
            elif "strict quality judge" in system:
                calls["quality"] += 1
                score = 4.0 if calls["quality"] == 1 else 9.0
                text = json.dumps({"score": score, "feedback": "be specific"})
            elif "prompt engineer improving ONE agent" in system:
                text = json.dumps({"agent": "researcher", "file": "skill",
                                   "content": "UPGRADED SKILL: verify + cite.",
                                   "rationale": "added checklist"})
            elif "harness upgrader" in system:
                text = json.dumps({
                    "upgrade_agents": [{"name": "researcher",
                                        "skill": "DEEPER skill.",
                                        "add_tools": ["plan"]}],
                    "new_agents": [{"name": "fact_checker", "role": "verifies",
                                    "identity": "You verify.",
                                    "skill": "Cross-check claims.",
                                    "output_format": "PASS/FAIL",
                                    "tools": ["web_search"], "model": "x/y"}],
                    "add_quality_criteria": ["claims verified"],
                    "summary": "deepened + added fact_checker"})
            else:
                text = "# Deliverable\n\nMock output with facts."
            if on_delta and text:
                on_delta(text)
            return app.Reply(text, [], "end", 10, 10,
                             {"role": "assistant", "content": text})

        def prune(self, messages, keep=4):
            return messages

        def tool_results(self, results):
            return []

    app.resolve = lambda model_string: (Fake(), "model")
    app.BUS.set(lambda k, kw: None)


def test_goal_loop_completes_and_persists(tmp_path):
    app = _make_app(tmp_path, "goal")
    res = app.goal_loop("write a report", max_cycles=6)
    assert res["ok"] is True
    assert all(s["status"] == "done" for s in res["state"]["steps"])
    assert list((app.HERE / "goal_state").glob("*.json")), "state not saved"


def test_goal_loop_resumes_from_state(tmp_path):
    app = _make_app(tmp_path, "goal")
    app.goal_loop("resumable goal", max_cycles=6)
    # second call with same objective loads the completed state -> instant done
    res2 = app.goal_loop("resumable goal", max_cycles=6)
    assert res2["ok"] is True


def test_improve_rewrites_prompt_until_pass(tmp_path):
    app = _make_app(tmp_path, "improve", with_criteria=True)
    skill = app.HERE / "skills" / "research_skill.md"
    before = skill.read_text()
    res = app.improve_prompts("rigorous cited report", max_rounds=3)
    assert res["score"] >= 7.0
    assert skill.read_text() != before
    assert "UPGRADED" in skill.read_text()
    assert res["changed"] == ["skills/research_skill.md"]
    assert list((app.HERE / "upgrades").glob("*/skills/*.md")), "no backup"


def test_improve_noop_without_criteria(tmp_path):
    app = _make_app(tmp_path, "improve", with_criteria=False)
    res = app.improve_prompts("anything", max_rounds=2)
    assert res["changed"] == []  # nothing to gate on -> no rewrite


def test_uploop_upgrades_segments_and_adds_agent(tmp_path):
    app = _make_app(tmp_path, "uploop")
    n_before = len(app.CFG["agents"])
    app.uploop("", rounds=1)
    assert len(app.CFG["agents"]) == n_before + 1
    assert (app.HERE / "prompts" / "fact_checker.md").exists()
    assert "claims verified" in app.CFG["eval"]["quality_criteria"]
    assert "plan" in app.CFG["agents"][0]["tools"]  # add_tools applied
    assert list((app.HERE / "upgrades").glob("*/harness.json")), "no backup"
