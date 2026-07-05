"""
Memory — direct implementation of the architecture diagram:

  Working Memory / Context RAM   -> assembled fresh each run (ephemeral)
  Procedural Memory (skill.md)   -> markdown files: how to act
  Semantic Memory (vector store) -> durable facts, user profile   (RAG top-k)
  Episodic Memory (SQL + dates)  -> dated events, past chat history
                                    (RAG for relevance + SQL for recency)
  Summarizer Agent (cheap model) -> consolidates episodic -> semantic
                                    only after N new sessions

Semantic search uses embeddings when an OpenAI-compatible key is present,
with a zero-dependency token-overlap fallback so the system always works.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Procedural memory — skill files (how to act)
# ---------------------------------------------------------------------------
def load_skills(harness_dir: Path, skill_names: list[str]) -> str:
    parts = []
    for name in skill_names:
        p = harness_dir / "skills" / (name if name.endswith(".md") else f"{name}.md")
        if p.exists():
            parts.append(f"## SKILL: {p.stem}\n{p.read_text()}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Semantic memory — durable facts with top-k retrieval
# ---------------------------------------------------------------------------
class SemanticMemory:
    def __init__(self, harness_dir: Path):
        self.path = harness_dir / "memory" / "semantic.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.items: list[dict] = (
            json.loads(self.path.read_text()) if self.path.exists() else [])

    def _save(self):
        self.path.write_text(json.dumps(self.items, indent=1))

    def _embed(self, text: str) -> list[float] | None:
        if not os.environ.get("OPENAI_API_KEY"):
            return None
        try:
            from openai import OpenAI
            resp = OpenAI().embeddings.create(
                model="text-embedding-3-small", input=text[:8000])
            return resp.data[0].embedding
        except Exception:
            return None

    def add(self, fact: str, source: str = ""):
        ts = datetime.now(timezone.utc).isoformat()
        self.items.append({"fact": fact, "source": source, "ts": ts,
                           "emb": self._embed(fact)})
        self._save()
        # human-readable md mirror — memory you can open, edit, and diff
        md = self.path.parent / "MEMORY.md"
        header = "" if md.exists() else "# Semantic memory (durable facts)\n\n"
        with open(md, "a") as f:
            f.write(f"{header}- {fact}  <!-- {source} {ts[:10]} -->\n")

    @staticmethod
    def _cos(a, b):
        dot = sum(x * y for x, y in zip(a, b))
        na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
        return dot / (na * nb + 1e-9)

    @staticmethod
    def _tokens(text):
        return set(re.findall(r"[a-z0-9]{3,}", text.lower()))

    def search(self, query: str, k: int = 5) -> list[str]:
        if not self.items:
            return []
        q_emb = self._embed(query)
        scored = []
        q_tok = self._tokens(query)
        for it in self.items:
            if q_emb and it.get("emb"):
                s = self._cos(q_emb, it["emb"])
            else:  # fallback: query-coverage overlap (always works, zero deps)
                t = self._tokens(it["fact"])
                s = len(q_tok & t) / (len(q_tok) + 1e-9)
            scored.append((s, it["fact"]))
        scored.sort(reverse=True, key=lambda x: x[0])
        return [f for s, f in scored[:k] if s > 0.2]


# ---------------------------------------------------------------------------
# Episodic memory — dated events; RAG for relevance + SQL for recency
# ---------------------------------------------------------------------------
class EpisodicMemory:
    def __init__(self, harness_dir: Path):
        db = harness_dir / "memory" / "episodic.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db)
        self.conn.execute("""CREATE TABLE IF NOT EXISTS episodes (
            id INTEGER PRIMARY KEY, ts TEXT, task TEXT, summary TEXT,
            consolidated INTEGER DEFAULT 0)""")
        self.conn.commit()

    def add(self, task: str, summary: str):
        self.conn.execute(
            "INSERT INTO episodes (ts, task, summary) VALUES (?,?,?)",
            (datetime.now(timezone.utc).isoformat(), task, summary[:4000]))
        self.conn.commit()

    def recent(self, n: int = 3) -> list[str]:
        rows = self.conn.execute(
            "SELECT ts, task, summary FROM episodes ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
        return [f"[{ts[:10]}] task: {task}\n  outcome: {summ}" for ts, task, summ in rows]

    def unconsolidated(self) -> list[tuple[int, str]]:
        return self.conn.execute(
            "SELECT id, task || ' -> ' || summary FROM episodes WHERE consolidated=0"
        ).fetchall()

    def mark_consolidated(self, ids: list[int]):
        self.conn.executemany("UPDATE episodes SET consolidated=1 WHERE id=?",
                              [(i,) for i in ids])
        self.conn.commit()


# ---------------------------------------------------------------------------
# Summarizer agent — cheaper model distills episodes into semantic facts,
# only after N new sessions (exactly the diamond in the diagram).
# ---------------------------------------------------------------------------
def maybe_consolidate(harness_dir: Path, memory_spec, trace=None):
    epi = EpisodicMemory(harness_dir)
    pending = epi.unconsolidated()
    if len(pending) < memory_spec.consolidate_after:
        return
    from ..providers import api
    provider, model = api.resolve(memory_spec.summarizer_model)
    episodes_text = "\n".join(p[1] for p in pending)[:12000]
    resp = provider.chat(
        model=model,
        system="You distill session logs into durable facts worth remembering "
               "across future runs (user preferences, domain facts, decisions, "
               "recurring pitfalls). Output ONLY a JSON array of short fact strings. "
               "Max 8 facts. No prose.",
        messages=[{"role": "user", "content": episodes_text}],
        max_tokens=1024,
    )
    try:
        text = re.sub(r"```(json)?|```", "", resp.text).strip()
        facts = json.loads(text)
        sem = SemanticMemory(harness_dir)
        for f in facts:
            sem.add(str(f), source="summarizer")
        epi.mark_consolidated([p[0] for p in pending])
        if trace:
            trace.log("memory_consolidated", n_episodes=len(pending), n_facts=len(facts))
    except Exception as e:
        if trace:
            trace.log("memory_consolidation_failed", error=str(e))


# ---------------------------------------------------------------------------
# Working memory / Context RAM — assembled fresh per agent run (ephemeral).
# System prompt + skills (procedural) + RAG top-k (semantic) + recency (episodic).
# ---------------------------------------------------------------------------
def assemble_context(harness_dir: Path, agent_spec, task: str, memory_spec) -> str:
    blocks = [agent_spec.system_prompt.strip()]

    skills = load_skills(harness_dir, agent_spec.skills)
    if skills:
        blocks.append(f"# PROCEDURAL MEMORY (how to act)\n{skills}")

    if memory_spec.semantic:
        facts = SemanticMemory(harness_dir).search(task, k=5)
        if facts:
            blocks.append("# SEMANTIC MEMORY (durable facts, top-k relevant)\n"
                          + "\n".join(f"- {f}" for f in facts))

    from .rag import RagStore
    rag = RagStore(harness_dir)
    if len(rag):
        hits = rag.search(task, k=3)
        if hits:
            blocks.append("# REFERENCE DOCUMENTS (RAG top-k for this task)\n"
                          + "\n\n".join(f"[{h['source']}]\n{h['text'][:800]}"
                                          for h in hits))

    if memory_spec.episodic:
        recent = EpisodicMemory(harness_dir).recent(3)
        if recent:
            blocks.append("# EPISODIC MEMORY (recent sessions, newest first)\n"
                          + "\n".join(recent))

    blocks.append("Use memory as context, not gospel: verify anything critical. "
                  "When you learn a durable fact worth keeping, call save_fact.")
    return "\n\n".join(blocks)
