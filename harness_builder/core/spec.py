"""
HarnessSpec — the contract between the BUILDER (which generates harnesses from
a user prompt) and the RUNTIME (which executes them).

A harness is a directory:

    harnesses/deep_research/
    ├── harness.yaml        <- this spec
    ├── skills/*.md         <- procedural memory (how each agent should act)
    └── memory/             <- semantic + episodic stores (created at runtime)

Everything inside an agent run is ephemeral (see architecture diagram);
the spec + skills + memory stores are what persist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

PATTERNS = ["pipeline", "fanout", "expert_pool", "producer_reviewer",
            "supervisor", "hierarchical"]


@dataclass
class AgentSpec:
    name: str
    role: str                                # one-line purpose
    system_prompt: str                       # full system prompt for this agent
    model: str = "anthropic/claude-sonnet-4-6"   # provider/model routing string
    tools: list[str] = field(default_factory=list)  # names from the tool registry
    skills: list[str] = field(default_factory=list) # skill files loaded into context
    max_turns: int = 12


@dataclass
class MemorySpec:
    semantic: bool = True          # vector-ish store of durable facts
    episodic: bool = True          # SQLite of dated events / past runs
    consolidate_after: int = 3     # summarizer agent runs after N new sessions
    summarizer_model: str = "anthropic/claude-haiku-4-5-20251001"  # cheaper model


@dataclass
class GuardrailSpec:
    max_total_tokens: int = 300_000
    max_wall_seconds: int = 900
    shell_deny_patterns: list[str] = field(default_factory=lambda: [
        r"\brm\s+-rf\s+/", r"\bsudo\b", r"\bmkfs\b", r"\bssh\b", r">\s*/dev/",
    ])


@dataclass
class EvalSpec:
    quality_criteria: list[str] = field(default_factory=list)  # judged by LLM
    judge_model: str = "anthropic/claude-sonnet-4-6"
    pass_threshold: float = 7.0    # 0-10; gate for the ralph loop / release


@dataclass
class HarnessSpec:
    name: str
    description: str
    pattern: str                              # one of PATTERNS
    agents: list[AgentSpec]
    flow: list = field(default_factory=list)  # pattern-specific wiring (agent names)
    supervisor: str | None = None             # for supervisor/hierarchical patterns
    memory: MemorySpec = field(default_factory=MemorySpec)
    guardrails: GuardrailSpec = field(default_factory=GuardrailSpec)
    eval: EvalSpec = field(default_factory=EvalSpec)

    # ------------------------------------------------------------------ io
    @staticmethod
    def load(path: str | Path) -> "HarnessSpec":
        path = Path(path)
        if path.is_dir():
            path = path / "harness.yaml"
        data = yaml.safe_load(path.read_text())
        return HarnessSpec.from_dict(data)

    @staticmethod
    def from_dict(d: dict) -> "HarnessSpec":
        agents = [AgentSpec(**a) for a in d.get("agents", [])]
        spec = HarnessSpec(
            name=d["name"], description=d.get("description", ""),
            pattern=d["pattern"], agents=agents,
            flow=d.get("flow", []), supervisor=d.get("supervisor"),
            memory=MemorySpec(**d.get("memory", {})),
            guardrails=GuardrailSpec(**d.get("guardrails", {})),
            eval=EvalSpec(**d.get("eval", {})),
        )
        spec.validate()
        return spec

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)

    def save(self, harness_dir: str | Path) -> Path:
        d = Path(harness_dir)
        (d / "skills").mkdir(parents=True, exist_ok=True)
        (d / "memory").mkdir(exist_ok=True)
        out = d / "harness.yaml"
        out.write_text(yaml.safe_dump(self.to_dict(), sort_keys=False,
                                      allow_unicode=True, width=100))
        return out

    # ------------------------------------------------------------ checks
    def validate(self):
        if self.pattern not in PATTERNS:
            raise ValueError(f"pattern must be one of {PATTERNS}, got '{self.pattern}'")
        names = [a.name for a in self.agents]
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate agent names: {names}")
        if self.pattern in ("supervisor", "hierarchical"):
            if not self.supervisor or self.supervisor not in names:
                raise ValueError(f"pattern '{self.pattern}' needs a valid 'supervisor' "
                                 f"agent name; got '{self.supervisor}'")
        flat = [x for item in self.flow
                for x in (item if isinstance(item, list) else [item])]
        unknown = [x for x in flat if x not in names]
        if unknown:
            raise ValueError(f"flow references unknown agents: {unknown}")

    def agent(self, name: str) -> AgentSpec:
        for a in self.agents:
            if a.name == name:
                return a
        raise KeyError(name)
