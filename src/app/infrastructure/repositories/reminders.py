"""Хранение обязательств и напоминаний."""
from __future__ import annotations

from datetime import datetime

from ...domain.enums import ReminderKind, ReminderStatus
from ...domain.models import Reminder
from .base import BaseRepository

_COLUMNS = """id, chat_id, text, kind, status, due_at, repeat_rule, urgency,
              fired_count, last_fired_at, source, source_turn_id, created_at, done_at"""


class PgReminderRepository(BaseRepository):
    @staticmethod
    def _row(row) -> Reminder:
        return Reminder(
            id=row["id"], chat_id=row["chat_id"], text=row["text"],
            kind=ReminderKind(row["kind"]), status=ReminderStatus(row["status"]),
            due_at=row["due_at"], repeat_rule=row["repeat_rule"], urgency=row["urgency"],
            fired_count=row["fired_count"], last_fired_at=row["last_fired_at"],
            source=row["source"], source_turn_id=row["source_turn_id"],
            created_at=row["created_at"], done_at=row["done_at"],
        )

    async def add(self, reminder: Reminder) -> Reminder:
        async with self._db.connection() as conn:
            row = await conn.fetchrow(
                f"""INSERT INTO reminders
                    (chat_id, text, kind, status, due_at, repeat_rule, urgency,
                     source, source_turn_id)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                    RETURNING {_COLUMNS}""",
                reminder.chat_id, reminder.text, str(reminder.kind), str(reminder.status),
                reminder.due_at, reminder.repeat_rule, reminder.urgency,
                reminder.source, reminder.source_turn_id,
            )
        return self._row(row)

    async def due(self, now: datetime, limit: int = 20) -> list[Reminder]:
        """Всё, чему пришёл срок. Порядок — от самого просроченного."""
        async with self._db.connection() as conn:
            rows = await conn.fetch(
                f"""SELECT {_COLUMNS} FROM reminders
                     WHERE status IN ('open', 'snoozed') AND due_at <= $1
                     ORDER BY due_at
                     LIMIT $2""",
                now, limit,
            )
        return [self._row(r) for r in rows]

    async def get(self, reminder_id: int) -> Reminder | None:
        async with self._db.connection() as conn:
            row = await conn.fetchrow(f"SELECT {_COLUMNS} FROM reminders WHERE id=$1", reminder_id)
        return self._row(row) if row else None

    async def update(self, reminder: Reminder) -> None:
        async with self._db.connection() as conn:
            await conn.execute(
                """UPDATE reminders
                      SET status=$2, due_at=$3, urgency=$4, fired_count=$5,
                          last_fired_at=$6, done_at=$7, text=$8, repeat_rule=$9,
                          kind=$10, updated_at=now()
                    WHERE id=$1""",
                reminder.id, str(reminder.status), reminder.due_at, reminder.urgency,
                reminder.fired_count, reminder.last_fired_at, reminder.done_at,
                reminder.text, reminder.repeat_rule,
                # kind менялся только при создании, и его отсутствие здесь
                # означало тихую потерю: обязательство, ставшее проверкой,
                # оставалось в базе обязательством и долбило по-старому.
                str(reminder.kind),
            )

    async def open_items(self, chat_id: int, limit: int = 50) -> list[Reminder]:
        async with self._db.connection() as conn:
            rows = await conn.fetch(
                f"""SELECT {_COLUMNS} FROM reminders
                     WHERE chat_id=$1 AND status IN ('open','snoozed')
                     ORDER BY due_at LIMIT $2""",
                chat_id, limit,
            )
        return [self._row(r) for r in rows]

    async def recently_done(self, chat_id: int, limit: int = 10) -> list[Reminder]:
        async with self._db.connection() as conn:
            rows = await conn.fetch(
                f"""SELECT {_COLUMNS} FROM reminders
                     WHERE chat_id=$1 AND status='done'
                     ORDER BY done_at DESC NULLS LAST LIMIT $2""",
                chat_id, limit,
            )
        return [self._row(r) for r in rows]
