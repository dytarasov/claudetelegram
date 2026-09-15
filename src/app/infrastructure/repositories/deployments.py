"""Хранение истории выкаток.

Зачем в БД, если есть git log: git знает, что коммит есть, но не знает, взлетел
ли он, сколько поднимался и почему откатились. Именно эти ответы нужны, когда
через неделю смотришь /versions и выбираешь, куда возвращаться.
"""
from __future__ import annotations

import json

from ...domain.enums import DeployPhase, TriggerKind
from ...domain.models import Deployment
from .base import BaseRepository

_COLUMNS = """id, note, prev_sha, new_sha, phase, trigger, files, reason, chat_id,
              started_at, finished_at, revived_in_s"""


class PgDeploymentRepository(BaseRepository):
    """Реализация DeploymentRepository поверх Postgres."""

    def _row(self, row) -> Deployment:
        return Deployment(
            id=row["id"],
            note=row["note"],
            prev_sha=row["prev_sha"],
            new_sha=row["new_sha"],
            phase=DeployPhase(row["phase"]),
            trigger=TriggerKind(row["trigger"]),
            files=self._json_list(row["files"]),
            reason=row["reason"],
            chat_id=row["chat_id"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            revived_in_s=row["revived_in_s"],
        )

    async def add(self, deployment: Deployment) -> Deployment:
        async with self._db.connection() as conn:
            row = await conn.fetchrow(
                f"""INSERT INTO deployments
                    (note, prev_sha, new_sha, phase, trigger, files, reason, chat_id, started_at)
                    VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8,$9)
                    RETURNING {_COLUMNS}""",
                deployment.note, deployment.prev_sha, deployment.new_sha,
                str(deployment.phase), str(deployment.trigger),
                json.dumps(deployment.files, ensure_ascii=False),
                deployment.reason, deployment.chat_id, deployment.started_at,
            )
        return self._row(row)

    async def update(self, deployment: Deployment) -> None:
        async with self._db.connection() as conn:
            await conn.execute(
                """UPDATE deployments
                      SET phase=$2, reason=$3, finished_at=$4, revived_in_s=$5
                    WHERE id=$1""",
                deployment.id, str(deployment.phase), deployment.reason,
                deployment.finished_at, deployment.revived_in_s,
            )

    async def get(self, deployment_id: int) -> Deployment | None:
        async with self._db.connection() as conn:
            row = await conn.fetchrow(f"SELECT {_COLUMNS} FROM deployments WHERE id=$1", deployment_id)
        return self._row(row) if row else None

    async def by_sha(self, sha: str) -> Deployment | None:
        """Последняя выкатка, приведшая к этому коммиту, — для /versions."""
        async with self._db.connection() as conn:
            row = await conn.fetchrow(
                f"SELECT {_COLUMNS} FROM deployments WHERE new_sha=$1 ORDER BY id DESC LIMIT 1", sha,
            )
        return self._row(row) if row else None

    async def in_flight(self) -> Deployment | None:
        """Незакрытая выкатка: пока она есть, новую начинать нельзя."""
        async with self._db.connection() as conn:
            row = await conn.fetchrow(
                f"""SELECT {_COLUMNS} FROM deployments
                     WHERE phase = ANY($1::text[]) ORDER BY id DESC LIMIT 1""",
                [DeployPhase.PENDING, DeployPhase.APPLYING, DeployPhase.SOAKING,
                 DeployPhase.ROLLING_BACK],
            )
        return self._row(row) if row else None

    async def recent(self, limit: int = 20) -> list[Deployment]:
        async with self._db.connection() as conn:
            rows = await conn.fetch(
                f"SELECT {_COLUMNS} FROM deployments ORDER BY id DESC LIMIT $1", limit,
            )
        return [self._row(r) for r in rows]
