"""Domain detection routes prompts to the right playbook."""
import pytest

from harness_builder.builder import playbooks


@pytest.mark.parametrize("prompt,expected", [
    ("build a coding agent that reviews pull requests and writes tests", "coding"),
    ("a higgsfield-style AI video generation pipeline for trailers", "video"),
    ("deep research harness with cited sources", "research"),
    ("write a fantasy novel with an editor", "writing"),
    ("ETL pipeline for CSV data with validation", "data"),
    ("design an accessible landing page with react components", "design"),
    ("plan a marketing campaign for a product launch", "marketing"),
])
def test_primary_domain(prompt, expected):
    _, domains = playbooks.guidance_for(prompt)
    assert domains[0] == expected


def test_unmatched_falls_back_to_general():
    guidance, domains = playbooks.guidance_for("help me tidy my sock drawer")
    assert domains == ["general"]
    assert "general" in guidance.lower()


def test_guidance_is_substantial():
    for pb in playbooks.PLAYBOOKS:
        assert len(pb.guidance) > 200, pb.name
        assert pb.keywords


def test_coding_guidance_mentions_tools():
    guidance, _ = playbooks.guidance_for("refactor this python codebase")
    assert "apply_patch" in guidance or "run_shell" in guidance


def test_video_guidance_mentions_generation():
    guidance, _ = playbooks.guidance_for("cinematic text-to-video shots")
    assert "generate_video" in guidance
