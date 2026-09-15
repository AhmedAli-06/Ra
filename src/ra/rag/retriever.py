"""Retrieval orchestration: embed the question, pull top-k, format for the LLM."""
from dataclasses import dataclass


@dataclass
class Result:
    score: float
    source_type: str
    source: str
    path: str
    text: str


def retrieve(store, embedder, query: str, k: int = 4, source_types: list = None,
             min_score: float = 0.0) -> list:
    query_vec = embedder.embed(query)
    rows = store.search(query_vec, k=k, source_types=source_types)
    results = [
        Result(
            score=r["score"],
            source_type=r["source_type"],
            source=r["source"],
            path=r["path"],
            text=r["text"],
        )
        for r in rows
        if r["score"] >= min_score
    ]
    return results


def format_context(results: list, max_chars: int = 2200) -> str:
    lines, used = [], 0
    for r in results:
        snippet = " ".join(r.text.split())
        if len(snippet) > 600:
            snippet = snippet[:597] + "..."
        if used + len(snippet) > max_chars:
            if not lines:
                lines.append(snippet)
            break
        used += len(snippet)
        lines.append(f"[{r.source_type} | {r.source} | score {r.score:.2f}]\n{snippet}")
    return "\n\n".join(lines)