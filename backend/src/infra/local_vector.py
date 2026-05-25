"""本地开发用的简易向量存储（SQLite + 内存向量搜索）"""
from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from typing import Any

from src.infra.embedding import embed

# local_vector.py is at backend/src/infra/local_vector.py → parents[2] = backend/
_DEFAULT_DB_PATH = str(Path(__file__).resolve().parents[2] / "data" / "local_vector.db")


class LocalVectorStore:
    """SQLite-backed vector store for local dev (no Docker needed)."""

    def __init__(self, db_path: str | None = None):
        # Default path is anchored to backend/, not CWD — keeps it portable
        # across uvicorn-from-backend, python-from-root, and systemd launches.
        self.db_path = Path(db_path or _DEFAULT_DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self._init_schema()

    def _init_schema(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS restaurants (
                id TEXT PRIMARY KEY,
                city TEXT NOT NULL,
                name TEXT NOT NULL,
                addr TEXT,
                cuisine TEXT,
                local_score REAL,
                signature_dishes TEXT,
                why_recommended TEXT,
                vector TEXT NOT NULL,
                payload TEXT NOT NULL
            )
        """)
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_city ON restaurants(city)")
        self.conn.commit()

    async def ensure_collection(self, vector_size: int) -> None:
        """No-op for SQLite — schema is auto-created on __init__.

        Kept for interface parity with QdrantStore so the factory can swap
        implementations transparently. vector_size is intentionally ignored:
        SQLite stores JSON-serialized vectors so dim is dynamic per row, but
        the caller should ensure consistency across runs.
        """
        _ = vector_size  # interface compliance

    async def upsert(self, points: list[dict[str, Any]]):
        """Upsert points with vectors."""
        for p in points:
            payload = p["payload"]
            vector_str = json.dumps(p["vector"])
            payload_str = json.dumps(payload)
            self.conn.execute(
                """
                INSERT OR REPLACE INTO restaurants
                (id, city, name, addr, cuisine, local_score, signature_dishes, why_recommended, vector, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    p["id"],
                    payload.get("city"),
                    payload.get("name"),
                    payload.get("addr"),
                    payload.get("cuisine"),
                    payload.get("local_score"),
                    json.dumps(payload.get("signature_dishes", [])),
                    payload.get("why_recommended"),
                    vector_str,
                    payload_str,
                ),
            )
        self.conn.commit()

    async def search(
        self, query_vector: list[float], city: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Search by vector similarity."""
        query = "SELECT id, vector, payload FROM restaurants"
        params = []
        if city:
            query += " WHERE city = ?"
            params.append(city)

        cursor = self.conn.execute(query, params)
        results = []
        for row in cursor:
            doc_id, vector_str, payload_str = row
            doc_vector = json.loads(vector_str)
            score = self._cosine_similarity(query_vector, doc_vector)
            results.append(
                {
                    "id": doc_id,
                    "score": score,
                    "vector": doc_vector,
                    "payload": json.loads(payload_str),
                }
            )

        # Sort by score descending
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:limit]

    def _cosine_similarity(self, a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b, strict=False))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        return dot / (na * nb + 1e-9)

    def close(self):
        self.conn.close()


# Singleton instance
_store: LocalVectorStore | None = None


def get_local_store() -> LocalVectorStore:
    global _store
    if _store is None:
        _store = LocalVectorStore()
    return _store
