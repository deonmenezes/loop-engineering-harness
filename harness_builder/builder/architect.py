"""
The ARCHITECT — the meta-agent that makes this an L3 meta-factory: a domain
sentence in, an excellent agent team out.

    harness build "Build a harness for deep research. I need an agent team..."

Flow (this is what makes every build *deliberately* good, not a one-shot guess):
  1. Detect the domain and inject a PLAYBOOK (builder/playbooks.py) — senior-
     practitioner guidance on team shape, tools, and what great output means.
  2. The architect designs a HarnessSpec: pattern, agents with full system
     prompts, flow, quality criteria, and a skill.md per agent.
  3. A CRITIC scores the design against a rubric and lists concrete fixes; the
     architect REVISES. Loop until it clears the bar (or the round budget).
  4. Validate + scaffold to harnesses/<name>/ — a standalone, runnable app.

The architect's system prompt is the most leveraged text in the codebase:
better architect prompt + better critique loop -> better every harness.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..core.spec import HarnessSpec
from ..core.tools import REGISTRY
from ..providers import api
from . import playbooks

ARCHITECT_SYSTEM = """You are a Harness Architect: you design multi-agent AI \
systems ("harnesses") from a user's domain description. You aim for harnesses \
that rival purpose-built tools in their field (a coding harness on par with \
Claude Code/Codex; a video harness on par with Higgsfield/Runway).

# Team-architecture patterns (pick exactly one)
- pipeline: sequential dependent stages (draft -> edit -> format). flow = ordered agent names.
- fanout: independent parallel angles merged at the end (multi-angle research, \
parallel code review). flow = [worker1, worker2, ..., merger]; LAST agent merges.
- expert_pool: heterogeneous incoming tasks routed to the right specialist. flow = [].
- producer_reviewer: quality via critique loops (writing, correctness-critical code). \
flow = [producer, reviewer].
- supervisor: one coordinator plans/delegates/integrates; specialists don't \
inter-communicate. Set supervisor = coordinator name.
- hierarchical: like supervisor but delegates can sub-delegate (big multi-layer \
projects). Set supervisor.

Selection heuristics: dependent stages -> pipeline. Independent angles + merge \
-> fanout. Quality-critical single deliverable -> producer_reviewer. Dynamic \
decomposition -> supervisor. Deep decomposition -> hierarchical. Varied \
one-off tasks -> expert_pool.

# DOMAIN PLAYBOOK (authoritative for this build — follow it closely)
{playbook}

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
markdown conventions). Be concrete — the playbook shows the bar.
- model: default "{default_model}". Use "{cheap_model}" for mechanical/low-\
judgment agents (formatting, extraction, routing).
- tools: subset of {tool_names}. Least privilege: only what the role needs. \
Follow the playbook's tool guidance. Research roles need web_search+fetch_url; \
builder/coder roles need read_file+write_file+apply_patch+run_shell+python_exec; \
planning-heavy roles benefit from plan; creative-generation roles may use \
generate_image/generate_video. save_fact/recall for roles that benefit from \
cross-run memory; search_docs for roles that consult the ingested corpus (RAG). \
Agents may also list "mcp:<server>" to use an external MCP server if mentioned.
- skills: [name] of ONE skill file you will also write. Skills are anatomy \
§3 BEHAVIORAL RULES — the LARGEST section (~800-1500 tokens): concrete \
procedures, checklists, domain heuristics, common pitfalls. This is where the \
craft lives; make it specific enough that a competent model becomes an expert.

# Launch command
Pick "command": the terminal command users type to launch this harness. Make it \
SHORT, memorable, and pronounceable — like `pi`, `claude`, or `youvid` for a \
YouTube video harness. 3-8 lowercase letters, no underscores. A real CLI name, \
not a truncation.

# Quality criteria
4-6 crisp, CHECKABLE criteria an LLM judge will score the final output against. \
Domain-specific and verifiable ("every shot has a model-ready prompt with camera \
and lighting"; "all changed code has passing tests"), never vague ("high quality").

# Output — respond with ONLY this JSON, no markdown fences, no prose:
{{
 "name": "snake_case_harness_name",
 "command": "shortcmd",
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

CRITIC_SYSTEM = """You are a ruthless design critic for multi-agent AI \
harnesses. You are given the user's request, the domain playbook, and a \
proposed design (JSON). Score it 0-10 on how well it would perform IN THE REAL \
WORLD against purpose-built tools, judged on this rubric:

1. Pattern fit — is the orchestration pattern right for the work?
2. Team decomposition — are the agents the right ones, well-separated, no gaps
   or redundancy? Does it follow the playbook's team shape?
3. Tool assignment — least privilege AND sufficient reach (a coder that can't
   run_shell/apply_patch, or a researcher without web_search, is a fail)?
4. Prompt specificity — are system_prompts domain-specific and sharp, not generic
   filler?
5. Output format precision — will outputs be concretely structured to the bar the
   playbook sets?
6. Quality criteria — are they specific and CHECKABLE by a judge?
7. Skill depth — do the skills encode real craft, checklists, and pitfalls?

Be specific and demanding. A generic-but-valid design should score ~5-6, not 8.
Reserve 9-10 for designs a domain expert would ship.

Respond with ONLY this JSON: {"score": <float 0-10>, "verdict": "<one line>", \
"fixes": ["<concrete, actionable fix>", "..."]}"""


def _extract_json(text: str) -> str:
    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M).strip()
    # tolerate leading/trailing prose by grabbing the outermost {...}
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start:end + 1]
    return text


