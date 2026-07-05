"""
Domain playbooks — the knowledge that makes a *generated* harness excellent
in its field, not merely generic.

The architect is one LLM call away from either a bland 3-agent skeleton or a
harness that rivals a purpose-built tool. The difference is domain knowledge:
which team shape a coding agent needs vs a video-generation pipeline, which
tools each demands, what "great output" even means. We can't fit all that in
one system prompt, so we detect the domain from the user's sentence and inject
the matching playbook.

Each playbook is opinionated on purpose. Vague guidance produces vague
harnesses. These read like a senior practitioner briefing the architect.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Playbook:
    name: str
    keywords: list[str]                      # detection signal (word-boundary)
    guidance: str                            # injected into the architect prompt
    weight: float = 1.0                      # tie-breaker for overlapping domains
    exemplars: list[str] = field(default_factory=list)  # what "great" looks like


PLAYBOOKS: list[Playbook] = [
    Playbook(
        name="coding",
        weight=1.15,
        keywords=[
            "code", "coding", "coder", "software", "programmer", "programming",
            "refactor", "debug", "bug", "compiler", "codebase", "repository",
            "repo", "pull request", "pr review", "unit test", "tests", "tdd",
            "api", "backend", "frontend", "cli", "sdk", "library", "function",
            "typescript", "javascript", "python", "rust", "golang", "java",
            "c++", "implement", "engineer", "devops", "ci", "lint", "patch",
        ],
        guidance="""\
DOMAIN: software engineering (target parity with Claude Code / Codex / Cursor).
- Pattern: prefer `supervisor` for open-ended "build/fix X" work (a lead that
  plans, delegates, and integrates) or `producer_reviewer` when correctness is
  paramount (implementer + reviewer looping to green). Use `pipeline` only for
  fixed stages (spec -> implement -> test -> document).
- Agents to consider: a `planner`/`lead` (decomposes, owns the todo list), an
  `implementer` (writes code), a `reviewer` (logic defects, edge cases, SOLID,
  security), a `test_engineer` (writes+runs tests, hardens flaky ones). 3-5 total.
- Tools (least privilege, but coding needs real reach): implementer & tester get
  `read_file`, `write_file`, `apply_patch`, `list_files`, `run_shell`,
  `python_exec`, `plan`. Reviewer gets read-only (`read_file`, `list_files`,
  `run_shell`). Everyone doing multi-step work benefits from `plan`.
- Output format: reviewers report severity-rated findings with `file:line` and a
  concrete fix; implementers state the diff they applied and how they verified it
  (command + observed result), never "should work".
- Quality criteria must be executable-grade: "all new/changed code has tests that
  pass", "no undefined names / imports resolve", "reviewer's critical+high
  findings are all resolved", "the described verification was actually run".
- Skills must encode: read-before-write, make the smallest change that works,
  match surrounding style, run the tests you claim pass, prefer editing to
  rewriting, never invent APIs — check them.""",
        exemplars=[
            "A reviewer that cites `auth/session.py:42` with the exact failing "
            "input and the one-line fix beats one that says 'improve error "
            "handling'.",
        ],
    ),
    Playbook(
        name="video",
        weight=1.1,
        keywords=[
            "video", "film", "cinematic", "footage", "b-roll", "shot", "scene",
            "storyboard", "animation", "animate", "motion", "vfx", "render",
            "higgsfield", "runway", "sora", "veo", "pika", "kling", "cutscene",
            "trailer", "reel", "short film", "music video", "ad spot",
            "text-to-video", "image-to-video", "keyframe",
        ],
        guidance="""\
DOMAIN: AI video generation (target parity with Higgsfield / Runway / Sora
workflows). The harness orchestrates a *creative production pipeline*, then
emits generation-ready prompts and (where a tool is wired) actual renders.
- Pattern: `pipeline` (concept -> script/beats -> shot list -> per-shot prompts
  -> style/continuity review) or `supervisor` (a creative director delegating to
  specialists). 4-6 agents.
- Agents to consider: `concept_developer` (logline, mood, references),
  `screenwriter`/`beat_planner` (story beats, pacing), `shot_designer` (shot
  list: framing, lens, camera move, duration), `prompt_engineer` (writes the
  literal text-to-video/image-to-video prompts with camera, lighting, motion,
  style tokens per model), `continuity_director` (character/style/lighting
  consistency across shots), optionally `sound_designer` (music + SFX cues).
- Tools: prompt_engineer gets `generate_image` and/or `generate_video` (wired to
  the user's provider — Higgsfield/Runway/etc.; falls back to writing a
  ready-to-run prompt spec if no key). Researchers get `web_search`+`fetch_url`
  for references. All get `write_file` to save the shot bible / prompt sheets.
- Output format: a *shot-by-shot production sheet* — per shot: id, duration,
  camera (angle/lens/movement), subject+action, lighting, style tokens, the
  exact generation prompt, and a negative prompt. Plus a one-paragraph continuity
  note tying shots together.
