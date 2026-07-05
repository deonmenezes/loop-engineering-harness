"""Shared fixtures. Tests are hermetic — no network, no API keys."""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def root():
    return ROOT


def load_generated_app(app_path: Path):
    """Import a scaffolded harness's app.py as an isolated module."""
    name = f"genapp_{abs(hash(str(app_path)))}"
    spec = importlib.util.spec_from_file_location(name, app_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod
