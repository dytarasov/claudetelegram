"""События: воспоминания с датой.

Отдельно от фактов, потому что и спрашивают их иначе. К фактам обращаются по
смыслу («что я знаю про его тётю»), к событиям — по времени («что было в
августе», «что предстоит на этой неделе»). Поэтому основной метод здесь between,
а не search.
"""
from __future__ import annotations

from datetime import datetime

from ...domain.models import Event
from .base import BaseRepository, or_terms

_COLUMNS = ("id, title, details, happened_at, date_precision, kind, people, tags, "
            "source, source_turn_id, created_at")


class PgEventRepository(BaseRepository):
    @staticmethod
    def _row(row) -> Event:
        return Event(
            id=row["id"], title=row["title"], details=row["details"],
            happened_at=row["happened_at"], date_precision=row["date_precision"],
            kind=row["kind"], people=list(row["people"] or []),
            tags=list(row["tags"] or []), source=row["source"],
            source_turn_id=row["source_turn_id"], created_at=row["created_at"],
        )

    async def add(self, event: Event) -> Event:
        async with self._db.connection() as conn:
            row = await conn.fetchrow(
                f"""INSERT INTO events (title, details, happened_at, date_precision,
                                        kind, people, tags, source, source_turn_id)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                    RETURNING {_COLUMNS}""",
                event.title, event.details, event.happened_at, event.date_precision,
                event.kind, event.people, event.tags, event.source, event.source_turn_id,
            )
        return self._row(row)

    async def get(self, event_id: int) -> Event | None:
        async with self._db.connection() as conn:
            row = await conn.fetchrow(f"SELECT {_COLUMNS} FROM events WHERE id = $1", event_id)
        return self._row(row) if row else None

    async def between(self, start: datetime, end: datetime, limit: int = 50) -> list[Event]:
        """Окно по времени, ближайшие к началу окна первыми."""
        async with self._db.connection() as conn:
            rows = await conn.fetch(
                f"""SELECT {_COLUMNS} FROM events
                     WHERE happened_at >= $1 AND happened_at < $2
                     ORDER BY happened_at LIMIT $3""",
                start, end, limit,
            )
        return [self._row(r) for r in rows]

    async def search(self, query: str, limit: int = 10) -> list[Event]:
        if not query.strip():
            async with self._db.connection() as conn:
                rows = await conn.fetch(
                    f"SELECT {_COLUMNS} FROM events ORDER BY happened_at DESC LIMIT $1", limit)
            return [self._row(r) for r in rows]
        loose = or_terms(query)
        async with self._db.connection() as conn:
            rows = await conn.fetch(
                f"""SELECT {_COLUMNS}
                      FROM events
                     WHERE search @@ websearch_to_tsquery('russian', $3) OR title % $1
                     ORDER BY greatest(ts_rank(search, websearch_to_tsquery('russian', $3)),
                                       similarity(title, $1)) DESC,
                              happened_at DESC
                     LIMIT $2""",
                query, limit, loose,
            )
        return [self._row(r) for r in rows]

    async def count(self) -> int:
        async with self._db.connection() as conn:
            return int(await conn.fetchval("SELECT count(*) FROM events") or 0)

    async def delete(self, event_id: int) -> bool:
        async with self._db.connection() as conn:
            result = await conn.execute("DELETE FROM events WHERE id = $1", event_id)
        return result.endswith("1")
