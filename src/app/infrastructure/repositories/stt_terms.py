"""Хранение живого словаря терминов."""
from __future__ import annotations

from ...domain.models import SttTerm
from .base import BaseRepository

_COLUMNS = "id, canonical, variant, source, created_at"


class PgTermRepository(BaseRepository):
    @staticmethod
    def _row(row) -> SttTerm:
        return SttTerm(id=row["id"], canonical=row["canonical"], variant=row["variant"],
                       source=row["source"], created_at=row["created_at"])

    async def add(self, term: SttTerm) -> SttTerm:
        """Добавить вариант. Повторное добавление просто переучивает его."""
        async with self._db.connection() as conn:
            row = await conn.fetchrow(
                f"""INSERT INTO stt_terms (canonical, variant, source)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (lower(variant))
                    DO UPDATE SET canonical = EXCLUDED.canonical, source = EXCLUDED.source
                    RETURNING {_COLUMNS}""",
                term.canonical, term.variant.strip(), term.source,
            )
        return self._row(row)

    async def remove(self, variant: str) -> bool:
        async with self._db.connection() as conn:
            res = await conn.execute("DELETE FROM stt_terms WHERE lower(variant) = lower($1)",
                                     variant.strip())
        return res.endswith("1")

    async def all(self) -> list[SttTerm]:
        async with self._db.connection() as conn:
            rows = await conn.fetch(f"SELECT {_COLUMNS} FROM stt_terms ORDER BY canonical, variant")
        return [self._row(r) for r in rows]
