"""
Runtime orchestrator — executes a HarnessSpec using its team pattern.

Six patterns (revfactory/harness taxonomy):

  pipeline           A -> B -> C (each output feeds the next)
  fanout             [A, B, C] in parallel -> merger agent fans results in
  expert_pool        router picks the right expert(s) for the task
  producer_reviewer  producer drafts -> reviewer critiques -> producer revises
                     (bounded rounds; reviewer must say APPROVED to stop early)
  supervisor         supervisor agent plans and delegates via a delegate tool
  hierarchical       supervisor whose delegates may themselves delegate (depth-capped)

Shared per run: one workspace dir, one Trace, one token budget, one memory set.
Each agent still runs the same inner loop from core/loop.py — patterns are
just wiring between inner loops. That composability is the whole design.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..core import tools as toolreg
from ..core.loop import run_agent
from ..core.memory import (EpisodicMemory, SemanticMemory, assemble_context,
                           maybe_consolidate)
from ..ops.trace import Trace


class Orchestrator:
    def __init__(self, spec, harness_dir: str | Path, workspace: str | Path | None = None):
        self.spec = spec
        self.harness_dir = Path(harness_dir)
        self.workspace = Path(workspace or (Path("workspace") / spec.name))
        self.trace = Trace(spec.name)
        self.tokens_used = 0
        self.semantic = SemanticMemory(self.harness_dir) if spec.memory.semantic else None

    # ------------------------------------------------------------------
    def _ctx(self) -> toolreg.ToolContext:
        return toolreg.ToolContext(self.workspace, self.spec.guardrails,
                                   semantic_memory=self.semantic, trace=self.trace)

    def _run_one(self, agent_name: str, task: str) -> str:
        agent = self.spec.agent(agent_name)
        system = assemble_context(self.harness_dir, agent, task, self.spec.memory)
        print(f"  ▶ {agent_name} [{agent.model}]")
        res = run_agent(agent_spec=agent, system=system, task=task,
                        ctx=self._ctx(), guardrails=self.spec.guardrails,
                        trace=self.trace, token_budget_used=self.tokens_used)
        self.tokens_used += res.input_tokens + res.output_tokens
        print(f"  ✔ {agent_name} ({res.turns} turns, {res.tool_calls} tool calls, "
              f"stopped: {res.stopped_by})")
        return res.reply

    # ------------------------------------------------------------------
    def run(self, task: str) -> str:
        self.trace.log("run_start", harness=self.spec.name,
                       pattern=self.spec.pattern, task=task)
        handler = getattr(self, f"_pattern_{self.spec.pattern}")
        reply = handler(task)
        self.trace.log("reply", text=reply[:2000])

        # Save the messages -> episodic memory; consolidate after N sessions
        if self.spec.memory.episodic:
            EpisodicMemory(self.harness_dir).add(task, reply[:1500])
            maybe_consolidate(self.harness_dir, self.spec.memory, self.trace)
        self.trace.finish()
        return reply

    # ----------------------------------------------------------- patterns
    def _pattern_pipeline(self, task: str) -> str:
        order = self.flow_or_all()
        payload = task
        for name in order:
            payload = self._run_one(
                name,
                f"OVERALL TASK:\n{task}\n\nINPUT FROM PREVIOUS STAGE:\n{payload}"
                if payload is not task else task)
        return payload

    def _pattern_fanout(self, task: str) -> str:
        names = self.flow_or_all()
        workers, merger = names[:-1], names[-1]
        with ThreadPoolExecutor(max_workers=min(4, len(workers))) as pool:
            futures = {n: pool.submit(self._run_one, n, task) for n in workers}
            results = {n: f.result() for n, f in futures.items()}
        merged_input = "\n\n".join(
            f"=== FINDINGS FROM {n.upper()} ===\n{r}" for n, r in results.items())
        return self._run_one(
            merger, f"OVERALL TASK:\n{task}\n\nMERGE THESE PARALLEL "
                    f"FINDINGS INTO ONE COHERENT DELIVERABLE:\n{merged_input}")

    def _pattern_expert_pool(self, task: str) -> str:
        experts = [a for a in self.spec.agents]
        roster = "\n".join(f"- {a.name}: {a.role}" for a in experts)
        from ..providers import api
        provider, model = api.resolve(self.spec.agents[0].model)
        resp = provider.chat(
            model=model,
            system="You are a router. Given a task and an expert roster, reply "
                   "ONLY with a comma-separated list of 1-3 expert names best "
                   "suited to handle it. No prose.",
            messages=[{"role": "user",
                       "content": f"TASK: {task}\n\nROSTER:\n{roster}"}],
            max_tokens=50)
        chosen = [n.strip() for n in resp.text.split(",")
                  if n.strip() in [a.name for a in experts]] or [experts[0].name]
        self.trace.log("router", chosen=chosen)
        print(f"  [router] -> {chosen}")
        outputs = [self._run_one(n, task) for n in chosen]
        if len(outputs) == 1:
            return outputs[0]
        return "\n\n".join(f"=== {n} ===\n{o}" for n, o in zip(chosen, outputs))

    def _pattern_producer_reviewer(self, task: str, rounds: int = 3) -> str:
        names = self.flow_or_all()
        producer, reviewer = names[0], names[1]
        draft = self._run_one(producer, task)
        for r in range(rounds):
            review = self._run_one(
                reviewer,
                f"TASK:\n{task}\n\nDRAFT TO REVIEW:\n{draft}\n\n"
                "Critique against the task and your skills. If the draft is "
                "genuinely ready, reply with exactly 'APPROVED' and nothing else. "
                "Otherwise list specific, actionable fixes.")
            if review.strip().upper().startswith("APPROVED"):
                self.trace.log("review_approved", round=r + 1)
                print(f"  [review] approved on round {r + 1}")
                break
            draft = self._run_one(
                producer,
                f"TASK:\n{task}\n\nYOUR PREVIOUS DRAFT:\n{draft}\n\n"
                f"REVIEWER FEEDBACK (address every point):\n{review}")
        return draft

    def _pattern_supervisor(self, task: str) -> str:
        return self._run_supervised(self.spec.supervisor, task, depth=0, max_depth=1)

    def _pattern_hierarchical(self, task: str) -> str:
        return self._run_supervised(self.spec.supervisor, task, depth=0, max_depth=3)

    def _run_supervised(self, name: str, task: str, depth: int, max_depth: int) -> str:
        """Give the supervisor a `delegate` tool wired to its team members."""
        agent = self.spec.agent(name)
        # team excludes self, and (below the top) the supervisor — subtasks
        # flow down the hierarchy, never back up (prevents delegation loops)
        excluded = {name} | ({self.spec.supervisor} if depth > 0 else set())
        team = [a for a in self.spec.agents if a.name not in excluded]
        roster = "\n".join(f"- {a.name}: {a.role}" for a in team)

        orch = self

        def delegate(ctx, agent_name: str, subtask: str):
            if agent_name not in [a.name for a in team]:
                return f"unknown team member '{agent_name}'. Roster:\n{roster}"
            if depth + 1 <= max_depth:
                return orch._run_supervised(agent_name, subtask,
                                            depth + 1, max_depth)
            return orch._run_one(agent_name, subtask)

        # register delegate as an ephemeral tool for this supervised run
        toolreg.REGISTRY["delegate"] = {
            "name": "delegate",
            "description": f"Delegate a subtask to a team member. Team:\n{roster}",
            "parameters": {"type": "object",
                           "properties": {"agent_name": {"type": "string"},
                                          "subtask": {"type": "string"}},
                           "required": ["agent_name", "subtask"]},
            "fn": delegate,
        }
        try:
            if "delegate" not in agent.tools:
                agent.tools = agent.tools + ["delegate"]
            system = assemble_context(self.harness_dir, agent, task, self.spec.memory)
            system += ("\n\nYou are the coordinator. Break the task down, use "
                       "`delegate` for specialist work, then integrate results "
                       "into the final deliverable yourself.")
            print(f"  ▶ {name} [{agent.model}] (supervisor, depth {depth})")
            res = run_agent(agent_spec=agent, system=system, task=task,
                            ctx=self._ctx(), guardrails=self.spec.guardrails,
                            trace=self.trace, token_budget_used=self.tokens_used)
            self.tokens_used += res.input_tokens + res.output_tokens
            return res.reply
        finally:
            toolreg.REGISTRY.pop("delegate", None)

    # ------------------------------------------------------------------
    def flow_or_all(self) -> list[str]:
        if self.spec.flow:
            return [x for item in self.spec.flow
                    for x in (item if isinstance(item, list) else [item])]
        return [a.name for a in self.spec.agents]
