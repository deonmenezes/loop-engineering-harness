"""
The ralph / goal loop — the OUTER loop (codex-style "keep going until the goal
is verifiably met"). Wraps entire harness runs:

    while goal not met:
        run the harness fresh against (task + spec + last verdict's feedback)
        judge the output against the harness's quality criteria (eval gate)
        pass  -> release (return the result)
        fail  -> feed the judge's diagnosis into the next iteration

This IS the LLM Ops box in the architecture diagram, folded into a loop:
Trace -> Eval -> Diagnose -> Gate -> (Release | improved prompt & re-run).

Two verifier flavors:
  - LLM-as-judge against spec.eval.quality_criteria (default; any domain)
  - deterministic shell command (--verify-cmd), exit code 0 = pass
    (strictly better when possible: models grade their own homework kindly)
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from ..ops.evaluate import judge


@dataclass
class GoalLoopResult:
    passed: bool
    iterations: int
    final_output: str
    verdicts: list


def run_goal_loop(*, run_harness_fn, spec, task: str, max_iterations: int = 3,
                  verify_cmd: str | None = None, workspace=None, trace=None) -> GoalLoopResult:
    """
    run_harness_fn(task_text) -> str : executes one FULL fresh harness run.
    """
    feedback = ""
    verdicts = []

    for i in range(1, max_iterations + 1):
        if trace:
            trace.log("goal_loop_iteration", i=i, max=max_iterations)
        print(f"\n═══ GOAL LOOP iteration {i}/{max_iterations} ═══")

        iter_task = task if not feedback else (
            f"{task}\n\n--- PREVIOUS ATTEMPT FAILED THE QUALITY GATE ---\n"
            f"Verifier diagnosis (fix these specifically):\n{feedback}")

        output = run_harness_fn(iter_task)

        # ---- GATE: deterministic verifier wins if provided
        if verify_cmd:
            proc = subprocess.run(verify_cmd, shell=True, cwd=workspace,
                                  capture_output=True, text=True, timeout=300)
            passed = proc.returncode == 0
            diagnosis = (proc.stdout + proc.stderr)[-2000:]
            verdicts.append({"iteration": i, "passed": passed, "score": None,
                             "diagnosis": diagnosis})
        else:
            verdict = judge(spec=spec, task=task, output=output)
            passed = verdict["score"] >= spec.eval.pass_threshold
            diagnosis = verdict["diagnosis"]
            verdicts.append({"iteration": i, "passed": passed,
                             "score": verdict["score"], "diagnosis": diagnosis})
            print(f"  [gate] score={verdict['score']}/10 "
                  f"(threshold {spec.eval.pass_threshold})")

        if trace:
            trace.log("gate", **verdicts[-1])
        from ..ops.trace import record_score
        record_score(spec.name, score=verdicts[-1]["score"], passed=passed,
                     kind="loop", note=task[:200])

        if passed:
            print("  [gate] PASSED -> release")
            return GoalLoopResult(True, i, output, verdicts)

        print("  [gate] failed -> diagnosing and re-running")
        feedback = diagnosis

    return GoalLoopResult(False, max_iterations, output, verdicts)
