# Loop Engineer Builder

**Prompt in → loop-engineered agent system out.**

The stack, loops on loops (prompt → context → harness → loop engineering):

```
┌─ EXTERNAL LOOP ─────────────────────────────────────┐   1. ✓ research topic
│  goal → planner → numbered checklist                │   2. ✓ draft report
│  ┌─ HARNESS (per checklist item, fresh) ─────────┐  │   3. · polish
│  │  ┌─ AGENT LOOP ────────────────────────────┐  │  │   4. · publish
│  │  │  ┌─ CONTEXT ─────────────┐              │  │  │
│  │  │  │ model ⇄ tools         │ ⇄ guardrails │  │  │
│  │  │  └───────────────────────┘              │  │  │
│  │  └─────────────────────────────────────────┘  │  │
│  └───────────────────────────────────────────────┘  │
│  per-item gate → ✓ check off → replan on failure    │
└──────────────────────────────────────────────────────┘
```

You describe a domain in one paragraph; an architect agent designs the agent
team (pattern, roles, system prompts, tools, skills, quality gate) as a
runnable harness — then the **external loop** drives that harness through a
planned checklist toward goals far bigger than any single run. The plan lives
in a FILE, not a context window: kill the process, rerun the same goal, it
resumes exactly where it stopped.

```bash
harness build "Build a harness for deep research. I need an agent team that can
investigate any topic from multiple angles — web search, academic sources,
community sentiment — then cross-validate findings and produce a report."

harness run harnesses/deep_research --task "Solid-state batteries: state of the art in 2026"

# retry goal loop: judge scores the run, failures feed back until it passes
harness run harnesses/deep_research --task "..." --loop --max-iterations 3

# THE EXTERNAL LOOP: plan the goal into a checklist, one fresh harness run per
# item, per-item gate, ✓ check-off, replan on failure — fully resumable
harness loop harnesses/deep_research --goal "publish a definitive report on X"
```

## Architecture

Faithful to the harness/loop/ops diagram this project was designed from:

```
┌─ HARNESS (persistent) ─────────────────────────────────────────────┐
│                          ┌─ LOOP (ephemeral run) ────────────┐     │
│  Working Memory /        │   LLM ⇄ tools                     │     │
│  Context RAM  ─────────▶ │   (inner agent loop,              │──▶ Reply
│    ▲    ▲    ▲           │    guardrails end the loop)       │     │
│    │    │    │           └───────────────────────────────────┘     │
│  Procedural  Semantic   Episodic                │ save messages    │
│  (skill.md)  (vectors)  (SQLite, dated)  ◀──────┘                  │
│                 ▲            │                                     │
│                 └── Summarizer agent (cheap model),                │
│                     only after N new sessions                      │
└────────────────────────────────────────────────────────────────────┘
           │ every run emits a trace
           ▼
┌─ LLM OPS ──────────────────────────────────────────────────────────┐
│  Trace (1/run, JSONL) → Eval (LLM-as-judge) → Diagnose → Gate      │
│      eval passed → Release          eval failed → re-run with      │
│                                     diagnosis  (= the goal loop)   │
└────────────────────────────────────────────────────────────────────┘
```

- **Working memory** is assembled fresh per agent run: system prompt +
  procedural skills + top-k semantic facts + recent episodes. Everything in a
  run is ephemeral; durable state exits only through memory and the reply.
- **Guardrails live in code**, not prompts: token/time budgets, shell
  denylists, workspace sandboxing, bounded turns.
- **The goal loop** (`--loop`) is codex-style loop engineering: run fresh →
  verify → feed the diagnosis forward → stop on pass or budget. Prefer a
  deterministic verifier when you have one: `--verify-cmd "pytest -q"`
  beats LLM-as-judge every time it's available.

## The six team patterns

The architect picks one per harness (taxonomy from revfactory/harness):

| Pattern | Wiring | Use when |
|---|---|---|
| `pipeline` | A → B → C | dependent sequential stages |
| `fanout` | [A,B,C] ∥ → merger | independent angles, merged |
| `expert_pool` | router → expert(s) | varied one-off tasks |
| `producer_reviewer` | draft ⇄ critique (bounded) | quality-critical deliverables |
| `supervisor` | coordinator + `delegate` tool | dynamic decomposition |
| `hierarchical` | supervisors of supervisors (depth-capped) | deep decomposition |

Patterns are just wiring between instances of the same inner agent loop —
that composability is the core design.

## Multi-provider (core, opencode-style)

Model strings route automatically; mix providers freely inside one team:

```yaml
agents:
  - name: researcher
    model: anthropic/claude-sonnet-4-6      # heavy reasoning
  - name: formatter
    model: anthropic/claude-haiku-4-5-20251001   # cheap mechanical work
  - name: local_drafter
    model: ollama/llama3.1                  # free, local
```

Supported: `anthropic/…`, `openai/…`, `groq/…`, `openrouter/…`, `ollama/…` —
adding a provider is ~10 lines in `providers/api.py` (OpenAI-compatible ones
are just a base_url).

Force a whole harness onto one model: `--model-override groq/llama-3.3-70b-versatile`.

## What a generated harness contains

