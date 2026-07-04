# Use Cases — Try These Prompts

Each prompt below regenerates (via `harness build "<prompt>"`) roughly the
matching bundled template — or just `harness use <name>` to start from ours.

**Deep Research** (`deep_research`) — Build a harness for deep research. I need an agent team that can investigate any topic from multiple angles — web search, academic sources, community sentiment — then cross-validate findings and produce a comprehensive report.

**Website Development** (`website_dev`) — Build a harness for full-stack website development. The team should handle design, frontend (React/Next.js), backend (API), and QA testing in a coordinated pipeline from wireframe to deployment.

**Webtoon Production** (`webtoon_production`) — Build a harness for webtoon episode production. I need agents for story writing, character design prompts, panel layout planning, and dialogue editing. They should review each other's work for style consistency.

**YouTube Content** (`youtube_content`) — Build a harness for YouTube content creation. The team should research trending topics, write scripts, optimize titles/tags for SEO, and plan thumbnail concepts — all coordinated by a supervisor agent.

**Code Review** (`code_review`) — Build a harness for comprehensive code review. I want parallel agents checking architecture, security vulnerabilities, performance bottlenecks, and code style — then merging all findings into a single report.

**Technical Documentation** (`tech_docs`) — Build a harness that generates API documentation from this codebase. Agents should analyze endpoints, write descriptions, generate usage examples, and review for completeness.

**Data Pipeline Design** (`data_pipeline`) — Build a harness for designing data pipelines. I need agents for schema design, ETL logic, data validation rules, and monitoring setup that delegate sub-tasks hierarchically.

**Marketing Campaign** (`marketing_campaign`) — Build a harness for marketing campaign creation. The team should research the target market, write ad copy, design visual concepts, and set up A/B test plans with iterative quality review.

## Power moves

```bash
# code review with a deterministic gate: keep iterating until tests pass
harness run harnesses/code_review --task "review and fix ./src" --loop --verify-cmd "pytest -q"

# run an entire harness on a free local model
harness run harnesses/youtube_content --task "video about home espresso" --model-override ollama/llama3.1

# read what actually happened
cat traces/<latest>.jsonl | python -m json.tool --json-lines | less
```
