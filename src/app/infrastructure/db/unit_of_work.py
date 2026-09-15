"""Единица работы: несколько репозиториев в одной транзакции.

Зачем. Обычный репозиторий берёт соединение из пула на каждый вызов — это
просто и для одиночных операций правильно. Но когда две записи должны попасть в
базу вместе или не попасть вовсе, такой подход не годится: между вызовами может
случиться что угодно, и половина изменений останется.

Приём простой: BoundConnection притворяется Database, но всегда отдаёт одно и то
же соединение — то, на котором открыта транзакция. Репозиторий об этом ничего не
знает и работает как обычно, а атомарность обеспечивает вызывающий:

    async with uow.begin() as repos:
        turn = await repos.turns.add(turn)
        await repos.kv.set("session_id", session_id)
    # вышли без исключения — обе записи зафиксированы, иначе обеих нет

Сейчас таких мест немного, но появиться они обязаны — и лучше, чтобы механизм
был готов заранее, чем чтобы кто-то в спешке писал транзакцию руками мимо слоя.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import asyncpg

from ..repositories.deployments import PgDeploymentRepository
from ..repositories.kv import KeyValueStore
from ..repositories.notes import PgNoteRepository
from ..repositories.stt_terms import PgTermRepository
from ..repositories.turns import PgTurnRepository
from .database import Database


class BoundConnection:
    """Подделка под Database с единственным, уже открытым соединением."""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[asyncpg.Connection]:
        yield self._conn

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[asyncpg.Connection]:
        # Вложенная транзакция стала бы savepoint'ом; нам это не нужно, а
        # молча делать не то — хуже, чем не делать. Отдаём то же соединение.
        yield self._conn


@dataclass(slots=True)
class Repositories:
    """Все репозитории, привязанные к одной транзакции."""

    turns: PgTurnRepository
    notes: PgNoteRepository
    deployments: PgDeploymentRepository
    terms: PgTermRepository
    kv: KeyValueStore


class UnitOfWork:
    def __init__(self, db: Database, state_file: Path) -> None:
        self._db = db
        self._state_file = state_file

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[Repositories]:
        async with self._db.transaction() as conn:
            bound = BoundConnection(conn)
            yield Repositories(
                turns=PgTurnRepository(bound),
                notes=PgNoteRepository(bound),
                deployments=PgDeploymentRepository(bound),
                terms=PgTermRepository(bound),
                kv=KeyValueStore(bound, self._state_file),
            )
