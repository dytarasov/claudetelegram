"""Журнал ходов и поиск по нему.

Поиск сделан на встроенном полнотексте Postgres (tsvector + gin), потому что он
работает здесь и сейчас, без ключей и без модели эмбеддингов. Колонка embedding
в таблице уже есть: когда появится Embedder, семантический поиск добавится
вторым запросом рядом, а не переписыванием этого файла.
"""
from __future__ import annotations

import json
from typing import Any

from ...domain.enums import TurnSource, TurnStatus
from ...domain.models import Turn
from .base import BaseRepository, vector_literal

_COLUMNS = """id, chat_id, user_id, prompt, answer, status, source, tools, session_id,
              goal_id, tokens_in, tokens_out, cost_usd, duration_s, created_at"""


class PgTurnRepository(BaseRepository):
    def _row(self, row) -> Turn:
        return Turn(
            id=row["id"], chat_id=row["chat_id"], user_id=row["user_id"],
            prompt=row["prompt"], answer=row["answer"],
            status=TurnStatus(row["status"]), source=TurnSource(row["source"]),
            tools=self._json_list(row["tools"]),
            session_id=row["session_id"], goal_id=row["goal_id"],
            tokens_in=row["tokens_in"],
            tokens_out=row["tokens_out"], cost_usd=row["cost_usd"],
            duration_s=row["duration_s"], created_at=row["created_at"],
        )

    async def add(self, turn: Turn) -> Turn:
        async with self._db.connection() as conn:
            row = await conn.fetchrow(
                f"""INSERT INTO turns
                    (chat_id, user_id, prompt, answer, status, source, tools, session_id,
                     goal_id, tokens_in, tokens_out, cost_usd, duration_s, created_at)
                    VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9,$10,$11,$12,$13,
                            coalesce($14, now()))
                    RETURNING {_COLUMNS}""",
                turn.chat_id, turn.user_id, turn.prompt, turn.answer, str(turn.status),
                str(turn.source), json.dumps(turn.tools, ensure_ascii=False), turn.session_id,
                turn.goal_id, turn.tokens_in, turn.tokens_out, turn.cost_usd, turn.duration_s,
                # Обычный ход пишется «сейчас», и время ставит база. Но перенос
                # старой переписки обязан сохранить настоящие даты, иначе вся
                # история окажется случившейся в момент импорта.
                turn.created_at,
            )
        return self._row(row)

    async def search(self, query: str, limit: int = 10) -> list[tuple[Turn, float]]:
        """Полнотекст по вопросу и ответу. Пустой запрос — просто свежие ходы."""
        if not query.strip():
            return [(t, 0.0) for t in await self.recent(limit)]
        async with self._db.connection() as conn:
            rows = await conn.fetch(
                f"""SELECT {_COLUMNS}, ts_rank(search, q) AS rank
                      FROM turns, websearch_to_tsquery('russian', $1) AS q
                     WHERE search @@ q
                     ORDER BY rank DESC, id DESC
                     LIMIT $2""",
                query, limit,
            )
        return [(self._row(r), float(r["rank"])) for r in rows]

    async def recent(self, limit: int = 10) -> list[Turn]:
        async with self._db.connection() as conn:
            rows = await conn.fetch(
                f"SELECT {_COLUMNS} FROM turns ORDER BY id DESC LIMIT $1", limit,
            )
        return [self._row(r) for r in rows]

    # ---- семантика --------------------------------------------------- #
    async def by_ids(self, ids) -> dict:
        """Ходы по списку id. Нужен поиску: он находит куски, а показать надо ход.

        Один запрос с ANY вместо цикла по id — иначе выдача из десяти кусков
        превращается в десять обращений к базе.
        """
        if not ids:
            return {}
        async with self._db.connection() as conn:
            rows = await conn.fetch(
                f"SELECT {_COLUMNS} FROM turns WHERE id = ANY($1::bigint[])", list(ids))
        return {row["id"]: self._row(row) for row in rows}

    async def pending_embeddings(self, model: str, limit: int = 100) -> list[tuple[int, str]]:
        """Записи без вектора или посчитанные другой моделью.

        Смена модели эмбеддингов делает старые векторы бесполезными: расстояния
        между разными пространствами ничего не значат. Поэтому условие не просто
        «вектора нет», а «вектор не от текущей модели».
        """
        async with self._db.connection() as conn:
            rows = await conn.fetch(
                """SELECT id, left(prompt, 2000) || ' ' || left(answer, 4000) AS text
                     FROM turns
                    WHERE embedding IS NULL OR embedding_model IS DISTINCT FROM $1
                    ORDER BY id DESC
                    LIMIT $2""",
                model, limit,
            )
        return [(r["id"], r["text"]) for r in rows]

    async def set_embedding(self, turn_id: int, vector: list[float], model: str) -> None:
        async with self._db.connection() as conn:
            await conn.execute(
                "UPDATE turns SET embedding = $2::vector, embedding_model = $3 WHERE id = $1",
                turn_id, vector_literal(vector), model,
            )

    async def search_semantic(self, vector: list[float], model: str,
                              limit: int = 10) -> list[tuple[Turn, float]]:
        """Ближайшие по смыслу. Второе значение — близость от 0 до 1."""
        async with self._db.connection() as conn:
            rows = await conn.fetch(
                f"""SELECT {_COLUMNS}, 1 - (embedding <=> $1::vector) AS score
                      FROM turns
                     WHERE embedding IS NOT NULL AND embedding_model = $2
                     ORDER BY embedding <=> $1::vector
                     LIMIT $3""",
                vector_literal(vector), model, limit,
            )
        return [(self._row(r), float(r["score"])) for r in rows]

    async def embedding_stats(self, model: str) -> dict[str, int]:
        async with self._db.connection() as conn:
            row = await conn.fetchrow(
                """SELECT count(*) AS total,
                          count(*) FILTER (WHERE embedding IS NOT NULL
                                             AND embedding_model = $1) AS indexed
                     FROM turns""",
                model,
            )
        return {"total": row["total"], "indexed": row["indexed"]}

    async def stats(self) -> dict[str, Any]:
        async with self._db.connection() as conn:
            row = await conn.fetchrow(
                """SELECT count(*) AS turns,
                          coalesce(sum(cost_usd), 0) AS cost,
                          coalesce(sum(tokens_in + tokens_out), 0) AS tokens,
                          min(created_at) AS since
                     FROM turns"""
            )
        return dict(row) if row else {}