def _design(provider, model, system, messages, max_tokens=8000) -> dict:
    resp = provider.chat(model=model, system=system, messages=messages,
                         max_tokens=max_tokens)
    text = _extract_json(resp.text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        Path("architect_failed.json").write_text(text)
        raise SystemExit(f"architect returned invalid JSON ({e}). Raw output "
                         f"saved to architect_failed.json") from e


def _critique(provider, model, prompt, playbook, design) -> tuple[float, str, list]:
    try:
        resp = provider.chat(
            model=model, system=CRITIC_SYSTEM,
            messages=[{"role": "user", "content":
                       f"USER REQUEST:\n{prompt}\n\nPLAYBOOK:\n{playbook}\n\n"
                       f"PROPOSED DESIGN:\n{json.dumps(design, indent=1)[:9000]}"}],
            max_tokens=900)
        data = json.loads(_extract_json(resp.text))
        return (float(data.get("score", 0)), str(data.get("verdict", "")),
                list(data.get("fixes", [])))
    except Exception:
        return 10.0, "(critic unavailable — accepting design)", []


def build_harness(prompt: str, *, output_root: str = "harnesses",
                  architect_model: str = "anthropic/claude-sonnet-4-6",
                  default_model: str = "anthropic/claude-sonnet-4-6",
                  cheap_model: str = "anthropic/claude-haiku-4-5-20251001",
                  refine_rounds: int = 2, pass_threshold: float = 8.0) -> Path:
    provider, model = api.resolve(architect_model)
    playbook, domains = playbooks.guidance_for(prompt)
    system = ARCHITECT_SYSTEM.format(
        default_model=default_model, cheap_model=cheap_model,
        tool_names=sorted(REGISTRY.keys()), playbook=playbook)

    print(f"[architect] domain: {', '.join(domains)}  "
          f"(designing with {architect_model})")
    messages = [{"role": "user", "content": prompt}]
    design = _design(provider, model, system, messages)

    # design -> critique -> revise, until it clears the bar or we run out of rounds
    for rnd in range(1, refine_rounds + 1):
        score, verdict, fixes = _critique(provider, model, prompt, playbook,
                                          design)
        print(f"[architect] design critique (round {rnd}): {score:.1f}/10 — "
              f"{verdict}")
        if score >= pass_threshold or not fixes:
            break
        for fx in fixes[:6]:
            print(f"             fix: {fx}")
        messages = [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": json.dumps(design)},
            {"role": "user", "content":
             "A design critic scored your design "
             f"{score:.1f}/10 ({verdict}). Revise the design to address EVERY "
             "point below, then output the COMPLETE improved JSON (same schema, "
             "no prose):\n" + "\n".join(f"- {f}" for f in fixes)},
        ]
        design = _design(provider, model, system, messages)

    skills_bodies = design.pop("skills", {})
    criteria = design.pop("quality_criteria", [])
    design["eval"] = {"quality_criteria": criteria}
    spec = HarnessSpec.from_dict(design)   # validates pattern/flow/supervisor/command

    harness_dir = Path(output_root) / spec.name
    spec.save(harness_dir)
    for skill_name, body in skills_bodies.items():
        (harness_dir / "skills" / f"{skill_name}.md").write_text(body)
    from .scaffold import scaffold, derive_command
    scaffold(spec, harness_dir)

    command = spec.command or derive_command(spec.name)
    print(f"[architect] pattern: {spec.pattern}"
          + (f" (supervisor: {spec.supervisor})" if spec.supervisor else ""))
    for a in spec.agents:
        print(f"  - {a.name}: {a.role}  [{a.model}] tools={a.tools}")
    print(f"[architect] standalone harness -> {harness_dir}/  "
          f"(launch command: {command})")
    print(f"\nIt is a complete app of its own (stdlib-only, pi-style TUI, "
          f"token streaming):\n"
          f"  cd {harness_dir}\n"
          f"  cp .env.example .env       # add keys for the providers it uses\n"
          f"  ./{command}                # interactive TUI\n"
          f"  ./{command} \"a task\"       # one-shot run\n"
          f"  ./install.sh               # then just type `{command}` anywhere\n"
          f"Prompts live in prompts/*.md with {{{{slot}}}} anatomy — see its README.")
    return harness_dir