- Quality criteria: "every shot has a complete, model-ready prompt", "camera and
  lighting are specified, not implied", "character/style descriptors are
  identical across shots (continuity)", "total runtime matches the brief",
  "prompts name concrete visual nouns, not adjectives like 'beautiful'".
- Skills must encode real prompt craft: describe camera moves (dolly, crane,
  whip-pan), lens (35mm, macro), lighting (golden hour, rim light), and lock a
  reusable style token block so every shot looks like one film.""",
        exemplars=[
            "Great: 'Shot 3, 4s. Slow dolly-in, 50mm, shallow DOF. Rain-soaked "
            "neon alley, subject in red coat (SAME as shot 1), rim-lit teal, "
            "volumetric fog. Prompt: <full model prompt>. Negative: blurry, "
            "extra fingers.'",
        ],
    ),
    Playbook(
        name="research",
        weight=1.0,
        keywords=[
            "research", "investigate", "analysis", "analyze", "report", "study",
            "literature", "sources", "cite", "citation", "evidence", "survey",
            "market research", "competitive", "due diligence", "fact-check",
            "deep research", "synthesize", "findings", "whitepaper",
        ],
        guidance="""\
DOMAIN: deep research (target parity with deep-research agents).
- Pattern: `fanout` — independent researchers each own an angle (web, academic,
  community/primary sources, contrarian/steelman), a final `synthesis_editor`
  merges into one cited report. LAST agent in `flow` is the merger.
- Tools: researchers get `web_search`, `fetch_url`, `search_docs` (RAG corpus),
  `save_fact`/`recall`. Editor gets `read_file`/`write_file`, `recall`.
- Output format: a structured report with an executive summary, thematic
  sections, and a Sources list; EVERY nontrivial claim carries an inline source
  URL fetched during the run.
- Quality criteria: "every claim cites a source URL that was actually fetched",
  "at least N independent sources triangulate the key finding", "disagreements
  between sources are surfaced, not hidden", "no fabricated citations".
- Skills must encode: search broadly then read deeply, prefer primary sources,
  quote precisely, flag uncertainty, and adversarially check the headline claim
  before asserting it.""",
    ),
    Playbook(
        name="writing",
        weight=1.0,
        keywords=[
            "write", "writing", "copy", "copywriting", "blog", "article",
            "essay", "story", "narrative", "novel", "script", "screenplay",
            "newsletter", "content", "editor", "editing", "prose", "ghostwrite",
            "webtoon", "comic", "manga", "poem", "book",
        ],
        guidance="""\
DOMAIN: long-form / creative writing.
- Pattern: `producer_reviewer` (writer + editor looping to a quality bar) for a
  single polished piece, or `pipeline` (outline -> draft -> line-edit -> proof)
  for structured production.
- Agents: `writer` (voice, structure, momentum), `editor` (clarity, cuts, fact
  and consistency checks), optionally `researcher` (feeds facts) and
  `stylist`/`proofreader`.
- Tools: writer/editor get `read_file`/`write_file`; researcher gets
  `web_search`/`fetch_url`/`search_docs`. Keep pure-writing roles tool-light.
- Output format: the finished piece in clean markdown, plus (for the editor) a
  short changelog of what was cut/tightened and why.
- Quality criteria: domain-specific and checkable — "opens with a hook, not a
  throat-clear", "every section earns its place", "consistent POV/tense",
  "no cliches or filler", "hits the target length +-10%".
- Skills must encode concrete craft: show-don't-tell, vary sentence rhythm, cut
  the first paragraph, kill adverbs, land the ending.""",
    ),
    Playbook(
        name="data",
        weight=1.0,
        keywords=[
            "data", "dataset", "etl", "pipeline", "sql", "database", "schema",
            "analytics", "dashboard", "warehouse", "pandas", "dataframe",
            "csv", "json", "transform", "ingest", "validation", "metrics",
            "chart", "visualization", "statistics", "ml", "model training",
        ],
        guidance="""\
DOMAIN: data engineering / analysis.
- Pattern: `hierarchical` or `supervisor` for design-heavy work (a lead that
  decomposes into schema, ingestion, validation, monitoring), or `pipeline` for
  a fixed ingest -> transform -> validate -> report flow.
- Agents: `data_architect` (schema, contracts), `etl_engineer` (extraction +
  transforms), `validator` (quality gates, null/range/dup checks), `analyst`
  (metrics, findings, viz spec).
- Tools: engineers get `read_file`/`write_file`/`run_shell`/`python_exec`/
  `list_files`; analyst gets `python_exec`, `read_file`, `write_file`.
- Output format: runnable artifacts (SQL/Python files written to workspace) plus
  a short design doc; the analyst returns findings with the numbers that back
  them and a chart/table spec.
