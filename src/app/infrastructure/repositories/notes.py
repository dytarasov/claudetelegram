"""Заметки себе на будущее.

Отличие от журнала ходов: ход пишется автоматически и хранит сырой разговор,
заметка добавляется осознанно и хранит вывод — «почему сделано так», «на что
не наступать». Поиск триграммный плюс полнотекстовый: короткие запросы вроде
«сторож» ловятся даже с опечаткой.
"""
from __future__ import annotations

from ...domain.models import Note
from .base import BaseRepository, vector_literal

_COLUMNS = "id, text, tags, source, created_at"


class PgNoteRepository(BaseRepository):
    @staticmethod
    def _row(row) -> Note:
        return Note(id=row["id"], text=row["text"], tags=list(row["tags"] or []),
                    source=row["source"], created_at=row["created_at"])

    async def add(self, note: Note) -> Note:
        async with self._db.connection() as conn:
            row = await conn.fetchrow(
                f"INSERT INTO notes (text, tags, source) VALUES ($1,$2,$3) RETURNING {_COLUMNS}",
                note.text, note.tags, note.source,
            )
        return self._row(row)

    async def search(self, query: str, limit: int = 10) -> list[Note]:
        if not query.strip():
            return await self.recent(limit)
        async with self._db.connection() as conn:
            rows = await conn.fetch(
                f"""SELECT {_COLUMNS},
                           ts_rank(search, websearch_to_tsquery('russian', $1)) AS rank,
                           similarity(text, $1) AS sim
                      FROM notes
                     WHERE search @@ websearch_to_tsquery('russian', $1)
                        OR text % $1
                     ORDER BY greatest(ts_rank(search, websearch_to_tsquery('russian', $1)),
                                       similarity(text, $1)) DESC,
                              id DESC
                     LIMIT $2""",
                query, limit,
            )
        return [self._row(r) for r in rows]

    async def recent(self, limit: int = 10) -> list[Note]:
        async with self._db.connection() as conn:
            rows = await conn.fetch(f"SELECT {_COLUMNS} FROM notes ORDER BY id DESC LIMIT $1", limit)
        return [self._row(r) for r in rows]

    # ---- семантика --------------------------------------------------- #
    async def pending_embeddings(self, model: str, limit: int = 100) -> list[tuple[int, str]]:
        async with self._db.connection() as conn:
            rows = await conn.fetch(
                """SELECT id, text FROM notes
                    WHERE embedding IS NULL OR embedding_model IS DISTINCT FROM $1
                    ORDER BY id DESC LIMIT $2""",
                model, limit,
            )
        return [(r["id"], r["text"]) for r in rows]

    async def set_embedding(self, note_id: int, vector: list[float], model: str) -> None:
        async with self._db.connection() as conn:
            await conn.execute(
                "UPDATE notes SET embedding = $2::vector, embedding_model = $3 WHERE id = $1",
                note_id, vector_literal(vector), model,
            )

    async def search_semantic(self, vector: list[float], model: str,
                              limit: int = 10) -> list[Note]:
        async with self._db.connection() as conn:
            rows = await conn.fetch(
                f"""SELECT {_COLUMNS} FROM notes
                     WHERE embedding IS NOT NULL AND embedding_model = $2
                     ORDER BY embedding <=> $1::vector
                     LIMIT $3""",
                vector_literal(vector), model, limit,
            )
        return [self._row(r) for r in rows]

    async def delete(self, note_id: int) -> bool:
        async with self._db.connection() as conn:
            res = await conn.execute("DELETE FROM notes WHERE id=$1", note_id)
        return res.endswith("1")
