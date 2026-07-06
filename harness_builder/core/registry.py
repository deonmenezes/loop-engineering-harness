"""
Global harness registry — ~/.harness/registry.json.

Every scaffold, activate, and install records the harness here, so `harness`
launched from ANY directory can list and open the whole fleet, not just
./harnesses relative to the cwd. Entries whose directories vanished are
pruned on read.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

REG_PATH = Path.home() / ".harness" / "registry.json"


def _load() -> dict:
    try:
        data = json.loads(REG_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def register(name: str, path: str | Path, *, command: str = "",
             pattern: str = "", description: str = ""):
    data = _load()
    h = data.setdefault("harnesses", {})
    h[name] = {"path": str(Path(path).resolve()),
               "command": command or name, "pattern": pattern,
               "description": " ".join(description.split())[:200],
               "registered": datetime.now(timezone.utc).isoformat()}
    REG_PATH.parent.mkdir(exist_ok=True)
    REG_PATH.write_text(json.dumps(data, indent=1))


def entries() -> list[dict]:
    """Registered harnesses that still exist on disk, name-sorted."""
    data = _load()
    h = data.get("harnesses", {})
    live, dead = [], []
    for name, e in sorted(h.items()):
        if Path(e.get("path", "")).is_dir():
            live.append({"name": name, **e})
        else:
            dead.append(name)
    if dead:
        for name in dead:
            h.pop(name, None)
        try:
            REG_PATH.write_text(json.dumps(data, indent=1))
        except OSError:
            pass
    return live
