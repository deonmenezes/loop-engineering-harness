"""
Document RAG per harness — ingest files/URLs into a chunk store, retrieve
top-k per query. Two consumers:
  1. the `search_docs` tool (agents query the corpus on demand)
  2. working-memory assembly (top-k relevant chunks auto-included per task)

Store: <harness>/memory/rag.json  [{text, source, emb|null}, ...]
Embeddings via OpenAI when a key is present; token-overlap fallback otherwise
(same policy as semantic memory — the system always works offline).
"""

from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path

CHUNK_CHARS = 1200
OVERLAP = 150


def _chunk(text: str) -> list[str]:
    """Split on paragraph boundaries into ~CHUNK_CHARS pieces with overlap."""
    paras = re.split(r"\n\s*\n", text)
    chunks, cur = [], ""
    for p in paras:
        if len(cur) + len(p) > CHUNK_CHARS and cur:
            chunks.append(cur.strip())
            cur = cur[-OVERLAP:] + "\n" + p
        else:
            cur += "\n\n" + p
    if cur.strip():
        chunks.append(cur.strip())
    return [c for c in chunks if len(c) > 50]


def _tokens(text: str) -> set:
    return set(re.findall(r"[a-z0-9]{3,}", text.lower()))


def _embed(texts: list[str]) -> list:
    if not os.environ.get("OPENAI_API_KEY"):
        return [None] * len(texts)
    try:
        from openai import OpenAI
        resp = OpenAI().embeddings.create(model="text-embedding-3-small",
                                          input=[t[:8000] for t in texts])
        return [d.embedding for d in resp.data]
    except Exception:
        return [None] * len(texts)


class RagStore:
    def __init__(self, harness_dir: Path):
        self.path = Path(harness_dir) / "memory" / "rag.json"
        self.items: list[dict] = (
            json.loads(self.path.read_text()) if self.path.exists() else [])

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.items))

    # ------------------------------------------------------------ ingest
    def add_text(self, text: str, source: str) -> int:
        chunks = _chunk(text)
        embs = _embed(chunks)
        for c, e in zip(chunks, embs):
            self.items.append({"text": c, "source": source, "emb": e})
        self._save()
        return len(chunks)

    def add_path_or_url(self, target: str) -> int:
        if target.startswith(("http://", "https://")):
            import urllib.request
            req = urllib.request.Request(target,
                                         headers={"User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=30).read() \
                .decode("utf-8", "ignore")
            text = re.sub(r"(?s)<(script|style).*?</\1>", " ", raw)
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"[ \t]+", " ", text)
            return self.add_text(text, target)
        p = Path(target)
        if p.is_dir():
            total = 0
            for f in sorted(p.rglob("*")):
                if f.suffix.lower() in (".md", ".txt", ".rst", ".py", ".rs",
                                        ".js", ".ts", ".json", ".yaml", ".yml",
                                        ".html", ".csv"):
                    total += self.add_text(f.read_text(errors="ignore"), str(f))
            return total
        return self.add_text(p.read_text(errors="ignore"), str(p))

    # ---------------------------------------------------------- retrieve
    def search(self, query: str, k: int = 4) -> list[dict]:
        if not self.items:
            return []
        q_emb = _embed([query])[0]
        q_tok = _tokens(query)
        scored = []
        for it in self.items:
            if q_emb and it.get("emb"):
                dot = sum(x * y for x, y in zip(q_emb, it["emb"]))
                na = math.sqrt(sum(x * x for x in q_emb))
                nb = math.sqrt(sum(x * x for x in it["emb"]))
                s = dot / (na * nb + 1e-9)
            else:
                t = _tokens(it["text"])
                # query coverage, not Jaccard: short queries vs long chunks
                s = len(q_tok & t) / (len(q_tok) + 1e-9)
            scored.append((s, it))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [it for s, it in scored[:k] if s > 0.25]

    def __len__(self):
        return len(self.items)
