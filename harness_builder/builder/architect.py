"""
The ARCHITECT — the meta-agent that makes this an L3 meta-factory
(revfactory/harness concept): a domain sentence in, an agent team out.

    harness build "Build a harness for deep research. I need an agent team..."

Flow:
  1. LLM designs a HarnessSpec: picks one of the 6 patterns, defines agents
     with full system prompts, wires flow, sets quality criteria.
  2. LLM writes a skill.md (procedural memory) per agent.
  3. We validate + save to harnesses/<name>/ — immediately runnable.

The architect's system prompt below encodes the pattern-selection heuristics.
It is the most leveraged prompt in the codebase: better architect prompt ->
better every generated harness.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..core.spec import HarnessSpec
from ..core.tools import REGISTRY
from ..providers import api

ARCHITECT_SYSTEM = """You are a Harness Architect: you design multi-agent AI \
systems ("harnesses") from a user's domain description.

# Team-architecture patterns (pick exactly one)
- pipeline: sequential dependent stages (draft -> edit -> format). flow = ordered agent names.
- fanout: independent parallel angles merged at the end (multi-angle research, \
parallel code review). flow = [worker1, worker2, ..., merger]; LAST agent merges.
- expert_pool: heterogeneous incoming tasks routed to the right specialist. flow = [].
- producer_reviewer: quality via critique loops (writing, design docs). \
flow = [producer, reviewer].
- supervisor: one coordinator plans/delegates/integrates; specialists don't \
inter-communicate. Set supervisor = coordinator name.
- hierarchical: like supervisor but delegates can sub-delegate (big multi-layer \
projects). Set supervisor.

Selection heuristics: dependent stages -> pipeline. Independent angles + merge \
-> fanout. Quality-critical single deliverable -> producer_reviewer. Dynamic \
decomposition -> supervisor. Deep decomposition -> hierarchical. Varied \
one-off tasks -> expert_pool.

# Agents
3-6 agents. Each needs:
- name: snake_case
- role: one line
- system_prompt: ONLY anatomy §1 IDENTITY & ROLE (~300 tokens): who the agent \
is, its objective, its method at a high level, its quality bar. Domain-specific, \
never generic. Do NOT include environment, safety rules, or output formatting \
here — the harness auto-injects §2 ENVIRONMENT (runtime values) and §5 SAFETY \
(hard constraints); formatting goes in output_format. You may use template \
variables {{{{working_directory}}}}, {{{{date}}}}, {{{{operating_system}}}}, \
{{{{shell}}}} — rendered at runtime.
- output_format: anatomy §4 (~100-400 tokens): exactly how this agent must \
structure its responses/deliverables (sections, file names, length rules, \
markdown conventions).
- model: default "{default_model}". Use "{cheap_model}" for mechanical/low-\
judgment agents (formatting, extraction, routing).
- tools: subset of {tool_names}. Least privilege: only what the role needs. \
Research roles need web_search+fetch_url; builder roles need file tools + \
run_shell; pure-reasoning roles need few or none. save_fact/recall for roles \
that benefit from cross-run memory. Give search_docs to any role that should \
consult the harness's ingested reference corpus (RAG). Agents may also list \
"mcp:<server>" to use an external MCP server's tools if the user mentions one.
- skills: [name] of ONE skill file you will also write. Skills are anatomy \
§3 BEHAVIORAL RULES — the LARGEST section (~800-1500 tokens): concrete \
procedures, checklists, domain heuristics, common pitfalls.

# Quality criteria
4-6 crisp, checkable criteria an LLM judge will score the final output \
against. Domain-specific ("all claims cite a fetched source URL"), not vague \
("high quality").

# Output — respond with ONLY this JSON, no markdown fences, no prose:
{{
 "name": "snake_case_harness_name",
 "description": "...",
 "pattern": "...",
 "supervisor": null,
 "flow": [],
 "agents": [{{"name": "...", "role": "...", "system_prompt": "...",
             "output_format": "...", "model": "...", "tools": [], "skills": ["..."]}}],
 "quality_criteria": ["..."],
 "skills": {{"skill_name": "markdown body: concrete procedures, checklists, \
domain heuristics, common pitfalls for that agent. 200-500 words."}}
}}"""


def build_harness(prompt: str, *, output_root: str = "harnesses",
                  architect_model: str = "anthropic/claude-sonnet-4-6",
                  default_model: str = "anthropic/claude-sonnet-4-6",
                  cheap_model: str = "anthropic/claude-haiku-4-5-20251001") -> Path:
    provider, model = api.resolve(architect_model)
    system = ARCHITECT_SYSTEM.format(
        default_model=default_model, cheap_model=cheap_model,
        tool_names=sorted(REGISTRY.keys()))

    print(f"[architect] designing harness with {architect_model} ...")
    resp = provider.chat(model=model, system=system,
                         messages=[{"role": "user", "content": prompt}],
                         max_tokens=8000)
    text = re.sub(r"^```(json)?|```$", "", resp.text.strip(), flags=re.M).strip()
    try:
        design = json.loads(text)
    except json.JSONDecodeError as e:
        raise SystemExit(f"architect returned invalid JSON ({e}). Raw output "
                         f"saved to architect_failed.json") from (
            Path("architect_failed.json").write_text(text) and None or e)

    skills_bodies = design.pop("skills", {})
    criteria = design.pop("quality_criteria", [])
    design["eval"] = {"quality_criteria": criteria}
    spec = HarnessSpec.from_dict(design)   # validates pattern/flow/supervisor

    harness_dir = Path(output_root) / spec.name
    spec.save(harness_dir)
    for skill_name, body in skills_bodies.items():
        (harness_dir / "skills" / f"{skill_name}.md").write_text(body)

    print(f"[architect] pattern: {spec.pattern}"
          + (f" (supervisor: {spec.supervisor})" if spec.supervisor else ""))
    for a in spec.agents:
        print(f"  - {a.name}: {a.role}  [{a.model}] tools={a.tools}")
    print(f"[architect] saved -> {harness_dir}/")
    print(f"\nRun it:\n  harness run {harness_dir} --task \"...\"")
    return harness_dir
