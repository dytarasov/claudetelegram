"""Факты: единственное место, где живёт SQL для слоя устойчивых утверждений.

Главная механика здесь — замещение. Когда приходит факт с тем же subject+key,
прежний не переписывается на месте, а закрывается: valid_until = now(),
superseded_by = id нового. Причина не в аккуратности, а в отладке: через месяц
на вопрос «почему ты решил, что порт 5433» надо уметь показать цепочку, а не
последнее состояние.
"""
from __future__ import annotations

from datetime import datetime

from ...domain.models import Fact
from .base import BaseRepository, or_terms

_COLUMNS = ("id, subject, key, value, kind, pinned, confidence, tags, source, "
            "source_turn_id, valid_from, valid_until, superseded_by, created_at")


class PgFactRepository(BaseRepository):
    @staticmethod
    def _row(row) -> Fact:
        return Fact(
            id=row["id"], subject=row["subject"], key=row["key"], value=row["value"],
            kind=row["kind"], pinned=row["pinned"], confidence=float(row["confidence"]),
            tags=list(row["tags"] or []), source=row["source"],
            source_turn_id=row["source_turn_id"], valid_from=row["valid_from"],
            valid_until=row["valid_until"], superseded_by=row["superseded_by"],
            created_at=row["created_at"],
        )

    async def add(self, fact: Fact) -> Fact:
        """Записать факт, закрыв предыдущую версию того же ключа.

        Обе операции идут одной транзакцией: наполовину применённое замещение
        оставило бы либо два действующих факта об одном, либо ни одного.
        """
        async with self._db.connection() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    f"""INSERT INTO facts (subject, key, value, kind, pinned, confidence,
                                           tags, source, source_turn_id)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                        RETURNING {_COLUMNS}""",
                    fact.subject, fact.key, fact.value, fact.kind, fact.pinned,
                    fact.confidence, fact.tags, fact.source, fact.source_turn_id,
                )
                if fact.key:
                    # Ключ задаёт тождество: старая версия уходит в историю.
                    # Себя новый факт закрыть не может — отсюда id <> $3.
                    await conn.execute(
                        """UPDATE facts
                              SET valid_until = now(), superseded_by = $3
                            WHERE subject = $1 AND key = $2
                              AND valid_until IS NULL AND id <> $3""",
                        fact.subject, fact.key, row["id"],
                    )
        return self._row(row)

    async def get(self, fact_id: int) -> Fact | None:
        async with self._db.connection() as conn:
            row = await conn.fetchrow(f"SELECT {_COLUMNS} FROM facts WHERE id = $1", fact_id)
        return self._row(row) if row else None

    async def active(self, subject: str | None = None, limit: int = 200) -> list[Fact]:
        async with self._db.connection() as conn:
            rows = await conn.fetch(
                f"""SELECT {_COLUMNS} FROM facts
                     WHERE valid_until IS NULL
                       AND ($1::text IS NULL OR subject = $1)
                     ORDER BY pinned DESC, subject, key NULLS LAST, id
                     LIMIT $2""",
                subject, limit,
            )
        return [self._row(r) for r in rows]

    async def pinned(self, limit: int = 60) -> list[Fact]:
        async with self._db.connection() as conn:
            rows = await conn.fetch(
                f"""SELECT {_COLUMNS} FROM facts
                     WHERE pinned AND valid_until IS NULL
                     ORDER BY subject, key NULLS LAST, id
                     LIMIT $1""",
                limit,
            )
        return [self._row(r) for r in rows]

    async def search(self, query: str, limit: int = 10,
                     include_retired: bool = False) -> list[Fact]:
        """Полнотекст плюс триграммы: короткий запрос ловится и с опечаткой.

        Ищем по OR-форме запроса (см. or_terms), ранжируем ею же: запись,
        совпавшая по трём словам из четырёх, встанет выше совпавшей по одному.
        Векторов тут нет намеренно — см. комментарий в миграции 0007.
        """
        if not query.strip():
            return await self.active(limit=limit)
        loose = or_terms(query)
        async with self._db.connection() as conn:
            rows = await conn.fetch(
                f"""SELECT {_COLUMNS}
                      FROM facts
                     WHERE (search @@ websearch_to_tsquery('russian', $4)
                            OR value % $1 OR coalesce(key,'') % $1)
                       AND ($3 OR valid_until IS NULL)
                     ORDER BY pinned DESC,
                              greatest(ts_rank(search, websearch_to_tsquery('russian', $4)),
                                       similarity(value, $1),
                                       similarity(coalesce(key,''), $1)) DESC,
                              id DESC
                     LIMIT $2""",
                query, limit, include_retired, loose,
            )
        return [self._row(r) for r in rows]

    async def retire(self, fact_id: int, superseded_by: int | None = None) -> bool:
        async with self._db.connection() as conn:
            result = await conn.execute(
                """UPDATE facts SET valid_until = now(), superseded_by = coalesce($2, superseded_by)
                    WHERE id = $1 AND valid_until IS NULL""",
                fact_id, superseded_by,
            )
        return result.endswith("1")

    async def history(self, subject: str, key: str, limit: int = 20) -> list[Fact]:
        async with self._db.connection() as conn:
            rows = await conn.fetch(
                f"""SELECT {_COLUMNS} FROM facts
                     WHERE subject = $1 AND key = $2
                     ORDER BY id DESC LIMIT $3""",
                subject, key, limit,
            )
        return [self._row(r) for r in rows]
