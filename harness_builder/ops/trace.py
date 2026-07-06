"""
LLM Ops: Trace (1 trace per run) + Observe (tokens, latency, errors).
Langfuse/LangSmith-shaped, file-based: append-only JSONL you can tail, grep,
or load into a dataframe. When (not if) a harness misbehaves, the trace is
how you replay exactly what every agent saw and did.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


class Trace:
    def __init__(self, harness_name: str, root: Path | None = None):
        self.run_id = uuid.uuid4().hex[:10]
        self.started = time.monotonic()
        root = root or Path("traces")
        root.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.path = root / f"{harness_name}_{stamp}_{self.run_id}.jsonl"
        self._counts = {"model_turn": 0, "tool_call": 0, "tool_errors": 0,
                        "in_tokens": 0, "out_tokens": 0}

    def log(self, event: str, **data):
        if event == "model_turn":
            self._counts["model_turn"] += 1
            self._counts["in_tokens"] += data.get("in_tokens", 0)
            self._counts["out_tokens"] += data.get("out_tokens", 0)
        elif event == "tool_call":
            self._counts["tool_call"] += 1
        elif event == "tool_result" and data.get("is_error"):
            self._counts["tool_errors"] += 1
        record = {"ts": datetime.now(timezone.utc).isoformat(),
                  "run_id": self.run_id, "event": event, **data}
        with open(self.path, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")

    def observe(self) -> dict:
        """The 'Observe (was it healthy?)' box: tokens, latency, errors."""
        return {**self._counts,
                "wall_seconds": round(time.monotonic() - self.started, 1)}

    def finish(self):
        health = self.observe()
        self.log("run_finished", **health)
        print(f"\n[observe] turns={health['model_turn']} "
              f"tools={health['tool_call']} tool_errors={health['tool_errors']} "
              f"tokens={health['in_tokens']}+{health['out_tokens']} "
              f"wall={health['wall_seconds']}s")
        print(f"[trace] {self.path}")


def record_score(harness: str, *, score, passed: bool, kind: str,
                 note: str = "", root: Path | None = None):
    """Append one gate verdict to the central ledger (traces/scores.jsonl).
    kind: 'birth' (post-build verification) | 'loop' (eval-gated run).
    This is what /scorecard aggregates — quality over time, per harness."""
    root = root or Path("traces")
    root.mkdir(exist_ok=True)
    with open(root / "scores.jsonl", "a") as f:
        f.write(json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(),
            "harness": harness, "score": score, "passed": passed,
            "kind": kind, "note": note[:300]}) + "\n")
