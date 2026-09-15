"""Автономная работа по целям.

Проверяются ограничители, а не «умеет ли агент думать». Автономность без
потолков — это счётчик расходов, который крутится, пока человек спит, поэтому
самое ценное здесь: остановка по бюджету, лимит ходов в сутки, тишина ночью и
то, что цель не запускает саму себя параллельно.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from app.domain.dto import TurnOutcome, TurnRequest
from app.domain.enums import GoalStatus, TurnSource
from app.domain.models import Goal, GoalStep
from app.services.goals import MARK_DONE, MARK_STUCK, GoalService
from app.settings import Settings


class FakeGoals:
    def __init__(self):
        self.items: list[Goal] = []
        self.steps: list[GoalStep] = []
        self._next = 1

    async def add(self, goal):
        goal.id = self._next; self._next += 1
        goal.next_run_at = goal.next_run_at or _at(0)
        self.items.append(goal)
        return goal

    async def due(self, now, limit=5):
        return [g for g in self.items
                if g.status is GoalStatus.ACTIVE and g.next_run_at <= now][:limit]

    async def get(self, goal_id):
        return next((g for g in self.items if g.id == goal_id), None)

    async def update(self, goal):
        self.items = [goal if g.id == goal.id else g for g in self.items]

    async def active(self, chat_id):
        return [g for g in self.items if g.status in (GoalStatus.ACTIVE, GoalStatus.PAUSED)]

    async def add_step(self, step):
        step.id = len(self.steps) + 1
        self.steps.append(step)
        return step

    async def recent_steps(self, goal_id, limit=8):
        return [s for s in self.steps if s.goal_id == goal_id][-limit:]


class FakeNotifier:
    def __init__(self): self.sent = []
    async def send(self, chat_id, html, **kw): self.sent.append(html)
    async def send_actions(self, chat_id, html, actions): self.sent.append(html)
    async def send_error(self, chat_id, title, detail): ...
    async def typing(self, chat_id): ...
    async def alive(self): return True


class FakeQueue:
    def __init__(self): self.requests: list[TurnRequest] = []
    async def __call__(self, request): self.requests.append(request); return 1


def _service(**overrides):
    settings = Settings(_env_file=None, telegram_bot_token="t", allowed_user_ids="1",
                        database_dsn="d", user_timezone="Europe/Moscow", **overrides)
    repo, notifier, queue = FakeGoals(), FakeNotifier(), FakeQueue()
    service = GoalService(repo, notifier, settings)
    service.bind(queue)
    return service, repo, notifier, queue


def _at(hour, day=1):
    from zoneinfo import ZoneInfo
    return datetime(2026, 9, day, hour, 0, tzinfo=ZoneInfo("Europe/Moscow"))


async def test_waking_puts_a_task_into_the_same_queue_as_a_human_message():
    service, _repo, _n, queue = _service()
    await service.create(1, "починить README", "все команды описаны", now=_at(0))
    assert await service.tick(_at(12)) == 1

    request = queue.requests[0]
    assert request.source is TurnSource.AGENT
    assert request.goal_id == 1
    assert "починить README" in request.text
    assert "все команды описаны" in request.text


async def test_task_forbids_irreversible_actions_and_asks_for_a_verdict():
    service, _repo, _n, queue = _service()
    await service.create(1, "цель", "критерий", now=_at(0))
    await service.tick(_at(12))
    task = queue.requests[0].text
    assert "НЕ делай сам" in task
    assert MARK_DONE in task and MARK_STUCK in task


async def test_goal_does_not_fire_twice_while_the_turn_is_running():
    """Следующий срок ставится сразу — иначе цель запустит саму себя параллельно."""
    service, repo, _n, queue = _service()
    await service.create(1, "цель", "критерий", cadence_min=60, now=_at(0))
    await service.tick(_at(12))
    await service.tick(_at(12))
    assert len(queue.requests) == 1
    assert (await repo.get(1)).next_run_at == _at(12) + timedelta(minutes=60)


async def test_night_is_quiet_even_for_goals():
    """Каждый автономный ход виден в чате, поэтому ночью не работаем."""
    service, repo, _n, queue = _service()
    goal = await service.create(1, "цель", "критерий", now=_at(0))
    goal.next_run_at = _at(2)          # срок пришёл ночью
    await repo.update(goal)
    assert await service.tick(_at(3)) == 0
    assert queue.requests == []
    assert (await repo.get(1)).next_run_at.astimezone(service._s.tz).hour == 8


async def test_budget_exhaustion_stops_the_goal_and_reports():
    service, repo, notifier, queue = _service()
    goal = await service.create(1, "дорогая цель", "критерий", budget_usd=0.10, now=_at(0))
    await service.record_turn(goal.id, TurnOutcome(ok=True, status="ok", answer="шаг сделан",
                                                   cost_usd=0.12))
    assert await service.tick(_at(12)) == 0
    saved = await repo.get(goal.id)
    assert saved.status is GoalStatus.FAILED
    assert "кончился бюджет" in notifier.sent[-1]
    assert queue.requests == []


async def test_daily_turn_limit_defers_to_next_morning():
    service, repo, _n, queue = _service()
    goal = await service.create(1, "цель", "критерий", max_turns_day=2, cadence_min=1, now=_at(0))
    goal.turns_today, goal.turns_day = 2, _at(12).date()
    await repo.update(goal)
    assert await service.tick(_at(12)) == 0
    assert (await repo.get(goal.id)).next_run_at.astimezone(service._s.tz).day == 2


async def test_new_day_resets_the_turn_counter():
    service, repo, _n, queue = _service()
    goal = await service.create(1, "цель", "критерий", max_turns_day=2, now=_at(0))
    goal.turns_today, goal.turns_day = 2, date(2026, 8, 31)
    await repo.update(goal)
    assert await service.tick(_at(12)) == 1


async def test_progress_is_remembered_between_wakeups():
    """Без журнала агент начинает с нуля и ходит по кругу, считая это работой."""
    service, _repo, _n, queue = _service()
    goal = await service.create(1, "цель", "критерий", now=_at(0))
    await service.record_turn(goal.id, TurnOutcome(ok=True, status="ok",
                                                   answer="Переписал раздел про слои",
                                                   cost_usd=0.01))
    await service.tick(_at(13))
    assert "Переписал раздел про слои" in queue.requests[-1].text


async def test_done_marker_closes_the_goal():
    service, repo, notifier, _q = _service()
    goal = await service.create(1, "цель", "критерий", now=_at(0))
    await service.record_turn(goal.id, TurnOutcome(
        ok=True, status="ok", answer=f"Всё готово\n{MARK_DONE}", cost_usd=0.02))
    assert (await repo.get(goal.id)).status is GoalStatus.DONE
    assert "закрыта" in notifier.sent[-1]


async def test_stuck_marker_pauses_and_asks_for_help():
    service, repo, notifier, _q = _service()
    goal = await service.create(1, "цель", "критерий", now=_at(0))
    await service.record_turn(goal.id, TurnOutcome(
        ok=True, status="ok", answer=f"нужен доступ к почте\n{MARK_STUCK}", cost_usd=0.02))
    assert (await repo.get(goal.id)).status is GoalStatus.PAUSED
    assert "нужна твоя помощь" in notifier.sent[-1]


async def test_spending_accumulates_across_steps():
    service, repo, _n, _q = _service()
    goal = await service.create(1, "цель", "критерий", budget_usd=5, now=_at(0))
    for _ in range(3):
        await service.record_turn(goal.id, TurnOutcome(ok=True, status="ok",
                                                        answer="шаг", cost_usd=0.25))
    saved = await repo.get(goal.id)
    assert saved.spent_usd == 0.75 and saved.steps_done == 3
    assert saved.budget_left == 4.25


async def test_paused_goal_is_not_woken():
    service, repo, _n, queue = _service()
    goal = await service.create(1, "цель", "критерий", now=_at(0))
    await service.set_status(goal.id, GoalStatus.PAUSED)
    assert await service.tick(_at(12)) == 0


def test_quiet_window_within_one_day_is_computed_correctly():
    """Регрессия: без скобок условие для окна «с 9 до 18» всегда было истинным."""
    service, *_ = _service(quiet_hours_from=9, quiet_hours_to=18)
    assert service._quiet(_at(12)) is True
    assert service._quiet(_at(20)) is False
    assert service._quiet(_at(3)) is False
