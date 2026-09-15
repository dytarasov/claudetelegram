"""Хранение целей и журнала продвижения к ним."""
from __future__ import annotations

from datetime import date, datetime

from ...domain.enums import GoalStatus
from ...domain.models import Goal, GoalStep
from .base import BaseRepository

_COLUMNS = """id, chat_id, text, done_when, status, cadence_min, budget_usd, spent_usd,
              max_turns_day, turns_today, turns_day, next_run_at, last_run_at,
              steps_done, last_note, created_at, finished_at"""


class PgGoalRepository(BaseRepository):
    @staticmethod
    def _row(row) -> Goal:
        return Goal(
            id=row["id"], chat_id=row["chat_id"], text=row["text"],
            done_when=row["done_when"], status=GoalStatus(row["status"]),
            cadence_min=row["cadence_min"], budget_usd=row["budget_usd"],
            spent_usd=row["spent_usd"], max_turns_day=row["max_turns_day"],
            turns_today=row["turns_today"], turns_day=row["turns_day"],
            next_run_at=row["next_run_at"], last_run_at=row["last_run_at"],
            steps_done=row["steps_done"], last_note=row["last_note"],
            created_at=row["created_at"], finished_at=row["finished_at"],
        )

    async def add(self, goal: Goal) -> Goal:
        async with self._db.connection() as conn:
            row = await conn.fetchrow(
                f"""INSERT INTO goals (chat_id, text, done_when, cadence_min, budget_usd,
                                       max_turns_day, next_run_at)
                    VALUES ($1,$2,$3,$4,$5,$6, coalesce($7, now()))
                    RETURNING {_COLUMNS}""",
                goal.chat_id, goal.text, goal.done_when, goal.cadence_min,
                goal.budget_usd, goal.max_turns_day, goal.next_run_at,
            )
        return self._row(row)

    async def due(self, now: datetime, limit: int = 5) -> list[Goal]:
        async with self._db.connection() as conn:
            rows = await conn.fetch(
                f"""SELECT {_COLUMNS} FROM goals
                     WHERE status = 'active' AND next_run_at <= $1
                     ORDER BY next_run_at LIMIT $2""",
                now, limit,
            )
        return [self._row(r) for r in rows]

    async def get(self, goal_id: int) -> Goal | None:
        async with self._db.connection() as conn:
            row = await conn.fetchrow(f"SELECT {_COLUMNS} FROM goals WHERE id=$1", goal_id)
        return self._row(row) if row else None

    async def update(self, goal: Goal) -> None:
        async with self._db.connection() as conn:
            await conn.execute(
                """UPDATE goals SET status=$2, cadence_min=$3, budget_usd=$4, spent_usd=$5,
                          max_turns_day=$6, turns_today=$7, turns_day=$8, next_run_at=$9,
                          last_run_at=$10, steps_done=$11, last_note=$12, finished_at=$13
                    WHERE id=$1""",
                goal.id, str(goal.status), goal.cadence_min, goal.budget_usd, goal.spent_usd,
                goal.max_turns_day, goal.turns_today, goal.turns_day, goal.next_run_at,
                goal.last_run_at, goal.steps_done, goal.last_note, goal.finished_at,
            )

    async def active(self, chat_id: int) -> list[Goal]:
        async with self._db.connection() as conn:
            rows = await conn.fetch(
                f"""SELECT {_COLUMNS} FROM goals
                     WHERE chat_id=$1 AND status IN ('active','paused')
                     ORDER BY id""", chat_id)
        return [self._row(r) for r in rows]

    async def add_step(self, step: GoalStep) -> GoalStep:
        async with self._db.connection() as conn:
            row = await conn.fetchrow(
                """INSERT INTO goal_steps (goal_id, summary, cost_usd, turn_id)
                   VALUES ($1,$2,$3,$4)
                   RETURNING id, goal_id, summary, cost_usd, turn_id, created_at""",
                step.goal_id, step.summary, step.cost_usd, step.turn_id,
            )
        return GoalStep(id=row["id"], goal_id=row["goal_id"], summary=row["summary"],
                        cost_usd=row["cost_usd"], turn_id=row["turn_id"],
                        created_at=row["created_at"])

    async def recent_steps(self, goal_id: int, limit: int = 8) -> list[GoalStep]:
        async with self._db.connection() as conn:
            rows = await conn.fetch(
                """SELECT id, goal_id, summary, cost_usd, turn_id, created_at
                     FROM goal_steps WHERE goal_id=$1 ORDER BY id DESC LIMIT $2""",
                goal_id, limit,
            )
        return [GoalStep(id=r["id"], goal_id=r["goal_id"], summary=r["summary"],
                         cost_usd=r["cost_usd"], turn_id=r["turn_id"],
                         created_at=r["created_at"]) for r in reversed(rows)]
