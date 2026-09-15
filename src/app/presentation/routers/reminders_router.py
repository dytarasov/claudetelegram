"""Напоминания в чате: поставить, посмотреть, закрыть касанием.

Команда намеренно свободная по форме — её набирают с телефона и диктуют
голосом: «/remind через 20 минут проверить выкатку», «/remind каждый день в 9
отчёт». Время вытаскивается из фразы, остальное становится текстом напоминания.
"""
from __future__ import annotations

from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message
from dishka.integrations.aiogram import FromDishka

from ...domain.enums import ReminderKind
from ...infrastructure.telegram.formatting import esc
from ...infrastructure.telegram.sender import MessageSender
from ...services.proactive import ProactiveService
from ...services.when import WhenParser
from .. import screens
from ...settings import Settings

router = Router(name="reminders")

HOWTO = ("Как ставить:\n"
         "<code>/remind через 20 минут проверить выкатку</code>\n"
         "<code>/remind завтра в 10 позвонить в банк</code>\n"
         "<code>/remind каждый день в 9 отчёт</code>\n\n"
         "Посмотреть висящее — /todo")


@router.message(Command("remind", "напомни"))
async def cmd_remind(message: Message, command: CommandObject,
                     parser: FromDishka[WhenParser], proactive: FromDishka[ProactiveService],
                     settings: FromDishka[Settings],
                     sender: FromDishka[MessageSender]) -> None:
    phrase = (command.args or "").strip()
    if not phrase:
        await sender.send(message.chat.id, HOWTO)
        return

    schedule = await parser.parse(phrase)
    if schedule is None:
        await sender.send(
            message.chat.id,
            f"Не понял, когда именно. {esc('Скажи точнее — «через час», «завтра в 10».')}\n\n"
            + HOWTO,
        )
        return

    kind = (ReminderKind.COMMITMENT if schedule.repeat and
            schedule.repeat.startswith("times:") else ReminderKind.REMINDER)
    reminder = await proactive.schedule(
        chat_id=message.chat.id, text=schedule.text or phrase,
        at=schedule.at, repeat=schedule.repeat, kind=kind,
    )
    when = schedule.at.astimezone(settings.tz).strftime("%d.%m в %H:%M")
    if schedule.repeat == "daily":
        repeat = " · каждый день"
    elif schedule.repeat and schedule.repeat.startswith("times:"):
        hours = ", ".join(schedule.repeat.removeprefix("times:").split(","))
        repeat = f" · и дальше в {hours}, пока не закроешь"
    else:
        repeat = ""
    await sender.send(
        message.chat.id,
        f"Напомню <b>{when}</b>{repeat}\n<i>{esc(reminder.text)}</i>",
    )


@router.message(Command("todo", "дела"))
async def cmd_todo(message: Message, proactive: FromDishka[ProactiveService],
                   settings: FromDishka[Settings],
                   sender: FromDishka[MessageSender]) -> None:
    text, keys = await screens.todo(proactive, message.chat.id, settings)
    await sender.send(message.chat.id, text, reply_markup=keys)


@router.message(Command("done"))
async def cmd_done(message: Message, command: CommandObject,
                   proactive: FromDishka[ProactiveService],
                   sender: FromDishka[MessageSender]) -> None:
    await _close_by_command(message, command, proactive, sender, drop=False)


@router.message(Command("drop"))
async def cmd_drop(message: Message, command: CommandObject,
                   proactive: FromDishka[ProactiveService],
                   sender: FromDishka[MessageSender]) -> None:
    await _close_by_command(message, command, proactive, sender, drop=True)


async def _close_by_command(message: Message, command: CommandObject,
                            proactive: ProactiveService, sender: MessageSender,
                            drop: bool) -> None:
    raw = (command.args or "").strip().lstrip("#")
    if not raw.isdigit():
        await sender.send(message.chat.id, "Укажи номер: <code>/done 12</code> (список — /todo)")
        return
    closer = proactive.drop if drop else proactive.complete
    reminder = await closer(int(raw))
    if reminder is None:
        await sender.send(message.chat.id, f"Не нашёл #{esc(raw)}.")
        return
    await sender.send(message.chat.id,
                      ("Снял: " if drop else "Закрыл: ") + f"<i>{esc(reminder.text)}</i>")


@router.callback_query(F.data.startswith("rem:"))
async def on_reminder_action(callback: CallbackQuery,
                             proactive: FromDishka[ProactiveService]) -> None:
    """Кнопки под напоминанием. Формат данных: rem:<действие>:<id>[:<минуты>]."""
    parts = (callback.data or "").split(":")
    action, reminder_id = parts[1], int(parts[2])

    if action == "done":
        await proactive.complete(reminder_id)
        answer = "закрыто"
    elif action == "drop":
        await proactive.drop(reminder_id)
        answer = "снято"
    elif action == "snooze":
        minutes = int(parts[3]) if len(parts) > 3 else 60
        reminder = await proactive.snooze(reminder_id, minutes)
        answer = ("вернусь " + reminder.due_at.strftime("%d.%m в %H:%M")
                  if reminder else "не нашёл")
    else:
        answer = "не понял кнопку"

    await callback.answer(answer)
    # Кнопки убираем: напоминание закрыто, повторное нажатие только запутает.
    if callback.message is not None:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001 — сообщение могло устареть, это не важно
            pass
