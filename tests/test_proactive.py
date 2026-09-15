"""Проактивность: когда бот заговаривает сам и как он не превращается в спам.

Здесь проверяются именно правила приличия — они и есть содержание сервиса.
Отправить сообщение по таймеру умеет кто угодно; ценность в том, чтобы не
разбудить ночью, не повторять одинаково и не забыть проигнорированное.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.domain.enums import ReminderKind, ReminderStatus
from app.domain.models import Reminder
from app.services.proactive import ProactiveService
from app.settings import Settings


class FakeReminders:
    def __init__(self, items=None):
        self.items: list[Reminder] = list(items or [])
        self._next_id = 1

    async def add(self, reminder):
        reminder.id = self._next_id
        reminder.created_at = reminder.created_at or datetime.now().astimezone()
        self._next_id += 1
        self.items.append(reminder)
        return reminder

    async def due(self, now, limit=20):
        return [r for r in self.items
                if r.status in (ReminderStatus.OPEN, ReminderStatus.SNOOZED)
                and r.due_at <= now][:limit]

    async def get(self, reminder_id):
        return next((r for r in self.items if r.id == reminder_id), None)

    async def update(self, reminder):
        self.items = [reminder if r.id == reminder.id else r for r in self.items]

    async def open_items(self, chat_id, limit=50):
        return [r for r in self.items if r.chat_id == chat_id
                and r.status in (ReminderStatus.OPEN, ReminderStatus.SNOOZED)]

    async def recently_done(self, chat_id, limit=10):
        return [r for r in self.items if r.status is ReminderStatus.DONE]


class FakeNotifier:
    def __init__(self):
        self.sent: list[tuple[int, str, list]] = []

    async def send(self, chat_id, html, **kw): self.sent.append((chat_id, html, []))
    async def send_actions(self, chat_id, html, actions):
        self.sent.append((chat_id, html, actions))
    async def send_error(self, chat_id, title, detail): ...
    async def typing(self, chat_id): ...
    async def alive(self): return True


def _service(**overrides):
    settings = Settings(_env_file=None, telegram_bot_token="t", allowed_user_ids="1",
                        database_dsn="d", **overrides)
    repo, notifier = FakeReminders(), FakeNotifier()
    return ProactiveService(repo, notifier, settings), repo, notifier


def _at(hour, minute=0, day=1):
    return datetime(2026, 9, day, hour, minute).astimezone()


async def test_due_reminder_is_delivered_with_buttons():
    service, repo, notifier = _service()
    await service.schedule(1, "проверить бэкап", _at(12))
    assert await service.tick(_at(12, 1)) == 1
    chat_id, text, actions = notifier.sent[0]
    assert chat_id == 1 and "проверить бэкап" in text
    assert [a[0] for a in actions] == ["Готово", "Через час", "Утром", "Снять"]
    # Стиль несёт роль, которую в других ботах играют картинки.
    assert [a[2] for a in actions] == ["success", "default", "default", "danger"]


async def test_nothing_fires_before_its_time():
    service, _repo, notifier = _service()
    await service.schedule(1, "рано", _at(12))
    assert await service.tick(_at(11, 59)) == 0
    assert notifier.sent == []


async def test_night_reminder_waits_for_morning():
    service, repo, notifier = _service()
    reminder = await service.schedule(1, "не будить", _at(3))
    reminder.created_at = _at(15, day=0 or 1)  # заведено днём
    await repo.update(reminder)
    assert await service.tick(_at(3, 1)) == 0
    assert notifier.sent == []
    assert (await repo.get(reminder.id)).due_at.hour == 8


async def test_night_reminder_set_at_night_still_fires():
    """Поставил в два часа ночи «через двадцать минут» — значит не спишь."""
    service, repo, notifier = _service()
    reminder = await service.schedule(1, "проверить выкатку", _at(2, 20))
    reminder.created_at = _at(2)
    await repo.update(reminder)
    assert await service.tick(_at(2, 21)) == 1


async def test_ignored_reminder_returns_later_not_forgotten():
    service, repo, _notifier = _service()
    reminder = await service.schedule(1, "висит", _at(12))
    await service.tick(_at(12, 1))
    saved = await repo.get(reminder.id)
    assert saved.status is ReminderStatus.SNOOZED
    assert saved.due_at == _at(12, 1) + timedelta(minutes=15)


async def test_pauses_grow_with_each_ignored_attempt():
    service, repo, _notifier = _service()
    reminder = await service.schedule(1, "упорное", _at(12))
    gaps = []
    now = _at(12, 1)
    for _ in range(3):
        await service.tick(now)
        saved = await repo.get(reminder.id)
        gaps.append((saved.due_at - now).total_seconds() / 60)
        now = saved.due_at + timedelta(seconds=1)
    assert gaps == [15, 60, 240], "пауза обязана расти, иначе это долбёжка"


async def test_wording_changes_from_reminder_to_question():
    service, repo, notifier = _service()
    reminder = await service.schedule(1, "надоевшее", _at(12))
    now = _at(12, 1)
    for _ in range(4):
        await service.tick(now)
        now = (await repo.get(reminder.id)).due_at + timedelta(seconds=1)
    assert "Напоминание" in notifier.sent[0][1]
    assert "ещё раз" in notifier.sent[1][1]
    assert "не первый раз" in notifier.sent[3][1]
    assert "сними" in notifier.sent[3][1]


async def test_daily_reminder_reschedules_itself():
    service, repo, _notifier = _service()
    reminder = await service.schedule(1, "отчёт", _at(9), repeat="daily")
    await service.tick(_at(9, 1))
    saved = await repo.get(reminder.id)
    assert saved.status is ReminderStatus.OPEN
    assert saved.due_at == _at(9, day=2)
    assert saved.fired_count == 0


async def test_snooze_resets_insistence():
    service, repo, _notifier = _service()
    reminder = await service.schedule(1, "потом", _at(12))
    await service.tick(_at(12, 1))
    await service.snooze(reminder.id, 30)
    saved = await repo.get(reminder.id)
    assert saved.fired_count == 0, "осознанный перенос — не игнорирование"


async def test_completed_reminder_stops_bothering():
    service, repo, notifier = _service()
    reminder = await service.schedule(1, "сделано", _at(12))
    await service.complete(reminder.id)
    assert await service.tick(_at(13)) == 0
    assert (await repo.get(reminder.id)).status is ReminderStatus.DONE


async def test_commitment_is_worded_more_strictly():
    service, _repo, notifier = _service()
    await service.schedule(1, "выкатить фикс", _at(12), kind=ReminderKind.COMMITMENT)
    await service.tick(_at(12, 1))
    assert "обещал" in notifier.sent[0][1]


@pytest.mark.parametrize("hour,quiet", [(23, True), (2, True), (7, True), (8, False), (15, False)])
def test_quiet_hours_window(hour, quiet):
    service, _repo, _notifier = _service()
    assert service.in_quiet_hours(_at(hour)) is quiet


async def test_times_rule_walks_through_the_day():
    """«в 11, 15 и 19» — это распорядок, а не одноразовое срабатывание."""
    service, repo, _notifier = _service(user_timezone="Europe/Moscow")
    reminder = await service.schedule(1, "отдать деньги", _at(11), repeat="times:11:00,15:00,19:00")
    reminder.created_at = _at(9)
    await repo.update(reminder)

    await service.tick(_at(11, 1))
    assert (await repo.get(reminder.id)).due_at.astimezone(service._s.tz).hour == 15

    await service.tick(_at(15, 1))
    assert (await repo.get(reminder.id)).due_at.astimezone(service._s.tz).hour == 19


async def test_times_rule_rolls_over_to_next_day_when_not_done():
    """Не закрыл за день — спросим завтра. В этом и смысл дисциплины."""
    service, repo, _notifier = _service(user_timezone="Europe/Moscow")
    reminder = await service.schedule(1, "позвонить бате", _at(19), repeat="times:11:00,15:00,19:00")
    reminder.created_at = _at(9)
    await repo.update(reminder)

    await service.tick(_at(19, 1))
    saved = await repo.get(reminder.id)
    local = saved.due_at.astimezone(service._s.tz)
    assert (local.day, local.hour) == (2, 11)


async def test_closing_a_task_stops_the_whole_schedule():
    service, repo, notifier = _service(user_timezone="Europe/Moscow")
    reminder = await service.schedule(1, "забрать оливки", _at(11), repeat="times:11:00,15:00,19:00")
    await service.complete(reminder.id)
    assert await service.tick(_at(15, 1)) == 0
