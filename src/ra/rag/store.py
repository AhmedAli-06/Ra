"""
SQLite-backed semantic store for Ra.

Each indexed chunk lives in `chunks`; its embedding lives in `vectors`.
Search loads all vectors and scores them with cosine similarity - simple,
fast, and honest for a personal knowledge base (thousands of chunks).
"""
import json
import sqlite3
from datetime import datetime, timezone


class SemanticStore:
    def __init__(self, path: str):
        self.path = path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self):
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS chunks("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " source_type TEXT NOT NULL,"
                " source TEXT NOT NULL,"
                " path TEXT,"
                " chunk_index INTEGER,"
                " text TEXT NOT NULL,"
                " created_at TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS vectors("
                " chunk_id INTEGER PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,"
                " vec TEXT NOT NULL)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_type ON chunks(source_type)")

    def add_chunks(self, chunks: list, embedder) -> int:
        with self._connect() as conn:
            for chunk in chunks:
                text = chunk["text"].strip()
                if not text:
                    continue
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                cur = conn.execute(
                    "INSERT INTO chunks(source_type, source, path, chunk_index, text, created_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (chunk["source_type"], chunk["source"], chunk.get("path"),
                     chunk.get("chunk_index"), text, now),
                )
                vec = embedder.embed(text)
                conn.execute(
                    "INSERT INTO vectors(chunk_id, vec) VALUES(?,?)",
                    (cur.lastrowid, json.dumps(vec)),
                )
        return len(chunks)

    def search(self, query_vec: list, k: int = 4, source_types: list = None) -> list:
        with self._connect() as conn:
            if source_types:
                q = ",".join("?" * len(source_types))
                rows = conn.execute(
                    f"SELECT c.id, c.source_type, c.source, c.path, c.text, v.vec"
                    " FROM chunks c JOIN vectors v ON v.chunk_id = c.id"
                    f" WHERE c.source_type IN ({q})", source_types,
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT c.id, c.source_type, c.source, c.path, c.text, v.vec"
                    " FROM chunks c JOIN vectors v ON v.chunk_id = c.id"
                ).fetchall()
        scored = []
        for row in rows:
            vec = json.loads(row["vec"])
            score = sum(a * b for a, b in zip(query_vec, vec))
            scored.append({
                "score": score,
                "source_type": row["source_type"],
                "source": row["source"],
                "path": row["path"],
                "text": row["text"],
            })
        scored.sort(key=lambda r: r["score"], reverse=True)
        return scored[:k]

    def delete_source(self, source: str) -> int:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM chunks WHERE source = ?", (source,))
        return cur.rowcount

    def clear(self) -> int:
        with self._connect() as conn:
            conn.execute("DELETE FROM vectors")
            cur = conn.execute("DELETE FROM chunks")
        return cur.rowcount

    def stats(self) -> dict:
        with self._connect() as conn:
            by_type = conn.execute(
                "SELECT source_type, COUNT(*) AS n FROM chunks"
                " GROUP BY source_type ORDER BY n DESC"
            ).fetchall()
            total = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
        return {"total_chunks": total, "by_type": {r["source_type"]: r["n"] for r in by_type}}

    def sources(self, limit: int = 100) -> list:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT source, source_type, path FROM chunks ORDER BY source LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]