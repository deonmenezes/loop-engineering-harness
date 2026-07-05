"""Every bundled template loads, has a friendly command, and scaffolds to a
compiling standalone app."""
import py_compile

import pytest

from harness_builder.builder.scaffold import derive_command, scaffold
from harness_builder.core.spec import HarnessSpec
from conftest import ROOT

TEMPLATES = sorted(p for p in (ROOT / "templates").iterdir()
                   if (p / "harness.yaml").exists())


@pytest.mark.parametrize("tdir", TEMPLATES, ids=lambda p: p.name)
def test_template_loads_and_scaffolds(tdir, tmp_path):
    spec = HarnessSpec.load(tdir)
    spec.validate()
    d = scaffold(spec, tmp_path / spec.name)
    py_compile.compile(str(d / "app.py"), doraise=True)
    command = spec.command or derive_command(spec.name)
    assert (d / command).exists(), f"launcher {command} missing"


def test_all_templates_have_command():
    for tdir in TEMPLATES:
        spec = HarnessSpec.load(tdir)
        assert spec.command, f"{spec.name} has no friendly command"
        spec.validate()


def test_templates_cover_expected_domains():
    names = {p.name for p in TEMPLATES}
    # sanity: the bundled set spans coding, research, video-ish, writing, data
    assert {"code_review", "deep_research", "youtube_content",
            "webtoon_production", "data_pipeline"} <= names
