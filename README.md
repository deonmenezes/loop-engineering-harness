# Loop Engineer Builder

**Prompt in → a completely new, standalone harness out.**

```bash
harness "prompt goes here"        # that's the whole interface
```

One quoted sentence and the architect designs an agent team, then compiles it
into its **own app** — not a config for this repo, a self-contained program:
one Python file (stdlib only, nothing to install), a **full TUI cloned from
[pi_agent_rust](https://github.com/Dicklesworthstone/pi_agent_rust)** with
**token streaming** (replies land word-by-word like Claude Code / Codex), and
every architectural prompt exposed as an editable file with explicit `{{slot}}`
markers showing exactly where each piece goes. Each harness gets a **short,
memorable launch command** — a YouTube harness is `youvid`, a research one
`digger`, a code reviewer `crev`.

```bash
cd harnesses/deep_research
./digger                          # pi-style TUI: alt-screen frame loop,
                                  # streaming text, collapsible tool output
                                  # (ctrl+o), spinner, themes, /-autocomplete
./digger "a task"                 # one-shot
./digger --loop "a task"          # judge-gated retries until quality passes
./install.sh                      # put `digger` on your PATH, run from anywhere
```

**Every build is deliberately good, not a lucky one-shot.** The architect
detects the domain (coding, video, research, writing, data, design, marketing)
and injects a senior-practitioner **playbook** — the right team shape, tools,
and quality bar for that field — then runs a **design → critique → refine
loop**: a ruthless critic scores the design and the architect revises until it
clears the bar. A coding harness comes out with `apply_patch` / `run_shell` /
`python_exec` / `plan` and executable-grade quality gates; a video harness
(Higgsfield/Runway/Sora-style) comes out with `generate_image` / `generate_video`
and shot-by-shot production prompts.

**Every generated harness is also self-driving, extensible, and self-improving:**

- **MCP toolkits** — drop an `mcp.json` next to the app and give any agent the
  tool `mcp:<server>`; it gains every tool that server exposes (filesystem,
  GitHub, Slack, browsers, databases, …). `/mcp` lists what's connected.
- **`/goal <objective>` — loop until the goal is met.** A planner decomposes the
  goal into steps; the team runs one step per fresh cycle; a per-step judge gates
  each; passing checks it off, failing retries then replans around the blocker.
  The plan lives in `goal_state/` — kill it, rerun the same goal, it resumes.
- **`/improve <task>` — metaprompting.** The harness rewrites its own weakest
  agent prompt until the task passes the quality gate (originals backed up).
- **`/uploop [PRD]` — the upgrade loop.** Iterates over *every segment* of the
  harness — each identity, each skill, each output format, the quality criteria —
  upgrades them, and even **adds new agents and tools**, steered by an optional
  PRD. This is how a harness upskills itself toward "the ultimate version."
- **Lifecycle hooks** — drop scripts in `hooks/` (`pre_run`, `post_agent`,
  `on_gate_fail`, `on_goal_step`, `on_uploop`, …) or declare commands in
  `harness.json`. They fire at every lifecycle point with a JSON payload on
  stdin; use them to run tests, commit, or notify Slack — never blocking.
- **Credential auto-detection — usually no keys to paste.** Each provider is
  resolved from its env var, then the logins you already have on this computer:
  Anthropic via `ANTHROPIC_API_KEY` → `CLAUDE_CODE_OAUTH_TOKEN`
  (`claude setup-token`) → the Claude Code CLI login (macOS Keychain /
  `~/.claude`); OpenAI via `OPENAI_API_KEY` → the Codex CLI (`~/.codex/auth.json`).
  `/auth` shows exactly what was detected and from where. (OAuth tokens are sent
  as Bearer tokens, subject to each provider's terms; an API key always works.)

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
# -> harnesses/deep_research/: a standalone app with its own TUI (see above);
#    the commands below are the OPTIONAL builder-side tooling on the same dir

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

## What a generated harness IS

A standalone directory that owns itself — `harness build` is the last time it
touches this repo:

```
harnesses/<name>/
├── <name>              executable launcher
├── app.py              the WHOLE harness: providers, tools, 6 patterns,
│                       guardrails, memory, judge gate, interactive TUI —
│                       Python stdlib only, zero pip installs
├── harness.json        team wiring the app reads
├── prompts/ANATOMY.md  the {{slot}} assembly template (see below)
├── prompts/<agent>.md  §1 IDENTITY per agent — edit and re-run, no rebuild
├── skills/*.md         §3 BEHAVIORAL RULES
├── memory/MEMORY.md    durable facts injected every run
├── docs/               drop .md/.txt files → agents search them
└── workspace/ runs/    sandbox + one JSONL trace per run
```

Every agent's system prompt is assembled fresh per run from
`prompts/ANATOMY.md`, whose five explicit slots show exactly where each
architectural prompt goes:

```
{{IDENTITY}}          <- prompts/<agent>.md          who the agent is
{{ENVIRONMENT}}       <- injected live               runtime truth, never hand-written
{{BEHAVIORAL_RULES}}  <- skills/*.md                 the craft — largest section
{{OUTPUT_FORMAT}}     <- harness.json output_format  response shape
{{SAFETY}}            <- generated from guardrails   prompt and code cannot drift
```

`/prompts` inside the generated TUI renders this map with per-file token
estimates. `harness scaffold <dir>` regenerates the app for an existing
harness without touching your prompt/skill/memory edits.

## What a generated harness contains

| Capability | How |
|---|---|
| System prompts | architect writes a full 150-400 word brief per agent |
| Memory as files | skills/*.md (procedural) · memory/MEMORY.md (facts, human-editable) · semantic.json · episodic store |
| Subagents | 6 team patterns; supervisors get a `delegate` tool |
| **MCP — consume** | `mcp_servers:` in harness.yaml + `tools: ["mcp:<name>"]` → agents use any external MCP server's tools |
| **MCP — deploy** | `harness serve-mcp` exposes build_harness / run_<h> / loop_<h> to Codex, Claude Code, and other MCP hosts |
| **RAG** | `harness rag <h> add <files\|dirs\|urls>` → chunked corpus; `search_docs` tool + auto top-k into each agent's working memory |
| Domain tool references | least-privilege per agent from the registry (files, shell, web, memory, docs, MCP) |
| Context management | working-memory assembly per run + in-loop pruning (oldest tool results collapsed, newest 4 kept, agent notes never touched) |
| Any LLM | anthropic / openai / groq / openrouter / ollama, mixable per agent |

## Anatomy of every agent's system prompt

Every agent's working memory is assembled in the 5-section anatomy, in order
(position matters), fresh per run (freshness matters):

| § | Section | Source | Budget |
|---|---|---|---|
| 1 | Identity & Role | architect-written `system_prompt` (supports `{{working_directory}}`, `{{date}}`, `{{operating_system}}`, `{{shell}}` template vars) | ~300 tok |
| 2 | Environment | **harness-injected** runtime values — never hand-written | ~200 tok |
| 3 | Behavioral Rules | `skills/*.md` — the LARGEST section | ~1500 tok |
| 4 | Output Format | per-agent `output_format` field | ~400 tok |
| 5 | Safety & Security | **generated from the actual guardrail config** — prompt and code cannot drift; hard NEVER constraints | ~500 tok |

Then memory blocks (semantic top-k, episodic recency, RAG top-k) — finite,
just-in-time, quality over quantity. `harness lint <h>` checks every agent
against the anatomy and token budgets, and reports the per-call context cost.

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

### Connect Codex directly over MCP

To let Codex build and run harnesses as tools, install this project and register
its stdio server in a project-scoped `.codex/config.toml`:

```bash
python -m venv .venv
.venv/bin/pip install -e .
```

```toml
[mcp_servers.harness_builder]
command = "/absolute/path/to/harness-builder/.venv/bin/python"
args = ["-m", "harness_builder.mcp.server"]
cwd = "/absolute/path/to/harness-builder"
tool_timeout_sec = 3600
```

Restart Codex after changing MCP configuration, then use `/mcp` to confirm the
server and its `build_harness` and `list_harnesses` tools are available. Each
harness created under `./harnesses` also adds `run_<name>` and `loop_<name>`
tools on the next MCP connection.

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
matching prompts in `examples/prompts.md`. `harness use <name>` copies one
into ./harnesses and scaffolds its standalone app in the same step.

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
├── builder/architect.py    the meta-agent: prompt → harness design
├── builder/scaffold.py     design → STANDALONE app (stdlib-only runtime +
│                           TUI + {{slot}} prompt anatomy, zero deps)
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
- **Better generated harnesses**: edit `builder/architect.py:ARCHITECT_SYSTEM`
  (the highest-leverage prompt) or add a domain playbook in
  `builder/playbooks.py` (team shape + tools + quality bar for a new field).

## Tests

```bash
pip install -e ".[dev]" && pytest        # 70 hermetic tests, no network/keys
```

Covers spec/command validation, domain-detection routing, scaffold output
(streaming code, themes, launchers, MCP + loop config, hooks dir), the
streamed-SSE parser for both provider dialects (incl. tool-call reassembly and
fallback), the embedded tools (`apply_patch`/`python_exec`/`plan`/
`generate_video`), the **autonomous engines** (goal loop completes + persists +
resumes; `/improve` rewrites a prompt until it passes; `/uploop` upgrades
segments and adds an agent), **MCP** (a real stdio server subprocess: connect →
list → call → route), **hooks** (fire from dir + config, silent when absent),
and every bundled template (loads → scaffolds → compiles).

The generated TUI is a Python port of the interactive mode of
[pi_agent_rust](https://github.com/Dicklesworthstone/pi_agent_rust) (MIT),
vendored under `vendor/` for reference.

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