- Quality criteria: "transforms are runnable and idempotent", "validation covers
  nulls/ranges/dupes/uniqueness", "every metric states its definition", "no
  silent data loss between stages".
- Skills must encode: define the contract first, validate at every boundary,
  make transforms deterministic, and never trust input shape.""",
    ),
    Playbook(
        name="design",
        weight=1.0,
        keywords=[
            "design", "ui", "ux", "interface", "frontend", "website", "web app",
            "landing page", "component", "figma", "css", "tailwind", "react",
            "brand", "logo", "visual", "layout", "wireframe", "prototype",
            "mockup", "responsive", "accessibility",
        ],
        guidance="""\
DOMAIN: product / UI design & frontend.
- Pattern: `pipeline` (UX/IA -> visual design -> component build -> a11y/QA) or
  `supervisor` (design lead delegating). 3-5 agents.
- Agents: `ux_designer` (flows, IA, states), `visual_designer` (type, color,
  spacing, brand), `frontend_engineer` (accessible, responsive components),
  `qa_reviewer` (a11y, responsiveness, visual polish).
- Tools: engineer gets file tools + `run_shell`; designers stay lighter with
  `write_file` for specs and `web_search` for references.
- Output format: engineers emit real component code; designers emit concrete
  specs (exact tokens: colors as hex, spacing scale, type ramp, states).
- Quality criteria: "meets WCAG AA contrast", "keyboard-navigable", "responsive
  at mobile/tablet/desktop", "uses a consistent spacing/type scale", "no generic
  AI-template look — has a deliberate point of view".
- Skills must encode: design tokens first, real content not lorem, states matter
  (hover/focus/empty/error/loading), and accessibility is not optional.""",
    ),
    Playbook(
        name="marketing",
        weight=0.95,
        keywords=[
            "marketing", "campaign", "ads", "advertising", "seo", "growth",
            "social media", "brand", "audience", "conversion", "funnel",
            "email", "launch", "positioning", "messaging", "gtm", "ugc",
            "influencer", "youtube", "tiktok", "thumbnail", "engagement",
        ],
        guidance="""\
DOMAIN: marketing / growth.
- Pattern: `supervisor` (a strategist/director coordinating research, copy,
  creative, and measurement) or `fanout` (parallel channel angles merged).
- Agents: `market_researcher` (audience, competitors, trends), `strategist`
  (positioning, messaging, plan), `copywriter` (headlines, hooks, CTAs),
  `creative_director` (thumbnail/visual concepts), `analyst` (A/B + KPI plan).
- Tools: researchers get `web_search`/`fetch_url`; creatives get `write_file`
  and optionally `generate_image` for concepts.
- Output format: a campaign brief with audience, positioning, 3+ headline
  variants, channel plan, and a measurable success metric per asset.
- Quality criteria: "positioning is specific to one audience", "hooks lead with
  a benefit or tension", "each asset has an owner metric", "claims are supported,
  not hype".
- Skills must encode: one audience per message, lead with the hook, write
  variants to test, and tie every asset to a KPI.""",
    ),
]


GENERAL_GUIDANCE = """\
DOMAIN: general / mixed. Choose the pattern from first principles:
- dependent stages -> pipeline; independent angles + merge -> fanout;
  quality-critical single deliverable -> producer_reviewer; dynamic
  decomposition -> supervisor; deep multi-layer decomposition -> hierarchical;
  varied one-off tasks -> expert_pool.
Give agents only the tools their role needs, make quality criteria concrete and
checkable, and put the real craft in the skills files."""


def detect(prompt: str, top_k: int = 2) -> list[Playbook]:
    """Rank playbooks by weighted keyword hits in the prompt. Returns the best
    matches (may be empty -> caller falls back to GENERAL_GUIDANCE)."""
    low = prompt.lower()
    scored: list[tuple[float, Playbook]] = []
    for pb in PLAYBOOKS:
        hits = 0
        for kw in pb.keywords:
            pat = re.escape(kw)
            # word-ish boundary so "api" doesn't match "capitalize"
            if re.search(rf"(?<![a-z]){pat}(?![a-z])", low):
                hits += 1
        if hits:
            scored.append((hits * pb.weight, pb))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [pb for _, pb in scored[:top_k]]


def guidance_for(prompt: str) -> tuple[str, list[str]]:
    """(injected guidance block, matched domain names) for a build prompt."""
    matches = detect(prompt)
    if not matches:
        return GENERAL_GUIDANCE, ["general"]
    blocks = []
    for pb in matches:
        block = pb.guidance
        if pb.exemplars:
            block += "\nExemplar — " + " ".join(pb.exemplars)
        blocks.append(block)
    # secondary domain gets a lighter touch
    joined = blocks[0]
    if len(blocks) > 1:
        joined += ("\n\nSECONDARY DOMAIN SIGNAL (blend in where relevant):\n"
                   + blocks[1].split("\n", 1)[0])
    return joined, [pb.name for pb in matches]
