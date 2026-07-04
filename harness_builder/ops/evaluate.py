"""
LLM Ops: Eval ("was it good?") via LLM-as-judge -> score + diagnosis
("where/why it was broken"). Feeds the gate in the goal loop.
"""

from __future__ import annotations

import json
import re

from ..providers import api

JUDGE_SYSTEM = """You are a strict quality judge for AI agent output.
Score the OUTPUT against the TASK and each CRITERION.
Be harsh: 9-10 means genuinely excellent, 7-8 solid, <7 has real problems.
Respond ONLY with JSON:
{"score": <float 0-10 overall>,
 "per_criterion": {"<criterion>": <float>},
 "diagnosis": "<specific, actionable list of what is broken and where — this
 text is fed to the next attempt, so write it as fix instructions>"}"""


def judge(*, spec, task: str, output: str) -> dict:
    provider, model = api.resolve(spec.eval.judge_model)
    criteria = spec.eval.quality_criteria or [
        "fully addresses the task", "factually careful", "clear and well-structured"]
    prompt = (f"TASK:\n{task}\n\nQUALITY CRITERIA:\n"
              + "\n".join(f"- {c}" for c in criteria)
              + f"\n\nOUTPUT TO JUDGE:\n{output[:20000]}")
    resp = provider.chat(model=model, system=JUDGE_SYSTEM,
                         messages=[{"role": "user", "content": prompt}],
                         max_tokens=1500)
    try:
        text = re.sub(r"```(json)?|```", "", resp.text).strip()
        data = json.loads(text)
        return {"score": float(data.get("score", 0)),
                "per_criterion": data.get("per_criterion", {}),
                "diagnosis": str(data.get("diagnosis", ""))}
    except Exception:
        # Unparseable judge output -> conservative fail with raw text as diagnosis
        return {"score": 0.0, "per_criterion": {},
                "diagnosis": f"judge output unparseable: {resp.text[:800]}"}
