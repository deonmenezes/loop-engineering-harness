"""HarnessSpec validation + command handling."""
import pytest

from harness_builder.core.spec import AgentSpec, HarnessSpec


def _spec(**kw):
    base = dict(
        name="demo", description="d", pattern="pipeline",
        agents=[AgentSpec(name="a", role="r", system_prompt="s"),
                AgentSpec(name="b", role="r", system_prompt="s")],
        flow=["a", "b"])
    base.update(kw)
    return HarnessSpec(**base)


def test_valid_command_accepted():
    s = _spec(command="youvid")
    s.validate()  # no raise


@pytest.mark.parametrize("bad", ["YouVid", "you vid", "1cmd", "way_too_long_command_name_here", "-x"])
def test_bad_command_rejected(bad):
    with pytest.raises(ValueError):
        _spec(command=bad).validate()


def test_blank_command_ok():
    _spec(command="").validate()


def test_unknown_pattern_rejected():
    with pytest.raises(ValueError):
        _spec(pattern="nonsense").validate()


def test_duplicate_agent_names_rejected():
    with pytest.raises(ValueError):
        _spec(agents=[AgentSpec(name="a", role="r", system_prompt="s"),
                      AgentSpec(name="a", role="r", system_prompt="s")]).validate()


def test_supervisor_pattern_needs_valid_supervisor():
    with pytest.raises(ValueError):
        _spec(pattern="supervisor", supervisor="ghost", flow=[]).validate()
    _spec(pattern="supervisor", supervisor="a", flow=[]).validate()


def test_flow_references_must_exist():
    with pytest.raises(ValueError):
        _spec(flow=["a", "missing"]).validate()


def test_from_dict_roundtrip_preserves_command():
    d = {"name": "x", "description": "", "pattern": "pipeline",
         "command": "zap", "flow": [],
         "agents": [{"name": "a", "role": "r", "system_prompt": "s"}]}
    s = HarnessSpec.from_dict(d)
    assert s.command == "zap"
    assert s.to_dict()["command"] == "zap"
