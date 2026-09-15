"""Куски записей: SQL для поиска по памяти.

Здесь два поисковых запроса — полнотекстовый и векторный, — и они намеренно
разделены. Слить их в один SQL заманчиво, но сравнивать ts_rank с косинусным
расстоянием нельзя: величины из разных миров. Сливаются они рангами, уже в
сервисе (JournalService, ранговая фузия).
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from ...domain.models import Chunk
from .base import BaseRepository, or_terms, vector_literal

_COLUMNS = "id, source_kind, source_id, ord, role, text, created_at"


class PgChunkRepository(BaseRepository):
    @staticmethod
    def _row(row) -> Chunk:
        return Chunk(id=row["id"], source_kind=row["source_kind"],
                     source_id=row["source_id"], ord=row["ord"], role=row["role"],
                     text=row["text"], created_at=row["created_at"])

    async def rebuild(self, source_kind: str, source_id: int,
                      created_at: datetime, pieces: Sequence[tuple[str, str]]) -> int:
        """Заменить нарезку записи целиком. Одной транзакцией.

        Без транзакции сбой между удалением и вставкой оставил бы запись вовсе
        без кусков, то есть невидимой для поиска, — и молча: искать «почему не
        находится» пришлось бы вручную.
        """
        async with self._db.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM chunks WHERE source_kind = $1 AND source_id = $2",
                    source_kind, source_id)
                if not pieces:
                    return 0
                await conn.executemany(
                    """INSERT INTO chunks (source_kind, source_id, ord, role, text, created_at)
                       VALUES ($1,$2,$3,$4,$5,$6)""",
                    [(source_kind, source_id, ord, role, text, created_at)
                     for ord, (role, text) in enumerate(pieces)])
        return len(pieces)

    async def sources_without_chunks(self, source_kind: str, limit: int = 50) -> list[int]:
        """Записи, которые ещё не нарезаны. Свежие первыми.

        NOT EXISTS, а не LEFT JOIN: планировщику проще, а главное — понятнее
        читается намерение «у которых нет ни одного куска».
        """
        table = {"turn": "turns", "note": "notes"}[source_kind]
        async with self._db.connection() as conn:
            rows = await conn.fetch(
                f"""SELECT t.id FROM {table} t
                     WHERE NOT EXISTS (SELECT 1 FROM chunks c
                                        WHERE c.source_kind = $1 AND c.source_id = t.id)
                     ORDER BY t.id DESC LIMIT $2""",
                source_kind, limit)
        return [r["id"] for r in rows]

    # ---- векторы ------------------------------------------------------------ #
    async def pending_embeddings(self, model: str, limit: int = 100) -> list[tuple[int, str]]:
        async with self._db.connection() as conn:
            rows = await conn.fetch(
                """SELECT id, text FROM chunks
                    WHERE embedding IS NULL OR embedding_model IS DISTINCT FROM $1
                    ORDER BY id DESC LIMIT $2""",
                model, limit)
        return [(r["id"], r["text"]) for r in rows]

    async def set_embedding(self, chunk_id: int, vector: list[float], model: str) -> None:
        async with self._db.connection() as conn:
            await conn.execute(
                "UPDATE chunks SET embedding = $2::vector, embedding_model = $3 WHERE id = $1",
                chunk_id, vector_literal(vector), model)

    # ---- поиск --------------------------------------------------------------- #
    async def search_text(self, query: str, limit: int = 20) -> list[tuple[Chunk, float]]:
        """Полнотекст по OR-форме запроса: полнота важнее точности, дальше фузия."""
        if not query.strip():
            return []
        loose = or_terms(query)
        async with self._db.connection() as conn:
            rows = await conn.fetch(
                f"""SELECT {_COLUMNS},
                           ts_rank(search, websearch_to_tsquery('russian', $1)) AS rank
                      FROM chunks
                     WHERE search @@ websearch_to_tsquery('russian', $1)
                     ORDER BY rank DESC, created_at DESC
                     LIMIT $2""",
                loose, limit)
        return [(self._row(r), float(r["rank"])) for r in rows]

    async def search_semantic(self, vector: list[float], model: str,
                              limit: int = 20) -> list[tuple[Chunk, float]]:
        async with self._db.connection() as conn:
            rows = await conn.fetch(
                f"""SELECT {_COLUMNS}, 1 - (embedding <=> $1::vector) AS score
                      FROM chunks
                     WHERE embedding IS NOT NULL AND embedding_model = $2
                     ORDER BY embedding <=> $1::vector
                     LIMIT $3""",
                vector_literal(vector), model, limit)
        return [(self._row(r), float(r["score"])) for r in rows]

    async def stats(self, model: str) -> dict[str, int]:
        async with self._db.connection() as conn:
            row = await conn.fetchrow(
                """SELECT count(*) AS total,
                          count(*) FILTER (WHERE embedding IS NOT NULL
                                             AND embedding_model = $1) AS indexed,
                          count(DISTINCT source_id) AS sources
                     FROM chunks""",
                model)
        return {"total": row["total"], "indexed": row["indexed"], "sources": row["sources"]}
