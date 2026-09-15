"""Доступ к Postgres: пул соединений и накат миграций.

Почему asyncpg, а не ORM. Слой репозиториев и так изолирует SQL от остального
кода, а ORM на 4 таблицы добавила бы больше понятий, чем сэкономила строк.
Плюс asyncpg — единственная зависимость, а pgvector с ним дружит через текстовый
литерал вектора.

Почему свой накатчик миграций, а не alembic. Миграций мало, они простые, а
самодопил должен уметь применять их сам при старте, без отдельной команды и без
второй точки отказа. Файлы в migrations/*.sql применяются по возрастанию имени,
каждый — в своей транзакции, применённые записываются в schema_migrations.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

# Ключ консультативной блокировки. Число произвольное, важно лишь чтобы оно было
# одинаковым во всех процессах и не совпадало с чужими блокировками в этой базе.
MIGRATION_LOCK_KEY = 8_251_907_314


async def _register_codecs(conn: asyncpg.Connection) -> None:
    """jsonb — сразу в питоновские структуры, а не строкой.

    Без этого каждый репозиторий вынужден звать json.loads руками, и рано или
    поздно кто-нибудь забудет. Кодек ставится один раз на соединение при выдаче
    его пулом.
    """
    await conn.set_type_codec(
        "jsonb", schema="pg_catalog",
        encoder=lambda value: json.dumps(value, ensure_ascii=False),
        decoder=json.loads,
    )


class Database:
    """Обёртка над пулом asyncpg.

    Живёт в APP-скоупе контейнера: один пул на процесс. Репозитории получают
    именно её, а не голый пул, чтобы им не приходилось знать про lifecycle —
    открыть/закрыть умеет только этот класс.
    """

    def __init__(self, dsn: str, min_size: int = 1, max_size: int = 4) -> None:
        self._dsn = dsn
        self._min = min_size
        self._max = max_size
        self._pool: asyncpg.Pool | None = None

    @property
    def ready(self) -> bool:
        return self._pool is not None

    async def connect(self) -> None:
        if self._pool is not None:
            return
        self._pool = await asyncpg.create_pool(
            self._dsn, min_size=self._min, max_size=self._max, command_timeout=30,
            init=_register_codecs,
        )
        log.info("подключился к postgres (пул %s..%s)", self._min, self._max)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[asyncpg.Connection]:
        """Соединение из пула. Все запросы репозиториев идут только через это."""
        if self._pool is None:
            raise RuntimeError("Database.connect() не вызывали")
        async with self._pool.acquire() as conn:
            yield conn

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[asyncpg.Connection]:
        """Соединение с открытой транзакцией — для операций «всё или ничего»."""
        async with self.connection() as conn, conn.transaction():
            yield conn

    async def healthy(self) -> bool:
        """Дешёвая проверка живости для /status и пульса."""
        try:
            async with self.connection() as conn:
                return await conn.fetchval("SELECT 1") == 1
        except Exception:  # noqa: BLE001 —проверка не должна ронять вызывающего
            return False

    async def migrate(self) -> list[str]:
        """Накатить недостающие миграции. Возвращает имена применённых.

        Две защиты, которых стоило добавить сразу:

        • Консультативная блокировка. Бот перезапускается сам, и в момент
          выкатки старый процесс ещё жив, а новый уже стартовал. Без блокировки
          оба вошли бы сюда одновременно, и спасала бы только идемпотентность
          самих миграций — то есть дисциплина, а не механизм.

        • Контрольные суммы. Если уже применённый файл потом отредактировать,
          на этой машине изменений не будет, а на новой — будут: базы разъедутся
          молча. Теперь такое расхождение видно в логе сразу.
        """
        applied: list[str] = []
        async with self.connection() as conn:
            await conn.execute("SELECT pg_advisory_lock($1)", MIGRATION_LOCK_KEY)
            try:
                await conn.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations ("
                    " name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
                )
                await conn.execute(
                    "ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS checksum text"
                )
                done = {r["name"]: r["checksum"]
                        for r in await conn.fetch("SELECT name, checksum FROM schema_migrations")}

                for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                    body = path.read_text()
                    checksum = hashlib.sha256(body.encode()).hexdigest()
                    if path.name in done:
                        await self._verify(conn, path.name, done[path.name], checksum)
                        continue
                    log.info("миграция %s", path.name)
                    async with conn.transaction():
                        await conn.execute(body)
                        await conn.execute(
                            "INSERT INTO schema_migrations(name, checksum) VALUES($1, $2)"
                            " ON CONFLICT (name) DO UPDATE SET checksum = EXCLUDED.checksum",
                            path.name, checksum,
                        )
                    applied.append(path.name)
            finally:
                await conn.execute("SELECT pg_advisory_unlock($1)", MIGRATION_LOCK_KEY)
        return applied

    @staticmethod
    async def _verify(conn, name: str, stored: str | None, actual: str) -> None:
        """Сверить контрольную сумму уже применённой миграции.

        Ронять процесс не будем: бот с расхождением в истории миграций всё равно
        полезнее, чем не поднявшийся бот. Но в лог это уходит предупреждением,
        которое видно в /errors.
        """
        if stored is None:
            # Миграция применена версией, которая сумм ещё не считала.
            log.info("миграция %s без контрольной суммы — записываю текущую", name)
            await conn.execute("UPDATE schema_migrations SET checksum=$2 WHERE name=$1",
                               name, actual)
        elif stored != actual:
            log.warning(
                "миграция %s изменилась после применения (%s → %s): базы на разных "
                "машинах могут разойтись", name, stored[:8], actual[:8],
            )
