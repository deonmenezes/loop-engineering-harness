"""
Birth certificate — every harness ships PROVEN, not just designed.

Right after a build, run one deliberately small smoke task through the
freshly scaffolded team, judge the output against the harness's own quality
gate, and stamp the result into BIRTH.md (scaffold never rewrites it, so the
stamp survives re-scaffolds). A harness that can't pass its own gate on day
one gets caught the minute it is born — not the first time you need it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


def smoke_task(spec) -> str:
    return (
        "BIRTH CERTIFICATE smoke run — prove this harness works end to end. "
        "Produce a deliberately SMALL but complete and representative example "
        "of your core deliverable: pick the simplest realistic request in "
        "your domain and carry it through your full process. Keep it minutes "
        "of work, not a production job.\n\n"
        f"Domain context: {' '.join(spec.description.split())[:400]}")


def birth_certificate(spec, harness_dir: str | Path, *, task: str | None = None,
                      on_token=None) -> dict:
    """One smoke run + judge. Returns {'score','passed','diagnosis','reply'}
    and writes BIRTH.md into the harness. Raises nothing it can help — a
    failed verification is a result, not an error."""
    from ..ops.evaluate import judge
    from ..ops.trace import record_score
    from ..runtime.orchestrator import Orchestrator

    task = task or smoke_task(spec)
    reply = Orchestrator(spec, harness_dir).run(task, on_token=on_token)
    verdict = judge(spec=spec, task=task, output=reply)
    passed = verdict["score"] >= spec.eval.pass_threshold
    record_score(spec.name, score=verdict["score"], passed=passed,
                 kind="birth")

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    crit = "\n".join(f"- {c}: {s}" for c, s in
                     verdict.get("per_criterion", {}).items()) or "- (overall only)"
    body = f"""# Birth certificate — {spec.name}

**Verdict:** {"PASSED" if passed else "DID NOT PASS"} \
({verdict['score']:.1f}/10 against a gate of {spec.eval.pass_threshold})
**When:** {stamp}
**Smoke task:** one small end-to-end run through the full team.

## Per-criterion
{crit}

## Judge diagnosis
{verdict['diagnosis'] or '(nothing to fix)'}

## Output excerpt
```
{reply[:1500]}
```

*Re-verify any time: activate the harness and run `/loop <a small task>`,
or rebuild the certificate from the builder with /certify.*
"""
    (Path(harness_dir) / "BIRTH.md").write_text(body)
    return {"score": verdict["score"], "passed": passed,
            "diagnosis": verdict["diagnosis"], "reply": reply}