| Capability | How |
|---|---|
| System prompts | architect writes a full 150-400 word brief per agent |
| Memory as files | skills/*.md (procedural) · memory/MEMORY.md (facts, human-editable) · semantic.json · episodic store |
| Subagents | 6 team patterns; supervisors get a `delegate` tool |
| **MCP — consume** | `mcp_servers:` in harness.yaml + `tools: ["mcp:<name>"]` → agents use any external MCP server's tools |
| **MCP — deploy** | `harness serve-mcp` exposes build_harness / run_<h> / loop_<h> to Claude Code etc: `claude mcp add loop-engineer -- harness serve-mcp` |
| **RAG** | `harness rag <h> add <files\|dirs\|urls>` → chunked corpus; `search_docs` tool + auto top-k into each agent's working memory |
| Domain tool references | least-privilege per agent from the registry (files, shell, web, memory, docs, MCP) |
| Context management | working-memory assembly per run + in-loop pruning (oldest tool results collapsed, newest 4 kept, agent notes never touched) |
| Any LLM | anthropic / openai / groq / openrouter / ollama, mixable per agent |

## Interactive TUI (opencode-style)

Bare `harness` drops you into an interactive shell — chat-first, like opencode:

```
❯ /use deep_research
✓ active: deep_research (fanout, 4 agents)
❯ what changed in EU AI regulation this quarter?        # plain text = run task
❯ /loop write the report                                # goal loop + eval gate
❯ /model ollama/llama3.1                                # hot-swap every agent
❯ /export claude-code                                   # compile to a plugin
```

Tab completion, persistent history (~/.harness_history), live agent progress,
status toolbar. `/help` lists everything.

## Run inside Claude Code / Codex / opencode

`harness export <harness> --to <target>` compiles a harness into the host's
native format, so the SAME team design runs inside your daily driver:

| Target | What's generated | How to use |
|---|---|---|
| `claude-code` | real plugin: `.claude-plugin/plugin.json`, `agents/*.md` (native subagents), `skills/*/SKILL.md`, `/run-<name>` command | `claude --plugin-dir exports/<name>-plugin`, then `/run-<name> <task>` |
| `codex` | `AGENTS.md` team charter + `prompts/<agent>.md` role briefs (Codex has no subagents — the charter orchestrates role-play with files as the message bus) | drop into repo root |
| `opencode` | `.opencode/agent/*.md` subagents + `/run-<name>` command | drop into project, `@agent` or `/run-<name>` |

Team structure, system prompts, skills, and pattern wiring port; our memory
stores, budgets, and goal loop don't — the hosts have their own equivalents.

## Setup

```bash
python -m venv venv && source venv/bin/activate
pip install -e .
cp .env.example .env   # add keys for the providers you use
harness templates       # see the 8 bundled domain harnesses
harness use deep_research
harness run harnesses/deep_research --task "..."
```

## Bundled domain templates

`deep_research` · `website_dev` · `webtoon_production` · `youtube_content` ·
`code_review` · `tech_docs` · `data_pipeline` · `marketing_campaign`

Each is exactly what `harness build` would generate for its use case — read
their `harness.yaml` + `skills/*.md` to learn how to write good ones. Try the
matching prompts in `examples/prompts.md`.

## Repository layout

```
harness_builder/
├── providers/api.py        unified multi-provider LLM API (normalized responses)
├── core/
│   ├── spec.py             HarnessSpec (+ LoopSpec): the YAML contract builder ↔ runtime
│   ├── external_loop.py    the EXTERNAL loop: plan → checklist → per-step harness runs
│   ├── loop.py             inner agent loop (provider-agnostic, guardrailed)
│   ├── ralph.py            outer goal loop with eval gate
│   ├── memory.py           working / procedural / semantic / episodic + summarizer
│   └── tools.py            sandboxed tool registry (files, shell, web, memory)
├── runtime/orchestrator.py the six team patterns
├── builder/architect.py    the meta-agent: prompt → harness
├── ops/                    trace (JSONL) + LLM-as-judge eval
└── cli.py                  build · run · templates · use · inspect
templates/                  8 domain harnesses
```

## Design lineage

- **opencode / pi-ai** — unified multi-provider layer, clean runtime↔CLI split
- **revfactory/harness** — the L3 meta-factory idea and 6-pattern taxonomy
- **codex-style goal loops** — run → verify → diagnose → re-run, verifier over vibes
- The harness/loop/LLM-ops architecture diagram this repo implements

## Extending

- **New tool**: one `@tool(...)` function in `core/tools.py`; every harness can use it.
- **New provider**: subclass or reuse `OpenAICompatProvider` in `providers/api.py`.
- **New pattern**: one `_pattern_<name>` method in `runtime/orchestrator.py`
  plus the name in `core/spec.py:PATTERNS` and the architect prompt.
- **Better generated harnesses**: edit `builder/architect.py:ARCHITECT_SYSTEM` —
  the highest-leverage prompt in the codebase.

## Rust runtime (harness-rs/)

A second, drop-in runtime in Rust — same `harness.yaml` contract, same
templates, same CLI surface (`build · run · templates · use · inspect`),
single ~4 MB static binary, zero Python needed at runtime:

```bash
cd harness-rs && cargo build --release
./target/release/harness use deep_research
./target/release/harness run harnesses/deep_research --task "..." --loop
```

Differences from the Python runtime: fanout workers run on real OS threads;
episodic memory is JSONL instead of SQLite; semantic search is token-overlap
(embedding-free). Harnesses built by either runtime run on both — the spec is
the contract, the runtime is a choice. Python keeps the TUI and the
Claude-Code/Codex/opencode exporters.
