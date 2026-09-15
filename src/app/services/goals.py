"""Цели: работа без реплики человека.

Как это устроено. Раз в минуту сервис смотрит, каким целям пришёл срок, и
кладёт задачу не в отправку сообщения, а **во входную очередь ходов** — ту
самую, куда попадают реплики человека. Дальше всё идёт обычным путём: я
просыпаюсь, читаю свой же журнал продвижения, делаю один шаг и отчитываюсь в
чат. Разница с напоминанием принципиальная: напоминание будит человека, цель
будит меня.

Почему не /loop и не cron самого Claude Code. Они живут в памяти сессии, а наш
бот перезапускает процесс claude при каждой самовыкатке — за ночь это десяток
раз. Расписание, которое испаряется на деплое, бесполезно. Здесь оно в
Postgres и переживает и перезапуск, и откат.

Ограничители — не украшение, а условие, при котором такое вообще можно
включать: у бота рут на машине и платный API.

  • Бюджет в долларах на цель. Кончился — цель останавливается сама.
  • Потолок ходов в сутки: агент, который сам себя будит, прекрасно умеет
    вертеться на месте, изображая работу.
  • Тихие часы: ночью не работаем, потому что каждый ход виден в чате.
  • Признак завершения (done_when) обязателен. Без него «сделано» не наступает
    никогда, а любое шевеление засчитывается за прогресс.
  • Необратимое — только с разрешения. Это записано прямо в задании, которое
    получает модель на каждом пробуждении.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta

from ..domain.dto import TurnOutcome, TurnRequest
from ..domain.enums import GoalStatus, TurnSource
from ..domain.models import Goal, GoalStep
from ..domain.ports import GoalRepository, Notifier
from ..settings import Settings
from . import events

log = logging.getLogger(__name__)

# Строки, которыми ход к цели сообщает о своём исходе. Просить модель отвечать
# строгим JSON здесь незачем — достаточно последней строки, а разбор простой.
MARK_DONE = "ЦЕЛЬ ДОСТИГНУТА"
MARK_STUCK = "ЗАСТРЯЛ"

TASK = """[Это автономный ход: тебя разбудил планировщик, человек ничего не писал.
Сейчас {now}.

ЦЕЛЬ #{goal_id}: {text}
СЧИТАТЬ ЗАКОНЧЕННЫМ, КОГДА: {done_when}

Что уже сделано (твой журнал по этой цели):
{progress}

Бюджет: потрачено ${spent:.2f} из ${budget:.2f}. Ходов сегодня: {today} из {max_today}.

Правила этого хода:
- сделай ОДИН содержательный шаг к цели и коротко отчитайся, что именно сделал;
- не пересказывай уже сделанное, не начинай заново — журнал выше это твоя память;
- необратимое (удаление данных, отправка чего-то наружу, деньги, письма от имени
  человека) НЕ делай сам — спроси и остановись;
- последней строкой напиши ровно одно: «{done}» если цель достигнута,
  «{stuck}» если нужна помощь человека, иначе «ПРОДОЛЖАЮ».]"""


class GoalService:
    def __init__(self, goals: GoalRepository, notifier: Notifier,
                 settings: Settings) -> None:
        self._repo = goals
        self._notifier = notifier
        self._s = settings
        self._enqueue = None  # ставится в main: очередь ходов появляется позже

    def bind(self, enqueue) -> None:
        """Подключить очередь ходов.

        Через сеттер, а не через конструктор: разговор и цели ссылаются друг на
        друга, и в контейнере это была бы циклическая зависимость. Здесь связь
        явная и в одном месте.
        """
        self._enqueue = enqueue

    # ---- управление -------------------------------------------------------- #
    async def create(self, chat_id: int, text: str, done_when: str,
                     cadence_min: int | None = None, budget_usd: float | None = None,
                     max_turns_day: int | None = None, start_now: bool = True,
                     now: datetime | None = None) -> Goal:
        """Завести цель. now задаётся явно только в тестах — см. tick."""
        now = now or datetime.now().astimezone()
        goal = await self._repo.add(Goal(
            chat_id=chat_id, text=text.strip(), done_when=done_when.strip(),
            cadence_min=cadence_min or self._s.goal_cadence_min,
            budget_usd=budget_usd if budget_usd is not None else self._s.goal_budget_usd,
            max_turns_day=max_turns_day or self._s.goal_max_turns_day,
            next_run_at=now if start_now
            else now + timedelta(minutes=cadence_min or 60),
        ))
        events.goal_created(goal.id or 0, goal.text[:60], goal.budget_usd)
        return goal

    async def active(self, chat_id: int) -> list[Goal]:
        return await self._repo.active(chat_id)

    async def set_status(self, goal_id: int, status: GoalStatus) -> Goal | None:
        goal = await self._repo.get(goal_id)
        if goal is None:
            return None
        goal.status = status
        if status in (GoalStatus.DONE, GoalStatus.STOPPED, GoalStatus.FAILED):
            goal.finished_at = datetime.now().astimezone()
        await self._repo.update(goal)
        events.goal_status(goal_id, str(status))
        return goal

    async def steps(self, goal_id: int, limit: int = 8) -> list[GoalStep]:
        return await self._repo.recent_steps(goal_id, limit)

    # ---- цикл --------------------------------------------------------------- #
    async def run_loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 — автономность не роняет бота
                log.warning("сбой цикла целей", exc_info=True)
            await asyncio.sleep(self._s.goal_tick_s)

    async def tick(self, now: datetime | None = None) -> int:
        now = now or datetime.now().astimezone()
        started = 0
        for goal in await self._repo.due(now):
            if self._quiet(now):
                goal.next_run_at = self._after_quiet(now)
                await self._repo.update(goal)
                continue
            if await self._stop_if_limits_hit(goal, now):
                continue
            await self._wake(goal, now)
            started += 1
        return started

    def _quiet(self, moment: datetime) -> bool:
        """Ночью не работаем: каждый автономный ход виден в чате."""
        start, end = self._s.quiet_hours_from, self._s.quiet_hours_to
        hour = moment.astimezone(self._s.tz).hour
        if start == end:
            return False
        if start < end:                       # окно внутри одних суток
            return start <= hour < end
        return hour >= start or hour < end    # окно через полночь

    def _after_quiet(self, moment: datetime) -> datetime:
        local = moment.astimezone(self._s.tz)
        target = local.replace(hour=self._s.quiet_hours_to, minute=0, second=0, microsecond=0)
        if target <= local:
            target += timedelta(days=1)
        return target

    async def _stop_if_limits_hit(self, goal: Goal, now: datetime) -> bool:
        """Деньги и лимит ходов проверяются до работы, а не после."""
        today = now.astimezone(self._s.tz).date()
        if goal.turns_day != today:
            goal.turns_day, goal.turns_today = today, 0

        if goal.exhausted:
            goal.status, goal.finished_at = GoalStatus.FAILED, now
            goal.last_note = "кончился бюджет"
            await self._repo.update(goal)
            await self._notifier.send(
                goal.chat_id,
                f"<b>Цель #{goal.id} остановлена</b>: кончился бюджет "
                f"(${goal.spent_usd:.2f} из ${goal.budget_usd:.2f}).\n"
                f"<i>{goal.text}</i>\n\nПродолжить — подними бюджет и запусти заново.")
            events.goal_status(goal.id or 0, "budget_exhausted")
            return True

        if goal.turns_today >= goal.max_turns_day:
            goal.next_run_at = self._tomorrow_morning(now)
            await self._repo.update(goal)
            log.info("цель #%s исчерпала лимит ходов на сегодня", goal.id)
            return True
        return False

    def _tomorrow_morning(self, now: datetime) -> datetime:
        local = now.astimezone(self._s.tz)
        return (local + timedelta(days=1)).replace(
            hour=self._s.quiet_hours_to, minute=5, second=0, microsecond=0)

    async def _wake(self, goal: Goal, now: datetime) -> None:
        """Положить задание во входную очередь — то же, что написал бы человек."""
        if self._enqueue is None:
            log.warning("очередь ходов не подключена, цель #%s пропущена", goal.id)
            return

        steps = await self._repo.recent_steps(goal.id or 0, 8)
        progress = "\n".join(
            f"  {i + 1}. {s.summary}" for i, s in enumerate(steps)) or "  (пока ничего)"
        task = TASK.format(
            now=now.astimezone(self._s.tz).strftime("%d.%m.%Y %H:%M"),
            goal_id=goal.id, text=goal.text, done_when=goal.done_when,
            progress=progress, spent=goal.spent_usd, budget=goal.budget_usd,
            today=goal.turns_today, max_today=goal.max_turns_day,
            done=MARK_DONE, stuck=MARK_STUCK,
        )
        await self._enqueue(TurnRequest(
            chat_id=goal.chat_id, user_id=None, text=task,
            source=TurnSource.AGENT, goal_id=goal.id,
        ))

        # Следующий срок ставим сразу: пока ход идёт, цель не должна сработать
        # второй раз и запустить саму себя параллельно.
        goal.last_run_at = now
        goal.turns_today += 1
        goal.next_run_at = now + timedelta(minutes=goal.cadence_min)
        await self._repo.update(goal)
        events.goal_woke(goal.id or 0, goal.turns_today, goal.budget_left)

    # ---- учёт после хода ----------------------------------------------------- #
    async def record_turn(self, goal_id: int, outcome: TurnOutcome,
                          turn_id: int | None = None) -> None:
        """Записать результат хода и решить, продолжать ли.

        Зовётся разговором после каждого автономного хода — это единственное
        место, где цель узнаёт, во что обошёлся её шаг.
        """
        goal = await self._repo.get(goal_id)
        if goal is None:
            return
        answer = (outcome.answer or "").strip()
        summary = self._summarize(answer)

        await self._repo.add_step(GoalStep(goal_id=goal_id, summary=summary,
                                           cost_usd=outcome.cost_usd, turn_id=turn_id))
        goal.spent_usd += outcome.cost_usd
        goal.steps_done += 1
        goal.last_note = summary[:400]

        tail = answer[-200:].upper()
        if MARK_DONE in tail:
            goal.status, goal.finished_at = GoalStatus.DONE, datetime.now().astimezone()
            await self._notifier.send(
                goal.chat_id,
                f"<b>Цель #{goal.id} закрыта</b>\n<i>{goal.text}</i>\n\n"
                f"Шагов: {goal.steps_done}, потрачено ${goal.spent_usd:.2f}.")
        elif MARK_STUCK in tail:
            goal.status = GoalStatus.PAUSED
            await self._notifier.send(
                goal.chat_id,
                f"<b>Цель #{goal.id} встала</b> — нужна твоя помощь.\n<i>{goal.text}</i>\n\n"
                f"Снять паузу: /goal resume {goal.id}")
        await self._repo.update(goal)
        events.goal_step(goal.id or 0, goal.steps_done, outcome.cost_usd, str(goal.status))

    @staticmethod
    def _summarize(answer: str) -> str:
        """Первая содержательная строка ответа — она и есть отчёт о шаге."""
        for line in answer.splitlines():
            clean = line.strip(" *#-—·")
            if len(clean) > 15 and not clean.upper().startswith(("ПРОДОЛЖАЮ", MARK_DONE)):
                return clean[:300]
        return (answer[:200] or "без описания").replace("\n", " ")
