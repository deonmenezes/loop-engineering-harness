"""
The EXTERNAL LOOP — the layer outside the harness (the pink box):

    goal ──▶ PLANNER ──▶ numbered checklist (1..N)      <- like the whiteboard
                              │
              ┌───────────────▼────────────────┐
              │  for each unchecked item:      │
              │    fresh HARNESS run on item   │  <- harness ⊃ agent loop ⊃ context
              │    per-item GATE (judge/none)  │
              │    pass → ✓ check off + note   │
              │    fail → retry, then REPLAN   │
              └───────────────┬────────────────┘
                              │ until list done | max_cycles | budget

Why this increases agent scope: the PLAN LIVES IN A FILE, not in any context
window. Each harness run is fresh and small; the checklist + completed-step
notes are the only state that crosses runs. Kill the process, run again with
the same goal — it resumes exactly where it left off.

State on disk:  <harness>/loop_state/<goal-hash>.json
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..providers import api

PLANNER_SYSTEM = """You are a planner for an autonomous agent loop.
Decompose the GOAL into 3-8 concrete, sequential steps a specialist agent team
will execute ONE PER RUN. Each step must be independently executable given only
the goal + notes from completed steps, and independently checkable.
Respond ONLY with JSON:
{"steps": [{"title": "<imperative, one line>",
            "details": "<what exactly to do, 1-3 sentences>",
            "done_when": "<concrete, checkable completion criterion>"}]}"""

REPLAN_SYSTEM = """You are revising the remaining plan of an autonomous agent
loop after a step kept failing. Given the GOAL, COMPLETED steps, the FAILED
step and its failure diagnosis, produce a revised list of remaining steps
(you may split, reorder, reword, or route around the failure — but the revised
plan must still achieve the goal). Respond ONLY with JSON:
{"steps": [{"title": "...", "details": "...", "done_when": "..."}]}"""

STEP_JUDGE_SYSTEM = """You are a strict per-step gate in an agent loop.
Given one STEP (with its done_when criterion) and the agent team's OUTPUT for
it, decide if the step is genuinely complete. Respond ONLY with JSON:
{"passed": true|false, "note": "<if passed: 1-2 sentence factual summary of
what was accomplished, for downstream steps>", "diagnosis": "<if failed:
specific fix instructions>"}"""


def _llm_json(model: str, system: str, prompt: str, max_tokens: int = 4000) -> dict:
    provider, m = api.resolve(model)
    resp = provider.chat(model=m, system=system,
                         messages=[{"role": "user", "content": prompt}],
                         max_tokens=max_tokens)
    text = re.sub(r"```(json)?|```", "", resp.text).strip()
    return json.loads(text)


@dataclass
class Step:
    title: str
    details: str = ""
    done_when: str = ""
    status: str = "pending"      # pending | done | failed
    attempts: int = 0
    note: str = ""               # summary passed to later steps


@dataclass
class LoopState:
    goal: str
    steps: list = field(default_factory=list)
    cycles: int = 0

    @staticmethod
    def path_for(harness_dir: Path, goal: str) -> Path:
        h = hashlib.sha1(goal.encode()).hexdigest()[:10]
        return harness_dir / "loop_state" / f"{h}.json"

    @staticmethod
    def load(path: Path) -> "LoopState | None":
        if not path.exists():
            return None
        d = json.loads(path.read_text())
        st = LoopState(goal=d["goal"], cycles=d.get("cycles", 0))
        st.steps = [Step(**s) for s in d["steps"]]
        return st

    def save(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {"goal": self.goal, "cycles": self.cycles,
             "steps": [vars(s) for s in self.steps]}, indent=1))

    def render(self) -> str:
        icons = {"done": "✓", "pending": "·", "failed": "✗"}
        return "\n".join(f"  {icons[s.status]} {i+1}. {s.title}"
                         + (f"  [{s.attempts} attempts]" if s.attempts > 1 else "")
                         for i, s in enumerate(self.steps))


