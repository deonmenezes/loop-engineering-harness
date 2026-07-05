"""
Builtin tool registry. Tools use a provider-neutral schema:
    {"name", "description", "parameters": <json-schema>}
Provider adapters translate to each API's dialect.

Agents get only the tools their spec lists — least privilege by default.
All file/shell activity is confined to the run's workspace directory.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


class ToolContext:
    """Injected runtime state: workspace, guardrails, memory handles, trace."""

    def __init__(self, workspace: Path, guardrails, semantic_memory=None,
                 trace=None, rag=None, mcp_pool=None):
        self.workspace = workspace
        self.guardrails = guardrails
        self.semantic_memory = semantic_memory
        self.trace = trace
        self.rag = rag
        self.mcp_pool = mcp_pool
        workspace.mkdir(parents=True, exist_ok=True)

    def safe_path(self, rel: str) -> Path:
        p = (self.workspace / rel).resolve()
        if not p.is_relative_to(self.workspace):
            raise ValueError(f"path escapes workspace: {rel}")
        return p


REGISTRY: dict[str, dict] = {}


def tool(name: str, description: str, parameters: dict):
    def deco(fn):
        REGISTRY[name] = {"name": name, "description": description,
                          "parameters": parameters, "fn": fn}
        return fn
    return deco


def schemas_for(names: list[str]) -> list[dict]:
    out = []
    for n in names:
        if n not in REGISTRY:
            raise ValueError(f"unknown tool '{n}'. Available: {sorted(REGISTRY)}")
        t = REGISTRY[n]
        out.append({"name": t["name"], "description": t["description"],
                    "parameters": t["parameters"]})
    return out


def execute(ctx: ToolContext, name: str, tool_input: dict) -> tuple[str, bool]:
    """Errors are feedback, not crashes: (result_text, is_error)."""
    if ctx.mcp_pool is not None and ctx.mcp_pool.owns(name):
        out, is_err = ctx.mcp_pool.call(name, tool_input)
        return out[:12000], is_err
    if name not in REGISTRY:
        return f"unknown tool '{name}'", True
    try:
        return str(REGISTRY[name]["fn"](ctx, **tool_input))[:12000], False
    except Exception as e:
        return f"{type(e).__name__}: {e}", True


# ------------------------------------------------------------------ files
@tool("read_file", "Read a UTF-8 text file (path relative to workspace).",
      {"type": "object", "properties": {"path": {"type": "string"}},
       "required": ["path"]})
def read_file(ctx, path: str):
    return ctx.safe_path(path).read_text(encoding="utf-8")


@tool("write_file", "Create or overwrite a UTF-8 text file (path relative to workspace).",
      {"type": "object",
       "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
       "required": ["path", "content"]})
def write_file(ctx, path: str, content: str):
    p = ctx.safe_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} chars to {path}"


@tool("list_files", "List files in the workspace (recursive).",
      {"type": "object", "properties": {}})
def list_files(ctx):
    files = [str(p.relative_to(ctx.workspace))
             for p in ctx.workspace.rglob("*") if p.is_file()]
    return "\n".join(sorted(files)) or "(workspace is empty)"


@tool("apply_patch", "Edit an existing file by exact search/replace. `find` "
                     "must appear verbatim (copy it from the file). Safer than "
                     "rewriting whole files — prefer this for code changes.",
      {"type": "object",
       "properties": {"path": {"type": "string"}, "find": {"type": "string"},
                      "replace": {"type": "string"},
                      "replace_all": {"type": "boolean"}},
       "required": ["path", "find", "replace"]})
def apply_patch(ctx, path: str, find: str, replace: str, replace_all=False):
    p = ctx.safe_path(path)
    text = p.read_text(encoding="utf-8")
    count = text.count(find)
    if count == 0:
        return (f"`find` not present in {path}. Read the file and copy an exact "
                "snippet (including whitespace).")
    if count > 1 and not replace_all:
        return (f"`find` appears {count}x in {path}; add surrounding context to "
                "make it unique, or set replace_all=true.")
    new = text.replace(find, replace)
    p.write_text(new, encoding="utf-8")
    return f"applied {count if replace_all else 1} edit(s) to {path} " \
           f"({len(new) - len(text):+d} chars)"


# ------------------------------------------------------------------ shell
@tool("run_shell", "Run a shell command in the workspace (cwd = workspace, 60s "
                   "timeout). Some commands are blocked by harness policy.",
      {"type": "object", "properties": {"command": {"type": "string"}},
       "required": ["command"]})
def run_shell(ctx, command: str):
    for pat in ctx.guardrails.shell_deny_patterns:
        if re.search(pat, command):
            return "DENIED by harness policy."
    proc = subprocess.run(command, shell=True, cwd=ctx.workspace,
                          capture_output=True, text=True, timeout=60)
    out = f"exit_code: {proc.returncode}\n"
    if proc.stdout:
        out += f"stdout:\n{proc.stdout}\n"
    if proc.stderr:
        out += f"stderr:\n{proc.stderr}\n"
    return out


@tool("python_exec", "Run a short Python 3 snippet in the workspace (cwd = "
                     "workspace, 30s). Returns stdout+stderr. Use for "
                     "computation, data work, and quick checks.",
      {"type": "object", "properties": {"code": {"type": "string"}},
       "required": ["code"]})
def python_exec(ctx, code: str):
    import sys
    proc = subprocess.run([sys.executable, "-c", code], cwd=ctx.workspace,
                          capture_output=True, text=True, timeout=30)
    out = f"exit_code: {proc.returncode}\n"
    if proc.stdout:
        out += f"stdout:\n{proc.stdout}\n"
    if proc.stderr:
        out += f"stderr:\n{proc.stderr}\n"
    return out


@tool("plan", "Record or update your working plan as a markdown checklist "
              "(saved to workspace/PLAN.md). Returns the plan so you can track "
              "progress across turns. Call again to revise it.",
      {"type": "object", "properties": {"plan": {"type": "string"}},
       "required": ["plan"]})
def plan(ctx, plan: str):
    (ctx.workspace / "PLAN.md").write_text(plan, encoding="utf-8")
    return "plan saved:\n" + plan


# ------------------------------------------------------------------ web
@tool("web_search", "Search the web via DuckDuckGo lite HTML. Returns titles, "
                    "URLs, snippets. Use for research; then fetch_url for depth.",
      {"type": "object", "properties": {"query": {"type": "string"}},
       "required": ["query"]})
def web_search(ctx, query: str):
    import urllib.parse
    import urllib.request
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    html = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
    results = re.findall(
        r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?'
        r'class="result__snippet"[^>]*>(.*?)</a>', html, re.S)[:6]
    clean = lambda s: re.sub(r"<[^>]+>", "", s).strip()
    if not results:
        return "no results (search endpoint may be blocked in this environment)"
    return "\n\n".join(f"{clean(t)}\n{u}\n{clean(s)}" for u, t, s in results)


@tool("fetch_url", "Fetch a URL and return its text content (HTML stripped).",
      {"type": "object", "properties": {"url": {"type": "string"}},
       "required": ["url"]})
def fetch_url(ctx, url: str):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    html = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
    text = re.sub(r"<(script|style).*?</\1>", " ", html, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text)[:10000]


@tool("http_get", "HTTP GET a URL and return the raw body (JSON/text, not "
                  "HTML-stripped). Use for APIs; use fetch_url for web pages.",
      {"type": "object", "properties": {"url": {"type": "string"}},
       "required": ["url"]})
def http_get(ctx, url: str):
    import urllib.request
    req = urllib.request.Request(url, headers={
        "User-Agent": "harness/1.0", "Accept": "application/json, */*"})
    return urllib.request.urlopen(req, timeout=20).read().decode(
        "utf-8", "ignore")[:10000]


# ---------------------------------------------------------------- generation
def _generate_media(ctx, kind: str, prompt: str, extra: dict, filename: str):
    """Call a configured <KIND>_API_URL endpoint, or (unconfigured) write a
    ready-to-run prompt spec to the workspace so the pipeline still ships an
    artifact. Pluggable to Higgsfield/Runway/DALL·E/Sora/etc. via env."""
    import json
    import os
    import urllib.request
    url = os.environ.get(f"{kind}_API_URL")
    if url:
        headers = {"Content-Type": "application/json"}
        key = os.environ.get(f"{kind}_API_KEY")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        body = json.dumps({"prompt": prompt, **extra}).encode()
        try:
            req = urllib.request.Request(url, data=body, headers=headers)
            resp = urllib.request.urlopen(req, timeout=180).read().decode(
                "utf-8", "ignore")
            return f"{kind.lower()} generation response:\n{resp[:3000]}"
        except Exception as e:
            return f"{kind.lower()} endpoint {url} error: {e}"
    spec = f"# {kind.title()} generation spec\nprompt: {prompt}\n" + \
        "\n".join(f"{k}: {v}" for k, v in extra.items())
    ctx.safe_path(filename).write_text(spec, encoding="utf-8")
    return (f"No {kind}_API_URL configured — wrote a ready-to-run prompt spec to "
            f"{filename}. Paste it into your generator, or set {kind}_API_URL "
            f"(+ optional {kind}_API_KEY) to render here.\n\n{spec}")


@tool("generate_image", "Generate an image from a prompt. Calls IMAGE_API_URL "
                        "if configured; otherwise writes a ready-to-run image "
                        "prompt spec to the workspace.",
      {"type": "object",
       "properties": {"prompt": {"type": "string"},
                      "aspect_ratio": {"type": "string"},
                      "style": {"type": "string"},
                      "filename": {"type": "string"}},
       "required": ["prompt"]})
def generate_image(ctx, prompt: str, aspect_ratio="16:9", style="",
                   filename="image_prompt.txt"):
    return _generate_media(ctx, "IMAGE", prompt,
                           {"aspect_ratio": aspect_ratio, "style": style},
                           filename)


@tool("generate_video", "Generate a video clip from a prompt. Calls "
                        "VIDEO_API_URL if configured (Higgsfield/Runway/Sora/"
                        "etc.); otherwise writes a ready-to-run video prompt "
                        "spec to the workspace.",
      {"type": "object",
       "properties": {"prompt": {"type": "string"},
                      "duration_s": {"type": "number"},
                      "aspect_ratio": {"type": "string"},
                      "camera": {"type": "string"},
                      "filename": {"type": "string"}},
       "required": ["prompt"]})
def generate_video(ctx, prompt: str, duration_s=4, aspect_ratio="16:9",
                   camera="", filename="video_prompt.txt"):
    return _generate_media(ctx, "VIDEO", prompt,
                           {"duration_s": duration_s,
                            "aspect_ratio": aspect_ratio, "camera": camera},
                           filename)


# ------------------------------------------------------------------ memory
@tool("save_fact", "Save a durable fact to semantic memory so future runs of "
                   "this harness remember it.",
      {"type": "object", "properties": {"fact": {"type": "string"}},
       "required": ["fact"]})
def save_fact(ctx, fact: str):
    if ctx.semantic_memory is None:
        return "semantic memory disabled for this harness"
    ctx.semantic_memory.add(fact, source="agent")
    return "saved"


@tool("recall", "Search semantic memory for durable facts relevant to a query.",
      {"type": "object", "properties": {"query": {"type": "string"}},
       "required": ["query"]})
def recall(ctx, query: str):
    if ctx.semantic_memory is None:
        return "semantic memory disabled for this harness"
    facts = ctx.semantic_memory.search(query, k=5)
    return "\n".join(f"- {f}" for f in facts) or "(nothing relevant stored)"


@tool("search_docs", "Search the harness's ingested document corpus (RAG). "
                     "Use for domain reference material added via `harness rag`.",
      {"type": "object", "properties": {"query": {"type": "string"}},
       "required": ["query"]})
def search_docs(ctx, query: str):
    if ctx.rag is None or len(ctx.rag) == 0:
        return "no documents ingested for this harness (harness rag <dir> add <path|url>)"
    hits = ctx.rag.search(query, k=4)
    return "\n\n".join(f"[source: {h['source']}]\n{h['text']}" for h in hits) \
        or "(nothing relevant in the corpus)"
