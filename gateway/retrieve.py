"""Vector retrieval over the accounts-payable policy corpus.

Each markdown file in the corpus root is one passage. Passages are embedded
with Ollama and ranked by cosine similarity to the query. A directory outside
the root, such as payroll, is never opened.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
from urllib import request

EMBED_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text")
MIN_SCORE = float(os.environ.get("RAG_MIN_SCORE", "0.50"))
_index: list[dict] | None = None
_index_root: Path | None = None


def search(root: Path, query: str, limit: int = 2) -> list[dict]:
    index = _load_index(root)
    if not index or not query.strip():
        return []
    query_vector = embed([query])[0]
    return rank(index, query_vector, limit)


def rank(chunks: list[dict], query_vector: list[float], limit: int, min_score: float | None = None) -> list[dict]:
    floor = MIN_SCORE if min_score is None else min_score
    scored = [(cosine(query_vector, chunk["vector"]), chunk) for chunk in chunks]
    scored.sort(key=lambda item: (-item[0], item[1]["source"]))
    out = []
    for score, chunk in scored[:limit]:
        if score < floor:
            continue
        out.append({"source": chunk["source"], "text": chunk["text"], "score": round(score, 4)})
    return out


def load_chunks(root: Path) -> list[dict]:
    chunks = []
    if not root.is_dir():
        return chunks
    for path in sorted(root.glob("*.md")):
        text = path.read_text().strip()
        if text:
            chunks.append({"source": path.name, "text": text})
    return chunks


def cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def embed(texts: list[str]) -> list[list[float]]:
    url = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/") + "/api/embed"
    body = json.dumps({"model": EMBED_MODEL, "input": texts}).encode()
    req = request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with request.urlopen(req, timeout=60) as resp:
        doc = json.loads(resp.read().decode())
    vectors = doc.get("embeddings")
    if not isinstance(vectors, list) or len(vectors) != len(texts):
        raise RuntimeError(f"embedding model {EMBED_MODEL} returned an unexpected payload")
    return vectors


def _load_index(root: Path) -> list[dict]:
    global _index, _index_root
    if _index is not None and _index_root == root:
        return _index
    chunks = load_chunks(root)
    if not chunks:
        _index, _index_root = [], root
        return _index
    vectors = embed([chunk["text"] for chunk in chunks])
    _index = [{**chunk, "vector": vector} for chunk, vector in zip(chunks, vectors)]
    _index_root = root
    return _index
