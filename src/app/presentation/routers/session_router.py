"""Управление сессией: состояние, контекст, модель, перезапуск, директория."""
from __future__ import annotations

from pathlib import Path

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from dishka.integrations.aiogram import FromDishka

from ...domain.ports import SpeechToText
from ...infrastructure.telegram.formatting import esc
from ...infrastructure.telegram.render import fmt_duration, fmt_tokens
from ...infrastructure.telegram.sender import MessageSender
from ...services.conversation import ConversationService
from ...services.health import HealthService
from ...settings import Settings
from .. import screens
from .common import enqueue

router = Router(name="session")


@router.message(Command("status"))
async def cmd_status(message: Message, conversation: FromDishka[ConversationService],
                     health: FromDishka[HealthService], stt: FromDishka[SpeechToText],
                     settings: FromDishka[Settings],
                     sender: FromDishka[MessageSender]) -> None:
    text, keys = await screens.status(conversation, health, stt, settings)
    await sender.send(message.chat.id, text, reply_markup=keys)


@router.message(Command("ctx", "context"))
async def cmd_ctx(message: Message, conversation: FromDishka[ConversationService],
                  sender: FromDishka[MessageSender]) -> None:
    await enqueue(message, conversation, sender, "/context")


@router.message(Command("compact"))
async def cmd_compact(message: Message, command: CommandObject,
                      conversation: FromDishka[ConversationService],
                      sender: FromDishka[MessageSender]) -> None:
    await enqueue(message, conversation, sender, f"/compact {(command.args or '').strip()}".strip())


@router.message(Command("model"))
async def cmd_model(message: Message, command: CommandObject,
                    conversation: FromDishka[ConversationService],
                    sender: FromDishka[MessageSender]) -> None:
    name = (command.args or "").strip()
    if not name:
        await sender.send(message.chat.id, "Укажи модель: <code>/model opus</code>")
        return
    await conversation.change_model(name)
    await enqueue(message, conversation, sender, f"/model {name}")


@router.message(Command("stop"))
async def cmd_stop(message: Message, conversation: FromDishka[ConversationService],
                   sender: FromDishka[MessageSender]) -> None:
    if not conversation.busy:
        await sender.send(message.chat.id, "Нечего прерывать — ход не идёт.")
        return
    ok = await conversation.interrupt()
    if not ok:
        await sender.send(message.chat.id, "Мягкое прерывание не сработало, перезапускаю процесс…")
        await conversation.restart()
    await sender.send(message.chat.id,
                      "<i>прервано</i>" if ok else "<i>процесс перезапущен</i>")


@router.message(Command("new"))
async def cmd_new(message: Message, conversation: FromDishka[ConversationService],
                  sender: FromDishka[MessageSender]) -> None:
    old = conversation.process.status().get("session_id")
    await conversation.restart(fresh=True)
    new = conversation.process.status().get("session_id")
    await sender.send(
        message.chat.id,
        f"<b>Новая сессия</b>\n<code>{esc(str(new))}</code>\n\n"
        f"<i>прошлая: {esc(str(old))} — её можно поднять через claude --resume на сервере</i>",
    )


@router.message(Command("restart"))
async def cmd_restart(message: Message, conversation: FromDishka[ConversationService],
                      sender: FromDishka[MessageSender]) -> None:
    await conversation.restart()
    await sender.send(
        message.chat.id,
        "<b>Процесс перезапущен</b>, контекст сохранён.\n"
        f"<code>{esc(str(conversation.process.status().get('session_id')))}</code>",
    )


@router.message(Command("cd"))
async def cmd_cd(message: Message, command: CommandObject,
                 conversation: FromDishka[ConversationService],
                 sender: FromDishka[MessageSender]) -> None:
    raw = (command.args or "").strip()
    current = Path(conversation.process.status()["cwd"])
    if not raw:
        await sender.send(message.chat.id, f"Сейчас: <code>{esc(str(current))}</code>")
        return
    target = Path(raw).expanduser()
    if not target.is_absolute():
        target = current / target
    target = target.resolve()
    if not target.is_dir():
        await sender.send(message.chat.id, f"Не директория: <code>{esc(str(target))}</code>")
        return
    await conversation.change_cwd(target)
    await sender.send(
        message.chat.id,
        f"<b>Рабочая директория</b>: <code>{esc(str(target))}</code>\n"
        "<i>процесс перезапущен, контекст сохранён</i>",
    )


@router.message(Command("menu", "start"))
async def cmd_menu(message: Message, conversation: FromDishka[ConversationService],
                   health: FromDishka[HealthService],
                   sender: FromDishka[MessageSender]) -> None:
    """Корень интерфейса. Единственная команда, которую стоит помнить."""
    text, keys = await screens.menu(conversation, health)
    await sender.send(message.chat.id, text, reply_markup=keys)
