"""Проактивность: бот заговаривает первым.

До сих пор бот был чисто реактивным — отвечал, когда спросят. Это отдельный
режим работы, и у него свои правила приличия, потому что инструмент, который
пишет сам, очень легко превращается в источник раздражения.

Правила, заложенные здесь:

  • Тихие часы. Ночью не будим — переносим на утро. Исключение только для
    просроченных обязательств, да и то они ждут до конца тихих часов.

  • Настойчивость растёт, но не громкостью, а формулировкой. Первое
    напоминание — обычное. Второе — с указанием, насколько просрочено. Третье и
    дальше — уже вопрос «это ещё актуально или снять?». Три одинаковых «не
    забудь» человек перестаёт читать, а вопрос требует решения.

  • Не сработало — не забыто. Если на напоминание не ответили, оно возвращается
    с растущей паузой (15 минут, час, четыре часа, потом раз в сутки), а не
    исчезает молча.

  • Всё, что бот прислал сам, можно закрыть одним нажатием: сделано, отложить,
    снять. Без этого дисциплинирующий инструмент превращается в спам.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta

from ..domain.enums import ReminderKind, ReminderStatus
from ..domain.models import Reminder
from ..domain.ports import Notifier, ReminderRepository
from ..settings import Settings
from . import events

log = logging.getLogger(__name__)

# Пауза до следующего показа, если на напоминание не отреагировали.
# Растёт, чтобы забытая мелочь не долбила каждые пятнадцать минут весь день.
BACKOFF_MINUTES = (15, 60, 240, 1440)


class ProactiveService:
    def __init__(self, reminders: ReminderRepository, notifier: Notifier,
                 settings: Settings) -> None:
        self._repo = reminders
        self._notifier = notifier
        self._s = settings

    # ---- расписание -------------------------------------------------------- #
    async def schedule(self, chat_id: int, text: str, at: datetime,
                       repeat: str | None = None,
                       kind: ReminderKind = ReminderKind.REMINDER,
                       source: str = "user", turn_id: int | None = None) -> Reminder:
        reminder = await self._repo.add(Reminder(
            chat_id=chat_id, text=text.strip(), due_at=at, repeat_rule=repeat,
            kind=kind, source=source, source_turn_id=turn_id,
        ))
        events.reminder_scheduled(reminder.id or 0, str(kind), at.isoformat(timespec="minutes"))
        return reminder

    async def complete(self, reminder_id: int) -> Reminder | None:
        return await self._close(reminder_id, ReminderStatus.DONE)

    async def drop(self, reminder_id: int) -> Reminder | None:
        return await self._close(reminder_id, ReminderStatus.DROPPED)

    async def snooze(self, reminder_id: int, minutes: int) -> Reminder | None:
        reminder = await self._repo.get(reminder_id)
        if reminder is None:
            return None
        reminder.status = ReminderStatus.SNOOZED
        reminder.due_at = datetime.now().astimezone() + timedelta(minutes=minutes)
        # Отложенное вручную — это осознанное решение, а не игнорирование:
        # счётчик настойчивости сбрасываем, иначе бот начнёт давить зря.
        reminder.fired_count = 0
        await self._repo.update(reminder)
        events.reminder_snoozed(reminder_id, minutes)
        return reminder

    async def _close(self, reminder_id: int, status: ReminderStatus) -> Reminder | None:
        reminder = await self._repo.get(reminder_id)
        if reminder is None:
            return None
        reminder.status = status
        reminder.done_at = datetime.now().astimezone()
        await self._repo.update(reminder)
        events.reminder_closed(reminder_id, str(status))
        return reminder

    async def open_items(self, chat_id: int) -> list[Reminder]:
        return await self._repo.open_items(chat_id)

    # ---- тихие часы -------------------------------------------------------- #
    def in_quiet_hours(self, moment: datetime) -> bool:
        start, end = self._s.quiet_hours_from, self._s.quiet_hours_to
        current = moment.time()
        if start == end:
            return False
        if start < end:                      # например с 01 до 08
            return time(start) <= current < time(end)
        return current >= time(start) or current < time(end)   # через полночь

    def _after_quiet(self, moment: datetime) -> datetime:
        """Ближайший момент за пределами тихих часов."""
        target = moment.replace(hour=self._s.quiet_hours_to, minute=0,
                                second=0, microsecond=0)
        if target <= moment:
            target += timedelta(days=1)
        return target

    # ---- цикл -------------------------------------------------------------- #
    async def run_loop(self) -> None:
        """Раз в полминуты смотрит, чему пришёл срок. Дёшево: один индексный запрос."""
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 — проактивность не роняет бота
                log.warning("сбой цикла напоминаний", exc_info=True)
            await asyncio.sleep(self._s.proactive_tick_s)

    async def tick(self, now: datetime | None = None) -> int:
        """Один проход. Возвращает, сколько напоминаний отправлено."""
        now = now or datetime.now().astimezone()
        sent = 0
        for reminder in await self._repo.due(now):
            if self._should_wait_for_morning(reminder, now):
                reminder.due_at = self._after_quiet(now)
                await self._repo.update(reminder)
                log.info("напоминание #%s перенесено за тихие часы", reminder.id)
                continue
            await self._fire(reminder, now)
            sent += 1
        return sent

    def _should_wait_for_morning(self, reminder: Reminder, now: datetime) -> bool:
        """Тихие часы не действуют на то, что человек завёл, будучи разбуженным.

        Иначе получается издевательство: в два часа ночи ставишь «через двадцать
        минут», а бот приходит в восемь утра. Раз напоминание создано в те же
        тихие часы — значит человек не спит и ждёт его сейчас.
        """
        if not self.in_quiet_hours(now):
            return False
        if reminder.created_at and self.in_quiet_hours(reminder.created_at):
            return False
        return True

    async def _fire(self, reminder: Reminder, now: datetime) -> None:
        reminder.fired_count += 1
        reminder.last_fired_at = now
        reminder.urgency = min(reminder.fired_count, len(BACKOFF_MINUTES))

        text, actions = self._compose(reminder, now)
        await self._notifier.send_actions(reminder.chat_id, text, actions)
        events.reminder_fired(reminder.id or 0, reminder.fired_count, str(reminder.kind))

        if reminder.repeat_rule:
            reminder.due_at = self._next_occurrence(reminder, now)
            reminder.status = ReminderStatus.OPEN
            reminder.fired_count = 0
        else:
            # Не закрываем: молчание не значит «сделано». Вернёмся позже.
            pause = BACKOFF_MINUTES[min(reminder.fired_count, len(BACKOFF_MINUTES)) - 1]
            reminder.due_at = now + timedelta(minutes=pause)
            reminder.status = ReminderStatus.SNOOZED
        await self._repo.update(reminder)

    def _next_occurrence(self, reminder: Reminder, now: datetime) -> datetime:
        """Когда сработать в следующий раз.

        Правило «times:11:00,15:00,19:00» — это не расписание на один день, а
        распорядок: напоминать в эти часы, пока дело не закрыто. Для задач,
        которые человек просит «напомнить три раза», так честнее — не сделал
        сегодня, значит спросим и завтра, а не потеряем.
        """
        rule = (reminder.repeat_rule or "").lower()
        if rule.startswith("times:"):
            moments = [m.strip() for m in rule.removeprefix("times:").split(",") if m.strip()]
            local = now.astimezone(self._s.tz)
            for moment in sorted(moments):
                hour, minute = (int(x) for x in moment.split(":"))
                candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if candidate > local:
                    return candidate
            hour, minute = (int(x) for x in sorted(moments)[0].split(":"))
            return (local + timedelta(days=1)).replace(hour=hour, minute=minute,
                                                       second=0, microsecond=0)
        if rule == "daily":
            return reminder.due_at + timedelta(days=1)
        if rule.startswith("weekly"):
            return reminder.due_at + timedelta(days=7)
        return now + timedelta(days=1)

    def _compose(self, reminder: Reminder, now: datetime) -> tuple[str, list[tuple[str, str]]]:
        """Текст и кнопки. Формулировка зависит от того, какой это заход."""
        late = (now - reminder.due_at).total_seconds() / 60
        body = reminder.text

        if reminder.nagging:
            head = "<b>Уже не первый раз спрашиваю</b>"
            tail = ("\n<i>Если это больше не нужно — сними, чтобы не мозолило. "
                    "Если нужно — назначь новый срок.</i>")
        elif reminder.fired_count > 1:
            head = "<b>Напоминаю ещё раз</b>"
            tail = f"\n<i>просрочено на {self._humanize(late)}</i>"
        elif reminder.kind is ReminderKind.COMMITMENT:
            head = "<b>Ты обещал</b>"
            tail = ""
        elif reminder.kind is ReminderKind.FOLLOWUP:
            head = "<b>Мы это не доделали</b>"
            tail = ""
        else:
            head = "<b>Напоминание</b>"
            tail = ""

        # Стиль несёт ту роль, которую в других ботах играют картинки:
        # зелёная — закрыть, красная — снять совсем, обычные — отложить.
        actions = [
            ("Готово", f"rem:done:{reminder.id}", "success"),
            ("Через час", f"rem:snooze:{reminder.id}:60", "default"),
            ("Утром", f"rem:snooze:{reminder.id}:{self._until_morning(now)}", "default"),
            ("Снять", f"rem:drop:{reminder.id}", "danger"),
        ]
        return f"{head}\n{body}{tail}", actions

    def _until_morning(self, now: datetime) -> int:
        return max(1, int((self._after_quiet(now) - now).total_seconds() // 60))

    @staticmethod
    def _humanize(minutes: float) -> str:
        if minutes < 60:
            return f"{minutes:.0f} мин"
        if minutes < 60 * 24:
            return f"{minutes / 60:.0f} ч"
        return f"{minutes / 60 / 24:.0f} дн"