def run_external_loop(*, spec, harness_dir: Path, goal: str,
                      run_harness_fn, fresh: bool = False,
                      trace=None) -> LoopState:
    cfg = spec.loop
    state_path = LoopState.path_for(harness_dir, goal)

    # ── PLAN (or resume) ────────────────────────────────────────────────
    state = None if fresh else LoopState.load(state_path)
    if state is None:
        print(f"[planner] decomposing goal with {cfg.planner_model} ...")
        plan = _llm_json(cfg.planner_model, PLANNER_SYSTEM, f"GOAL:\n{goal}")
        state = LoopState(goal=goal,
                          steps=[Step(**s) for s in plan["steps"]])
        state.save(state_path)
    else:
        print(f"[loop] resuming from {state_path.name} "
              f"({sum(s.status == 'done' for s in state.steps)}"
              f"/{len(state.steps)} done)")
    print("\nPLAN:")
    print(state.render(), "\n")

    # ── THE LOOP ────────────────────────────────────────────────────────
    while state.cycles < cfg.max_cycles:
        step = next((s for s in state.steps if s.status == "pending"), None)
        if step is None:
            print("\n[loop] all steps checked off — goal complete")
            print(state.render())
            return state

        idx = state.steps.index(step) + 1
        state.cycles += 1
        step.attempts += 1
        print(f"═══ CYCLE {state.cycles}/{cfg.max_cycles} · "
              f"step {idx}: {step.title} (attempt {step.attempts}) ═══")

        completed_notes = "\n".join(
            f"- ({i+1}) {s.title}: {s.note or 'done'}"
            for i, s in enumerate(state.steps) if s.status == "done")
        task = (f"OVERALL GOAL (for context — do NOT do it all now):\n{state.goal}\n\n"
                + (f"COMPLETED STEPS (build on these):\n{completed_notes}\n\n"
                   if completed_notes else "")
                + f"YOUR CURRENT STEP — do exactly this and only this:\n"
                  f"{idx}. {step.title}\n{step.details}\n"
                  f"Definition of done: {step.done_when}")

        output = run_harness_fn(task)   # fresh harness run (inner loops inside)

        # ── per-item gate ───────────────────────────────────────────────
        if cfg.step_verify == "judge":
            try:
                verdict = _llm_json(
                    spec.eval.judge_model, STEP_JUDGE_SYSTEM,
                    f"STEP:\n{step.title}\n{step.details}\n"
                    f"done_when: {step.done_when}\n\nOUTPUT:\n{output[:20000]}",
                    max_tokens=1000)
            except Exception as e:
                verdict = {"passed": False, "diagnosis": f"judge failed: {e}"}
        else:
            verdict = {"passed": True,
                       "note": output[:300]}

        if trace:
            trace.log("loop_step", step=idx, title=step.title,
                      attempt=step.attempts, passed=verdict.get("passed"))

        if verdict.get("passed"):
            step.status = "done"
            step.note = verdict.get("note", "")[:500]
            print(f"  ✓ step {idx} checked off")
        else:
            diagnosis = verdict.get("diagnosis", "")[:800]
            print(f"  ✗ step {idx} failed gate: {diagnosis[:120]}")
            if step.attempts < cfg.max_attempts_per_step:
                step.details += f"\nPREVIOUS ATTEMPT FAILED — fix: {diagnosis}"
            elif cfg.replan_on_failure:
                print("  [replan] revising remaining plan around the failure")
                try:
                    done = [s for s in state.steps if s.status == "done"]
                    plan = _llm_json(
                        cfg.planner_model, REPLAN_SYSTEM,
                        f"GOAL:\n{state.goal}\n\nCOMPLETED:\n"
                        + "\n".join(f"- {s.title}: {s.note}" for s in done)
                        + f"\n\nFAILED STEP:\n{step.title}\n\nDIAGNOSIS:\n{diagnosis}")
                    step.status = "failed"
                    state.steps = done + [step] + [Step(**s) for s in plan["steps"]]
                except Exception as e:
                    print(f"  [replan] failed ({e}); marking step failed and continuing")
                    step.status = "failed"
            else:
                step.status = "failed"

        state.save(state_path)          # ← resumability: state survives crashes
        print("\n" + state.render() + "\n")

    print(f"[loop] stopped: max_cycles ({cfg.max_cycles}) reached")
    return state
